package com.naggingcoach.satellite;

import com.naggingcoach.satellite.BuildConfig;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.location.Location;
import android.location.LocationManager;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.BatteryManager;
import android.os.Build;
import java.io.InputStream;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;
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
import java.util.Collections;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

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
    // 같은 트리거 5분 로컬 쿨다운 (백엔드 10분 쿨다운과 별개).
    private static final long LOCAL_COOLDOWN_MS = 5 * 60_000L;

    // 능동적 도파민 스크롤 15분 연속 세션.
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

    // 과몰입 딴짓 30분 연속 세션.
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
    private final Map<String, String> labelCache = new HashMap<>();
    private UsageStatsManager usm;
    private PowerManager pm;
    private PackageManager packageManager;
    private SensorManager sensorManager;
    private Sensor stepCounter;
    private AudioManager audioManager;
    private LocationManager locationManager;

    // 등록된 장소 목록 — 1시간마다 백엔드 /places 에서 fetch. JSON 그대로 캐시.
    private JSONArray cachedPlaces = new JSONArray();
    private long placesFetchedAt = 0L;
    private static final long PLACES_FETCH_INTERVAL_MS = 60 * 60_000L;

    // 만보계 — STEP_COUNTER 는 부팅 후 누적값. 자정 마커 빼서 '오늘 걸음'.
    // 콜백 단발성이라 onSensorChanged 마다 lifetime 갱신, getStepsToday() 가
    // 자정 마커 갱신·차분 계산.
    private long lifetimeStepsSnapshot = -1L;   // 최신 sensor 값
    private long midnightStepsMark = -1L;       // 오늘 자정 시점의 lifetime 값
    private int midnightDayOfYear = -1;

    // 화면 ON/OFF 누적 추적 (분 단위, POLL_INTERVAL_MS 가 1분이라 그냥 1씩)
    private int sustainedUseMin = 0;
    private int offStreakMin = 0;
    private String lateNightFiredOnDate = "";  // "yyyy-MM-dd"

    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            try {
                // 위치 매칭용 등록 좌표는 1시간마다 갱신 — 별도 스레드 (HTTP)
                new Thread(() -> fetchPlacesIfStale()).start();
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
        packageManager = getPackageManager();
        audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        locationManager = (LocationManager) getSystemService(LOCATION_SERVICE);
        // 만보계 — ACTIVITY_RECOGNITION 권한·센서 미지원 시 stepCounter = null,
        // getStepsToday() 가 -1 반환. 페이로드에선 omit 처리.
        sensorManager = (SensorManager) getSystemService(SENSOR_SERVICE);
        if (sensorManager != null) {
            stepCounter = sensorManager.getDefaultSensor(Sensor.TYPE_STEP_COUNTER);
            if (stepCounter != null) {
                try {
                    sensorManager.registerListener(
                            stepListener, stepCounter,
                            SensorManager.SENSOR_DELAY_NORMAL);
                    Log.i(TAG, "step counter registered");
                } catch (Throwable t) {
                    Log.w(TAG, "step counter register failed", t);
                }
            } else {
                Log.i(TAG, "device has no STEP_COUNTER sensor");
            }
        }
        createNotificationChannel();
    }

    private final SensorEventListener stepListener = new SensorEventListener() {
        @Override
        public void onSensorChanged(SensorEvent event) {
            if (event.values != null && event.values.length > 0) {
                lifetimeStepsSnapshot = (long) event.values[0];
            }
        }

        @Override
        public void onAccuracyChanged(Sensor sensor, int accuracy) {
            // no-op
        }
    };

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
        if (sensorManager != null && stepCounter != null) {
            try {
                sensorManager.unregisterListener(stepListener);
            } catch (Throwable t) {
                // ignore
            }
        }
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

        // --- 약점 앱 *현재/마지막 세션 길이* 측정 (누적 X) ---
        // 오늘 누적 시간으로 잡으면 알림 답장 누적 등으로 일상 사용도 발사됨.
        // PC tracker 의 OVER_IMMERSION 처럼 *연속* 세션으로만 잡아야 한다.
        Set<String> allTargets = new HashSet<>();
        Collections.addAll(allTargets, DOPAMINE_APPS);
        Collections.addAll(allTargets, ADDICTIVE_APPS);
        Map<String, Long> sessions = getCurrentSessionsMs(allTargets, now);

        for (Map.Entry<String, Long> entry : sessions.entrySet()) {
            String pkg = entry.getKey();
            long sessionMs = entry.getValue();
            if (sessionMs <= 0) {
                continue;
            }

            // 능동 도파민 스크롤 — 영상·SNS 앱 한 세션 N분
            if (sessionMs >= DOPAMINE_THRESHOLD_MS
                    && contains(DOPAMINE_APPS, pkg)) {
                tryFire("능동적 도파민 스크롤", pkg, sessionMs, now);
                continue;
            }
            // 과몰입 딴짓 — 게임·메신저류 한 세션 N분
            if (sessionMs >= ADDICTIVE_THRESHOLD_MS
                    && contains(ADDICTIVE_APPS, pkg)) {
                tryFire("과몰입 딴짓", pkg, sessionMs, now);
            }
        }
    }

    /**
     * 약점 앱들의 *현재/마지막 세션 길이* (ms) 를 한 번의 queryEvents 호출로 계산.
     *
     * 알고리즘: WINDOW_MS 안의 이벤트를 훑어 각 앱별 *마지막* ACTIVITY_RESUMED 와
     * ACTIVITY_PAUSED/STOPPED 시각을 기억.
     *   - pause > resume: 이미 끝난 세션, 길이 = pause - resume
     *   - pause 없거나 pause < resume: 현재 포그라운드, 길이 = now - resume
     *   - resume 자체가 없으면: 0
     *
     * 즉 "오늘 누적" 이 아니라 *가장 최근 연속 세션 한 번* 의 길이만 반환.
     * 다른 앱으로 잠깐 전환했다 돌아오면 새 세션으로 카운트 (PC tracker 의
     * POST_RESUME_COOLDOWN 사상과 같음).
     */
    private Map<String, Long> getCurrentSessionsMs(Set<String> targets, long now) {
        Map<String, Long> lastResume = new HashMap<>();
        Map<String, Long> lastPause = new HashMap<>();

        UsageEvents events = usm.queryEvents(now - WINDOW_MS, now);
        UsageEvents.Event event = new UsageEvents.Event();
        while (events.hasNextEvent()) {
            events.getNextEvent(event);
            String pkg = event.getPackageName();
            if (pkg == null || !targets.contains(pkg)) {
                continue;
            }
            int type = event.getEventType();
            if (type == UsageEvents.Event.ACTIVITY_RESUMED) {
                lastResume.put(pkg, event.getTimeStamp());
            } else if (type == UsageEvents.Event.ACTIVITY_PAUSED
                    || type == UsageEvents.Event.ACTIVITY_STOPPED) {
                lastPause.put(pkg, event.getTimeStamp());
            }
        }

        Map<String, Long> result = new HashMap<>();
        for (String pkg : targets) {
            Long resume = lastResume.get(pkg);
            if (resume == null) {
                result.put(pkg, 0L);
                continue;
            }
            Long pause = lastPause.get(pkg);
            if (pause != null && pause > resume) {
                // 이미 끝난 세션 — 사용자가 이미 폰 내려놨으니 잔소리 의미 없음.
                // (Railway 재시작 시 옛 세션이 또 발사되던 버그 fix)
                result.put(pkg, 0L);
            } else {
                // 현재 포그라운드 — 진행 중인 세션만 잡는다 (PC tracker 의
                // active_window 만 보는 사상과 일치)
                result.put(pkg, now - resume);
            }
        }
        return result;
    }

    /**
     * 패키지명 → 사용자 OS 에 등록된 앱 표시 이름. 첫 호출 후 캐싱.
     * 못 찾으면 패키지명 그대로 fall-back.
     */
    private String getAppLabel(String pkg) {
        String cached = labelCache.get(pkg);
        if (cached != null) {
            return cached;
        }
        String label = pkg;
        try {
            ApplicationInfo info = packageManager.getApplicationInfo(pkg, 0);
            CharSequence cs = packageManager.getApplicationLabel(info);
            if (cs != null) {
                label = cs.toString();
            }
        } catch (PackageManager.NameNotFoundException ignored) {
            // 앱 미설치 — 패키지명 그대로
        } catch (Throwable ignored) {
            // 안전망 — 어떤 이유로든 실패 시 패키지명
        }
        labelCache.put(pkg, label);
        return label;
    }

    /** JSON body 직렬화 시 특수문자 escape (앱 라벨에 따옴표·백슬래시 가능). */
    private static String escapeJson(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '\\': sb.append("\\\\"); break;
                case '"':  sb.append("\\\""); break;
                case '\n': sb.append(' '); break;
                case '\r': sb.append(' '); break;
                case '\t': sb.append(' '); break;
                default:
                    if (c < 0x20) {
                        sb.append(' ');
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }

    private void tryFire(String triggerValue, String appPkg, long totalMs, long now) {
        String key = triggerValue + ":" + appPkg;
        Long last = lastFireMs.get(key);
        if (last != null && (now - last) < LOCAL_COOLDOWN_MS) {
            return;
        }
        lastFireMs.put(key, now);
        // 디바이스 status snapshot — postTrigger 안에서 다시 캡쳐하면 워커
        // 스레드에서 system service 접근이 어색해질 수 있어 메인 스레드에서 먼저.
        final boolean dndActive = isDndActive();
        final boolean charging = isCharging();
        final boolean screenOn = (pm != null) && pm.isInteractive();
        final long stepsToday = getStepsToday();
        final boolean headphones = isHeadphonesConnected();
        final String placeCategory = matchPlaceCategory();   // null·"home"·"other" 등
        new Thread(() ->
            postTrigger(triggerValue, appPkg, totalMs,
                    dndActive, charging, screenOn, stepsToday, headphones,
                    placeCategory)
        ).start();
    }

    // ============================================================ 디바이스 상태 캡쳐
    private boolean isDndActive() {
        // DND(방해 금지) 활성 = INTERRUPTION_FILTER_NONE/ALARMS/PRIORITY.
        // INTERRUPTION_FILTER_ALL 만 "모두 허용" (DND off).
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm == null) return false;
            int filter = nm.getCurrentInterruptionFilter();
            return filter != NotificationManager.INTERRUPTION_FILTER_ALL
                    && filter != NotificationManager.INTERRUPTION_FILTER_UNKNOWN;
        } catch (Throwable t) {
            return false;
        }
    }

    private boolean isCharging() {
        try {
            IntentFilter ifilter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
            Intent battery = registerReceiver(null, ifilter);
            if (battery == null) return false;
            int status = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            return status == BatteryManager.BATTERY_STATUS_CHARGING
                    || status == BatteryManager.BATTERY_STATUS_FULL;
        } catch (Throwable t) {
            return false;
        }
    }

    /** 자정 마커와 비교한 '오늘 걸음 수'. 권한 거부·센서 없음 시 -1. */
    private long getStepsToday() {
        if (stepCounter == null) return -1L;
        long current = lifetimeStepsSnapshot;
        if (current < 0) return -1L;
        Calendar cal = Calendar.getInstance();
        int today = cal.get(Calendar.DAY_OF_YEAR);
        if (today != midnightDayOfYear) {
            midnightStepsMark = current;
            midnightDayOfYear = today;
        }
        long today_steps = current - midnightStepsMark;
        return today_steps < 0 ? 0 : today_steps;
    }

    /** 가장 최근 위치를 등록 장소들과 매칭 → 라벨 또는 "other". 권한 없음·위치
     * 끔 시 null (페이로드 omit). 정확 좌표는 백엔드로 보내지 않는다. */
    private String matchPlaceCategory() {
        if (locationManager == null) return null;
        // PROVIDER 우선순위: NETWORK (배터리 친화) → GPS
        Location best = null;
        try {
            if (checkSelfPermission(android.Manifest.permission.ACCESS_COARSE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                Location net = null;
                Location gps = null;
                try {
                    net = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
                } catch (Throwable ignored) {}
                try {
                    gps = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
                } catch (Throwable ignored) {}
                // 더 최신 + 정확도 좋은 거 선택
                if (gps != null && (net == null || gps.getTime() > net.getTime())) {
                    best = gps;
                } else {
                    best = net;
                }
            }
        } catch (Throwable t) {
            return null;
        }
        if (best == null) return null;

        // 1시간 이상 묵은 위치는 신뢰도 ↓ — 무시
        if (System.currentTimeMillis() - best.getTime() > 3600_000L) return null;

        double bestDist = Double.MAX_VALUE;
        String bestLabel = null;
        for (int i = 0; i < cachedPlaces.length(); i++) {
            JSONObject p = cachedPlaces.optJSONObject(i);
            if (p == null) continue;
            double lat = p.optDouble("lat", Double.NaN);
            double lng = p.optDouble("lng", Double.NaN);
            int radius = p.optInt("radius_m", 200);
            if (Double.isNaN(lat) || Double.isNaN(lng)) continue;
            // 짧은 거리 — 위·경도 차이 m 변환 후 피타고라스. (위도 1° ≈ 111km,
            // 경도는 위도에 따라 cos 보정).
            double dLat = (best.getLatitude() - lat) * 111_000.0;
            double avgLat = Math.toRadians((best.getLatitude() + lat) / 2.0);
            double dLng = (best.getLongitude() - lng) * 111_000.0 * Math.cos(avgLat);
            double dist = Math.sqrt(dLat * dLat + dLng * dLng);
            if (dist <= radius && dist < bestDist) {
                bestDist = dist;
                bestLabel = p.optString("label", null);
            }
        }
        return bestLabel != null ? bestLabel : "other";
    }

    /** 백엔드에서 등록된 장소 목록 fetch. 실패해도 기존 캐시 유지. */
    private void fetchPlacesIfStale() {
        if (System.currentTimeMillis() - placesFetchedAt < PLACES_FETCH_INTERVAL_MS) {
            return;
        }
        HttpURLConnection conn = null;
        try {
            URL url = new URL(BuildConfig.NAGGING_COACH_URL + "/places");
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setRequestProperty(
                    "Authorization", "Bearer " + BuildConfig.TRIGGER_SECRET);
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(10_000);
            if (conn.getResponseCode() != 200) return;
            try (InputStream is = conn.getInputStream()) {
                byte[] buf = new byte[4096];
                StringBuilder sb = new StringBuilder();
                int n;
                while ((n = is.read(buf)) > 0) {
                    sb.append(new String(buf, 0, n, StandardCharsets.UTF_8));
                }
                JSONObject obj = (JSONObject) new JSONTokener(sb.toString()).nextValue();
                JSONArray arr = obj.optJSONArray("places");
                if (arr != null) {
                    cachedPlaces = arr;
                    placesFetchedAt = System.currentTimeMillis();
                    Log.i(TAG, "places fetched: " + arr.length() + " entries");
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "places fetch failed (keep cache)", t);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** 블루투스 헤드셋·A2DP·유선·USB 헤드폰 등 외장 오디오 출력 연결 여부. */
    private boolean isHeadphonesConnected() {
        if (audioManager == null) return false;
        try {
            AudioDeviceInfo[] devices =
                    audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
            if (devices == null) return false;
            for (AudioDeviceInfo d : devices) {
                int type = d.getType();
                if (type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                        || type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                        || type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES
                        || type == AudioDeviceInfo.TYPE_WIRED_HEADSET
                        || type == AudioDeviceInfo.TYPE_USB_HEADSET) {
                    return true;
                }
            }
        } catch (Throwable t) {
            // ignore
        }
        return false;
    }

    // =========================================================== HTTP
    private void postTrigger(
            String triggerValue, String appPkg, long totalMs,
            boolean dndActive, boolean charging, boolean screenOn,
            long stepsToday, boolean headphones,
            String placeCategory) {
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

            int sessionMinutes = (int) (totalMs / 60_000L);
            String label = appPkg.contains(".") ? getAppLabel(appPkg) : appPkg;
            StringBuilder snap = new StringBuilder();
            snap.append("\"active_window\":\"").append(escapeJson(label)).append("\",");
            snap.append("\"idle_time\":0,");
            snap.append("\"switch_count\":0,");
            snap.append("\"session_minutes\":").append(sessionMinutes).append(",");
            snap.append("\"dnd_active\":").append(dndActive).append(",");
            snap.append("\"charging\":").append(charging).append(",");
            snap.append("\"screen_on\":").append(screenOn);
            if (stepsToday >= 0) {
                snap.append(",\"steps_today\":").append(stepsToday);
            }
            snap.append(",\"headphones_connected\":").append(headphones);
            if (placeCategory != null) {
                snap.append(",\"place_category\":\"")
                    .append(escapeJson(placeCategory)).append("\"");
            }
            String body = "{"
                    + "\"trigger\":\"" + escapeJson(triggerValue) + "\","
                    + "\"device\":\"phone\","
                    + "\"snapshot\":{" + snap + "}}";

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
                .setSmallIcon(android.R.drawable.sym_def_app_icon)
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
