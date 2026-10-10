package dev.openmob.companion

import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import dev.openmob.companion.gate.Admission
import dev.openmob.companion.gate.GateHost
import android.service.quicksettings.TileService
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.nio.ByteBuffer
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Foreground service holding the WebSocket to the OpenMob server (protocol v1, see
 * docs/phone-protocol.md). Runs calls through PhoneActions, reconnects with backoff, and keeps
 * a notification visible the whole time the agent can act, with a Disconnect button. While a
 * controller event names who is driving the phone, the notification says so ("Claude is using your
 * phone") and the button reads Stop. Approval requests go to [Approvals].
 *
 * If the owner allowed screen streaming when connecting, it also holds a [ScreenStreamer] so the
 * web dashboard can show the screen as smooth video (the agent keeps using screenshots).
 */
class ConnectionService : Service() {
    companion object {
        private const val ACTION_START = "dev.openmob.START"
        private const val ACTION_STOP = "dev.openmob.STOP"
        private const val ACTION_NEVER_OFF = "dev.openmob.NEVER_OFF"
        private const val ACTION_RESUME = "dev.openmob.GATE_RESUME"
        private const val LOCK_ID = 4 // "Unlock your phone to continue"
        private const val RESUME_ID = 5 // "Paused after repeated denials"
        private const val DIALOG_ID = 6 // "Kalshi is asking for location: choose on your phone"
        private const val CHANNEL = "agent"
        private const val NOTIFICATION_ID = 1
        private const val OFFER_ID = 3 // "Share your screen for the live view?"
        private const val PROTOCOL_VERSION = 1
        private const val CLOSE_BAD_TOKEN = 4401
        private const val CLOSE_BAD_HELLO = 4400
        private const val EXTRA_PROJECTION = "dev.openmob.PROJECTION"
        private const val EXTRA_PROJECTION_CODE = "dev.openmob.PROJECTION_CODE"

        @Volatile private var running: ConnectionService? = null

        /** [projection]: the result of Android's screen-capture consent, if the owner allowed it. */
        fun start(context: Context, projection: Intent? = null, projectionCode: Int = 0) = context.startForegroundService(
            Intent(context, ConnectionService::class.java).setAction(ACTION_START).apply {
                projection?.let { putExtra(EXTRA_PROJECTION, it).putExtra(EXTRA_PROJECTION_CODE, projectionCode) }
            }
        )

        fun stop(context: Context) = context.startService(
            Intent(context, ConnectionService::class.java).setAction(ACTION_STOP)
        )

        val isRunning: Boolean get() = running != null

        /** The owner's answer to an approval request (see Approvals). False if not connected. */
        fun sendApproval(
            id: String, approved: Boolean, signature: String? = null, timestamp: Long = 0, always: Boolean = false,
        ): Boolean =
            AppState.connected && running?.send(
                JSONObject().put("type", "approval").put("id", id).put("approved", approved).apply {
                    if (signature != null) put("signature", signature).put("timestamp", timestamp)
                    if (always) put("always", true) // "Always allow this", signed with `|always`
                }
            ) ?: false

        /** "Turn off" agents acting without asking: back to auto. A tightening, so nothing to sign. */
        fun turnOffNever(): Boolean = AppState.connected && running?.send(JSONObject().put("type", "permissions_off")) ?: false

        /** Send a spoken or typed request to the agent. False if not connected. Typed or spoken on this phone, it
         *  is a phone-known instruction: one matching send, post or delete it names runs without a prompt (D6). */
        fun sendCommand(text: String): Boolean = (running?.send(
            JSONObject().put("type", "command").put("text", text)
        ) ?: false).also { if (it) { GateHost.noteInstruction(text); ScreenKeeper.ownerActive() } }

        /** The gatekeeper paused after repeated denials: offer Resume in a notification. */
        fun showResume() { running?.let { s -> s.main.post { s.postResume() } } }

        /**
         * http(s)://host[/prefix] or ws(s)://… → ws(s)://host[/prefix]/phone/ws?t=<token>
         * (no query when [token] is empty, as in accounts mode where the key goes in the hello).
         */
        fun socketUrl(server: String, token: String): String {
            var base = server.trim().trimEnd('/')
            base = when {
                base.startsWith("https://") -> "wss://" + base.removePrefix("https://")
                base.startsWith("http://") -> "ws://" + base.removePrefix("http://")
                base.startsWith("wss://") || base.startsWith("ws://") -> base
                else -> "wss://$base"
            }
            if (token.isEmpty()) return "$base/phone/ws"
            return "$base/phone/ws?t=" + URLEncoder.encode(token, "UTF-8")
        }
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS) // keeps tunnels and NATs from dropping an idle socket
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private val calls = Executors.newFixedThreadPool(4)
    private val main = Handler(Looper.getMainLooper())
    private var socket: WebSocket? = null
    private var stopped = false
    private var backoffSeconds = 1.0
    private var wakeLock: PowerManager.WakeLock? = null
    private var statusLine = "" // the notification's current text, kept when the title changes
    @Volatile private var streamer: ScreenStreamer? = null
    private lateinit var settings: Settings
    private val actions: PhoneActions?
        get() = OpenMobAccessibilityService.instance?.let { PhoneActions(it) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        GateHost.init(this)
        GateHost.sink = { message -> if (running === this) send(message) }
        GateHost.onOwnerDialogClosed = { main.post { ownerDialogClosed() } }
        registerReceiver(lockWatcher, android.content.IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_OFF); addAction(Intent.ACTION_USER_PRESENT); addAction(Intent.ACTION_SCREEN_ON)
        })
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutDown("Disconnected")
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_RESUME) {
            GateHost.resume()
            getSystemService(NotificationManager::class.java).cancel(RESUME_ID)
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_NEVER_OFF) {
            GateHost.dropNever() // local and immediate: a tightening needs nothing
            if (turnOffNever()) {
                AppState.setNeverUntil(0)
                AppState.addEvent("status", getString(R.string.never_turned_off))
                updateNotification(statusLine)
            }
            return START_NOT_STICKY
        }
        @Suppress("DEPRECATION")
        val projection = intent?.getParcelableExtra<Intent>(EXTRA_PROJECTION)
        // Android 14+: the service must be a mediaProjection foreground service before it may use
        // the consent result, so declare that type whenever one came with this start.
        val types = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE or
            (if (projection != null) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION else 0)
        if (running == null) {
            running = this
            stopped = false
            ScreenKeeper.ownerActive() // the owner just pressed Connect
            startForeground(NOTIFICATION_ID, notification("Connecting to your agent…"), types)
            projection?.let { startStreaming(intent.getIntExtra(EXTRA_PROJECTION_CODE, 0), it) }
            // Keep the CPU awake so the agent can keep working with the screen off between steps.
            wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "openmob:agent").apply { acquire(4 * 60 * 60 * 1000L) }
            connect()
        } else if (projection != null) {
            startForeground(NOTIFICATION_ID, notification("Agent connected · ready"), types)
            startStreaming(intent.getIntExtra(EXTRA_PROJECTION_CODE, 0), projection)
        }
        return START_NOT_STICKY
    }

    /** Hold the screen-streaming approval for this connection; video only flows while someone watches. */
    private fun startStreaming(code: Int, data: Intent) {
        streamer?.release()
        streamer = try {
            val projection = getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data)
                ?: return
            ScreenStreamer(this, projection, object : ScreenStreamer.Sink {
                override fun send(header: JSONObject, data: ByteArray) = sendBinary(header, data)
                override fun queuedBytes() = socket?.queueSize() ?: 0L
            }, protectedNow = { GateHost.protectedNow() }, onStopped = {
                main.post {
                    streamer = null
                    send(JSONObject().put("type", "video_state").put("available", false).put("reason", "Screen sharing stopped"))
                    setScreenSharing(false)
                }
            })
        } catch (e: Exception) {
            null // e.g. the approval was already used; the dashboard falls back to screenshots
        }
        streamer?.let { send(JSONObject().put("type", "video_state").put("available", true)) }
        setScreenSharing(streamer != null)
    }

    private fun setScreenSharing(on: Boolean) {
        AppState.screenSharing = on
        if (on) getSystemService(NotificationManager::class.java).cancel(OFFER_ID)
        AppState.notifyListeners()
    }

    /**
     * Screen sharing isn't asked for at setup or on Connect. The first time someone opens this phone's
     * live view without it, offer it once: a notification that opens the app's "Share screen" step.
     */
    private fun offerScreenSharing() {
        if (streamer?.alive == true || !settings.smoothLiveView || running !== this) return
        AppState.liveViewRequested = true
        AppState.notifyListeners()
        val open = PendingIntent.getActivity(
            this, 2, Intent(this, MainActivity::class.java).setAction(Shortcuts.SHARE_SCREEN), PendingIntent.FLAG_IMMUTABLE,
        )
        getSystemService(NotificationManager::class.java).notify(
            OFFER_ID,
            Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setContentTitle(getString(R.string.live_view_offer_title))
                .setContentText(getString(R.string.live_view_offer_text))
                .setContentIntent(open)
                .setAutoCancel(true)
                .build(),
        )
    }

    override fun onDestroy() {
        runCatching { unregisterReceiver(lockWatcher) }
        shutDown(null)
        calls.shutdownNow()
        super.onDestroy()
    }

    private fun connect() {
        if (stopped) return
        // Checked again here, so a stored address the app wouldn't accept today is never used.
        if (!ServerPolicy.isAllowed(settings.server)) {
            shutDown("This server address isn't allowed: use https:// (plain http only on your own network). Pair again.")
            return
        }
        // Accounts mode authenticates with the device key in the hello; never leak the old token.
        val url = socketUrl(settings.server, if (settings.deviceKey.isNotEmpty()) "" else settings.token)
        AppState.setConnection("Connecting to ${Uri.parse(settings.server).host ?: settings.server}…", false)
        socket = client.newWebSocket(Request.Builder().url(url).build(), Listener())
    }

    private fun scheduleReconnect(reason: String) {
        if (stopped) return
        val delay = backoffSeconds * (0.8 + Random.nextDouble() * 0.4)
        AppState.setConnection("$reason. Retrying in ${delay.toInt().coerceAtLeast(1)} s…", false)
        setController("") // the server says again who is driving once we're back
        updateNotification("Reconnecting to your agent…")
        main.postDelayed({ connect() }, (delay * 1000).toLong())
        backoffSeconds = (backoffSeconds * 2).coerceAtMost(30.0)
    }

    private fun shutDown(status: String?) {
        stopped = true
        main.removeCallbacksAndMessages(null)
        socket?.close(1000, "User disconnected")
        socket = null
        streamer?.release()
        streamer = null
        setScreenSharing(false)
        getSystemService(NotificationManager::class.java).cancel(OFFER_ID)
        wakeLock?.takeIf { it.isHeld }?.release()
        running = null
        // The kill switch acts locally: every permit, prompt and purchase flow goes, and `never` ends (2.7).
        GateHost.revokeAll()
        ScreenKeeper.release()
        getSystemService(NotificationManager::class.java).cancel(LOCK_ID)
        Approvals.clear(this)
        setController("")
        status?.let { AppState.setConnection(it, false) }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /** The server refused our credentials (close 4401 or HTTP 401/403). */
    private fun rejected(webSocket: WebSocket) {
        // Ignore a late close from a socket we already replaced, so it can't wipe a fresh key.
        if (webSocket !== socket) return
        if (settings.deviceKey.isNotEmpty()) {
            settings.deviceKey = ""
            shutDown("This phone was unpaired or its key was rejected. Scan a new code from your dashboard.")
        } else {
            shutDown("Server rejected the pairing token. Scan the QR code again.")
        }
    }

    private fun send(message: JSONObject): Boolean = socket?.send(message.toString()) ?: false

    /** A result or video packet with raw bytes (a live-view screenshot, H.264) as one binary message: a 4-byte big-endian
     *  header length, the JSON [header], then [payload]. Saves base64's extra third and its encode time. */
    private fun sendBinary(header: JSONObject, payload: ByteArray): Boolean {
        val head = header.toString().toByteArray(Charsets.UTF_8)
        val frame = ByteBuffer.allocate(4 + head.size + payload.size).putInt(head.size).put(head).put(payload)
        return socket?.send(frame.array().toByteString()) ?: false
    }

    private fun hello() = JSONObject()
        .put("type", "hello")
        .put("protocol", PROTOCOL_VERSION)
        .apply { settings.deviceKey.takeIf { it.isNotEmpty() }?.let { put("key", it) } }
        // The screen-lock-bound key approvals are signed with (kept by the server the first time).
        .apply { if (settings.deviceKey.isNotEmpty()) ApprovalKeys.publicKey()?.let { put("approval_key", it) } }
        .put(
            "device", JSONObject()
                .put("id", settings.deviceId)
                .put("model", Build.MODEL)
                .put("manufacturer", Build.MANUFACTURER)
                .put("android", Build.VERSION.RELEASE)
                .put("app_version", packageManager.getPackageInfo(packageName, 0).versionName)
                .put("video", streamer?.alive == true)
                // Optional methods this app answers (older apps send none; the server falls back).
                .put("capabilities", JSONArray().put("wait_idle").put("resolve_url").put("wait_for").put("open_settings")
                    .put("approval_digest").put("gatekeeper").put("device_lock").put("task_grant"))
                // Which built-in rules and owner policy this phone enforces (the dashboard shows "Phone protection: on").
                .put("policy_hash", GateHost.policyHash())
                .put("gatekeeper_rules", GateHost.rulesVersion)
        )

    private fun handleCall(message: JSONObject) {
        val id = message.getString("id")
        val method = message.getString("method")
        val params = message.optJSONObject("params") ?: JSONObject()
        calls.execute {
            val reply = JSONObject().put("type", "result").put("id", id)
            try {
                // D1: the gatekeeper decides first, for every method. A server can at most request.
                val started = SystemClock.elapsedRealtime()
                val service = OpenMobAccessibilityService.instance
                val admission = GateHost.admit(service, method, toMap(params))
                if (admission.decision == "refuse" || admission.decision == "ask") {
                    send(gateReply(reply, admission, SystemClock.elapsedRealtime() - started))
                    return@execute
                }
                // Only an action the gate admitted keeps the screen on (B3): reads, waits and refusals never do.
                if (admission.allowed && method in GateHost.actMethods && method != "wake_unlock") ScreenKeeper.touch()
                if (admission.decision == "redact" && service != null) { // a blocked app is visible: black, or []
                    val result = PhoneActions(service).redacted(method, params)
                    if (result is PhoneActions.Frame) sendBinary(reply.put("ok", true).put("result", result.meta()), result.jpeg)
                    else send(reply.put("ok", true).put("result", result))
                    return@execute
                }
                if (method == "task_grant") { // admitted: the owner approved this task (or typed it on this phone)
                    send(reply.put("ok", true).put("result", JSONObject().put("granted", true).put("rule", admission.rule)))
                    return@execute
                }
                if (method == "device_state") { // no accessibility needed: is the phone usable right now?
                    reply.put("ok", true).put("result", deviceState())
                    send(reply)
                    return@execute
                }
                if (method.startsWith("stream_")) {
                    reply.put("ok", true).put("result", handleStream(method, params) ?: JSONObject.NULL)
                    send(reply)
                    return@execute
                }
                val handler = actions
                    ?: error("Accessibility is off. Turn on OpenMob in Settings › Accessibility.")
                var result = handler.handle(method, params, admission)
                // N4: a blocked app may have come up while the screen was being read: check again, after.
                if ((method == "screenshot" || method == "ui_elements" || method == "wait_for") && GateHost.refreshProtected(service)) {
                    result = handler.redacted(method, params)
                }
                if (result is PhoneActions.Frame) {
                    sendBinary(reply.put("ok", true).put("result", result.meta()), result.jpeg)
                    return@execute
                }
                reply.put("ok", true).put("result", result ?: JSONObject.NULL)
            } catch (e: GateRefusal) { // refused right before the side effect (the screen changed, D8)
                gateReply(reply, e.admission, 0)
            } catch (e: Throwable) {
                reply.put("ok", false).put("error", "${e.javaClass.simpleName}: ${e.message}")
            }
            send(reply)
        }
    }

    /** A refusal or a pending approval, with its stable code (docs/phone-protocol.md, "Gatekeeper"). */
    private fun gateReply(reply: JSONObject, a: Admission, ms: Long): JSONObject {
        reply.put("ok", false).put("code", a.code).put("error", a.message)
        val gate = JSONObject().put("rule", a.rule).put("package", a.pkg).put("ms", ms)
        if (a.decision == "ask") {
            reply.put("approval_id", a.approvalId)
            gate.put("kind", a.kind).put("question", a.wording)
        }
        if (a.code == "phone_locked") main.post { lockedMidTask() }
        if (a.code == "needs_owner_dialog") main.post { ownerDialogOpened(a.message) }
        return reply.put("gate", gate)
    }

    private fun toMap(o: JSONObject): Map<String, Any?> = o.keys().asSequence().associateWith { k ->
        when (val v = o.opt(k)) {
            is Int -> v.toLong()
            JSONObject.NULL -> null
            is JSONObject, is org.json.JSONArray -> v.toString()
            else -> v
        }
    }

    // --- the lock screen mid-task ---------------------------------------------------------------

    /** The screen went off or the keyguard came up while the agent was working. A swipe-only lock is woken and
     *  dismissed; a secure one pauses the task with "Unlock your phone to continue" until the owner unlocks. */
    private val lockWatcher = object : android.content.BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (running !== this@ConnectionService) return
            when (intent.action) {
                Intent.ACTION_USER_PRESENT -> { ScreenKeeper.ownerActive(); unlocked() }
                Intent.ACTION_SCREEN_OFF, Intent.ACTION_SCREEN_ON ->
                    if (ScreenKeeper.busy || AppState.controller.isNotEmpty()) main.postDelayed({ checkLock() }, 700)
            }
        }
    }

    private var pausedByLock = false

    private fun checkLock() {
        val keyguard = getSystemService(KeyguardManager::class.java)
        if (!keyguard.isKeyguardLocked) return
        // N5: the screen went off mid-task (the keep-awake overlay prevents timeouts, so most likely the owner's
        // power press). Never wake and unlock it behind their back, even a swipe lock: pause until they unlock.
        lockedMidTask(keyguard.isKeyguardSecure)
    }

    private fun lockedMidTask(secure: Boolean = true) {
        if (pausedByLock) return
        pausedByLock = true
        send(JSONObject().put("type", "device_lock").put("locked", true).put("secure", secure))
        AppState.addEvent("status", getString(R.string.lock_paused_event))
        val open = PendingIntent.getActivity(this, 3, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        getSystemService(NotificationManager::class.java).notify(LOCK_ID, Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentTitle(getString(R.string.lock_paused_title))
            .setContentText(getString(R.string.lock_paused_text))
            .setContentIntent(open).setAutoCancel(true).build())
    }

    private fun unlocked() {
        if (!pausedByLock) return
        pausedByLock = false
        send(JSONObject().put("type", "device_lock").put("locked", false).put("secure", true))
        getSystemService(NotificationManager::class.java).cancel(LOCK_ID)
        AppState.addEvent("status", getString(R.string.lock_resumed_event))
    }

    // --- system dialogs the phone can't read (Android 14+ hides permission dialogs) ------------------------

    private fun ownerDialogOpened(message: String) {
        if (GateHost.ownerDialog != null) return
        GateHost.ownerDialogOpened(message)
        send(JSONObject().put("type", "owner_dialog").put("open", true).put("message", message))
        AppState.addEvent("status", message)
        getSystemService(NotificationManager::class.java).notify(DIALOG_ID, Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(getString(R.string.owner_dialog_title))
            .setContentText(message)
            .setStyle(Notification.BigTextStyle().bigText(message))
            .build())
        // It may already have closed between the refusal and now.
        OpenMobAccessibilityService.instance?.let { GateHost.refreshOwnerDialog(it) }
    }

    private fun ownerDialogClosed() {
        send(JSONObject().put("type", "owner_dialog").put("open", false))
        getSystemService(NotificationManager::class.java).cancel(DIALOG_ID)
    }

    private fun postResume() {
        val resume = PendingIntent.getService(this, 4, Intent(this, ConnectionService::class.java).setAction(ACTION_RESUME),
            PendingIntent.FLAG_IMMUTABLE)
        getSystemService(NotificationManager::class.java).notify(RESUME_ID, Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_alert)
            .setContentTitle("OpenMob paused agents on this phone")
            .setContentText("You denied 3 requests in 10 minutes. Tap Resume when you want agents to continue.")
            .addAction(Notification.Action.Builder(null, getString(R.string.gate_resume), resume).build())
            .build())
    }

    /** Screen on and keyguard state, so the server can say "unlock to continue" instead of timing out.
     *  `secure`: a PIN, pattern, password or biometric lock, which only the owner can open. A non-secure
     *  (swipe) lock is dismissed by wake_unlock. */
    private fun deviceState(): JSONObject {
        val keyguard = getSystemService(KeyguardManager::class.java)
        return JSONObject()
            .put("interactive", getSystemService(PowerManager::class.java).isInteractive)
            .put("locked", keyguard.isKeyguardLocked)
            .put("secure", keyguard.isKeyguardSecure)
    }

    /** Live-view video calls (see ScreenStreamer). */
    private fun handleStream(method: String, params: JSONObject): JSONObject? {
        val video = streamer?.takeIf { it.alive }
            ?: error("Screen streaming isn't allowed on this connection. Reconnect the app and tap Start.")
        when (method) {
            "stream_start" -> return video.start(
                params.optInt("max_side", 1280), params.optInt("bitrate", 2_500_000), params.optInt("fps", 30),
            )
            "stream_stop" -> video.stop()
            "stream_keyframe" -> video.requestKeyframe()
            else -> error("Unknown method $method")
        }
        return null
    }

    private fun handleEvent(kind: String, text: String) {
        if (kind == "controller") {
            main.post { setController(text.trim()) }
            return
        }
        if (kind == "live_view") { // the dashboard opened the live view and this connection has no screen sharing
            main.post { offerScreenSharing() }
            return
        }
        AppState.addEvent(kind, text)
        if (kind == "status" && text.startsWith("Working on:")) updateNotification(text) // the server can't keep the screen on (B3)
        if (kind == "result" || kind == "error") { updateNotification("Agent connected · ready"); ScreenKeeper.release() }
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            webSocket.send(hello().toString())
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val message = try { JSONObject(text) } catch (e: Exception) { return }
            when (message.optString("type")) {
                "welcome" -> {
                    backoffSeconds = 1.0
                    AppState.setConnection("Connected to ${Uri.parse(settings.server).host ?: "server"}", true)
                    updateNotification("Agent connected · ready")
                }
                "call" -> handleCall(message)
                "event" -> handleEvent(message.optString("kind"), message.optString("message"))
                "approval_request" -> main.post {
                    if (running === this@ConnectionService) Approvals.request(this@ConnectionService, message)
                }
                // Answered on another of the owner's phones (or no longer needed): drop the prompt.
                "approval_cancel" -> main.post { Approvals.cancel(this@ConnectionService, message.optString("id")) }
                // The phone's permission mode: while agents may act without asking, the notification says so.
                "permissions" -> main.post {
                    // The banner and the mode come from this phone's own signed policy (GateHost), never from the
                    // server's claim. A server saying `never` is off is a tightening, taken from anywhere.
                    if (message.optString("ceiling") != "never") GateHost.dropNever() else GateHost.refreshBanner()
                    // Permission-dialog defaults: only tightenings are taken from the server (loosening is signed here).
                    if (message.has("everyday") && !message.optBoolean("everyday")) GateHost.everydayOff() // a tightening
                    message.optJSONObject("permission_prompts")?.let { p ->
                        p.keys().forEach { GateHost.tightenPermission(it, p.optString(it)) }
                    }
                    updateNotification(statusLine)
                    scheduleNeverExpiry()
                }
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, null)
            when (code) {
                CLOSE_BAD_TOKEN -> main.post { rejected(webSocket) }
                CLOSE_BAD_HELLO -> main.post { shutDown("Server and app versions don't match ($reason).") }
                else -> if (webSocket === socket) main.post { scheduleReconnect("Connection closed") }
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (webSocket !== socket) return
            if (response?.code == 401 || response?.code == 403) {
                main.post { rejected(webSocket) }
            } else {
                main.post { scheduleReconnect(t.message ?: "Connection failed") }
            }
        }
    }

    /** Who is using the phone right now ("" for nobody): shown in the notification, app and tile. */
    private fun setController(who: String) {
        val previous = AppState.controller
        if (who == previous) return
        AppState.setController(who)
        if (who.isNotEmpty()) {
            AppState.addEvent("controller", getString(R.string.controller_using, who))
        } else if (running === this) {
            AppState.addEvent("controller", getString(R.string.controller_stopped, previous))
        }
        updateNotification(statusLine)
        TileService.requestListeningState(this, ComponentName(this, OpenMobTileService::class.java))
    }

    /** Redraw the notification when `never` runs out, so the banner goes away on time. */
    private fun scheduleNeverExpiry() {
        main.removeCallbacksAndMessages(NEVER_TOKEN)
        val left = AppState.neverUntil * 1000 - System.currentTimeMillis()
        if (left > 0) main.postAtTime({ AppState.setNeverUntil(0); updateNotification(statusLine) }, NEVER_TOKEN,
                                      android.os.SystemClock.uptimeMillis() + left)
    }

    private val NEVER_TOKEN = Any()

    private fun notification(text: String): Notification {
        statusLine = text
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, "Agent connection", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val disconnect = PendingIntent.getService(
            this, 1, Intent(this, ConnectionService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        val who = AppState.controller
        // Don't ask: a persistent banner, with Turn off right in the notification.
        val neverUntil = AppState.neverUntil
        val never = neverUntil * 1000 > System.currentTimeMillis()
        val banner = if (never) getString(
            if (AppState.neverMoney) R.string.never_banner_money else R.string.never_banner,
            android.text.format.DateFormat.getTimeFormat(this).format(java.util.Date(neverUntil * 1000)),
        ) else ""
        val builder = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle(when {
                who.isNotEmpty() -> getString(R.string.controller_using, who)
                never -> banner
                else -> getString(R.string.notif_title)
            })
            .setContentText(if (never && who.isNotEmpty()) "$banner · $text" else text)
            .setOngoing(true)
            .setContentIntent(open)
        if (never) {
            val off = PendingIntent.getService(
                this, 2, Intent(this, ConnectionService::class.java).setAction(ACTION_NEVER_OFF), PendingIntent.FLAG_IMMUTABLE,
            )
            builder.setStyle(Notification.BigTextStyle().bigText("$banner\n$text"))
            builder.addAction(Notification.Action.Builder(null, getString(R.string.never_turn_off), off).build())
        }
        return builder
            .addAction(
                Notification.Action.Builder(
                    null, getString(if (who.isEmpty()) R.string.notif_disconnect else R.string.notif_stop), disconnect,
                ).build()
            )
            .build()
    }

    private fun updateNotification(text: String) {
        if (running === this) {
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
        }
    }
}
