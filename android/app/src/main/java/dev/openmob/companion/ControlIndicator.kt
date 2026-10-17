package dev.openmob.companion

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Choreographer
import dev.openmob.companion.gate.GateHost
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The "an agent is controlling this phone" indicator: when it shows ([IndicatorPolicy]), with the owner's settings
 * ([IndicatorPrefs]), drawn by [IndicatorOverlay] from the accessibility service. See docs/design/control-indicator.md.
 *
 * The phone decides, from its own knowledge: every action the gatekeeper admits shows it before the action's
 * gesture runs ([beforeAct]); the phone's own approval prompt turns it amber; a task paused on the owner (locked,
 * a hidden system dialog, paused after denials) makes it dashed. The server can only end it early after its last
 * action ([ended]), never hide it while acting.
 */
object ControlIndicator {
    private val main = Handler(Looper.getMainLooper())
    private val token = Any()
    private val policy = IndicatorPolicy()
    private var overlay: IndicatorOverlay? = null
    private lateinit var app: Context
    @Volatile private var prefs: IndicatorPrefs? = null

    /** What is showing (or about to). */
    @Volatile var state: IndicatorState = IndicatorState.HIDDEN
        private set
    /** ConnectionService: the phone locked mid-task and the task waits for the owner to unlock. */
    @Volatile var lockPaused = false
        set(v) { field = v; refresh() }

    private const val SHOW_WAIT_MS = 500L

    fun init(context: Context) {
        if (!::app.isInitialized) app = context.applicationContext
    }

    fun prefs(context: Context): IndicatorPrefs {
        prefs?.let { return it }
        val sp = context.applicationContext.getSharedPreferences(IndicatorPrefs.FILE, Context.MODE_PRIVATE)
        return IndicatorPrefs(object : IndicatorStore {
            override fun bool(key: String, default: Boolean) = sp.getBoolean(key, default)
            override fun str(key: String, default: String) = sp.getString(key, default) ?: default
            override fun put(key: String, value: Boolean) = sp.edit().putBoolean(key, value).apply()
            override fun put(key: String, value: String) = sp.edit().putString(key, value).apply()
        }).also { prefs = it }
    }

    /**
     * On the call's thread, after the gatekeeper admitted an action and before its side effect: show the indicator
     * and wait (at most half a second) until its windows are on screen, so no agent action runs with it hidden.
     */
    fun beforeAct() {
        if (Looper.myLooper() == Looper.getMainLooper()) { policy.act(SystemClock.uptimeMillis()); apply(); return }
        val ready = CountDownLatch(1)
        main.post {
            val wasShown = overlay?.shown == true
            policy.act(SystemClock.uptimeMillis())
            apply()
            // Newly added: let it reach the screen (the next frame) before the gesture.
            if (!wasShown && overlay?.shown == true) Choreographer.getInstance().postFrameCallback { ready.countDown() }
            else ready.countDown()
        }
        ready.await(SHOW_WAIT_MS, TimeUnit.MILLISECONDS)
    }

    /** Any other call from the server (a read, a wait): keeps an active session's indicator up; never starts one. */
    fun call() = main.post { policy.call(SystemClock.uptimeMillis()); apply() }

    /** The server says the task ended (its result, or nobody in control any more). */
    fun ended() = main.post { policy.ended(SystemClock.uptimeMillis()); apply() }

    /** The owner stopped or disconnected: off at once. */
    fun stop() = main.post { policy.stop(); apply() }

    /** Something it depends on changed (a prompt, the lock, a dialog, the accessibility service, the settings). */
    fun refresh() = main.post { apply() }

    /** The owner's preview (OpenMob's own settings card). */
    fun preview(context: Context) {
        init(context)
        main.post { policy.preview(SystemClock.uptimeMillis(), 4_000); apply() }
    }

    /** Where the Stop pill is on screen, if it's showing: OCR drops its text (PermissionOcr). */
    fun ownRects(): List<IntArray> = listOfNotNull(overlay?.pillRect)

    fun serviceGone() = main.post { overlay?.removeAll(); overlay = null; state = IndicatorState.HIDDEN }

    private fun apply() {
        val now = SystemClock.uptimeMillis()
        policy.prompt = runCatching { GateHost.promptPending() }.getOrDefault(false)
        policy.paused = lockPaused || GateHost.ownerDialog != null || GateHost.suspended
        val next = policy.state(now)
        state = next
        main.removeCallbacksAndMessages(token)
        val service = OpenMobAccessibilityService.instance
        if (service == null || !::app.isInitialized) {
            overlay?.removeAll(); overlay = null
        } else {
            if (overlay?.let { it.owner !== service } == true) { overlay?.removeAll(); overlay = null }
            if (next != IndicatorState.HIDDEN && overlay == null) overlay = IndicatorOverlay(service, ::onPillTapped)
            overlay?.apply(next, prefs(app).snapshot())
        }
        // Look again when the hold runs out; while a prompt is open, every 2 s (it expires after 2 minutes).
        val again = listOfNotNull(policy.recheckIn(now), if (next == IndicatorState.APPROVAL) 2_000L else null).minOrNull()
        if (again != null) main.postAtTime({ apply() }, token, now + again)
    }

    /** The owner tapped Stop on the pill: the same as the notification's Stop (close the connection with 1000, so
     *  the server stops the task and clears its queue; every permit and prompt goes), and every phone prompt is
     *  withdrawn first, so the server hears it. During a preview only the preview ends. */
    private fun onPillTapped() {
        val now = SystemClock.uptimeMillis()
        if (!policy.agentActive(now) && !policy.prompt) { policy.endPreview(); apply(); return }
        GateHost.withdrawPrompts()
        policy.stop()
        apply()
        AppState.addEvent("status", app.getString(R.string.indicator_stopped_event))
        if (ConnectionService.isRunning) ConnectionService.stop(app)
    }
}
