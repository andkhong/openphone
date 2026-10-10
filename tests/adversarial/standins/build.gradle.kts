plugins { id("com.android.application") version "8.7.3" apply false }

subprojects {
    // Each stand-in is one tiny Java app with no dependencies; its namespace is com.example.<module>.
    pluginManager.apply("com.android.application")
    extensions.configure<com.android.build.api.dsl.ApplicationExtension> {
        namespace = "com.example.${project.name}"
        compileSdk = 35
        defaultConfig {
            applicationId = "com.example.${project.name}"
            minSdk = 30
            targetSdk = 35
            versionCode = 1
            versionName = "1"
        }
        compileOptions {
            sourceCompatibility = JavaVersion.VERSION_17
            targetCompatibility = JavaVersion.VERSION_17
        }
    }
}
