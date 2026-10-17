package dev.openmob.companion

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.util.TypedValue
import android.view.Gravity
import android.view.RoundedCorner
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.TextView

/**
 * The indicator's two windows, both TYPE_ACCESSIBILITY_OVERLAY from the accessibility service (no extra permission):
 * a full-screen border that never takes touches or focus, and the Stop pill, the only touchable part. Main thread
 * only. They go when the service does (Android removes a dead service's overlays).
 */
class IndicatorOverlay(private val service: OpenMobAccessibilityService, private val onStop: () -> Unit) {
    private val wm = service.getSystemService(WindowManager::class.java)
    /** The service these windows belong to. */
    val owner: OpenMobAccessibilityService get() = service
    private var border: BorderView? = null
    private var pill: TextView? = null
    private var pillCorner: PillCorner? = null
    private var pillColor = 0
    /** The pill's bounds on screen (left, top, right, bottom), once laid out: OCR drops its text (PermissionOcr). */
    @Volatile var pillRect: IntArray? = null
        private set

    val shown: Boolean get() = border?.isAttachedToWindow == true || pill?.isAttachedToWindow == true

    private fun dp(v: Float) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, service.resources.displayMetrics)

    /** Show [state] with the owner's [s]ettings (HIDDEN fades everything out). */
    fun apply(state: IndicatorState, s: IndicatorPrefs.Snapshot) {
        if (state == IndicatorState.HIDDEN) { hide(); return }
        if (s.border) showBorder(state, s) else removeBorder()
        if (s.pill) showPill(s) else removePill()
    }

    fun hide() {
        border?.let { b -> b.animate().cancel(); b.animate().alpha(0f).setDuration(FADE_MS).withEndAction { if (border === b) removeBorder() } }
        pill?.let { p -> p.isClickable = false; p.animate().cancel()
            p.animate().alpha(0f).setDuration(FADE_MS).withEndAction { if (pill === p) removePill() } }
    }

    fun removeAll() { removeBorder(); removePill() }

    private fun showBorder(state: IndicatorState, s: IndicatorPrefs.Snapshot) {
        val view = border ?: BorderView(service).also { v ->
            val lp = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.TOP or Gravity.START
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
                fitInsetsTypes = 0
                title = "OpenMob indicator"
            }
            // Owner-facing only; nothing for TalkBack to stop on (the pill says what's going on).
            v.importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            v.alpha = 0f
            if (runCatching { wm.addView(v, lp) }.isFailure) return
            border = v
        }
        view.set(state, s, IndicatorPrefs.pulseRuns(s, ValueAnimator.areAnimatorsEnabled(), state), dp(BORDER_DP))
        view.animate().cancel()
        view.animate().alpha(1f).setDuration(FADE_IN_MS)
    }

    private var pillLp: WindowManager.LayoutParams? = null

    private fun insets(): IntArray = runCatching {
        wm.currentWindowMetrics.windowInsets.getInsetsIgnoringVisibility(
            WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout()).let { intArrayOf(it.left, it.top, it.right, it.bottom) }
    }.getOrDefault(IntArray(4))

    /** Margin kept between a dodged pill and the gesture's points. */
    val dodgeMarginPx: Int get() = dp(8f).toInt()

    /** The pill's bounds if it sat in [corner] (its current size), or null while it isn't showing. */
    fun pillRectIn(corner: PillCorner): IntArray? {
        val v = pill ?: return null
        val w = if (v.width > 0) v.width else v.measuredWidth.takeIf { it > 0 } ?: return null
        val h = if (v.height > 0) v.height else dp(48f).toInt()
        val b = wm.currentWindowMetrics.bounds
        return IndicatorGeometry.pillRect(corner, w, h, b.width(), b.height(), insets(), dp(12f).toInt(), dp(8f).toInt())
    }

    private fun place(lp: WindowManager.LayoutParams, corner: PillCorner) {
        val i = insets()
        lp.gravity = (if (corner.top) Gravity.TOP else Gravity.BOTTOM) or (if (corner.right) Gravity.END else Gravity.START)
        lp.x = dp(12f).toInt() + (if (corner.right) i[2] else i[0])
        // Below the status bar (top corners), above the navigation bar (bottom ones).
        lp.y = dp(8f).toInt() + (if (corner.top) i[1] else i[3])
    }

    private fun showPill(s: IndicatorPrefs.Snapshot) {
        if (pill != null && pillColor != s.argb) removePill()
        val existing = pill
        val lp0 = pillLp
        if (existing != null && lp0 != null && pillCorner != s.corner) {
            // Moved (it dodged an agent's gesture, or the owner changed the corner): the same window, never hidden.
            place(lp0, s.corner)
            runCatching { wm.updateViewLayout(existing, lp0) }
            pillCorner = s.corner
        }
        val view = pill ?: TextView(service).also { v ->
            v.text = service.getString(R.string.indicator_stop)
            v.contentDescription = service.getString(R.string.indicator_stop_description)
            v.setTextColor(0xFFFFFFFF.toInt())
            v.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            v.typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            v.gravity = Gravity.CENTER
            v.minWidth = dp(96f).toInt()
            v.minHeight = dp(48f).toInt() // Material's minimum touch target
            v.setPadding(dp(18f).toInt(), 0, dp(18f).toInt(), 0)
            v.background = GradientDrawable().apply {
                cornerRadius = dp(24f)
                setColor(0xE6202124.toInt())
                setStroke(dp(1.5f).toInt(), s.argb)
            }
            v.setOnClickListener { onStop() }
            val lp = WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT, dp(48f).toInt(),
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                    WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT,
            ).apply {
                layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS
                fitInsetsTypes = 0
                title = "OpenMob Stop"
                place(this, s.corner)
            }
            v.alpha = 0f
            v.addOnLayoutChangeListener { view, _, _, _, _, _, _, _, _ ->
                val at = IntArray(2).also(view::getLocationOnScreen)
                pillRect = intArrayOf(at[0], at[1], at[0] + view.width, at[1] + view.height)
            }
            if (runCatching { wm.addView(v, lp) }.isFailure) return
            pill = v
            pillLp = lp
            pillCorner = s.corner
            pillColor = s.argb
        }
        view.isClickable = true
        view.animate().cancel()
        view.animate().alpha(1f).setDuration(FADE_IN_MS)
    }

    private fun removeBorder() {
        border?.let { it.stopPulse(); runCatching { wm.removeView(it) } }
        border = null
    }

    private fun removePill() {
        pill?.let { runCatching { wm.removeView(it) } }
        pill = null
        pillLp = null
        pillCorner = null
        pillRect = null
    }

    /** The glowing edge. Working: a steady line with a soft inner glow (an optional slow pulse). Approval: a band twice
     *  as thick in amber with the owner's colour inside it. Paused: dashed and dim. A hairline keeps it visible on
     *  content of the same brightness (IndicatorColors.outline). Follows the screen's rounded corners. */
    private class BorderView(context: Context) : View(context) {
        private var state = IndicatorState.WORKING
        private var color = 0
        private var lineW = 0f
        private var glow = 1f
        private var pulse: ValueAnimator? = null
        private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
        private val path = Path()
        private val rect = RectF()

        fun set(state: IndicatorState, s: IndicatorPrefs.Snapshot, pulseOn: Boolean, widthPx: Float) {
            this.state = state
            color = s.argb
            lineW = widthPx
            if (pulseOn && pulse == null) {
                pulse = ValueAnimator.ofFloat(1f, 0.45f).apply {
                    duration = 1_400
                    repeatMode = ValueAnimator.REVERSE
                    repeatCount = ValueAnimator.INFINITE
                    addUpdateListener { glow = it.animatedValue as Float; invalidate() }
                    start()
                }
            } else if (!pulseOn) {
                stopPulse()
            }
            invalidate()
        }

        fun stopPulse() { pulse?.cancel(); pulse = null; glow = 1f }

        private fun radius(): Float {
            if (Build.VERSION.SDK_INT < 31) return 0f
            val insets = rootWindowInsets ?: return 0f
            return (insets.getRoundedCorner(RoundedCorner.POSITION_TOP_LEFT)?.radius ?: 0).toFloat()
        }

        private fun stroke(inset: Float, w: Float, argb: Int, alpha: Float, dashed: Boolean = false) {
            rect.set(inset, inset, width - inset, height - inset)
            val r = (radius() - inset).coerceAtLeast(0f)
            path.reset()
            path.addRoundRect(rect, r, r, Path.Direction.CW)
            paint.strokeWidth = w
            paint.color = argb
            paint.alpha = (((argb ushr 24) and 0xFF) * alpha).toInt().coerceIn(0, 255)
            paint.pathEffect = if (dashed) DashPathEffect(floatArrayOf(w * 4, w * 3), 0f) else null
            canvas?.drawPath(path, paint)
        }

        private var canvas: Canvas? = null

        override fun onDraw(c: Canvas) {
            canvas = c
            val w = lineW
            when (state) {
                IndicatorState.WORKING -> {
                    stroke(w * 2.2f, w * 2.4f, color, 0.14f * glow) // the soft glow, inward
                    stroke(w * 1.4f, w * 1.2f, color, 0.30f * glow)
                    stroke(w / 2, w, color, 1f)
                    IndicatorColors.outline(color)?.let { stroke(w + 0.5f, 1.5f, it, 1f) }
                }
                IndicatorState.APPROVAL -> {
                    val band = w * 2
                    stroke(band * 1.6f, band, IndicatorColors.APPROVAL, 0.25f)
                    stroke(band / 2, band, IndicatorColors.APPROVAL, 1f)
                    if (IndicatorColors.nearApproval(color)) stroke(band + 1f, 2f, 0xFF000000.toInt(), 0.9f)
                    stroke(band + w * 0.6f, w * 0.6f, color, 1f) // the owner's colour, inside the amber
                }
                IndicatorState.PAUSED -> {
                    stroke(w / 2, w, color, 0.55f, dashed = true)
                    IndicatorColors.outline(color)?.let { stroke(w + 0.5f, 1.5f, it, 0.5f, dashed = true) }
                }
                IndicatorState.HIDDEN -> Unit
            }
            canvas = null
        }

        override fun onDetachedFromWindow() { stopPulse(); super.onDetachedFromWindow() }
    }

    companion object {
        const val BORDER_DP = 5f
        const val FADE_MS = 300L
        const val FADE_IN_MS = 150L
    }
}
