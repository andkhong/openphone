package dev.openmob.companion

/** Which listed packages an agent can open: not archived (Android 15 keeps an archived app listed after removing
 *  its APK), and with a launch intent. Pure, so it's unit-tested without a phone. */
object AppListing {
    fun launchable(listed: List<String>, archived: (String) -> Boolean, hasLaunchIntent: (String) -> Boolean): List<String> =
        listed.distinct().filter { !archived(it) && hasLaunchIntent(it) }.sorted()
}
