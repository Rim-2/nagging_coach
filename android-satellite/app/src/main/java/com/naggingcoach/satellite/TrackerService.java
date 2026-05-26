package com.naggingcoach.satellite;

import com.naggingcoach.satellite.BuildConfig;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 폰 활동 감시 위성 — PC 의 trigger_satellite.py 와 같은 역할.
 *
 * 1분 주기로 UsageStatsManager 폴링 → 약점 앱별 사용 시간이 임계치를 넘으면
 * Railway 봇의 /trigger 엔드포인트로 HTTP POST. Bearer 인증.
 *
 * 같은 (트리거, 앱) 쌍은 로컬에서 5분 쿨다운 — 클라우드의 10분 쿨다운과 별도로
 * 한 번 더 막아 폭주를 방지한다 (PC 위성의 60초 쿨다운과 같은 패턴).
 */
public class TrackerService extends Service {

    private static final String TAG = "NaggingSatellite";
    private static final String CHANNEL_ID = "nagging_satellite";
    private static final int NOTI_ID = 1;

    private static final long POLL_INTERVAL_MS = 60_000L;       // 1분 폴링
    private static final long WINDOW_MS = 6 * 60 * 60_000L;     // 최근 6시간 사용량 조회
    private static final long LOCAL_COOLDOWN_MS = 5 * 60_000L;  // 같은 (트리거,앱) 5분 쿨다운

    // 능동적 도파민 스크롤 — 영상·SNS 앱 N분 이상 누적
    private static final long DOPAMINE_THRESHOLD_MS = 15 * 60_000L;
    private static final String[] DOPAMINE_APPS = {
            "com.instagram.android",
            "com.google.android.youtube",
            "com.zhiliaoapp.musically",        // TikTok 글로벌
            "com.ss.android.ugc.trill",        // TikTok 일부 지역
            "com.facebook.katana",
            "com.twitter.android",
            "com.reddit.frontpage",
            "com.snapchat.android",
    };

    // 과몰입 딴짓 — 게임·메신저류 30분 이상 누적
    private static final long ADDICTIVE_THRESHOLD_MS = 30 * 60_000L;
    private static final String[] ADDICTIVE_APPS = {
            "com.kakao.talk",
            "com.discord",
            "com.nhn.android.band",
            "com.linecorp.linelite",
    };

    // 휴식 없는 과로 — 화면 ON 누적 N분 (5분 OFF 가 휴식 인정)
    private static final int OVERWORK_THRESHOLD_MIN = 120;
    private static final int BREAK_MIN = 5;

    // 늦은 밤 — 새벽 N~M시 + 화면 ON. 하루 한 번만.
    private static final int LATE_NIGHT_START_HOUR = 1;
    private static final int LATE_NIGHT_END_HOUR = 5;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<String, Long> lastFireMs = new HashMap<>();
    private UsageStatsManager usm;
    private PowerManager pm;

    // 화면 ON/OFF 누적 추적 (분 단위, POLL_INTERVAL_MS 가 1분이라 그냥 1씩)
    private int sustainedUseMin = 0;
    private int offStreakMin = 0;
    private String lateNightFiredOnDate = "";  // "yyyy-MM-dd"

    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            try {
                check();
            } catch (Throwable t) {
                Log.e(TAG, "check error", t);
            }
            handler.postDelayed(this, POLL_INTERVAL_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        usm = (UsageStatsManager) getSystemService(USAGE_STATS_SERVICE);
        pm = (PowerManager) getSystemService(POWER_SERVICE);
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTI_ID, buildNotification("폰 활동 감시 중"));
        handler.removeCallbacks(poller);
        handler.post(poller);
        Log.i(TAG, "TrackerService started");
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        handler.removeCallbacks(poller);
        Log.i(TAG, "TrackerService stopped");
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ============================================================ 폴링
    private void check() {
        long now = System.currentTimeMillis();

        // --- 화면 ON/OFF 누적 (휴식 없는 과로 + 늦은 밤 트리거 둘 다 씀) ---
        boolean screenOn = (pm != null) && pm.isInteractive();
        if (screenOn) {
            sustainedUseMin++;
            offStreakMin = 0;
        } else {
            offStreakMin++;
            if (offStreakMin >= BREAK_MIN) {
                // BREAK_MIN 이상 화면 OFF = 휴식으로 인정, 누적 리셋
                sustainedUseMin = 0;
            }
        }

        // --- 휴식 없는 과로: 화면 ON 누적 N분 ---
        if (sustainedUseMin >= OVERWORK_THRESHOLD_MIN) {
            tryFire("휴식 없는 과로", "phone-screen", sustainedUseMin * 60_000L, now);
            sustainedUseMin = 0;  // 발사 후 누적 리셋
        }

        // --- 늦은 밤: 새벽 시간대 + 화면 ON + 하루 한 번만 ---
        Calendar cal = Calendar.getInstance();
        int hour = cal.get(Calendar.HOUR_OF_DAY);
        String today = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date());
        if (screenOn
                && hour >= LATE_NIGHT_START_HOUR
                && hour < LATE_NIGHT_END_HOUR
                && !today.equals(lateNightFiredOnDate)) {
            lateNightFiredOnDate = today;
            tryFire("늦은 밤", "phone-screen", 0L, now);
        }

