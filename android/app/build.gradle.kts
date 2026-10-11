import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The owner's release key lives outside the repo (om keygen writes it; OPENMOB_SIGNING points elsewhere). Its
// passwords are read by Gradle only; never commit or print them. Without it, the signed builds fail to sign.
val signingFile = file(System.getenv("OPENMOB_SIGNING")
    ?: "${System.getProperty("user.home")}/.config/openmob-envs/release-signing.properties")

android {
    namespace = "dev.openmob.companion"
    compileSdk = 35

    defaultConfig {
        // 64-bit ARM only: the bundled OCR model (PermissionOcr) ships a native library per ABI (~11 MB each).
        ndk { abiFilters += listOf("arm64-v8a") }
        applicationId = "dev.openmob.companion"
        minSdk = 30 // AccessibilityService.takeScreenshot
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    signingConfigs {
        if (signingFile.exists()) {
            create("owner") {
                val p = Properties().apply { signingFile.inputStream().use { load(it) } }
                storeFile = file(p.getProperty("storeFile"))
                storePassword = p.getProperty("storePassword")
                keyAlias = p.getProperty("keyAlias")
                keyPassword = p.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        // Plain http:// is for a developer's own server on the local network. Release builds turn
        // cleartext off at the platform level too; in every build ServerPolicy only lets http://
        // reach local addresses (Android's network config can't express private IP ranges).
        debug {
            manifestPlaceholders["usesCleartext"] = "true"
        }
        // What phones download from a hosted server: the owner's key, not debuggable, https only.
        release {
            isMinifyEnabled = false
            manifestPlaceholders["usesCleartext"] = "false"
            signingConfig = signingConfigs.findByName("owner")
        }
        // The owner's own phone against their own server on the local network: the same key (so it updates
        // over a release install and back), not debuggable, and http:// to local addresses (ServerPolicy).
        create("local") {
            initWith(getByName("release"))
            manifestPlaceholders["usesCleartext"] = "true"
            matchingFallbacks += listOf("release")
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
    // On-device OCR of the permission dialogs Android 14+ hides from accessibility: the bundled Latin model
    // (offline, no network, no Play services download).
    implementation("com.google.mlkit:text-recognition:16.0.1")
    testImplementation("junit:junit:4.13.2")
}
