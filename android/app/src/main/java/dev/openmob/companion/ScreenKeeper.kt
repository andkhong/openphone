package dev.openmob.companion

import android.os.Handler
import android.os.Looper
import android.os.SystemClock

/**
 * When the screen may be held on (review B3; plain Kotlin, tested in the JVM). Only actions the gatekeeper admitted
 * count as work ([act]); reads, waits, refusals and anything the server says don't. And never for longer than
 * [capMs] after the owner last did something on the phone (unlocked it, typed or said a request, pressed Connect,
 * approved a prompt): after that the phone's normal screen timeout and lock apply again, however busy the agent is.
 */
class KeepAwakePolicy(private val idleMs: Long = 20_000, private val capMs: Long = 10 * 60_000) {
    private var lastOwner = Long.MIN_VALUE / 2
    private var lastAct = Long.MIN_VALUE / 2

    fun owner(now: Long) { lastOwner = now }
    fun act(now: Long) { lastAct = now }
    fun stop() { lastAct = Long.MIN_VALUE / 2 }

    fun hold(now: Long): Boolean = now - lastAct < idleMs && now - lastOwner < capMs

    /** When to look again (ms from now): the earlier of the idle and the cap deadline. */
    fun recheckIn(now: Long): Long = maxOf(1L, minOf(lastAct + idleMs, lastOwner + capMs) - now)
}

/**
 * Keeps the screen on while the agent is actually working, so the lock screen doesn't appear mid-task (an owner
 * report: it stalled the agent), using the screen-on overlay (OpenMobAccessibilityService.keepScreenOn), as
 * [KeepAwakePolicy] allows. Released at once when the task ends and on Stop.
 */
object ScreenKeeper {
    private val main = Handler(Looper.getMainLooper())
    private val token = Any()
    private val policy = KeepAwakePolicy()
    @Volatile var busy = false
        private set

    /** The owner did something on the phone: keep-awake may run for up to 10 minutes from now. */
    fun ownerActive() = main.post { policy.owner(SystemClock.uptimeMillis()); apply() }

    /** The gatekeeper admitted an action. */
    fun touch() = main.post {
        busy = true
        policy.act(SystemClock.uptimeMillis())
        apply()
    }

    /** The task ended, or the owner pressed Stop: let the screen sleep as usual right away. */
    fun release() = main.post {
        busy = false
        policy.stop()
        apply()
    }

    private fun apply() {
        val now = SystemClock.uptimeMillis()
        val hold = policy.hold(now)
        OpenMobAccessibilityService.instance?.keepScreenOn(hold)
        main.removeCallbacksAndMessages(token)
        if (hold) main.postAtTime({ apply() }, token, now + policy.recheckIn(now))
        else busy = false
    }
}
