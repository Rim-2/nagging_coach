package com.naggingcoach.satellite;

import android.Manifest;
import android.app.AppOpsManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Process;
import android.provider.Settings;
import android.widget.Button;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
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

    // 위치 권한 요청 런처 — 장소(집/회사/카페) 감지용. 결과와 무관하게 위성은
    // 가동한다 (위치는 부가 기능이라 거부해도 나머지 트리거는 그대로 동작).
    private ActivityResultLauncher<String[]> locationPermLauncher;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        statusView = findViewById(R.id.status);
        Button permButton = findViewById(R.id.btn_permission);
        Button startButton = findViewById(R.id.btn_start);

        locationPermLauncher = registerForActivityResult(
                new ActivityResultContracts.RequestMultiplePermissions(),
                result -> {
                    boolean granted =
                            Boolean.TRUE.equals(result.get(Manifest.permission.ACCESS_FINE_LOCATION))
                            || Boolean.TRUE.equals(result.get(Manifest.permission.ACCESS_COARSE_LOCATION));
                    if (!granted) {
                        // 거부돼도 위성은 가동 — 장소 감지만 빠진다.
                        statusView.setText("위치 권한 없이 가동 (장소 감지 제외).");
                    }
                    startTracker();
                });

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
            // 위치 권한이 아직 없으면 요청 — 결과 콜백에서 위성을 가동한다.
            // 이미 있으면 곧장 가동.
            if (!hasLocationPermission()) {
                locationPermLauncher.launch(new String[]{
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION,
                });
            } else {
                startTracker();
            }
        });
    }

    private void startTracker() {
        Intent serviceIntent = new Intent(this, TrackerService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
        statusView.setText("위성 가동 중 — 알림창에서 상태 보임. 백그라운드 OK.");
    }

    private boolean hasLocationPermission() {
        return checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
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
