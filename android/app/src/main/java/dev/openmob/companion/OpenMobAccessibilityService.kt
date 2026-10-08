package dev.openmob.companion

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent

/**
 * The part of the app that can see the screen and act on it. The user turns it on in
 * Settings › Accessibility; PhoneActions does the actual work through this instance.
 */
class OpenMobAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: OpenMobAccessibilityService? = null
            private set

        private const val SYSTEM_UI = "com.android.systemui"
    }

    /** Foreground app, from window changes. Keyboards and system overlays are ignored. */
    @Volatile var currentPackage: String? = null
    /** Uptime when [currentPackage] was noted, and of the last window change: a remembered app older than the
     *  last window change may be the previous one (ForegroundApp). */
    @Volatile var currentPackageAt: Long = 0L
        private set
    @Volatile var windowChangeAt: Long = 0L
        private set

    /** Uptime (ms) of the last window or content change on screen: wait_idle returns once this is
     *  old enough (PhoneActions.waitIdle). */
    @Volatile var lastChangeAt: Long = 0L
        private set

    override fun onServiceConnected() {
        instance = this
        AppState.notifyListeners()
        if (AppState.returnAfterAccessibility) {
            // The owner just turned us on from the app's setup screen: take them back to it.
            AppState.returnAfterAccessibility = false
            try {
                startActivity(Intent(this, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
            } catch (e: RuntimeException) {
                // Android declined to switch apps: they come back themselves, nothing lost
            }
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        noteChange(event)
        if ((event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
                event.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED) && event.packageName?.toString() != packageName) {
            windowChangeAt = SystemClock.uptimeMillis()
        }
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val cls = event.className?.toString() ?: return
        // Activities only: IME and dialog windows would otherwise look like "the current app".
        if (cls.endsWith("Activity") || cls.contains(".activities.")) {
            event.packageName?.toString()?.let { currentPackage = it; currentPackageAt = SystemClock.uptimeMillis() }
        }
    }

    /** Only a timestamp, so it costs nothing however often the screen changes. Our own windows
     *  (overlay, notification) and the status bar's ticking content (clock, network speed) don't
     *  count: they would keep a settled screen looking busy. */
    private fun noteChange(event: AccessibilityEvent) {
        val pkg = event.packageName?.toString()
        if (pkg == packageName) return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED -> if (pkg == SYSTEM_UI) return
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED -> Unit
            else -> return
        }
        lastChangeAt = SystemClock.uptimeMillis()
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
