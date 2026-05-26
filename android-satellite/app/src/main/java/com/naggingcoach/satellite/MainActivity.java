package com.naggingcoach.satellite;

import android.app.AppOpsManager;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Process;
import android.provider.Settings;
import android.widget.Button;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

/**
 * 권한 안내 + 위성 가동 버튼만 있는 minimal Activity.
 *
 * 흐름:
 *   1. 사용자가 'Usage Access 권한' 버튼 → 시스템 설정 화면 → 직접 허용
 *   2. 돌아오면 자동으로 권한 상태 갱신
 *   3. '위성 시작' 버튼 → Foreground Service 가동
 */
public class MainActivity extends AppCompatActivity {

    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        statusView = findViewById(R.id.status);
        Button permButton = findViewById(R.id.btn_permission);
        Button startButton = findViewById(R.id.btn_start);

        permButton.setOnClickListener(v -> {
            Intent intent = new Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS);
            // 사용자가 설정에서 우리 앱을 찾을 수 있게 호스트 명시 — Android 11+ 에선
            // 일부 OEM 이 무시하지만 표준 동작.
            startActivity(intent);
        });

        startButton.setOnClickListener(v -> {
            if (!hasUsageStatsPermission()) {
                statusView.setText("Usage Access 권한 먼저 켜야 해.");
                return;
            }
            Intent serviceIntent = new Intent(this, TrackerService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(serviceIntent);
            } else {
                startService(serviceIntent);
            }
            statusView.setText("위성 가동 중 — 알림창에서 상태 보임. 백그라운드 OK.");
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (hasUsageStatsPermission()) {
            statusView.setText("권한 OK · 아래 '위성 시작' 눌러줘.");
        } else {
            statusView.setText("Usage Access 권한 필요 — 위 버튼 눌러 시스템 설정에서 켜.");
        }
    }

    private boolean hasUsageStatsPermission() {
        AppOpsManager appOps = (AppOpsManager) getSystemService(APP_OPS_SERVICE);
        if (appOps == null) {
            return false;
        }
        int mode;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            mode = appOps.unsafeCheckOpNoThrow(
                    AppOpsManager.OPSTR_GET_USAGE_STATS,
                    Process.myUid(),
                    getPackageName());
        } else {
            mode = appOps.checkOpNoThrow(
                    AppOpsManager.OPSTR_GET_USAGE_STATS,
                    Process.myUid(),
                    getPackageName());
        }
        return mode == AppOpsManager.MODE_ALLOWED;
    }
}
