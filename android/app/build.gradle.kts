plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.openmob.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.openmob.companion"
        minSdk = 30 // AccessibilityService.takeScreenshot
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        // Plain http:// is for a developer's own server on the local network. Release builds turn
        // cleartext off at the platform level too; in every build ServerPolicy only lets http://
        // reach local addresses (Android's network config can't express private IP ranges).
        debug {
            manifestPlaceholders["usesCleartext"] = "true"
        }
        release {
            isMinifyEnabled = false
            manifestPlaceholders["usesCleartext"] = "false"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    // The gatekeeper's shared scenario fixtures (also run by pytest through the Python reference engine).
    sourceSets.getByName("test").resources.srcDir("../../tests/fixtures/gatekeeper")
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
}
