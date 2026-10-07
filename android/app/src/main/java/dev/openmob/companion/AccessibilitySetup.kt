package dev.openmob.companion

import android.app.AppOpsManager

/** Read-only interpretation of Android's restricted-settings app op; never changes a permission. */
internal object AccessibilitySetup {
    enum class Restriction { NONE, FIRST_ATTEMPT, NEEDS_APPROVAL, UNKNOWN }

    fun restriction(sdk: Int, mode: Int?): Restriction {
        if (sdk < 33) return Restriction.NONE
        return when (mode) {
            AppOpsManager.MODE_ALLOWED -> Restriction.NONE
            AppOpsManager.MODE_ERRORED -> Restriction.FIRST_ATTEMPT
            AppOpsManager.MODE_IGNORED -> Restriction.NEEDS_APPROVAL
            // Newer Android decides MODE_DEFAULT using a privileged ECM check. It does not mean
            // unrestricted. Keep the user-facing App info shortcut available when we cannot know.
            else -> Restriction.UNKNOWN
        }
    }
}
