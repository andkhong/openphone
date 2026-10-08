package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The fixed key-to-intent table behind `open_settings`: only its pages, with validated packages. */
class SettingsPagesTest {
    @Test fun theTableHoldsTheEverydayPagesAndNoSecurityCriticalOnes() {
        assertEquals(listOf("display", "wifi", "bluetooth", "sound", "battery", "location", "notifications", "apps",
            "storage", "airplane_mode", "date_time", "app_details", "app_notifications"), SettingsPages.ALL.keys.toList())
        for (p in SettingsPages.ALL.values) {
            assertTrue(p.key, p.action.startsWith("android.settings.") || p.action.startsWith("android.intent.action."))
            // Nothing the gatekeeper hard-denies can be reached through a key (docs/design/phone-gatekeeper.md §2.3).
            assertFalse(p.action, Regex("(?i)ACCESSIBILITY|DEVELOPMENT|SECURITY|VPN|LOCALE|LANGUAGE|USER|ACCOUNT|" +
                "DEVICE_ADMIN|NOTIFICATION_LISTENER|NOTIFICATION_POLICY|USAGE_ACCESS|MANAGE_UNKNOWN|OVERLAY|" +
                "INPUT_METHOD|WIRELESS_DEBUG|RESET").containsMatchIn(p.action))
        }
        assertEquals(SettingsPages.Tier.ASK, SettingsPages.lookup("date_time")!!.tier)
        assertEquals(listOf("app_details", "app_notifications"), SettingsPages.ALL.values.filter { it.needsPackage }.map { it.key })
    }

    @Test fun anUnknownKeyIsRefused() {
        assertNull(SettingsPages.lookup("accessibility"))
        assertNull(SettingsPages.lookup("android.settings.ACCESSIBILITY_SETTINGS"))
        val e = runCatching { SettingsPages.request("developer_options", null) }.exceptionOrNull()
        assertTrue(e is IllegalArgumentException && e.message!!.startsWith("Unknown Settings page 'developer_options'"))
        // The server can't smuggle an action or a component through the key.
        assertTrue(runCatching { SettingsPages.request("display;component=com.android.settings/.Foo", null) }.isFailure)
    }

    @Test fun appPagesNeedAWellFormedPackageThatIsntOpenMob() {
        val ok = SettingsPages.request("app_details", "com.android.chrome")
        assertEquals("android.settings.APPLICATION_DETAILS_SETTINGS", ok.page.action)
        assertEquals("com.android.chrome", ok.pkg)
        for (bad in listOf(null, "", "chrome", "com.android.chrome; rm -rf", "package:com.android.chrome", ".com.x",
                           "com..x", "com.1x", "a".repeat(300))) {
            val e = runCatching { SettingsPages.request("app_notifications", bad) }.exceptionOrNull()
            assertTrue("$bad", e is IllegalArgumentException && e.message!!.contains("needs an app's package name"))
        }
        val own = runCatching { SettingsPages.request("app_details", SettingsPages.OWN_PACKAGE) }.exceptionOrNull()
        assertTrue(own is IllegalArgumentException && own.message!!.contains("OpenMob"))
    }

    @Test fun aPackageOnAPlainPageIsIgnored() {
        val r = SettingsPages.request("display", "com.android.chrome")
        assertEquals("display", r.page.key)
        assertNull(r.pkg)
    }
}
