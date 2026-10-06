package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class AppListingTest {
    @Test
    fun archivedAndUnlaunchablePackagesAreLeftOut() {
        val listed = listOf("com.chess", "com.whatsapp", "com.whatsapp", "com.stub", "com.android.settings")
        val out = AppListing.launchable(listed, archived = { it == "com.chess" }, hasLaunchIntent = { it != "com.stub" })
        assertEquals(listOf("com.android.settings", "com.whatsapp"), out)
    }
}
