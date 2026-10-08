package dev.openmob.companion

import android.app.AppOpsManager
import org.junit.Assert.assertEquals
import org.junit.Test

class AccessibilitySetupTest {
    @Test
    fun downloadedApkMustReachTheDenialBeforeAppInfoCanOfferApproval() {
        // Android only exposes Allow restricted settings after the initial denial is shown.
        val modes = listOf(AppOpsManager.MODE_ERRORED, AppOpsManager.MODE_IGNORED, AppOpsManager.MODE_ALLOWED)
        assertEquals(
            listOf(AccessibilitySetup.Restriction.FIRST_ATTEMPT,
                AccessibilitySetup.Restriction.NEEDS_APPROVAL, AccessibilitySetup.Restriction.NONE),
            modes.map { AccessibilitySetup.restriction(33, it) },
        )
    }

    @Test
    fun modernDefaultDoesNotProveThatTheApkIsUnrestricted() {
        for (sdk in listOf(35, 36)) {
            assertEquals(AccessibilitySetup.Restriction.UNKNOWN,
                AccessibilitySetup.restriction(sdk, AppOpsManager.MODE_DEFAULT))
        }
    }

    @Test
    fun unreadableOrUnrecognisedModesKeepTheManualRecoveryPath() {
        for (mode in listOf(null, AppOpsManager.MODE_FOREGROUND, 999)) {
            assertEquals(AccessibilitySetup.Restriction.UNKNOWN, AccessibilitySetup.restriction(35, mode))
        }
    }

    @Test
    fun userApprovalClearsTheRestrictionOnNewAndroidToo() {
        assertEquals(AccessibilitySetup.Restriction.NONE,
            AccessibilitySetup.restriction(36, AppOpsManager.MODE_ALLOWED))
    }

    @Test
    fun androidBeforeThirteenDoesNotNeedRestrictedSettingsApproval() {
        for (sdk in listOf(30, 31, 32)) {
            assertEquals(AccessibilitySetup.Restriction.NONE,
                AccessibilitySetup.restriction(sdk, AppOpsManager.MODE_ERRORED))
        }
    }
}
