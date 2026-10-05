package dev.openmob.companion

import android.content.Context
import android.view.Surface
import org.json.JSONArray
import org.json.JSONObject
import org.webrtc.DataChannel
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.Logging
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpParameters
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoSource
import org.webrtc.VideoTrack
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The dashboard's live view over WebRTC: the screen goes straight to the viewer's browser when the
 * network allows (else through the server's TURN relay), with WebRTC's own congestion control and
 * loss recovery. Lowest delay, and no freezes on a lossy cellular link.
 *
 * The server only relays the browser's offer and this phone's answer, after checking the viewer may
 * watch; it closes the session when the viewer's socket closes. Screen frames come from
 * [ScreenStreamer]'s virtual display, lent to WebRTC while at least one session is open.
 */
class RtcStreamer(
    private val context: Context,
    private val screen: ScreenStreamer,
    private val onLent: (Boolean) -> Unit, // true while WebRTC has the screen (no WebSocket stream then)
) {
    private val lock = Any()
    private val sessions = HashMap<String, PeerConnection>()
    private var egl: EglBase? = null
    private var factory: PeerConnectionFactory? = null
    private var source: VideoSource? = null
    private var track: VideoTrack? = null
    private var helper: SurfaceTextureHelper? = null
    private var surface: Surface? = null

    /** Answer the browser's [offer] for [session]; returns the answer SDP with this phone's ICE candidates.
     *  Blocks (call off the main thread). */
    fun answer(session: String, offer: String, iceServers: JSONArray, maxSide: Int, bitrate: Int, fps: Int): String {
        close(session)
        val video = synchronized(lock) { startCapture(maxSide, fps) }
        val gathered = CountDownLatch(1)
        val config = PeerConnection.RTCConfiguration(parseIceServers(iceServers)).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.REQUIRE
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            // On Wi-Fi, don't offer the cellular network too: the browser could pick it and the
            // video would cost mobile data and take the long way round. Cellular-only still works.
            candidateNetworkPolicy = PeerConnection.CandidateNetworkPolicy.LOW_COST
        }
        val pc = factory!!.createPeerConnection(config, object : PeerConnection.Observer {
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
                if (state == PeerConnection.IceGatheringState.COMPLETE) gathered.countDown()
            }
            override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
                if (state == PeerConnection.PeerConnectionState.FAILED) Thread { close(session) }.start()
            }
            override fun onSignalingChange(state: PeerConnection.SignalingState) {}
            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {}
            override fun onIceConnectionReceivingChange(receiving: Boolean) {}
            override fun onIceCandidate(candidate: IceCandidate) {}
            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}
            override fun onAddStream(stream: MediaStream) {}
            override fun onRemoveStream(stream: MediaStream) {}
            override fun onDataChannel(channel: DataChannel) {}
            override fun onRenegotiationNeeded() {}
        }) ?: error("Couldn't create a WebRTC connection")
        synchronized(lock) { sessions[session] = pc }
        try {
            await("set the offer") { pc.setRemoteDescription(it, SessionDescription(SessionDescription.Type.OFFER, offer)) }
            // The browser offered to receive video; send the screen on that transceiver.
            val transceiver = pc.transceivers.firstOrNull { it.mediaType == MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO }
                ?: error("The offer has no video")
            transceiver.direction = RtpTransceiver.RtpTransceiverDirection.SEND_ONLY
            transceiver.sender.setTrack(video, false)
            transceiver.sender.setStreams(listOf("screen"))
            val answer = awaitCreate { pc.createAnswer(it, MediaConstraints()) }
            await("set the answer") { pc.setLocalDescription(it, answer) }
            // Start at full quality (WebRTC otherwise starts at ~300 kbps and a tiny picture, and a
            // still screen sends no frames to ramp up with); congestion control still backs off.
            pc.setBitrate(MIN_BITRATE, bitrate, bitrate)
            transceiver.sender.parameters.let { params ->
                // Under congestion lower quality and frame rate together, rather than freezing.
                params.degradationPreference = RtpParameters.DegradationPreference.BALANCED
                params.encodings.forEach { it.maxBitrateBps = bitrate; it.maxFramerate = fps }
                transceiver.sender.setParameters(params)
            }
            // No trickle ICE: the answer carries every candidate (bounded wait; STUN is usually fast).
            gathered.await(ICE_GATHER_MS, TimeUnit.MILLISECONDS)
            return pc.localDescription?.description ?: error("No local description")
        } catch (e: Exception) {
            close(session)
            throw e
        }
    }

    fun close(session: String) {
        val pc = synchronized(lock) { sessions.remove(session) } ?: return
        pc.close()
        pc.dispose()
        synchronized(lock) { if (sessions.isEmpty()) stopCapture() }
    }

    fun release() {
        synchronized(lock) { sessions.keys.toList() }.forEach { close(it) }
        synchronized(lock) {
            stopCapture()
            track?.dispose(); track = null
            source?.dispose(); source = null
            factory?.dispose(); factory = null
            egl?.release(); egl = null
        }
    }

    /** Lend the screen to WebRTC on the first session; returns the screen's video track. */
    private fun startCapture(maxSide: Int, fps: Int): VideoTrack {
        ensureFactory()
        if (helper == null) {
            val (w, h) = screen.encodedSize(maxSide)
            val textures = SurfaceTextureHelper.create("openmob-rtc", egl!!.eglBaseContext)
            textures.setTextureSize(w, h)
            val target = Surface(textures.surfaceTexture)
            val observer = source!!.capturerObserver
            observer.onCapturerStarted(true)
            textures.startListening { frame -> observer.onFrameCaptured(frame) }
            source!!.adaptOutputFormat(w, h, fps)
            screen.lend(target, w, h)
            helper = textures
            surface = target
            onLent(true)
        }
        return track!!
    }

    private fun stopCapture() {
        val textures = helper ?: return
        helper = null
        screen.giveBack()
        textures.stopListening()
        source?.capturerObserver?.onCapturerStopped()
        surface?.release(); surface = null
        textures.dispose()
        onLent(false)
    }

    private fun ensureFactory() {
        if (factory != null) return
        if (!initialized) {
            PeerConnectionFactory.initialize(PeerConnectionFactory.InitializationOptions.builder(context).createInitializationOptions())
            Logging.enableLogToDebugOutput(Logging.Severity.LS_ERROR) // so a native failure says why in logcat
            initialized = true
        }
        val base = EglBase.create()
        egl = base
        factory = PeerConnectionFactory.builder()
            // Hardware H.264 (High profile) when the phone has it; the browser is asked to prefer H.264.
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(base.eglBaseContext, true, true))
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(base.eglBaseContext))
            .createPeerConnectionFactory()
        source = factory!!.createVideoSource(false) // not "screencast": keep motion smooth over sharpness
        track = factory!!.createVideoTrack("screen", source)
    }

    private fun parseIceServers(list: JSONArray): List<PeerConnection.IceServer> = (0 until list.length()).mapNotNull { i ->
        val server = list.optJSONObject(i) ?: return@mapNotNull null
        val urls = server.optJSONArray("urls")?.let { a -> (0 until a.length()).map { a.getString(it) } }
            ?: listOfNotNull(server.optString("urls").takeIf { it.isNotEmpty() })
        if (urls.isEmpty()) return@mapNotNull null
        PeerConnection.IceServer.builder(urls).apply {
            server.optString("username").takeIf { it.isNotEmpty() }?.let { setUsername(it) }
            server.optString("credential").takeIf { it.isNotEmpty() }?.let { setPassword(it) }
        }.createIceServer()
    }

    private fun await(what: String, call: (SdpObserver) -> Unit) {
        val done = CountDownLatch(1)
        var failure: String? = null
        call(object : SdpObserver {
            override fun onSetSuccess() { done.countDown() }
            override fun onSetFailure(error: String?) { failure = error ?: "failed"; done.countDown() }
            override fun onCreateSuccess(description: SessionDescription?) {}
            override fun onCreateFailure(error: String?) {}
        })
        if (!done.await(SDP_TIMEOUT_S, TimeUnit.SECONDS)) error("Timed out trying to $what")
        failure?.let { error("Couldn't $what: $it") }
    }

    private fun awaitCreate(call: (SdpObserver) -> Unit): SessionDescription {
        val done = CountDownLatch(1)
        var result: SessionDescription? = null
        var failure: String? = null
        call(object : SdpObserver {
            override fun onCreateSuccess(description: SessionDescription?) { result = description; done.countDown() }
            override fun onCreateFailure(error: String?) { failure = error ?: "failed"; done.countDown() }
            override fun onSetSuccess() {}
            override fun onSetFailure(error: String?) {}
        })
        if (!done.await(SDP_TIMEOUT_S, TimeUnit.SECONDS)) error("Timed out creating the answer")
        failure?.let { error("Couldn't create the answer: $it") }
        return result ?: error("No answer")
    }

    private companion object {
        const val ICE_GATHER_MS = 2000L
        const val MIN_BITRATE = 300_000
        const val SDP_TIMEOUT_S = 5L
        @Volatile var initialized = false
    }
}
