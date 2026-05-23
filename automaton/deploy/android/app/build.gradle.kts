plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.automaton"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.automaton.app"
        minSdk = 26          // Android 8.0 — covers ~94% of active devices
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        compose = true
    }
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.10"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // Jetpack Compose BOM — keeps all Compose libraries version-consistent.
    val composeBom = platform("androidx.compose:compose-bom:2024.02.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.navigation:navigation-compose:2.7.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.7.0")

    // HTTP client
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // JSON serialization (mirrors iOS's Codable + convertFromSnakeCase)
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    // Token storage: EncryptedSharedPreferences so token survives
    // reinstalls but isn't readable by other apps.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // WorkManager for background polling (battery-safe on Android)
    implementation("androidx.work:work-runtime-ktx:2.9.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
}
