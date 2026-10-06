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
    /** Someone opened this phone's live view on the dashboard while it had no screen sharing: the
     *  setup checklist then offers it (screen sharing is never asked for at setup). */
    @Volatile var liveViewRequested: Boolean = false
    /** The connection holds a screen-sharing approval right now (smooth video on the dashboard). */
    @Volatile var screenSharing: Boolean = false

    /** Who the server says is driving the phone right now (e.g. "Claude"); "" for nobody. */
    @Volatile var controller: String = ""
        private set
    /** Agents may act without asking until this unix time (seconds); 0: they ask as usual. */
    @Volatile var neverUntil: Long = 0
        private set
    /** While never is on: purchases also run without asking ("Also allow purchases"). */
    @Volatile var neverMoney: Boolean = false
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

    fun setNeverUntil(until: Long, money: Boolean = false) {
        neverUntil = until
        neverMoney = until > 0 && money
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
 * Server address, credentials, and this phone's stable id. Accounts mode uses [deviceKey] (this
 * phone's own key from pairing, kept encrypted by a Keystore key: see [SecureStore]); legacy token
 * mode uses [token]. The phone never keeps a dashboard session. Empty string means "none".
 */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("openmob", Context.MODE_PRIVATE)

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(value) = prefs.edit().putString("server", value.trim()).apply()

    /** Legacy token mode (a developer's own server): also kept encrypted. */
    var token: String
        get() = secret("token")
        set(value) = putSecret("token", value)

    /** Email of the account this phone is paired to, for display only (not a credential). */
    var email: String
        get() = prefs.getString("email", "") ?: ""
        set(value) = prefs.edit().putString("email", value.trim()).apply()

    /** Offer screen sharing when someone opens the live view, so the web dashboard shows smooth video.
     *  Turned off when the owner declines Android's prompt, so declining is remembered. */
    var smoothLiveView: Boolean
        get() = prefs.getBoolean("smooth_live_view", true)
        set(value) = prefs.edit().putBoolean("smooth_live_view", value).apply()

    /** This phone's device key, sent in the hello message. */
    var deviceKey: String
        get() = secret("device_key")
        set(value) = putSecret("device_key", value)

    private companion object {
        val decrypted = java.util.concurrent.ConcurrentHashMap<String, String>()
    }

    /** Stable across reconnects and app restarts, so the server keeps this phone's session. */
    val deviceId: String
        get() = prefs.getString("device_id", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("device_id", it).apply()
        }

    /**
     * A dashboard session an older version of the app kept after pairing (enough to make API keys).
     * Returned once so it can be signed out on the server, and deleted from the phone.
     */
    fun takeLegacySession(): String {
        val session = prefs.getString("session", "") ?: ""
        if (prefs.contains("session")) prefs.edit().remove("session").apply()
        return session
    }

    /** Encrypted values live under "<name>_enc"; a plaintext one from an older app is moved over once. */
    private fun secret(name: String): String {
        prefs.getString("${name}_enc", null)?.let { blob ->
            // Keystore operations take milliseconds (more with StrongBox) and the screen reads this often.
            return decrypted[blob] ?: SecureStore.decrypt(blob).orEmpty().also { if (it.isNotEmpty()) decrypted[blob] = it }
        }
        val legacy = prefs.getString(name, "") ?: ""
        if (legacy.isNotEmpty()) putSecret(name, legacy)
        return legacy
    }

    private fun putSecret(name: String, value: String) {
        val v = value.trim()
        val edit = prefs.edit().remove(name)
        if (v.isEmpty()) edit.remove("${name}_enc") else edit.putString("${name}_enc", SecureStore.encrypt(v))
        edit.commit() // written before anything connects with it
    }
}
