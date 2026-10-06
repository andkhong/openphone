/*
 * PhoneActions: the phone side of every OpenMob protocol v1 method (docs/phone-protocol.md, section 4).
 *
 * The connection service receives a `call`, invokes handle(method, params) on a background thread and
 * sends back whatever it returns as `result` (or the exception message as `error`). Everything here goes
 * through the bound OpenMobAccessibilityService: screenshots, the accessibility tree, gestures, global
 * actions and text input. The reference behaviour for each method is the adb driver
 * (openmob/phone/adb.py).
 */
package dev.openmob.companion

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.KeyguardManager
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Path
import android.graphics.Point
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PersistableBundle
import android.os.PowerManager
import android.os.SystemClock
import android.util.Base64
import android.view.Display
import android.view.ViewConfiguration
import dev.openmob.companion.gate.Admission
import dev.openmob.companion.gate.GateHost
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/** The gatekeeper refused the call right before its side effect (GateHost.reverify): sent back with its code. */
class GateRefusal(val admission: Admission) : RuntimeException(admission.message)

class PhoneActions(private val service: OpenMobAccessibilityService) {

    private val pm: PackageManager get() = service.packageManager
    /** What the gatekeeper admitted for this call; re-checked right before each side effect (D8). */
    private var admission: Admission? = null

    /** Re-check the admitted action on the screen as it is now (throws [GateRefusal]). */
    private fun gate() {
        GateHost.reverify(service, admission)?.let { throw GateRefusal(it) }
    }

    /** Runs one protocol call. Called on a background thread (never the main thread); may block up to the
     *  method's timeout. Returns a value org.json can serialize as `result`; throws on failure. */
    fun handle(method: String, params: JSONObject, admitted: Admission? = null): Any? {
        admission = admitted
        return run(method, params)
    }

    /** A read the gatekeeper redacted: a blocked app is visible in some window, so a black frame or no elements. */
    fun redacted(method: String, params: JSONObject): Any? = when (method) {
        "screenshot" -> blackFrame(params.optInt("max_side", MAX_SIDE).coerceIn(MIN_SIDE, MAX_SIDE),
            params.optInt("quality", JPEG_QUALITY).coerceIn(MIN_QUALITY, 100)).let { if (params.optBoolean("binary")) it else it.toJson() }
        "wait_for" -> redactedWait(0L)
        else -> JSONArray()
    }

    private fun run(method: String, params: JSONObject): Any? = when (method) {
        "screenshot" -> {
            // The dashboard's live view asks for smaller frames, as raw bytes; the agent sends no params.
            val frame = screenshot(
                params.optInt("max_side", MAX_SIDE).coerceIn(MIN_SIDE, MAX_SIDE),
                params.optInt("quality", JPEG_QUALITY).coerceIn(MIN_QUALITY, 100),
            )
            if (params.optBoolean("binary")) frame else frame.toJson()
        }
        "ui_elements" -> uiElements()
        "current_app" -> currentApp()
        "launchable_packages" -> launchablePackages()
        "app_labels" -> appLabels(params.reqArray("packages"))
        "launch_app" -> launchApp(params.reqString("package"))
        "tap" -> {
            val (x, y) = point(params.reqInt("x"), params.reqInt("y"))
            gesture(Path().apply { moveTo(x, y) }, TAP_MS)
        }
        "long_press" -> {
            val (x, y) = point(params.reqInt("x"), params.reqInt("y"))
            // Always a real long press: a "long press" too short to be one is a tap (judged as one by the gate anyway).
            gesture(Path().apply { moveTo(x, y) },
                    max(params.optLong("duration_ms", 800), ViewConfiguration.getLongPressTimeout() + 100L))
        }
        "swipe" -> {
            val (x1, y1) = point(params.reqInt("x1"), params.reqInt("y1"))
            val (x2, y2) = point(params.reqInt("x2"), params.reqInt("y2"))
            gesture(Path().apply { moveTo(x1, y1); lineTo(x2, y2) }, params.optLong("duration_ms", 300))
        }
        "type_text" -> typeText(params.reqString("text"))
        "key" -> key(params.reqString("name"))
        "open_url" -> openUrl(params.reqString("url"))
        "resolve_url" -> resolveUrl(params.reqString("url"))
        "wake_unlock" -> wakeUnlock()
        "wait_idle" -> waitIdle(
            params.optLong("quiet_ms", DEFAULT_QUIET_MS).coerceIn(MIN_QUIET_MS, MAX_IDLE_MS),
            params.optLong("max_ms", DEFAULT_IDLE_MAX_MS).coerceIn(0, MAX_IDLE_MS),
        )
        "wait_for" -> waitFor(
            WaitCondition.from(params.getJSONObject("condition")),
            params.optLong("timeout_ms", MAX_WAIT_FOR_MS).coerceIn(0, MAX_WAIT_FOR_MS),
        )
        else -> throw IllegalArgumentException("Unknown method $method")
    }

