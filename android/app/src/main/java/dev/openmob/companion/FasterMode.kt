package dev.openmob.companion

import android.content.Context

/**
 * Faster mode (docs/design/faster-mode.md): while it's on, the server may send small screenshots of screens without
 * readable text to TypeSafe, a second AI service. Off by default.
 *
 * - **On** only from this app: the owner taps "Turn on" (this session only, or always on this phone), the server sends
 *   back a `mode_change` approval, and [ApprovalActivity] shows the warning ([gate.ModeChange.describe]) behind the
 *   screen lock. What the owner approved is kept here ([approved]).
 * - **Off** with one tap from the app or the ongoing notification (no lock), or from the dashboard or the API.
 * - The server says what it applies in each `permissions` push ([serverScope]). The phone shows "Faster mode is on"
 *   only for a scope its owner approved here, and tells the server to turn it off when the server claims more
 *   ([decide]). A compromised server could still send images without saying so: the threat model says that plainly.
 */
object FasterMode {
    private const val PREFS = "openmob_faster_mode"
    private const val KEY_APPROVED = "approved"

    /** What the server applies now: "phone", "session" or "off". */
    @Volatile var serverScope: String = "off"
        private set

    /** What the owner approved on this phone (screen lock): "phone", "session", or null. */
    @Volatile var approved: String? = null
        private set

    /** On, as the phone shows it: the server applies it and it is what the owner approved. */
    val isOn: Boolean get() = serverScope != "off" && serverScope == approved

    fun load(context: Context) {
        val saved = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_APPROVED, null)
        if (approved == null && saved == "phone") approved = saved // "session" never outlives the connection
    }

    /** The owner approved turning it on, with the screen lock. */
    fun approve(context: Context, scope: String) {
        if (scope != "phone" && scope != "session") return
        approved = scope
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_APPROVED, if (scope == "phone") "phone" else null).apply()
    }

    /** Off here at once (a tightening needs nothing), whoever turned it off. */
    fun turnOff(context: Context) {
        approved = null
        serverScope = "off"
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(KEY_APPROVED).apply()
    }

    /** The connection ended: a session-only Faster mode ends with it. */
    fun connectionEnded() {
        if (approved == "session") approved = null
        serverScope = "off"
    }

    /** A `permissions` push from the server. True: the server claims Faster mode is on in a way this phone's owner
     *  never approved, so the phone asks it to turn it off. */
    fun onServerPush(context: Context, scope: String): Boolean {
        val s = if (scope == "phone" || scope == "session") scope else "off"
        serverScope = s
        if (s == "off") {
            if (approved == "phone") turnOff(context) // turned off elsewhere (dashboard, API): the phone follows
            else if (approved == "session") approved = null
            return false
        }
        return decide(s, approved)
    }

    /** The rule [onServerPush] applies: anything on that isn't exactly what the owner approved is turned off. */
    fun decide(serverScope: String, approved: String?): Boolean = serverScope != "off" && serverScope != approved
}
