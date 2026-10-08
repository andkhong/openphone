package dev.openmob.companion

/*
 * The Settings pages the `open_settings` method can open (docs/phone-protocol.md §4.17, docs/design/deep-links.md).
 *
 * The server sends only a page *key*; this fixed table maps it to an Android Settings intent action. The server
 * can never name an action, component or extra, so whatever it sends, the phone reaches only these pages.
 * Security-critical pages (accessibility, language, security, developer options, special app access…) have no
 * key at all. The server's table (openmob/phone/settings_pages.py) must match this one: a Python test compares
 * the two files, so keep the entries one per line in the shape below.
 *
 * Plain Kotlin, no Android imports: the JVM unit tests run it. The action strings are the values of the
 * android.provider.Settings constants named in the comments (checked against the SDK's android.jar).
 */
object SettingsPages {
    enum class Tier { EVERYDAY, ASK }

    class Page(val key: String, val action: String, val title: String, val tier: Tier, val needsPackage: Boolean)

    /** What a validated request opens: the page and, for an app's own pages, the app. */
    class Request(val page: Page, val pkg: String?)

    private fun page(key: String, action: String, title: String, tier: Tier = Tier.EVERYDAY, needsPackage: Boolean = false) =
        key to Page(key, action, title, tier, needsPackage)

    /** key -> page. One entry per line (the Python parity test reads them). */
    val ALL: Map<String, Page> = linkedMapOf(
        page("display", "android.settings.DISPLAY_SETTINGS", "Display"), // Settings.ACTION_DISPLAY_SETTINGS
        page("wifi", "android.settings.WIFI_SETTINGS", "Wi-Fi"), // Settings.ACTION_WIFI_SETTINGS
        page("bluetooth", "android.settings.BLUETOOTH_SETTINGS", "Bluetooth"), // Settings.ACTION_BLUETOOTH_SETTINGS
        page("sound", "android.settings.SOUND_SETTINGS", "Sound"), // Settings.ACTION_SOUND_SETTINGS
        page("battery", "android.intent.action.POWER_USAGE_SUMMARY", "Battery"), // Intent.ACTION_POWER_USAGE_SUMMARY
        page("location", "android.settings.LOCATION_SOURCE_SETTINGS", "Location"), // Settings.ACTION_LOCATION_SOURCE_SETTINGS
        page("notifications", "android.settings.NOTIFICATION_SETTINGS", "Notifications"), // not a public constant; AOSP Settings handles it
        page("apps", "android.settings.MANAGE_APPLICATIONS_SETTINGS", "Apps"), // Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS
        page("storage", "android.settings.INTERNAL_STORAGE_SETTINGS", "Storage"), // Settings.ACTION_INTERNAL_STORAGE_SETTINGS
        page("airplane_mode", "android.settings.AIRPLANE_MODE_SETTINGS", "Airplane mode"), // Settings.ACTION_AIRPLANE_MODE_SETTINGS
        page("date_time", "android.settings.DATE_SETTINGS", "Date & time", Tier.ASK), // Settings.ACTION_DATE_SETTINGS; manual date and time asks (gatekeeper D2)
        page("app_details", "android.settings.APPLICATION_DETAILS_SETTINGS", "App info", needsPackage = true), // Settings.ACTION_APPLICATION_DETAILS_SETTINGS, data package:<pkg>
        page("app_notifications", "android.settings.APP_NOTIFICATION_SETTINGS", "App notifications", needsPackage = true), // Settings.ACTION_APP_NOTIFICATION_SETTINGS, extra APP_PACKAGE
    )

    /** The intent extra an app's notification page takes (Settings.EXTRA_APP_PACKAGE). */
    const val EXTRA_APP_PACKAGE = "android.provider.extra.APP_PACKAGE"

    /** An Android application id: dotted identifiers, each starting with a letter. */
    val PACKAGE = Regex("^[A-Za-z][A-Za-z0-9_]*(\\.[A-Za-z][A-Za-z0-9_]*)+$")
    const val OWN_PACKAGE = "dev.openmob.companion"

    fun lookup(key: String): Page? = ALL[key]

    fun validPackage(pkg: String?): Boolean = !pkg.isNullOrEmpty() && pkg.length <= 255 && PACKAGE.matches(pkg)

    /** Validate a call's params: the key must be in the table, an app page needs a well-formed package that isn't
     *  OpenMob's own, and a package on a page that takes none is ignored. Throws with a message for the agent. */
    fun request(key: String, pkg: String?): Request {
        val page = lookup(key) ?: throw IllegalArgumentException(
            "Unknown Settings page '$key'. Known pages: ${ALL.keys.joinToString(", ")}.")
        if (!page.needsPackage) return Request(page, null)
        if (!validPackage(pkg)) throw IllegalArgumentException("The ${page.title} page needs an app's package name.")
        if (pkg == OWN_PACKAGE) throw IllegalArgumentException("OpenMob's own Settings pages are off-limits to agents.")
        return Request(page, pkg)
    }
}
