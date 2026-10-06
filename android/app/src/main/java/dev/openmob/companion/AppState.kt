package dev.openmob.companion

import android.content.Context
import android.os.Handler
import android.os.Looper
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList

/** In-process state shared by the connection service and the UI. Listeners run on the main thread. */
object AppState {
    data class Event(val kind: String, val message: String)

    /** The app sent the owner to Accessibility settings: once the service is on, bring the app back
     *  (OpenMobAccessibilityService.onServiceConnected) instead of making them navigate back. */
    @Volatile var returnAfterAccessibility: Boolean = false

    @Volatile var connection: String = "Disconnected"
        private set
    @Volatile var connected: Boolean = false
        private set
    /** Who the server says is driving the phone right now (e.g. "Claude"); "" for nobody. */
    @Volatile var controller: String = ""
        private set
    /** Agents may act without asking until this unix time (seconds); 0: they ask as usual. */
    @Volatile var neverUntil: Long = 0
        private set

    private val events = ArrayDeque<Event>()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun events(): List<Event> = synchronized(events) { events.toList() }

    fun setConnection(status: String, isConnected: Boolean) {
        connection = status
        connected = isConnected
        notifyListeners()
    }

    fun setController(who: String) {
        controller = who
        notifyListeners()
    }

    fun setNeverUntil(until: Long) {
        neverUntil = until
        notifyListeners()
    }

    fun addEvent(kind: String, message: String) {
        synchronized(events) {
            events.addFirst(Event(kind, message))
            while (events.size > 100) events.removeLast()
        }
        notifyListeners()
    }

    fun notifyListeners() = main.post { listeners.forEach { it() } }

    /** Returns an unsubscribe function. */
    fun listen(listener: () -> Unit): () -> Unit {
        listeners.add(listener)
        return { listeners.remove(listener) }
    }
}

/**
 * Server address, credentials, and this phone's stable id. Accounts mode uses [session] (the
 * signed-in OpenMob account) and [deviceKey] (this phone's own key from /api/pair/redeem);
 * legacy token mode uses [token]. Empty string means "none".
 */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("openmob", Context.MODE_PRIVATE)

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(value) = prefs.edit().putString("server", value.trim()).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value.trim()).apply()

    /** Account session for [server] (sent as X-OpenMob-Session). */
    var session: String
        get() = prefs.getString("session", "") ?: ""
        set(value) = prefs.edit().putString("session", value.trim()).apply()

    /** Email of the signed-in account, for display. */
    var email: String
        get() = prefs.getString("email", "") ?: ""
        set(value) = prefs.edit().putString("email", value.trim()).apply()

    /** Ask Android for screen sharing on Connect, so the web dashboard shows smooth video. Turned off
     *  when the owner declines that prompt, so declining is remembered rather than asked every time. */
    var smoothLiveView: Boolean
        get() = prefs.getBoolean("smooth_live_view", true)
        set(value) = prefs.edit().putBoolean("smooth_live_view", value).apply()

    /** This phone's device key, sent in the hello message. */
    var deviceKey: String
        get() = prefs.getString("device_key", "") ?: ""
        set(value) = prefs.edit().putString("device_key", value.trim()).apply()

    /** Stable across reconnects and app restarts, so the server keeps this phone's session. */
    val deviceId: String
        get() = prefs.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("device_id", it).apply()
        }
}
