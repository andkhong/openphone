package dev.openmob.companion

import android.content.Context
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.view.Surface
import android.view.WindowManager
import org.json.JSONObject
import kotlin.math.max
import kotlin.math.min

/**
 * Smooth live view for the web dashboard: the screen as H.264 video (MediaProjection → hardware
 * encoder), sent as binary packets over the agent's WebSocket. The agent itself never uses this;
 * it keeps taking accessibility screenshots.
 *
 * The owner approves screen streaming once per connection. The projection (and its virtual
 * display) lives for the whole connection, but the encoder only runs between [start] and [stop],
 * i.e. while someone is watching. If the upload can't keep up (weak cellular signal), frames are
 * dropped until the next keyframe and the bitrate is lowered, so delay never builds up; it rises
 * again once the connection recovers.
 */
class ScreenStreamer(
    private val context: Context,
    private val projection: MediaProjection,
    private val sink: Sink,
    private val onStopped: () -> Unit,
) {
    interface Sink {
        /** Send one packet: [header] is {"type": "video", "key", "config", "pts"}. */
        fun send(header: JSONObject, data: ByteArray): Boolean
        /** Bytes waiting in the socket's send queue (how far the upload is behind). */
        fun queuedBytes(): Long
    }

    private val thread = HandlerThread("openmob-projection").apply { start() }
    private val handler = Handler(thread.looper)
    private val display: VirtualDisplay
    private val lock = Any()
    private var codec: MediaCodec? = null
    private var drain: Thread? = null
    @Volatile private var keyRequested = false
    @Volatile var alive = true
        private set
    /** The display is lent to WebRTC (see [lend]): no WebSocket stream until it's given back. */
    @Volatile var lent = false
        private set

    init {
        projection.registerCallback(object : MediaProjection.Callback() {
            // The owner stopped sharing (status bar chip), or the system ended it.
            override fun onStop() {
                alive = false
                stop()
                onStopped()
            }
        }, handler)
        val (w, h) = encodedSize(DEFAULT_MAX_SIDE)
        // One virtual display per approval (Android 14+): created now with no surface, and pointed
        // at an encoder's input surface whenever a stream runs.
        display = projection.createVirtualDisplay(
            "openmob-live", w, h, context.resources.displayMetrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, null, null, handler,
        )
    }

    /** Start encoding (stopping any stream already running). Returns the video's {width, height}. */
    fun start(maxSide: Int, bitrate: Int, fps: Int): JSONObject = synchronized(lock) {
        check(alive) { "Screen streaming ended. Reconnect the app to allow it again." }
        check(!lent) { "The screen is being sent over WebRTC right now." }
        stop()
        val (w, h) = encodedSize(maxSide.coerceIn(MIN_SIDE, MAX_SIDE))
        val target = bitrate.coerceIn(MIN_BITRATE, MAX_BITRATE)
        val encoder = MediaCodec.createEncoderByType(MIME)
        val format = MediaFormat.createVideoFormat(MIME, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, target)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps.coerceIn(5, 60))
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
            // A still screen produces no frames; repeat the last one so viewers that join (or skip
            // ahead) get a keyframe quickly, and the decoder doesn't sit on a stale frame.
            setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 250_000)
            setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0) // B-frames add a frame of delay
            setInteger(MediaFormat.KEY_LATENCY, 1)
            setInteger(MediaFormat.KEY_PRIORITY, 0) // realtime
            setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1) // SPS/PPS before each keyframe
            val caps = encoder.codecInfo.getCapabilitiesForType(MIME).encoderCapabilities
            if (caps.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)) {
                setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
            }
        }
        encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        val surface: Surface = encoder.createInputSurface()
        encoder.start()
        display.resize(w, h, context.resources.displayMetrics.densityDpi)
        display.surface = surface
        codec = encoder
        keyRequested = false
        drain = Thread({ drain(encoder, surface, target) }, "openmob-encoder").apply { start() }
        JSONObject().put("width", w).put("height", h)
    }

    /** Stop encoding (the projection stays approved for the next viewer). */
    fun stop() = synchronized(lock) {
        val encoder = codec ?: return@synchronized
        codec = null
        display.surface = null
        drain?.join(500)
        drain = null
        runCatching { encoder.stop() }
        encoder.release()
    }

    /** Point the screen at [surface] (WebRTC's capture), [width]×[height]; stops any WebSocket stream.
     *  One virtual display per approval means one consumer at a time. */
    fun lend(surface: Surface, width: Int, height: Int) = synchronized(lock) {
        check(alive) { "Screen streaming ended. Reconnect the app to allow it again." }
        stop()
        lent = true
        display.resize(width, height, context.resources.displayMetrics.densityDpi)
        display.surface = surface
    }

    fun giveBack() = synchronized(lock) {
        if (!lent) return@synchronized
        lent = false
        display.surface = null
    }

    fun requestKeyframe() {
        synchronized(lock) { codec }?.let { requestSync(it) }
    }

    /** End screen streaming for good (disconnect). */
    fun release() {
        alive = false
        stop()
        display.release()
        runCatching { projection.stop() }
        thread.quitSafely()
    }

    private fun requestSync(encoder: MediaCodec) {
        runCatching { encoder.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }) }
    }

    private fun setBitrate(encoder: MediaCodec, bitrate: Int) {
        runCatching { encoder.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bitrate) }) }
    }

    private fun drain(encoder: MediaCodec, surface: Surface, target: Int) {
        val info = MediaCodec.BufferInfo()
        var bitrate = target
        var dropping = false // upload fell behind: skip frames until a keyframe
        var lastDrop = 0L
        var lastRaise = SystemClock.uptimeMillis()
        try {
            while (codec === encoder) {
                val index = encoder.dequeueOutputBuffer(info, 50_000)
                val queued = sink.queuedBytes()
                val now = SystemClock.uptimeMillis()
                // Recovered for a while: step the bitrate back up towards the target.
                if (!dropping && bitrate < target && queued < LOW_WATER && now - lastDrop > 3000 && now - lastRaise > 2000) {
                    bitrate = min(target, (bitrate * 1.25).toInt())
                    setBitrate(encoder, bitrate)
                    lastRaise = now
                }
                if (dropping && queued < LOW_WATER && !keyRequested) {
                    keyRequested = true
                    requestSync(encoder) // caught up: restart from a fresh keyframe
                }
                if (index < 0) continue
                val buffer = encoder.getOutputBuffer(index)
                val bytes = ByteArray(info.size)
                buffer?.position(info.offset)
                buffer?.get(bytes, 0, info.size)
                val flags = info.flags
                val pts = info.presentationTimeUs
                encoder.releaseOutputBuffer(index, false)
                val config = flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                val key = flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
                if (!config && !key && (dropping || queued > HIGH_WATER)) {
                    if (!dropping) {
                        dropping = true
                        keyRequested = false
                        lastDrop = now
                        bitrate = max(MIN_BITRATE, (bitrate * 0.6).toInt())
                        setBitrate(encoder, bitrate)
                    }
                    continue
                }
                if (key) {
                    dropping = false
                    keyRequested = false
                }
                val header = JSONObject().put("type", "video").put("key", key).put("config", config).put("pts", pts)
                sink.send(header, bytes)
            }
        } catch (e: IllegalStateException) {
            // The encoder was stopped under us: the stream is over.
        } finally {
            surface.release()
        }
    }

    /** Screen size scaled so the long side is at most [maxSide], in multiples of 16 (encoders want that). */
    fun encodedSize(maxSide: Int): Pair<Int, Int> {
        val bounds = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds
        val scale = min(1.0, maxSide.toDouble() / max(bounds.width(), bounds.height()))
        val w = max(16, (bounds.width() * scale).toInt() / 16 * 16)
        val h = max(16, (bounds.height() * scale).toInt() / 16 * 16)
        return w to h
    }

    private companion object {
        const val MIME = MediaFormat.MIMETYPE_VIDEO_AVC
        const val DEFAULT_MAX_SIDE = 1280
        const val MIN_SIDE = 320
        const val MAX_SIDE = 1920
        const val MIN_BITRATE = 300_000
        const val MAX_BITRATE = 8_000_000
        const val HIGH_WATER = 256 * 1024L // ~1 s behind at 2 Mbps: start dropping
        const val LOW_WATER = 32 * 1024L // caught up
    }
}
