package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ForegroundAppTest {
    private val ignored = setOf("com.android.systemui", "dev.openmob.companion")

    @Test
    fun theActiveWindowsPackageWins() {
        assertEquals("com.android.settings", ForegroundApp.pick("com.android.settings", "com.sec.android.gallery3d", 5, 1, ignored))
    }

    @Test
    fun anUnreadableWindowAfterAWindowChangeIsUnknownNotThePreviousApp() {
        // Gallery was noted at t=100; Settings' unreadable Developer options page came up at t=200.
        assertNull(ForegroundApp.pick(null, "com.sec.android.gallery3d", 100, 200, ignored))
    }

    @Test
    fun theRememberedAppStandsWhenNoWindowChangedSince() {
        assertEquals("com.example.game", ForegroundApp.pick(null, "com.example.game", 300, 200, ignored))
    }

    @Test
    fun ignoredPackagesAreNeverReported() {
        assertNull(ForegroundApp.pick("com.android.systemui", "dev.openmob.companion", 300, 200, ignored))
        assertEquals("com.android.settings", ForegroundApp.pick("com.android.systemui", "com.android.settings", 300, 200, ignored))
    }
}
