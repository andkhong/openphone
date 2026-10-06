# Live screen mirroring

How the dashboard shows a phone's screen, and what real video would take. Option 5 below is a design note only; it is not implemented.

## What ships today

| Step | What happens |
|---|---|
| Viewer | The dashboard opens `GET /api/devices/{id}/screen/ws` (WebSocket; same auth as the REST routes: session cookie or header in accounts mode, `?t=` in token mode; another account's phone gets close code 4404 before anything is sent, signed out gets 4401). |
| Server | `openmob/screencast.py`: while at least one viewer is connected, a `ScreenFeed` pulls frames back to back and fans each one out to every viewer as a binary JPEG message. A slow viewer skips frames rather than queueing them. With zero viewers the feed stops. Errors (phone offline, accessibility off) go to viewers as `{"error": "..."}` text messages, retried every 2 s. |
| Frames | `CachedScreenshots.preview()` reuses the agent's own frame when it is newer than the last one sent (and at most 1 s old). Otherwise it captures a **preview frame**: long side 720 px, JPEG quality 55. Preview frames are cached apart from the agent's, so `screenshot()` never returns one. The agent's frames stay at 1280 px, quality 80. |
| Phone | The `screenshot` call gets `{"max_side": 720, "quality": 55, "binary": true}` and answers with one binary message (length-prefixed JSON header plus raw JPEG; see `docs/phone-protocol.md` §4.1). Older apps ignore the params and send their usual JSON frame, which still works. |
| Fallback | If the socket fails, the dashboard polls `screen.jpg?after=<X-Frame-Time>` back to back (the server answers with a newer frame), and tries the socket again 30 s later. Hidden tab or panel: one poll every 2 s. |

The ceiling is `AccessibilityService.takeScreenshot`, which Android throttles to about 3 captures a second. A preview frame is roughly 4-5 times smaller than the agent's base64 frame (about 50 KB versus 250 KB on a busy screen), so on a phone's uplink the transfer time drops from hundreds of milliseconds to tens. Expect about 3 fps with roughly 250-400 ms from screen change to dashboard, versus about 1.5 fps and often over a second before (400 ms sleep, up to 1 s cache reuse, base64 frames). These are estimates, not measurements on a real phone.

## Option 5: real video (not implemented)

Stream the screen as H.264 video instead of a series of screenshots.

### How it would work

1. **Capture.** `MediaProjectionManager.createScreenCaptureIntent()` asks the user for consent. The resulting `MediaProjection` drives a `VirtualDisplay` whose surface is the input surface of a `MediaCodec` H.264 encoder (for example 720p, 2-4 Mbps, 30 fps, a key frame every 1-2 s, low-latency profile where the device supports `KEY_LOW_LATENCY`).
2. **Transport.** Two choices:
   * **WebRTC.** The phone is a WebRTC peer (for example the `org.webrtc` library or LiveKit's Android SDK). The server relays signalling over the existing phone WebSocket. Media goes peer to peer when possible, otherwise through a TURN server. The browser plays it in a plain `<video>` element.
   * **WebCodecs over WebSocket.** The phone sends encoded H.264 access units (NAL units plus timestamps) as binary messages on the existing socket. The server fans them out unchanged to viewer sockets, and the browser decodes them with `VideoDecoder` and draws to a canvas. This needs no TURN or SFU, and reuses the auth and fan-out already built for screenshots.
3. **Viewer join.** A viewer that joins mid-stream needs a key frame: the server caches the last SPS/PPS and key frame, or asks the phone for one (`MediaCodec.setParameters(PARAMETER_KEY_REQUEST_SYNC_FRAME)`).
4. **Agent.** The agent keeps using `takeScreenshot` at full quality. Video frames are lossy and timed for display, not for reading small text.

### Tradeoffs

| | Screenshots (today) | MediaProjection video |
|---|---|---|
| Consent | Accessibility permission, granted once | A system "start recording or casting?" dialog **every session** (each new `MediaProjection`). Android 14+ also requires a new consent token per projection and a `mediaProjection` foreground service type. It can't be granted silently, so a remote viewer can't start it while nobody is holding the phone. |
| Notification | The existing "agent connected" notification | The system shows its own casting indicator while projecting (a status-bar chip on recent versions), in addition to our foreground notification. Users see that the screen is being recorded. |
| Frame rate | About 3 fps (system throttle) | 30-60 fps |
| Latency | About 250-400 ms (estimate) | About 100-200 ms glass to glass with WebRTC on a good network; about 150-300 ms with WebCodecs over the server's WebSocket (TCP adds head-of-line blocking on lossy mobile links) |
| Bandwidth | About 50 KB per frame, about 150 KB/s at 3 fps | About 250-500 KB/s at 2-4 Mbps, constant while streaming |
| Secure windows | Black frame (`FLAG_SECURE`) | Also black; same behaviour |
| Battery and heat | Low, and only while someone watches | Hardware encoder plus constant uplink: noticeable on long sessions |
| Complexity | Small: one extra call parameter and one binary message | Large: projection lifecycle (revoked when the user stops casting, on rotation and on display changes), encoder configuration per device, key frame handling, and either a WebRTC stack with STUN/TURN (and an SFU to fan out to many viewers) or a WebCodecs player with jitter handling. Safari's WebCodecs support is recent, and older browsers need a fallback. |
| Server cost | Frames pass through the server | WebRTC can bypass it, but TURN relays cost bandwidth. WebCodecs passes all video through the server. |

### Recommendation

Keep screenshots as the default: they need no extra consent and work while the phone is unattended, which is the main OpenMob use case. If smoother video becomes important, add video as an opt-in "Watch live (video)" mode started from the phone, using WebCodecs over the existing WebSocket first (it reuses auth and fan-out and needs no TURN), and fall back to screenshots whenever projection is not running.