    // --- screenshot ------------------------------------------------------------------------------

    /** One encoded screenshot. The connection service sends it as JSON (base64) or, when the server
     *  asked for `binary`, as a binary WebSocket message (see ConnectionService.sendBinaryResult). */
    class Frame(val jpeg: ByteArray, val width: Int, val height: Int, val deviceWidth: Int, val deviceHeight: Int) {
        /** The result object without the image, for the binary message's header. */
        fun meta(): JSONObject = JSONObject()
            .put("width", width).put("height", height)
            .put("device_width", deviceWidth).put("device_height", deviceHeight)

        fun toJson(): JSONObject = meta().put("jpeg", Base64.encodeToString(jpeg, Base64.NO_WRAP))
    }

    private fun screenshot(maxSide: Int, quality: Int): Frame {
        repeat(SCREENSHOT_ATTEMPTS) { attempt ->
            // The system allows one capture per ~333 ms. Wait out exactly the rest of that window
            // instead of getting refused and sleeping a fixed retry delay: back-to-back live-view
            // frames then arrive every ~1/3 s rather than every ~1/2 s.
            val wait = CaptureClock.at + MIN_CAPTURE_INTERVAL_MS - SystemClock.uptimeMillis()
            if (wait > 0) SystemClock.sleep(wait)
            val requested = SystemClock.uptimeMillis()
            val outcome = captureOnce()
            if (outcome.result != null) CaptureClock.at = requested
            outcome.result?.let { result ->
                return result.hardwareBuffer.use { buffer ->
                    // A hardware bitmap lives in GPU memory and can't be scaled/compressed directly;
                    // copy it into a software ARGB_8888 bitmap first.
                    val hw = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace)
                        ?: throw IllegalStateException("Screenshot failed: could not wrap the hardware buffer")
                    val sw = try {
                        hw.copy(Bitmap.Config.ARGB_8888, false)
                            ?: throw IllegalStateException("Screenshot failed: could not copy the bitmap")
                    } finally {
                        hw.recycle()
                    }
                    encodeFrame(sw, sw.width, sw.height, maxSide, quality)
                }
            }
            when (outcome.error) {
                // Still too soon by the system's clock (e.g. another app captured): retry shortly.
                AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT ->
                    if (attempt < SCREENSHOT_ATTEMPTS - 1) SystemClock.sleep(RATE_LIMIT_RETRY_MS)
                // FLAG_SECURE window (API 34+ reports this; older versions just return black pixels).
                // The spec asks for an all-black frame so the agent knows the app blocks capture.
                AccessibilityService.ERROR_TAKE_SCREENSHOT_SECURE_WINDOW -> return blackFrame(maxSide, quality)
                else -> throw IllegalStateException("Screenshot failed (${screenshotError(outcome.error)})")
            }
        }
        throw IllegalStateException("Screenshot failed: the system rate-limited capture, try again")
    }


    private class Capture(val result: AccessibilityService.ScreenshotResult?, val error: Int)

    private fun captureOnce(): Capture {
        val latch = CountDownLatch(1)
        var capture = Capture(null, AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERNAL_ERROR)
        service.takeScreenshot(Display.DEFAULT_DISPLAY, DIRECT_EXECUTOR,
            object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                    capture = Capture(screenshot, 0); latch.countDown()
                }
                override fun onFailure(errorCode: Int) {
                    capture = Capture(null, errorCode); latch.countDown()
                }
            })
        if (!latch.await(10, TimeUnit.SECONDS)) throw IllegalStateException("Screenshot timed out")
        return capture
    }

    /** The gatekeeper's prompt picture: the screen around device point (x, y) with a crosshair on it, as JPEG.
     *  Null when the screen can't be captured (the prompt then shows only its words). */
    /** The whole screen as a software bitmap (the caller recycles it), for on-device OCR; null if it can't be
     *  captured. */
    fun screenBitmap(): Bitmap? {
        var result: AccessibilityService.ScreenshotResult? = null
        for (attempt in 0 until SCREENSHOT_ATTEMPTS) { // another capture (a screenshot call) may have just run
            val wait = CaptureClock.at + MIN_CAPTURE_INTERVAL_MS - SystemClock.uptimeMillis()
            if (wait > 0) SystemClock.sleep(wait)
            val outcome = captureOnce()
            result = outcome.result
            if (result != null || outcome.error != AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT) break
            SystemClock.sleep(RATE_LIMIT_RETRY_MS * 4)
        }
        if (result == null) return null
        CaptureClock.at = SystemClock.uptimeMillis()
        return result.hardwareBuffer.use { buffer ->
            val hw = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: return null
            try { hw.copy(Bitmap.Config.ARGB_8888, false) } finally { hw.recycle() }
        }
    }

    fun crop(x: Int, y: Int): ByteArray? {
        val wait = CaptureClock.at + MIN_CAPTURE_INTERVAL_MS - SystemClock.uptimeMillis()
        if (wait > 0) SystemClock.sleep(wait)
        val result = captureOnce().result ?: return null
        CaptureClock.at = SystemClock.uptimeMillis()
        return result.hardwareBuffer.use { buffer ->
            val hw = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: return null
            val full = try { hw.copy(Bitmap.Config.ARGB_8888, false) } finally { hw.recycle() } ?: return null
            val cw = min(CROP_W, full.width)
            val ch = min(CROP_H, full.height)
            val left = (x - cw / 2).coerceIn(0, full.width - cw)
            val top = (y - ch / 2).coerceIn(0, full.height - ch)
            val part = Bitmap.createBitmap(full, left, top, cw, ch).copy(Bitmap.Config.ARGB_8888, true)
            full.recycle()
            val paint = android.graphics.Paint().apply { color = Color.RED; strokeWidth = 5f; style = android.graphics.Paint.Style.STROKE }
            android.graphics.Canvas(part).apply {
                val cx = (x - left).toFloat()
                val cy = (y - top).toFloat()
                drawCircle(cx, cy, 36f, paint)
                drawLine(cx - 60, cy, cx + 60, cy, paint)
                drawLine(cx, cy - 60, cx, cy + 60, paint)
            }
            val out = ByteArrayOutputStream()
            part.compress(Bitmap.CompressFormat.JPEG, 80, out)
            part.recycle()
            out.toByteArray()
        }
    }

    private fun blackFrame(maxSide: Int, quality: Int): Frame {
        val size = displaySize()
        val (w, h) = scaledSize(size.x, size.y, maxSide)
        val frame = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.BLACK) }
        return encodeFrame(frame, size.x, size.y, maxSide, quality)
    }

    /** Downscales [src] (long side <= [maxSide]), JPEG-encodes it and recycles it. */
    private fun encodeFrame(src: Bitmap, deviceWidth: Int, deviceHeight: Int, maxSide: Int, quality: Int): Frame {
        val (w, h) = scaledSize(src.width, src.height, maxSide)
        val scaled = if (w == src.width && h == src.height) src else Bitmap.createScaledBitmap(src, w, h, true)
        try {
            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
            return Frame(out.toByteArray(), w, h, deviceWidth, deviceHeight)
        } finally {
            if (scaled !== src) scaled.recycle()
            src.recycle()
        }
    }

    private fun scaledSize(w: Int, h: Int, maxSide: Int): Pair<Int, Int> {
        val ratio = min(1.0, maxSide.toDouble() / max(w, h))
        return max(1, (w * ratio).roundToInt()) to max(1, (h * ratio).roundToInt())
    }

    private fun screenshotError(code: Int) = when (code) {
        AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERNAL_ERROR -> "internal error"
        AccessibilityService.ERROR_TAKE_SCREENSHOT_NO_ACCESSIBILITY_ACCESS -> "no screenshot permission for the accessibility service"
        AccessibilityService.ERROR_TAKE_SCREENSHOT_INVALID_DISPLAY -> "invalid display"
        else -> "error code $code"
    }

    // --- wait_idle -------------------------------------------------------------------------------

    /** Blocks until no window or content change has arrived for [quietMs] (counted from the later of
     *  the last change and this call, so it always waits at least [quietMs]: a tap's first events have
     *  time to arrive), or until [maxMs]. The next screenshot isn't held up by it: captures are
     *  throttled to one per MIN_CAPTURE_INTERVAL_MS, and the agent's last one is older than that. */
    private fun waitIdle(quietMs: Long, maxMs: Long): JSONObject {
        val start = SystemClock.uptimeMillis()
        while (true) {
            val now = SystemClock.uptimeMillis()
            val quietFor = now - max(service.lastChangeAt, start)
            val waited = now - start
            if (quietFor >= quietMs || waited >= maxMs) {
                return JSONObject().put("waited_ms", waited).put("idle", quietFor >= quietMs)
            }
            SystemClock.sleep(max(1L, minOf(IDLE_POLL_MS, quietMs - quietFor, maxMs - waited)))
        }
    }

    // --- wait_for --------------------------------------------------------------------------------

    /** The elements a wait may look at: exactly what ui_elements would answer now, checked against the
     *  gatekeeper before and after the read (like ConnectionService does for ui_elements). Null: a protected
     *  app is visible, so the wait ends without a verdict (a "disappeared" would leak a bit about it). */
    private fun waitElements(): List<WaitElement>? =
        if (GateHost.refreshProtected(service)) null else WaitElement.parse(uiElements()).takeUnless { GateHost.refreshProtected(service) }

    /** A wait the gatekeeper withheld (a protected app is visible): nothing about the screen, not even a verdict. */
    private fun redactedWait(waited: Long): JSONObject = JSONObject().put("met", false).put("redacted", true)
        .put("waited_ms", waited).put("seen", "").put("package", JSONObject.NULL)

    /** Read-only: returns as soon as [cond] holds on this phone's element list, or at [timeoutMs]. The list is
     *  read again on each window or content change (at most every WAIT_FOR_READ_MS) and at least every
     *  WAIT_FOR_RECHECK_MS; between reads it costs only a timestamp check. Never acts on the screen. */
    private fun waitFor(cond: WaitCondition, timeoutMs: Long): JSONObject {
        val start = SystemClock.uptimeMillis()
        var els: List<WaitElement>? = null
        var readStamp = 0L
        var readAt = 0L
        var baseline: Set<String>? = null
        while (true) {
            val now = SystemClock.uptimeMillis()
            val stamp = service.lastChangeAt
            val fresh = els == null || (stamp != readStamp && now - readAt >= WAIT_FOR_READ_MS) ||
                now - readAt >= WAIT_FOR_RECHECK_MS
            if (fresh) {
                readStamp = stamp
                readAt = now
                els = waitElements() ?: return redactedWait(now - start)
            }
            val pkg = currentApp()
            val current = els ?: emptyList()
            if (baseline == null) baseline = WaitCondition.signature(pkg, current)
            // A change since the last read makes the list stale: don't call the screen quiet on it.
            val quietFor = if (stamp != readStamp) 0L else now - max(stamp, start)
            val seen = cond.check(pkg, current, baseline, quietFor)
            if (seen != null || now - start >= timeoutMs) {
                return JSONObject().put("met", seen != null).put("waited_ms", now - start)
                    .put("seen", seen ?: "").put("package", pkg ?: JSONObject.NULL)
            }
            SystemClock.sleep(max(1L, minOf(WAIT_FOR_POLL_MS, timeoutMs - (now - start))))
        }
    }

    // --- ui_elements -----------------------------------------------------------------------------

    /** Never errors: any failure yields []. */
    private fun uiElements(): JSONArray {
        val out = JSONArray()
        // A permission dialog Android hides from accessibility: its text as the phone's OCR reads it (class OCRText),
        // first, so the agent can target "Only this time" (the gatekeeper judges taps on the same reading).
        runCatching { GateHost.ocrNodes(service) }.getOrNull()?.forEach { n ->
            out.put(JSONObject().put("text", n.text).put("desc", "")
                .put("resource_id", if (n.id == "permission_message") "ocr_permission_message" else "")
                .put("cls", "OCRText").put("clickable", n.clickable)
                .put("checked", n.checked ?: JSONObject.NULL)
                .put("bounds", JSONArray().put(n.bounds[0]).put(n.bounds[1]).put(n.bounds[2]).put(n.bounds[3])))
        }
        try {
            val deadline = SystemClock.uptimeMillis() + UI_DEADLINE_MS
            val windows = service.windows
            // Windows that can catch a tap meant for a lower one: dialogs, the keyboard, system bars.
            val catchers = windows.filter { it.type != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }
                .map { it.layer to Rect().also(it::getBoundsInScreen) }
            for ((root, layer) in windowRoots(windows)) {
                val above = catchers.filter { it.first > layer }.map { it.second }
                try {
                    walk(root, out, deadline, above)
                } finally {
                    recycle(root)
                }
                if (out.length() >= MAX_ELEMENTS || SystemClock.uptimeMillis() > deadline) break
            }
        } catch (_: Exception) {
            // Partial or empty list is fine (games, canvas apps, windows that vanished mid-walk).
        }
        return out
    }

    /** Roots of the interactive windows with their z-order layer: the active one first (so the app's
     *  elements get the low indices and aren't crowded out by system bars), then the rest in z-order.
     *  The keyboard and accessibility overlays (including our own) are skipped. */
    private fun windowRoots(all: List<AccessibilityWindowInfo>): List<Pair<AccessibilityNodeInfo, Int>> {
        val windows = all
            .filter { it.type != AccessibilityWindowInfo.TYPE_INPUT_METHOD &&
                it.type != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY }
            .sortedByDescending { it.isActive }
        val roots = windows.mapNotNull { w -> w.root?.let { it to w.layer } }
        return roots.ifEmpty { listOfNotNull(service.rootInActiveWindow).map { it to Int.MAX_VALUE } }
    }

    /** Depth-first pre-order walk, same inclusion rules as adb parse_ui_xml (+ isVisibleToUser).
     *  [above]: bounds of the windows stacked over this one. */
    private fun walk(node: AccessibilityNodeInfo, out: JSONArray, deadline: Long, above: List<Rect>) {
        if (out.length() >= MAX_ELEMENTS || SystemClock.uptimeMillis() > deadline) return
        element(node, above)?.let { out.put(it) }
        for (i in 0 until node.childCount) {
            if (out.length() >= MAX_ELEMENTS) return
            val child = node.getChild(i) ?: continue
            try {
                walk(child, out, deadline, above)
            } finally {
                recycle(child)
            }
        }
    }

    private fun element(node: AccessibilityNodeInfo, above: List<Rect>): JSONObject? {
        val text = node.text?.toString()?.trim().orEmpty()
        val desc = node.contentDescription?.toString()?.trim().orEmpty()
        val clickable = node.isClickable || node.isLongClickable || node.isCheckable
        if (text.isEmpty() && desc.isEmpty() && !clickable) return null
        if (!node.isVisibleToUser) return null
        val r = Rect().also(node::getBoundsInScreen)
        if (r.right <= r.left || r.bottom <= r.top) return null
        return JSONObject()
            .put("text", text)
            .put("desc", desc)
            .put("resource_id", node.viewIdResourceName.orEmpty().substringAfterLast('/'))
            .put("cls", node.className?.toString().orEmpty().substringAfterLast('.'))
            .put("clickable", clickable)
            .put("checked", if (node.isCheckable) node.isChecked else JSONObject.NULL)
            .put("bounds", JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom))
            // Optional, only when true: lets the server keep the model away from password fields.
            .apply { if (node.isPassword) put("password", true) }
            .apply { if (node.isEditable) put("editable", true) }
            // Only when greyed out: the Play Store's "Open" while an install runs (wait_for's element_enabled).
            .apply { if (!node.isEnabled) put("enabled", false) }
            // The field typing and ENTER go to, and its hint ("Search", "Card number"): the server tells
            // a search box from a form, and finds the address bar.
            .apply { if (node.isFocused && (node.isEditable || node.isPassword)) put("focused", true) }
            .apply { node.hintText?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { put("hint", it.take(80)) } }
            // Its center is under a higher window (a dialog, the keyboard): a tap there would land on that.
            .apply { if (above.any { it.contains(r.centerX(), r.centerY()) }) put("covered", true) }
    }

    // --- apps ------------------------------------------------------------------------------------

    /** Never errors. Skips the notification shade and our own overlay, per the spec. */
    private fun currentApp(): String? {
        val ignored = setOf("com.android.systemui", service.packageName)
        val fromWindow = runCatching { service.rootInActiveWindow?.packageName?.toString() }.getOrNull()
        return listOfNotNull(fromWindow, service.currentPackage).firstOrNull { it !in ignored }
    }

    @Suppress("DEPRECATION") // int-flags overload; the Flags variant is API 33+
    private fun launchablePackages(): JSONArray {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val packages = pm.queryIntentActivities(intent, 0).map { it.activityInfo.packageName }.distinct().sorted()
        return JSONArray(packages)
    }

    @Suppress("DEPRECATION") // int-flags overload; the Flags variant is API 33+
    private fun appLabels(packages: JSONArray): JSONObject {
        val out = JSONObject()
        for (i in 0 until packages.length()) {
            val pkg = packages.getString(i)
            val label = try {
                pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
            } catch (_: PackageManager.NameNotFoundException) {
                null
            }
            out.put(pkg, label ?: JSONObject.NULL)
        }
        return out
    }

    private fun launchApp(pkg: String): Any? {
        val intent = pm.getLaunchIntentForPackage(pkg)
            ?: throw IllegalStateException("$pkg has no launchable activity")
        // A bound accessibility service is exempt from background activity-start restrictions.
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        gate()
        service.startActivity(intent)
        return null
    }

    private fun openUrl(url: String): Any? {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        gate()
        try {
            service.startActivity(intent) // ACTION_VIEW on tel: opens the dialer; it never places a call
        } catch (_: ActivityNotFoundException) {
            throw IllegalStateException("No app can open $url")
        }
        return null
    }

    /** Which app would open [url], without opening it: {package (null for the chooser or nothing),
     *  candidates}. The server refuses a link into a blocked app before it is opened. */
    @Suppress("DEPRECATION") // int-flags overloads; the Flags variants are API 33+
    private fun resolveUrl(url: String): JSONObject {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        val chosen = pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName
        val candidates = pm.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY)
            .map { it.activityInfo.packageName }.distinct().sorted()
        // "android" is the system's chooser (ResolverActivity): no single app is the handler.
        val handler = chosen?.takeIf { it != "android" }
        return JSONObject().put("package", handler ?: JSONObject.NULL).put("candidates", JSONArray(candidates))
    }

    // --- gestures --------------------------------------------------------------------------------

    /** Validates a point against the current screen (StrokeDescription also rejects negative paths). */
    private fun point(x: Int, y: Int): Pair<Float, Float> {
        val size = displaySize()
        if (x < 0 || y < 0 || x >= size.x || y >= size.y) {
            throw IllegalArgumentException("Coordinates ($x, $y) are outside the screen (${size.x}x${size.y})")
        }
        return x.toFloat() to y.toFloat()
    }

    /** Dispatches one stroke and blocks until it completes. Serialized: a new gesture would cancel one
     *  still in flight. */
    private fun gesture(path: Path, durationMs: Long): Any? = synchronized(gestureLock) {
        gate() // under the gesture lock, right before the stroke
        val duration = durationMs.coerceIn(1, GestureDescription.getMaxGestureDuration())
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        val latch = CountDownLatch(1)
        var completed = false
        val callback = object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription) {
                completed = true; latch.countDown()
            }
            override fun onCancelled(gestureDescription: GestureDescription) = latch.countDown()
        }
        // null handler: callbacks arrive on the main thread, which we never block.
        if (!service.dispatchGesture(gesture, callback, null)) {
            throw IllegalStateException("Gesture was rejected by the system")
        }
        if (!latch.await(duration + 5_000, TimeUnit.MILLISECONDS)) throw IllegalStateException("Gesture timed out")
        if (!completed) throw IllegalStateException("Gesture was cancelled")
        GateHost.completed(admission) // only now does the gate record a location-card choice (review B1)
        null
    }

    // --- text input ------------------------------------------------------------------------------

    private fun typeText(text: String): Any? {
        val node = focusedEditable() ?: throw IllegalStateException("No focused text field. Tap the field first.")
        gate()
        // Defense in depth: the server should never ask, but a password is never typed for anyone.
        if (node.isPassword) throw IllegalStateException("I don't type passwords. Please type it yourself.")
        val old = currentText(node)
        // Insert at the cursor, replacing any selection; unknown selection (-1) means append.
        val (start, end) = selection(node, old) ?: (old.length to old.length)
        if (!setText(node, old.substring(0, start) + text + old.substring(end), start + text.length)) {
            // Custom fields (e.g. Gmail's recipient chips) often reject SET_TEXT but accept a paste.
            paste(node, text)
        }
        return null
    }

    /**
     * Paste [text] through the clipboard, then put the owner's clipboard back. What OpenMob typed (an
     * OTP, an address) must not stay on the clipboard for other apps or the next paste: the clip is
     * marked sensitive (Android 13+ keeps it out of the clipboard preview and history), and right after
     * the paste the previous clip is restored, or the clipboard cleared when we couldn't read it
     * (Android only lets the foreground app or keyboard read it).
     */
    private fun paste(node: AccessibilityNodeInfo, text: String) {
        val clipboard = service.getSystemService(ClipboardManager::class.java)
        val previous = runCatching { clipboard.primaryClip }.getOrNull()
        val clip = ClipData.newPlainText("OpenMob", text)
        if (Build.VERSION.SDK_INT >= 33) {
            clip.description.extras = PersistableBundle().apply { putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true) }
        }
        clipboard.setPrimaryClip(clip)
        try {
            // Synchronous: the field has read the clip by the time this returns.
            if (!node.performAction(AccessibilityNodeInfo.ACTION_PASTE)) {
                throw IllegalStateException("The focused field did not accept text (set text and paste both failed)")
            }
        } finally {
            runCatching {
                if (previous != null) clipboard.setPrimaryClip(previous) else clipboard.clearPrimaryClip()
            }
        }
    }

    /** Backspace: removes the selection, or the code point before the cursor. */
    private fun deleteBackward(): Any? {
        val node = focusedEditable() ?: throw IllegalStateException("No focused text field. Tap the field first.")
        val old = currentText(node)
        var (start, end) = selection(node, old) ?: (old.length to old.length)
        if (start == end) {
            if (start == 0) return null
            start = old.offsetByCodePoints(end, -1)
        }
        if (!setText(node, old.substring(0, start) + old.substring(end), start)) {
            throw IllegalStateException("The focused field did not accept the deletion")
        }
        return null
    }

    /** False if the field rejected SET_TEXT. */
    private fun setText(node: AccessibilityNodeInfo, newText: String, cursor: Int): Boolean {
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, newText)
        }
        if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return false
        // SET_TEXT usually leaves the cursor at the end (or start); put it right after the edit so the
        // next type_text / DELETE continues from the same place, like a real keyboard. Best effort.
        val sel = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, cursor)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, cursor)
        }
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
        return true
    }

    /** The field's real content: a field showing only its hint counts as empty. */
    private fun currentText(node: AccessibilityNodeInfo): String =
        if (node.isShowingHintText) "" else node.text?.toString().orEmpty()

    /** Ordered, clamped selection, or null when the node doesn't report one. */
    private fun selection(node: AccessibilityNodeInfo, text: String): Pair<Int, Int>? {
        val a = node.textSelectionStart
        val b = node.textSelectionEnd
        if (a < 0 || b < 0) return null
        return min(a, b).coerceAtMost(text.length) to max(a, b).coerceAtMost(text.length)
    }

    /** The focused text input in any window (custom inputs may not report isEditable). */
    private fun focusedEditable(): AccessibilityNodeInfo? {
        service.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.let { if (acceptsText(it)) return it }
        val roots = listOfNotNull(service.rootInActiveWindow) + service.windows.mapNotNull { it.root }
        for (root in roots) {
            root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.let { if (acceptsText(it)) return it }
            findFocusedEditable(root)?.let { return it }
        }
        return null
    }

    private fun acceptsText(node: AccessibilityNodeInfo): Boolean = node.isEditable ||
        node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_SET_TEXT || it.id == AccessibilityNodeInfo.ACTION_PASTE }

    private fun findFocusedEditable(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isFocused && acceptsText(node)) return node
        for (i in 0 until node.childCount) {
            node.getChild(i)?.let(::findFocusedEditable)?.let { return it }
        }
        return null
    }

    // --- keys ------------------------------------------------------------------------------------

    private fun key(rawName: String): Any? {
        val name = rawName.uppercase()
        when (name) {
            "BACK" -> global(AccessibilityService.GLOBAL_ACTION_BACK, name)
            "HOME" -> global(AccessibilityService.GLOBAL_ACTION_HOME, name)
            "APP_SWITCH" -> global(AccessibilityService.GLOBAL_ACTION_RECENTS, name)
            "POWER" -> global(AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN, name)
            "ENTER" -> { gate(); imeEnter() }
            "DELETE" -> { gate(); deleteBackward() }
            "WAKEUP" -> wakeScreen()
            "VOLUME_UP", "VOLUME_DOWN" -> service.getSystemService(AudioManager::class.java).adjustStreamVolume(
                AudioManager.STREAM_MUSIC,
                if (name == "VOLUME_UP") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER,
                AudioManager.FLAG_SHOW_UI,
            )
            in UNSUPPORTED_KEYS -> throw UnsupportedOperationException(
                "Key $name is not supported by the OpenMob app (supported: ${SUPPORTED_KEYS.joinToString()})")
            else -> throw IllegalArgumentException("Unknown key $rawName")
        }
        return null
    }

    private fun global(action: Int, name: String) {
        if (!service.performGlobalAction(action)) throw IllegalStateException("Key $name failed")
    }

    private fun imeEnter() {
        val node = focusedEditable() ?: throw IllegalStateException("No focused text field to press ENTER in")
        if (!node.performAction(AccessibilityAction.ACTION_IME_ENTER.id)) {
            throw IllegalStateException("The focused field does not support ENTER (ACTION_IME_ENTER failed)")
        }
    }

    // --- wake / unlock ---------------------------------------------------------------------------

    /** Wakes the screen and, if the lock screen is NOT secure, swipes it away. A secure keyguard
     *  (PIN/pattern/password/biometric) is left alone: the user must unlock it themselves; the agent sees
     *  the lock screen and asks. An accessibility service has no keyguard-dismiss API
     *  (requestDismissKeyguard needs an Activity), so the swipe-up gesture is a best effort. */
    private fun wakeUnlock(): Any? {
        wakeScreen()
        val keyguard = service.getSystemService(KeyguardManager::class.java)
        if (keyguard.isKeyguardLocked && !keyguard.isKeyguardSecure) {
            runCatching {
                val size = displaySize()
                val x = size.x / 2f
                gesture(Path().apply { moveTo(x, size.y * 0.85f); lineTo(x, size.y * 0.25f) }, 250)
            }
            // Give the lock screen a moment to go; the server checks device_state afterwards and
            // reports phone_locked if it's still there. A secure keyguard is never touched.
            val deadline = SystemClock.uptimeMillis() + 1_500
            while (keyguard.isKeyguardLocked && SystemClock.uptimeMillis() < deadline) SystemClock.sleep(100)
        }
        return null
    }

    @Suppress("DEPRECATION") // SCREEN_BRIGHT_WAKE_LOCK is deprecated but still the only service-side way to wake
    private fun wakeScreen() {
        val power = service.getSystemService(PowerManager::class.java)
        if (power.isInteractive) return
        power.newWakeLock(
            PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "openmob:wake",
        ).acquire(3_000)
        val deadline = SystemClock.uptimeMillis() + 2_000
        while (!power.isInteractive && SystemClock.uptimeMillis() < deadline) SystemClock.sleep(100)
        if (!power.isInteractive) throw IllegalStateException("Could not wake the screen")
    }

    // --- helpers ---------------------------------------------------------------------------------

    /** Real display size in the current orientation (the gesture coordinate space). */
    @Suppress("DEPRECATION") // getRealSize: the simplest exact full-display size from a service context
    private fun displaySize(): Point = Point().also {
        service.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY).getRealSize(it)
    }

    @Suppress("DEPRECATION") // no-op on API 33+, still needed below that to return nodes to the pool
    private fun recycle(node: AccessibilityNodeInfo) = node.recycle()

    /** Required params: a clear "Missing parameter" error instead of org.json's generic message. */
    private fun JSONObject.param(name: String): JSONObject = apply {
        if (!has(name) || isNull(name)) throw IllegalArgumentException("Missing parameter '$name'")
    }
    private fun JSONObject.reqInt(name: String): Int = param(name).getInt(name)
    private fun JSONObject.reqString(name: String): String = param(name).getString(name)
    private fun JSONObject.reqArray(name: String): JSONArray = param(name).getJSONArray(name)

    private companion object {
        /** One gesture at a time across calls (each call has its own PhoneActions). */
        val gestureLock = Any()
        const val CROP_W = 900
        const val CROP_H = 600
        const val MAX_SIDE = 1280
        const val JPEG_QUALITY = 80
        const val MIN_SIDE = 160 // floor for the live view's max_side param
        const val MIN_QUALITY = 10
        const val SCREENSHOT_ATTEMPTS = 10 // first try + retries on rate limiting (~0.5 s at most)
        const val MIN_CAPTURE_INTERVAL_MS = 340L // the system's limit is 333 ms between captures
        const val RATE_LIMIT_RETRY_MS = 50L
        const val MAX_ELEMENTS = 150
        const val UI_DEADLINE_MS = 10_000L
        const val TAP_MS = 50L
        const val DEFAULT_QUIET_MS = 300L
        const val MIN_QUIET_MS = 50L
        const val DEFAULT_IDLE_MAX_MS = 3_000L
        const val MAX_IDLE_MS = 8_000L // well inside the server's 10 s timeout
        const val IDLE_POLL_MS = 20L
        const val MAX_WAIT_FOR_MS = 10_000L // per call, inside the server's 15 s timeout; the server calls again
        const val WAIT_FOR_POLL_MS = 50L
        const val WAIT_FOR_READ_MS = 150L // at most this often re-read the tree while events pour in
        const val WAIT_FOR_RECHECK_MS = 1_000L // and at least this often (changes without events)
        val DIRECT_EXECUTOR = Executor { it.run() }
        val SUPPORTED_KEYS = listOf("BACK", "HOME", "APP_SWITCH", "ENTER", "DELETE", "WAKEUP", "POWER",
            "VOLUME_UP", "VOLUME_DOWN")
        val UNSUPPORTED_KEYS = setOf("TAB", "MENU", "SEARCH", "ESCAPE")
    }
}

/** Uptime of the last successful capture request, shared by every PhoneActions (the system limits the service). */
internal object CaptureClock { @Volatile var at = 0L }
