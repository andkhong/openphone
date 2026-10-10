// Stand-in apps for the emulator evil-server suite (tests/adversarial/evil_server.py). Never shipped.
// Build: ../../../android/gradlew -p . assembleDebug   (JAVA_HOME = JDK 17, ANDROID_HOME = the SDK)
pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
dependencyResolutionManagement { repositories { google(); mavenCentral() } }
rootProject.name = "GateStandIns"
include(":bank", ":shop", ":login")
