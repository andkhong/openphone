package dev.openmob.companion

import android.graphics.Bitmap
import android.os.SystemClock
import android.util.Log
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import dev.openmob.companion.gate.GOcrLine
import java.util.concurrent.TimeUnit

/**
 * Reads the permission dialog Android 14+ hides from accessibility: the phone's own screenshot, through ML Kit text
 * recognition (the bundled Latin model: on the device, offline). The gatekeeper rebuilds the dialog from these lines
 * (Gatekeeper.withOcr) and judges taps on it; ui_elements lists them (class OCRText). Never on the main thread.
 */
object PermissionOcr {
    private const val CACHE_MS = 1500L
    private val recognizer by lazy { TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS) }
    /** A reading belongs to one window: a stale one never identifies another window (an app loading after the
     *  dialog closed). */
    private class Reading(val window: Int, val changed: Long, val at: Long, val lines: List<GOcrLine>)
    @Volatile private var cache: Reading? = null

    /** The screen's text lines, or null when the screen can't be captured or read. [fresh]: never the cached reading
     *  (the re-check right before an act: a dialog can swap its content in place with no event, review M1). */
    @Synchronized
    fun read(service: OpenMobAccessibilityService, window: Int, fresh: Boolean = false): List<GOcrLine>? {
        val now = SystemClock.uptimeMillis()
        if (!fresh) cache?.let { if (it.window == window && it.changed == service.lastChangeAt && now - it.at < CACHE_MS) return it.lines }
        val changed = service.lastChangeAt
        val bitmap = runCatching { PhoneActions(service).screenBitmap() }.getOrNull()
        if (bitmap == null) {
            Log.i(TAG, "ocr: screen capture failed")
            return null
        }
        val lines = try { recognize(bitmap) } finally { bitmap.recycle() }
        val ms = SystemClock.uptimeMillis() - now
        if (lines == null) {
            Log.i(TAG, "ocr: recognition failed after ${ms} ms")
            return null
        }
        Log.i(TAG, "ocr: ${lines.size} lines in ${ms} ms, min conf ${"%.2f".format(lines.minOfOrNull { it.conf } ?: 0.0)}")
        cache = Reading(window, changed, SystemClock.uptimeMillis(), lines)
        return lines
    }

    /** The last reading if it's under 3 s old, without blocking (for the live view's gate, on any thread). A reading
     *  over a second old is refreshed in the background meanwhile. */
    fun cached(service: OpenMobAccessibilityService, window: Int): List<GOcrLine>? {
        val c = cache?.takeIf { it.window == window }
        val age = if (c == null) Long.MAX_VALUE else SystemClock.uptimeMillis() - c.at
        if (age > 1_000) refreshAsync(service, window)
        return if (age < 3_000) c?.lines else null
    }

    /** Forget the last reading (after a gesture: what it read may no longer be on screen). */
    fun invalidate() { cache = null }

    private val worker =java.util.concurrent.Executors.newSingleThreadExecutor()

    /** Load the model ahead of time: its first use takes seconds (measured 3.5 s on an emulator), too long for a
     *  dialog to wait black. Runs once, in the background. */
    fun warmUp() {
        if (!busy.compareAndSet(false, true)) return
        worker.execute {
            try {
                val t = SystemClock.uptimeMillis()
                val bmp = Bitmap.createBitmap(64, 32, Bitmap.Config.ARGB_8888).apply { eraseColor(android.graphics.Color.WHITE) }
                recognize(bmp)
                bmp.recycle()
                Log.i(TAG, "ocr: model ready in ${SystemClock.uptimeMillis() - t} ms")
            } finally { busy.set(false) }
        }
    }
    private val busy = java.util.concurrent.atomic.AtomicBoolean(false)

    /** Read in the background (one at a time); [cached] has it afterwards. */
    fun refreshAsync(service: OpenMobAccessibilityService, window: Int) {
        if (!busy.compareAndSet(false, true)) return
        worker.execute { try { runCatching { read(service, window) } } finally { busy.set(false) } }
    }

    private fun recognize(bitmap: Bitmap): List<GOcrLine>? = runCatching {
        val text = Tasks.await(recognizer.process(InputImage.fromBitmap(bitmap, 0)), 5, TimeUnit.SECONDS)
        text.textBlocks.flatMap { it.lines }.mapNotNull { line ->
            val r = line.boundingBox ?: return@mapNotNull null
            GOcrLine(line.text, intArrayOf(r.left, r.top, r.right, r.bottom), line.confidence.toDouble())
        }.let { lines ->
            // A model that reports no confidence at all (every line 0) isn't "unsure": treat it as unreported.
            if (lines.isNotEmpty() && lines.all { it.conf <= 0.0 }) lines.map { GOcrLine(it.text, it.bounds, 1.0) } else lines
        }
    }.onFailure { Log.i(TAG, "ocr: ${it.javaClass.simpleName}: ${it.message}") }.getOrNull()

    const val TAG = "OpenMobGate"
}
