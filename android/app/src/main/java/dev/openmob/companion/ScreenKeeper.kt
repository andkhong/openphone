package dev.openmob.companion

import android.os.Handler
import android.os.Looper

/**
 * Keeps the screen on while the agent is actually working, so the lock screen doesn't appear mid-task (an owner
 * report: it stalled the agent). Every call the phone runs, and every "Working on" status, marks the phone busy;
 * the screen-on overlay (OpenMobAccessibilityService.keepScreenOn) goes away [IDLE_MS] after the last one, at
 * once when the task ends (a result or error event), and on Stop. It never holds the screen on while idle.
 */
object ScreenKeeper {
    private const val IDLE_MS = 20_000L
    private val main = Handler(Looper.getMainLooper())
    private val token = Any()
    @Volatile var busy = false
        private set

    /** Work is happening now (a call, or a task status). */
    fun touch() = main.post {
        busy = true
        OpenMobAccessibilityService.instance?.keepScreenOn(true)
        main.removeCallbacksAndMessages(token)
        main.postAtTime({ release() }, token, android.os.SystemClock.uptimeMillis() + IDLE_MS)
    }

    /** The task ended, or the owner pressed Stop: let the screen sleep as usual right away. */
    fun release() = main.post {
        busy = false
        main.removeCallbacksAndMessages(token)
        OpenMobAccessibilityService.instance?.keepScreenOn(false)
    }
}
