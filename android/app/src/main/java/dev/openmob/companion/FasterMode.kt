package dev.openmob.companion

import android.content.Context
import dev.openmob.companion.gate.ModeChange

/**
 * Faster mode (docs/design/faster-mode.md): while it's on, the server may send small screenshots of screens without
 * readable text to TypeSafe, a second AI service. Off by default. The phone is the authority:
 *
 * - **On** only from this app: the owner taps "Turn on", the server sends back a `mode_change` approval, and
 *   [ApprovalActivity] shows the warning ([ModeChange.describe]) behind the screen lock. The signed approval is
 *   kept here as the [Grant]: its scope and, for a session, its limits (hard expiry, idle time, reconnect window)
 *   are inside the signed change.
 * - **This session only** lasts until the owner disconnects (Disconnect, or the notification's Stop) or an hour
 *   without task activity (calls from the server). After a network blip of up to the grant's reconnect window the
 *   phone re-presents the same signed grant ([resumeMessage]); the server treats it as on only then.
 * - **Off** with one tap from the app or the ongoing notification (no lock), or from the dashboard or the API.
 * - The server says what it applies in each `permissions` push. The phone shows "Faster mode is on" only for an
 *   active grant of that scope, and tells the server to turn it off when the server claims more ([decide]).
 */
object FasterMode {
    private const val PREFS = "openmob_faster_mode"

    /** The owner's signed approval. Times in unix ms; [untilMs], [idleMs], [resumeMs] are 0 for "phone". */
    data class Grant(
        val scope: String, val approvalId: String, val taskId: String, val signature: String?, val timestamp: Long,
        val untilMs: Long = 0, val idleMs: Long = 0, val resumeMs: Long = 0,
    ) {
        /** Still good at [now]: a session within its signed expiry, not idle past its idle time, and, while
         *  disconnected ([disconnectedAt] > 0), not away longer than its reconnect window. "phone": always. */
        fun active(now: Long, lastActivity: Long, disconnectedAt: Long): Boolean = scope == "phone" || (
            scope == "session" && now < untilMs && now - lastActivity <= idleMs &&
                (disconnectedAt <= 0 || now - disconnectedAt <= resumeMs))
    }

    /** What the server applies now: "phone", "session" or "off". */
    @Volatile var serverScope: String = "off"
        private set

    @Volatile var grant: Grant? = null
        private set
    @Volatile private var lastActivity: Long = 0
    @Volatile private var disconnectedAt: Long = 0

    /** On, as the phone shows it: the server applies it, and it is an active grant of that scope. */
    val isOn: Boolean get() = grant?.let {
        serverScope != "off" && serverScope == it.scope && it.active(System.currentTimeMillis(), lastActivity, disconnectedAt)
    } ?: false

    /** The grant a signed approval makes, from the signed change itself (null: not a Faster mode change). Limits
     *  looser than the app allows are clamped to them. */
    fun grantFrom(input: String, approvalId: String, taskId: String, signature: String?, timestamp: Long): Grant? {
        val scope = ModeChange.fasterScope(input) ?: return null
        if (scope == "phone") return Grant(scope, approvalId, taskId, signature, timestamp)
        val p = ModeChange.patch(input)
        fun num(key: String) = (p[key] as? Number)?.toLong() ?: 0L
        val signedAt = if (timestamp > 0) timestamp * 1000 else System.currentTimeMillis() // unsigned: token mode
        val until = minOf(num("faster_until") * 1000, signedAt + MAX_SESSION_MS)
        return Grant(scope, approvalId, taskId, signature, timestamp, until,
                     minOf(num("faster_idle_s") * 1000, MAX_IDLE_MS), minOf(num("faster_resume_s") * 1000, MAX_RESUME_MS))
    }

    const val MAX_SESSION_MS = 86_400_000L
    const val MAX_IDLE_MS = 3_600_000L
    const val MAX_RESUME_MS = 300_000L

    fun load(context: Context) {
        if (grant != null) return
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (prefs.getString("scope", null) == "phone") { // a session grant never outlives the app process
            grant = Grant("phone", prefs.getString("approval_id", "").orEmpty(), prefs.getString("task_id", "").orEmpty(),
                          prefs.getString("signature", null), prefs.getLong("timestamp", 0))
        }
    }

    /** The owner approved turning it on, with the screen lock. */
    fun approve(context: Context, g: Grant) {
        grant = g
        lastActivity = System.currentTimeMillis()
        disconnectedAt = 0
        val edit = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear()
        if (g.scope == "phone") {
            edit.putString("scope", "phone").putString("approval_id", g.approvalId).putString("task_id", g.taskId)
                .putString("signature", g.signature).putLong("timestamp", g.timestamp)
        }
        edit.apply()
    }

    /** Off here at once (a tightening needs nothing), whoever turned it off. */
    fun turnOff(context: Context) {
        grant = null
        serverScope = "off"
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
    }

    /** A call from the server: the agent is working (a session-only grant's activity). */
    fun touch() { lastActivity = System.currentTimeMillis() }

    /** The owner disconnected (Disconnect, or Stop on the notification): a session-only grant ends. */
    fun ownerDisconnected() {
        if (grant?.scope == "session") grant = null
        serverScope = "off"
        disconnectedAt = 0
    }

    /** The connection dropped (not the owner's choice): a session-only grant waits for the reconnect. */
    fun connectionLost() {
        serverScope = "off"
        if (disconnectedAt == 0L) disconnectedAt = System.currentTimeMillis()
    }

    /** After a reconnect: the same signed grant again, if it is still good (else null, and it ends). */
    fun resumeMessage(now: Long = System.currentTimeMillis()): Map<String, Any?>? {
        val g = grant ?: return null
        if (g.scope != "session") return null
        if (!g.active(now, lastActivity, disconnectedAt)) {
            grant = null
            return null
        }
        disconnectedAt = 0
        // Exactly as the approval carried them: the signature and its time only when it was signed.
        return mapOf("type" to "faster_mode_resume", "approval_id" to g.approvalId) +
            (if (g.signature != null) mapOf("signature" to g.signature, "timestamp" to g.timestamp) else emptyMap())
    }

    /** A `permissions` push. True: the server claims Faster mode is on in a way this phone doesn't hold (never
     *  approved, another scope, or past the grant's signed limits), so the phone asks it to turn it off. */
    fun onServerPush(context: Context, scope: String, resumePending: Boolean = false): Boolean {
        val s = if (scope == "phone" || scope == "session") scope else "off"
        serverScope = s
        val now = System.currentTimeMillis()
        val g = grant
        if (s == "off") {
            // Turned off elsewhere (dashboard, API): the phone follows. A session waiting for its re-confirmation
            // after a reconnect stays (the phone re-presents it right after the welcome).
            if (g != null && !(resumePending && g.scope == "session")) turnOff(context)
            return false
        }
        val off = decide(s, g, now, lastActivity, disconnectedAt)
        if (off && g != null && !g.active(now, lastActivity, disconnectedAt)) turnOff(context)
        return off
    }

    /** The rule [onServerPush] applies: anything on that isn't an active grant of exactly that scope is turned off. */
    fun decide(serverScope: String, grant: Grant?, now: Long, lastActivity: Long, disconnectedAt: Long = 0): Boolean =
        serverScope != "off" && (grant == null || grant.scope != serverScope || !grant.active(now, lastActivity, disconnectedAt))
}
