package dev.openmob.companion

import android.graphics.Bitmap
import android.os.SystemClock
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
    private var cache: Triple<Long, Long, List<GOcrLine>>? = null // (screen change, read at, lines)

    /** The screen's text lines, or null when the screen can't be captured or read. */
    @Synchronized
    fun read(service: OpenMobAccessibilityService): List<GOcrLine>? {
        val now = SystemClock.uptimeMillis()
        cache?.let { (changed, at, lines) -> if (changed == service.lastChangeAt && now - at < CACHE_MS) return lines }
        val changed = service.lastChangeAt
        val bitmap = runCatching { PhoneActions(service).screenBitmap() }.getOrNull() ?: return null
        val lines = try { recognize(bitmap) } finally { bitmap.recycle() } ?: return null
        cache = Triple(changed, SystemClock.uptimeMillis(), lines)
        return lines
    }

    /** The last reading if it's under 3 s old, without blocking (for the live view's gate, on any thread). */
    fun cached(): List<GOcrLine>? = cache?.takeIf { SystemClock.uptimeMillis() - it.second < 3_000 }?.third

    private val worker = java.util.concurrent.Executors.newSingleThreadExecutor()
    private val busy = java.util.concurrent.atomic.AtomicBoolean(false)

    /** Read in the background (one at a time); [cached] has it afterwards. */
    fun refreshAsync(service: OpenMobAccessibilityService) {
        if (!busy.compareAndSet(false, true)) return
        worker.execute { try { runCatching { read(service) } } finally { busy.set(false) } }
    }

    private fun recognize(bitmap: Bitmap): List<GOcrLine>? = runCatching {
        val text = Tasks.await(recognizer.process(InputImage.fromBitmap(bitmap, 0)), 5, TimeUnit.SECONDS)
        text.textBlocks.flatMap { it.lines }.mapNotNull { line ->
            val r = line.boundingBox ?: return@mapNotNull null
            GOcrLine(line.text, intArrayOf(r.left, r.top, r.right, r.bottom), line.confidence.toDouble())
        }
    }.getOrNull()
}
