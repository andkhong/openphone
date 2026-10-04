package dev.openmob.companion

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.view.accessibility.AccessibilityEvent

/**
 * The part of the app that can see the screen and act on it. The user turns it on in
 * Settings › Accessibility; PhoneActions does the actual work through this instance.
 */
class OpenMobAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: OpenMobAccessibilityService? = null
            private set
    }

    /** Foreground app, from window changes. Keyboards and system overlays are ignored. */
    @Volatile var currentPackage: String? = null

    override fun onServiceConnected() {
        instance = this
        AppState.notifyListeners()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val cls = event.className?.toString() ?: return
        // Activities only: IME and dialog windows would otherwise look like "the current app".
        if (cls.endsWith("Activity") || cls.contains(".activities.")) {
            event.packageName?.toString()?.let { currentPackage = it }
        }
    }

    override fun onInterrupt() {}

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        AppState.notifyListeners()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        AppState.notifyListeners()
        super.onDestroy()
    }
}