        // --- 앱 사용 시간 기반 트리거 (능동 도파민 / 과몰입) ---
        List<UsageStats> stats = usm.queryUsageStats(
                UsageStatsManager.INTERVAL_DAILY, now - WINDOW_MS, now);
        if (stats == null || stats.isEmpty()) {
            return;
        }

        for (UsageStats s : stats) {
            String pkg = s.getPackageName();
            long total = s.getTotalTimeInForeground();
            if (total <= 0) {
                continue;
            }

            // 능동 도파민 스크롤
            if (total >= DOPAMINE_THRESHOLD_MS && contains(DOPAMINE_APPS, pkg)) {
                tryFire("능동적 도파민 스크롤", pkg, total, now);
                continue;  // 같은 앱이 두 카테고리 둘 다일 일은 거의 없음
            }
            // 과몰입 딴짓
            if (total >= ADDICTIVE_THRESHOLD_MS && contains(ADDICTIVE_APPS, pkg)) {
                tryFire("과몰입 딴짓", pkg, total, now);
            }
        }
    }

    private void tryFire(String triggerValue, String appPkg, long totalMs, long now) {
        String key = triggerValue + ":" + appPkg;
        Long last = lastFireMs.get(key);
        if (last != null && (now - last) < LOCAL_COOLDOWN_MS) {
            return;
        }
        lastFireMs.put(key, now);
        new Thread(() -> postTrigger(triggerValue, appPkg, totalMs)).start();
    }

    // =========================================================== HTTP
    private void postTrigger(String triggerValue, String appPkg, long totalMs) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(BuildConfig.NAGGING_COACH_URL + "/trigger");
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty(
                    "Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty(
                    "Authorization", "Bearer " + BuildConfig.TRIGGER_SECRET);
            conn.setDoOutput(true);
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(20_000);

            String body = "{"
                    + "\"trigger\":\"" + triggerValue + "\","
                    + "\"snapshot\":{"
                    + "\"active_window\":\"" + appPkg + "\","
                    + "\"idle_time\":0,"
                    + "\"switch_count\":0"
                    + "}}";

            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
            int code = conn.getResponseCode();
            Log.i(TAG, "POST " + triggerValue + " " + appPkg
                    + " (total=" + (totalMs / 60_000) + "min) -> HTTP " + code);
        } catch (Throwable t) {
            Log.e(TAG, "postTrigger error", t);
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    // ============================================================ 알림
    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "잔소리 코치 위성",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("백그라운드에서 폰 활동을 가볍게 감시함");
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(ch);
            }
        }
    }

    private Notification buildNotification(String text) {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(this, CHANNEL_ID);
        } else {
            b = new Notification.Builder(this);
        }
        return b.setContentTitle("잔소리 코치")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_data_usage)
                .setOngoing(true)
                .build();
    }

    // ============================================================ helper
    private static boolean contains(String[] arr, String s) {
        for (String x : arr) {
            if (x.equals(s)) {
                return true;
            }
        }
        return false;
    }
}
