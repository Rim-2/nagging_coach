plugins {
    id("com.android.application")
}

android {
    namespace = "com.naggingcoach.satellite"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.naggingcoach.satellite"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"

        // 백엔드 URL · 인증 시크릿을 BuildConfig 로 주입.
        // 메인 봇과 같은 TRIGGER_SECRET 을 그대로 박는다 — 본인 폰에서만 쓰는 PoC.
        buildConfigField(
            "String",
            "NAGGING_COACH_URL",
            "\"https://naggingcoach-production.up.railway.app\"",
        )
        buildConfigField(
            "String",
            "TRIGGER_SECRET",
            "\"7gEUw8Iy01Hdirnqe3sl93dsqIZ8R__NoLIIHHSbdUA\"",
        )
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.6.1")
}
