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
    /** A blocked or unidentified app is on screen now: no frame goes out (StreamGate). */
    private val protectedNow: () -> Boolean = { false },
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
        stop()
        val target = bitrate.coerceIn(MIN_BITRATE, MAX_BITRATE)
        val encoder = MediaCodec.createEncoderByType(MIME)
        val caps = encoder.codecInfo.getCapabilitiesForType(MIME)
        // The size the viewer's panel needs, stepped down until this encoder accepts it.
        var side = maxSide.coerceIn(MIN_SIDE, MAX_SIDE)
        var (w, h) = encodedSize(side)
        while (side > MIN_SIDE && !caps.videoCapabilities.isSizeSupported(w, h)) {
            side -= 160
            encodedSize(side).let { w = it.first; h = it.second }
        }
        fun format(highProfile: Boolean, prependHeaders: Boolean = true) = MediaFormat.createVideoFormat(MIME, w, h).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, target)
            setInteger(MediaFormat.KEY_FRAME_RATE, fps.coerceIn(5, 60))
            // Viewers that join or fall behind ask for a keyframe, so periodic ones are only a safety
            // net; fewer of them leaves more bits for sharp text in between.
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 4)
            // A still screen produces no frames; repeat the last one so viewers that join (or skip
            // ahead) get a keyframe quickly, and a still screen keeps getting sharper.
            setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 250_000)
            setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0) // B-frames add a frame of delay
            setInteger(MediaFormat.KEY_LATENCY, 1) // hand out each frame as soon as it's encoded
            setInteger(MediaFormat.KEY_PRIORITY, 0) // realtime
            // SPS/PPS before each keyframe. Optional: the server also prepends the codec config it caches.
            if (prependHeaders) setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1)
            // VBR: a still screen costs almost nothing and a change (a keyframe of text) can take the
            // bits it needs. CBR evens every frame out, so keyframes and scrolling text come out soft.
            val modes = caps.encoderCapabilities
            when {
                modes.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR) ->
                    setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)
                modes.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR) ->
                    setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
            }
            // High profile (CABAC, 8x8 transforms): roughly 10-20% fewer bits for the same picture
            // than Baseline. Every browser's H.264 decoder handles it.
            val high = caps.profileLevels.filter { it.profile == MediaCodecInfo.CodecProfileLevel.AVCProfileHigh }
            if (highProfile && high.isNotEmpty()) {
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileHigh)
                setInteger(MediaFormat.KEY_LEVEL, high.maxOf { it.level })
            }
        }
        try {
            encoder.configure(format(highProfile = true), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        } catch (e: Exception) {
            // Some encoders list High but refuse the combination: the defaults always work.
            encoder.reset()
            try {
                encoder.configure(format(highProfile = false), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (e: Exception) {
                // Some software encoders (the emulator's c2.android.avc) reject the prepend-headers key.
                encoder.reset()
                encoder.configure(format(highProfile = false, prependHeaders = false), null, null,
                                  MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
        }
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
        // Upload queue limits scale with the bitrate, so "behind" means the same delay at any
        // quality (about a third of a second), with room for one large keyframe.
        val highWater = max(MIN_HIGH_WATER, target.toLong() / 8 * HIGH_WATER_MS / 1000)
        val lowWater = highWater / 8
        // Under a weak upload, trade frame rate (dropped frames) before sharpness: never go below
        // this, or text turns to blocks.
        val floor = max(MIN_BITRATE, target * 2 / 5)
        var dropping = false // upload fell behind: skip frames until a keyframe
        var lastDrop = 0L
        var lastRaise = SystemClock.uptimeMillis()
        val gate = StreamGate<Pair<JSONObject, ByteArray>>()
        try {
            while (codec === encoder) {
                val index = encoder.dequeueOutputBuffer(info, 50_000)
                val protectedScreen = protectedNow()
                for ((h, b) in gate.flush(SystemClock.uptimeMillis(), protectedScreen)) sink.send(h, b)
                if (gate.needKey && !protectedScreen && !keyRequested) {
                    keyRequested = true
                    requestSync(encoder) // the protected app is gone: restart from a fresh keyframe
                }
                val queued = sink.queuedBytes()
                val now = SystemClock.uptimeMillis()
                // Recovered for a while: step the bitrate back up towards the target.
                if (!dropping && bitrate < target && queued < lowWater && now - lastDrop > 3000 && now - lastRaise > 2000) {
                    bitrate = min(target, (bitrate * 1.25).toInt())
                    setBitrate(encoder, bitrate)
                    lastRaise = now
                }
                if (dropping && queued < lowWater && !keyRequested) {
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
                if (!config && !key && (dropping || queued > highWater)) {
                    if (!dropping) {
                        dropping = true
                        keyRequested = false
                        lastDrop = now
                        bitrate = max(floor, (bitrate * 0.6).toInt())
                        setBitrate(encoder, bitrate)
                    }
                    continue
                }
                if (key) {
                    dropping = false
                    keyRequested = false
                }
                val header = JSONObject().put("type", "video").put("key", key).put("config", config).put("pts", pts)
                for ((h, b) in gate.offer(SystemClock.uptimeMillis(), header to bytes, key, config, protectedNow())) sink.send(h, b)
            }
        } catch (e: IllegalStateException) {
            // The encoder was stopped under us: the stream is over.
        } finally {
            surface.release()
        }
    }

    /** Screen size scaled so the long side is at most [maxSide], in multiples of 16 (encoders want that). */
    private fun encodedSize(maxSide: Int): Pair<Int, Int> {
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
        const val HIGH_WATER_MS = 350L // this much video waiting to upload: start dropping frames
        const val MIN_HIGH_WATER = 128 * 1024L // but always room for one big keyframe
    }
}
