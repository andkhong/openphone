package dev.openmob.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
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
import android.speech.tts.TextToSpeech
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.net.URLEncoder
import java.nio.ByteBuffer
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/**
 * Foreground service holding the WebSocket to the OpenMob server (protocol v1, see
 * docs/phone-protocol.md). Runs calls through PhoneActions, reconnects with backoff, and keeps
 * a notification visible the whole time the agent can act, with a Disconnect button.
 *
 * If the owner allowed screen streaming when connecting, it also holds a [ScreenStreamer] so the
 * web dashboard can show the screen as smooth video (the agent keeps using screenshots).
 */
class ConnectionService : Service() {
    companion object {
        private const val ACTION_START = "dev.openmob.START"
        private const val ACTION_STOP = "dev.openmob.STOP"
        private const val CHANNEL = "agent"
        private const val NOTIFICATION_ID = 1
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

        /** Send a spoken or typed request to the agent. False if not connected. */
        fun sendCommand(text: String): Boolean = running?.send(
            JSONObject().put("type", "command").put("text", text)
        ) ?: false

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
    private var tts: TextToSpeech? = null
    @Volatile private var streamer: ScreenStreamer? = null
    @Volatile private var rtc: RtcStreamer? = null
    private lateinit var settings: Settings
    private val actions: PhoneActions?
        get() = OpenMobAccessibilityService.instance?.let { PhoneActions(it) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        tts = TextToSpeech(this) {}
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutDown("Disconnected")
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
        rtc?.release()
        rtc = null
        streamer?.release()
        streamer = try {
            val projection = getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data)
                ?: return
            ScreenStreamer(this, projection, object : ScreenStreamer.Sink {
                override fun send(header: JSONObject, data: ByteArray) = sendBinary(header, data)
                override fun queuedBytes() = socket?.queueSize() ?: 0L
            }) {
                main.post {
                    rtc?.release()
                    rtc = null
                    streamer = null
                    send(JSONObject().put("type", "video_state").put("available", false).put("rtc", false)
                        .put("reason", "Screen sharing stopped"))
                }
            }
        } catch (e: Exception) {
            null // e.g. the approval was already used; the dashboard falls back to screenshots
        }
        streamer?.let { screen ->
            // While WebRTC has the screen, the WebSocket stream isn't available (one display per approval).
            rtc = RtcStreamer(this, screen) { lent ->
                send(JSONObject().put("type", "video_state").put("available", !lent).put("rtc", true))
            }
            send(JSONObject().put("type", "video_state").put("available", true).put("rtc", true))
        }
    }

    override fun onDestroy() {
        shutDown(null)
        tts?.shutdown()
        calls.shutdownNow()
        super.onDestroy()
    }

    private fun connect() {
        if (stopped) return
        // Accounts mode authenticates with the device key in the hello; never leak the old token.
        val url = socketUrl(settings.server, if (settings.deviceKey.isNotEmpty()) "" else settings.token)
        AppState.setConnection("Connecting to ${Uri.parse(settings.server).host ?: settings.server}…", false)
        socket = client.newWebSocket(Request.Builder().url(url).build(), Listener())
    }

    private fun scheduleReconnect(reason: String) {
        if (stopped) return
        val delay = backoffSeconds * (0.8 + Random.nextDouble() * 0.4)
        AppState.setConnection("$reason. Retrying in ${delay.toInt().coerceAtLeast(1)} s…", false)
        updateNotification("Reconnecting to your agent…")
        main.postDelayed({ connect() }, (delay * 1000).toLong())
        backoffSeconds = (backoffSeconds * 2).coerceAtMost(30.0)
    }

    private fun shutDown(status: String?) {
        stopped = true
        main.removeCallbacksAndMessages(null)
        socket?.close(1000, "User disconnected")
        socket = null
        rtc?.release()
        rtc = null
        streamer?.release()
        streamer = null
        wakeLock?.takeIf { it.isHeld }?.release()
        running = null
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
        .put(
            "device", JSONObject()
                .put("id", settings.deviceId)
                .put("model", Build.MODEL)
                .put("manufacturer", Build.MANUFACTURER)
                .put("android", Build.VERSION.RELEASE)
                .put("app_version", packageManager.getPackageInfo(packageName, 0).versionName)
                .put("video", streamer?.let { it.alive && !it.lent } == true)
                .put("rtc", streamer?.alive == true && rtc != null)
        )

    private fun handleCall(message: JSONObject) {
        val id = message.getString("id")
        val method = message.getString("method")
        val params = message.optJSONObject("params") ?: JSONObject()
        calls.execute {
            val reply = JSONObject().put("type", "result").put("id", id)
            try {
                if (method.startsWith("stream_") || method.startsWith("rtc_")) {
                    reply.put("ok", true).put("result", handleStream(method, params) ?: JSONObject.NULL)
                    send(reply)
                    return@execute
                }
                val handler = actions
                    ?: error("Accessibility is off. Turn on OpenMob in Settings › Accessibility.")
                val result = handler.handle(method, params)
                if (result is PhoneActions.Frame) {
                    sendBinary(reply.put("ok", true).put("result", result.meta()), result.jpeg)
                    return@execute
                }
                reply.put("ok", true).put("result", result ?: JSONObject.NULL)
            } catch (e: Throwable) {
                reply.put("ok", false).put("error", "${e.javaClass.simpleName}: ${e.message}")
            }
            send(reply)
        }
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
            "rtc_offer" -> return JSONObject().put("sdp", (rtc ?: error("WebRTC isn't available")).answer(
                params.getString("session"), params.getString("sdp"), params.optJSONArray("ice_servers") ?: org.json.JSONArray(),
                params.optInt("max_side", 1280), params.optInt("bitrate", 2_500_000), params.optInt("fps", 30),
            ))
            "rtc_close" -> rtc?.close(params.getString("session"))
            else -> error("Unknown method $method")
        }
        return null
    }

    private fun handleEvent(kind: String, text: String) {
        AppState.addEvent(kind, text)
        if (kind == "question" || kind == "result") {
            tts?.speak(text.replace(Regex("""\(\d+ steps\)$"""), ""), TextToSpeech.QUEUE_FLUSH, null, kind)
        }
        if (kind == "status" && text.startsWith("Working on:")) updateNotification(text)
        if (kind == "result" || kind == "error") updateNotification("Agent connected · ready")
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

    private fun notification(text: String): Notification {
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
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("OpenMob agent can control this phone")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Disconnect", disconnect).build())
            .build()
    }

    private fun updateNotification(text: String) {
        if (running === this) {
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
        }
    }
}
