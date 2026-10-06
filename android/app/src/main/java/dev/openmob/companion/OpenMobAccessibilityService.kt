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
    /** Its activity class (e.g. com.android.settings.SubSettings). */
    @Volatile var currentActivity: String? = null
    /** The last window-state event from another app: its package and text (GScreen.eventPackage/eventText). */
    @Volatile var lastEventPackage: String = ""
    @Volatile var lastEventText: String = ""
    /** When that event arrived (uptime ms), and when each window id was first seen: a permission-controller event
     *  identifies a hidden window only if it isn't older than the window (GScreen.eventAt, GWindow.since). */
    @Volatile var lastEventAt: Long = 0L
    val windowSince = java.util.concurrent.ConcurrentHashMap<Int, Long>()

    private fun noteWindows() {
        val now = SystemClock.uptimeMillis()
        val ids = runCatching { windows.map { it.id } }.getOrDefault(emptyList())
        for (id in ids) windowSince.putIfAbsent(id, now)
        windowSince.keys.retainAll(ids.toSet())
    }
    private var keepAwake: android.view.View? = null

    /**
     * Keep the screen on while the agent works (the lock screen appearing mid-task stalls it): a 1-pixel,
     * untouchable accessibility overlay with FLAG_KEEP_SCREEN_ON. Held only while there is active work
     * ([ScreenKeeper]) and removed as soon as it goes idle. Main thread only.
     */
    fun keepScreenOn(on: Boolean) {
        val wm = getSystemService(android.view.WindowManager::class.java)
        if (on && keepAwake == null) {
            val view = android.view.View(this)
            val lp = android.view.WindowManager.LayoutParams(
                1, 1, android.view.WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
                android.graphics.PixelFormat.TRANSLUCENT,
            ).apply { gravity = android.view.Gravity.TOP or android.view.Gravity.START }
            if (runCatching { wm.addView(view, lp) }.isSuccess) keepAwake = view
        } else if (!on) {
            keepAwake?.let { runCatching { wm.removeView(it) } }
            keepAwake = null
        }
    }

    /** Uptime (ms) of the last window or content change on screen: wait_idle returns once this is
     *  old enough (PhoneActions.waitIdle). */
    @Volatile var lastChangeAt: Long = 0L
        private set
    /** Uptime of the last window change (a window or pane appeared or went): content updating inside a window,
     *  such as live odds, doesn't count. The gate's launch-race wait uses this, not [lastChangeAt]. */
    @Volatile var lastWindowChangeAt: Long = 0L
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
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
            event.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED) noteWindows()
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            // Android 14+ hides some system dialogs' trees, but their state events (package, text) still arrive.
            event.packageName?.toString()?.takeIf { it != packageName }?.let {
                lastEventPackage = it
                lastEventText = event.text.joinToString(" ") { t -> t?.toString().orEmpty() }.trim()
                lastEventAt = SystemClock.uptimeMillis()
            }
        }
        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
            event.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            // A blocked app may have just come up: the live view stops sending frames at once (StreamGate).
            dev.openmob.companion.gate.GateHost.refreshProtected(this)
            dev.openmob.companion.gate.GateHost.refreshOwnerDialog(this) // a hidden system dialog came or went
        }
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val cls = event.className?.toString() ?: return
        // Activities only: IME and dialog windows would otherwise look like "the current app".
        if (cls.endsWith("Activity") || cls.contains(".activities.")) {
            event.packageName?.toString()?.let { currentPackage = it }
            currentActivity = cls // the gatekeeper recognizes Settings pages by their activity too
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
            AccessibilityEvent.TYPE_WINDOWS_CHANGED -> lastWindowChangeAt = SystemClock.uptimeMillis()
            else -> return
        }
        lastChangeAt = SystemClock.uptimeMillis()
    }

    override fun onInterrupt() {}

    override fun onUnbind(intent: Intent?): Boolean {
        keepScreenOn(false)
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
