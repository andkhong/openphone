package dev.openmob.companion

import dev.openmob.companion.gate.GOcrLine
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/*
 * The "an agent is controlling this phone" indicator, in plain Kotlin (tested on the JVM): the owner's settings, the
 * colours, and when it shows (docs/design/control-indicator.md). The views are in IndicatorOverlay, the glue in
 * ControlIndicator.
 */

/** What the indicator shows. Each state differs by pattern, not colour alone. */
enum class IndicatorState {
    /** Nothing: no agent is acting on the phone. */
    HIDDEN,
    /** An agent is acting: a thin steady border in the owner's colour (optionally a gentle pulse). */
    WORKING,
    /** The phone's own approval prompt is waiting for the owner: a thicker amber band, whatever colour is chosen. */
    APPROVAL,
    /** The task is paused on the owner (the phone locked, a system dialog only they can answer, paused after
     *  denials): a dim, dashed border. */
    PAUSED,
}

enum class PillCorner(val key: String) {
    TOP_RIGHT("top_right"), TOP_LEFT("top_left"), BOTTOM_RIGHT("bottom_right"), BOTTOM_LEFT("bottom_left");

    val top: Boolean get() = this == TOP_RIGHT || this == TOP_LEFT
    val right: Boolean get() = this == TOP_RIGHT || this == BOTTOM_RIGHT

    companion object {
        fun of(key: String?): PillCorner = values().firstOrNull { it.key == key } ?: TOP_RIGHT
    }
}

object IndicatorColors {
    class Preset(val key: String, val label: String, val argb: Int)

    /** Blue is the default. Orange is a red-orange, kept away from the approval amber. */
    val PRESETS = listOf(
        Preset("blue", "Blue", 0xFF2F7BFF.toInt()),
        Preset("purple", "Purple", 0xFFA855F7.toInt()),
        Preset("green", "Green", 0xFF00C853.toInt()),
        Preset("orange", "Orange", 0xFFFF5722.toInt()),
        Preset("pink", "Pink", 0xFFFF4F9A.toInt()),
        Preset("white", "White (high contrast)", 0xFFFFFFFF.toInt()),
    )
    const val DEFAULT = "blue"
    /** The approval state's accent, the same whatever colour the owner chose. */
    const val APPROVAL: Int = 0xFFFFB300.toInt()
    private val HEX = Regex("^#[0-9A-Fa-f]{6}$")

    /** A setting value: a preset's key, or a custom "#RRGGBB". */
    fun valid(value: String?): Boolean = value != null && (PRESETS.any { it.key == value } || HEX.matches(value))

    fun argb(value: String?): Int {
        PRESETS.firstOrNull { it.key == value }?.let { return it.argb }
        if (value != null && HEX.matches(value)) return (0xFF000000 or value.substring(1).toLong(16)).toInt()
        return PRESETS.first { it.key == DEFAULT }.argb
    }

    /** WCAG relative luminance (0..1). */
    fun luminance(argb: Int): Double {
        fun ch(shift: Int): Double {
            val c = ((argb shr shift) and 0xFF) / 255.0
            return if (c <= 0.03928) c / 12.92 else ((c + 0.055) / 1.055).pow(2.4)
        }
        return 0.2126 * ch(16) + 0.7152 * ch(8) + 0.0722 * ch(0)
    }

    fun contrast(a: Int, b: Int): Double {
        val la = luminance(a); val lb = luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
    }

    private const val WHITE = 0xFFFFFFFF.toInt()
    private const val BLACK = 0xFF000000.toInt()

    /** A hairline drawn along the border's inner edge so it stands out on any content: dark for a colour that is
     *  faint on white (green, white, a light custom colour), light for one that is faint on black, else none. */
    fun outline(argb: Int): Int? = when {
        contrast(argb, WHITE) < 3.0 -> 0xE6000000.toInt()
        contrast(argb, BLACK) < 3.0 -> 0xE6FFFFFF.toInt()
        else -> null
    }

    /** The worst contrast of the border (colour plus its hairline) against plain white or black content. */
    fun worstContrast(argb: Int): Double {
        val line = outline(argb)?.let { it or 0xFF000000.toInt() }
        fun best(bg: Int) = max(contrast(argb, bg), line?.let { contrast(it, bg) } ?: 0.0)
        return min(best(WHITE), best(BLACK))
    }

    /** Hue (degrees) of a colour, or null for a grey. */
    fun hue(argb: Int): Double? {
        val r = ((argb shr 16) and 0xFF) / 255.0; val g = ((argb shr 8) and 0xFF) / 255.0; val b = (argb and 0xFF) / 255.0
        val mx = max(r, max(g, b)); val mn = min(r, min(g, b)); val d = mx - mn
        if (d < 0.15) return null
        val h = when (mx) {
            r -> 60 * (((g - b) / d) % 6)
            g -> 60 * ((b - r) / d + 2)
            else -> 60 * ((r - g) / d + 4)
        }
        return (h + 360) % 360
    }

    /** A chosen colour that is itself amber-like: the approval state then outlines its amber band in black so it
     *  still reads as different (it is thicker and doubled anyway). */
    fun nearApproval(argb: Int): Boolean {
        val h = hue(argb) ?: return false
        val a = hue(APPROVAL)!!
        val d = Math.abs(h - a).let { min(it, 360 - it) }
        return d < 25
    }
}

/** A key-value store (SharedPreferences on the phone, a map in tests). */
interface IndicatorStore {
    fun bool(key: String, default: Boolean): Boolean
    fun str(key: String, default: String): String
    fun put(key: String, value: Boolean)
    fun put(key: String, value: String)
}

/**
 * The owner's indicator settings. Changed only in OpenMob's own screen on the phone (MainActivity): no server, API or
 * protocol method reads or writes them, and agents can't tap OpenMob's UI (the gate's own_packages rule).
 */
class IndicatorPrefs(private val store: IndicatorStore) {
    class Snapshot(val border: Boolean, val color: String, val pulse: Boolean, val pill: Boolean, val corner: PillCorner) {
        val argb: Int get() = IndicatorColors.argb(color)
        /** Nothing to show at all. */
        val off: Boolean get() = !border && !pill
    }

    var border: Boolean
        get() = store.bool(BORDER, true)
        set(v) = store.put(BORDER, v)
    /** A preset key or "#RRGGBB"; anything else is ignored (the default stays). */
    var color: String
        get() = store.str(COLOR, IndicatorColors.DEFAULT).takeIf { IndicatorColors.valid(it) } ?: IndicatorColors.DEFAULT
        set(v) { if (IndicatorColors.valid(v)) store.put(COLOR, if (v.startsWith("#")) v.uppercase() else v) }
    var pulse: Boolean
        get() = store.bool(PULSE, true)
        set(v) = store.put(PULSE, v)
    var pill: Boolean
        get() = store.bool(PILL, true)
        set(v) = store.put(PILL, v)
    var corner: PillCorner
        get() = PillCorner.of(store.str(CORNER, PillCorner.TOP_RIGHT.key))
        set(v) = store.put(CORNER, v.key)

    fun snapshot() = Snapshot(border, color, pulse, pill, corner)

    companion object {
        const val FILE = "openmob_indicator"
        private const val BORDER = "border"
        private const val COLOR = "color"
        private const val PULSE = "pulse"
        private const val PILL = "pill"
        private const val CORNER = "pill_corner"

        /** The pulse runs only if the owner wants it and Android's "Remove animations" is off. */
        fun pulseRuns(s: Snapshot, animatorsEnabled: Boolean, state: IndicatorState) =
            s.border && s.pulse && animatorsEnabled && state == IndicatorState.WORKING
    }
}

/**
 * When the indicator shows (docs/design/control-indicator.md, "Show and hide"). Only the phone's own knowledge turns
 * it on: an action the gatekeeper admitted ([act]) or the phone's own approval prompt ([prompt]). What the server
 * says can't hide it while an agent acts: a "task ended" ([ended]) only shortens the hold after the last action to
 * [endedHoldMs], and the next admitted action shows it again (before its gesture runs: ControlIndicator.beforeAct).
 * Times are uptime ms.
 */
class IndicatorPolicy(
    /** Shown this long after the last admitted action. */
    private val actHoldMs: Long = 10_000,
    /** While the session keeps calling the phone (screenshots, waits): shown until this long after the last call... */
    private val callHoldMs: Long = 10_000,
    /** ...but never longer than this after the last action (an idle drive session that only reads). */
    private val sessionMaxMs: Long = 5 * 60_000,
    /** After the server says the task ended: the border stays this long after the last action, then fades. */
    private val endedHoldMs: Long = 1_500,
) {
    private var lastAct = NEVER
    private var lastCall = NEVER
    private var endedAt = NEVER
    private var previewUntil = NEVER
    /** The phone's own approval prompt is open. */
    var prompt = false
    /** The task waits on the owner (locked mid-task, a hidden system dialog, paused after denials). */
    var paused = false

    /** The gatekeeper admitted an action (before its side effect). */
    fun act(now: Long) { lastAct = now; lastCall = now; if (endedAt >= now) endedAt = NEVER }

    /** Any other call from the server (a read or a wait): keeps an active session's border up, never starts one. */
    fun call(now: Long) { if (lastAct != NEVER) lastCall = now }

    /** The server says the task ended (its result, or the controller line cleared). */
    fun ended(now: Long) { endedAt = now }

    /** The owner pressed Stop (the pill, the notification, Disconnect): off at once. */
    fun stop() { lastAct = NEVER; lastCall = NEVER; endedAt = NEVER; previewUntil = NEVER; prompt = false; paused = false }

    /** The owner's preview from the settings screen. */
    fun preview(now: Long, ms: Long) { previewUntil = now + ms }
    fun endPreview() { previewUntil = NEVER }

    /** An agent is acting (not just a preview). */
    fun agentActive(now: Long): Boolean {
        if (lastAct == NEVER) return false
        val sinceAct = now - lastAct
        if (endedAt >= lastAct) return sinceAct < endedHoldMs
        if (sinceAct < actHoldMs) return true
        if (sinceAct >= sessionMaxMs) return false
        return paused || now - lastCall < callHoldMs
    }

    fun state(now: Long): IndicatorState = when {
        prompt -> IndicatorState.APPROVAL
        agentActive(now) -> if (paused) IndicatorState.PAUSED else IndicatorState.WORKING
        now < previewUntil -> IndicatorState.WORKING
        else -> IndicatorState.HIDDEN
    }

    /** When to look again (ms from now) for the state to change by itself, or null if it won't. */
    fun recheckIn(now: Long): Long? {
        val deadlines = mutableListOf<Long>()
        if (lastAct != NEVER) {
            if (endedAt >= lastAct) deadlines += lastAct + endedHoldMs
            else { deadlines += lastAct + actHoldMs; deadlines += lastCall + callHoldMs; deadlines += lastAct + sessionMaxMs }
        }
        if (previewUntil != NEVER) deadlines += previewUntil
        return deadlines.filter { it > now }.minOrNull()?.let { max(1L, it - now) }
    }

    private companion object { const val NEVER = Long.MIN_VALUE / 2 }
}

/**
 * Where the Stop pill sits (docs/design/control-indicator.md, "The pill dodges"). The owner's corner is the default.
 * When an agent's gesture the gate would accept lands on the pill, the phone moves it to another visible corner that
 * clears the gesture's points; it stays there until the session's indicator goes, then the owner's corner applies again.
 */
class PillPlacement {
    /** The corner the pill moved to for this session, or null: the owner's. */
    var dodged: PillCorner? = null
        private set

    fun corner(owner: PillCorner): PillCorner = dodged ?: owner

    /** The session ended (the indicator went): back to the owner's corner next time. */
    fun reset() { dodged = null }

    /** Move out of the way of [points] (screen px), keeping [margin] px clear; [rectOf] is the pill's bounds in a corner.
     *  Returns the new corner, or null if every other corner would cover a point too (the gesture stays refused). */
    fun dodge(owner: PillCorner, points: List<Pair<Double, Double>>, margin: Int, rectOf: (PillCorner) -> IntArray): PillCorner? {
        val current = corner(owner)
        if (!IndicatorGeometry.covers(rectOf(current), points, margin)) return null // not in the way
        val to = IndicatorGeometry.order(current).firstOrNull { !IndicatorGeometry.covers(rectOf(it), points, margin) } ?: return null
        dodged = to
        return to
    }
}

object IndicatorGeometry {
    /** Where to try next: the other corner of the same edge, then the other edge on the same side, then the diagonal
     *  (top right: top left, bottom right, bottom left). */
    fun order(from: PillCorner): List<PillCorner> {
        fun at(top: Boolean, right: Boolean) = PillCorner.values().first { it.top == top && it.right == right }
        return listOf(at(from.top, !from.right), at(!from.top, from.right), at(!from.top, !from.right))
    }

    /** Any of [points] inside [r] grown by [margin]. */
    fun covers(r: IntArray, points: List<Pair<Double, Double>>, margin: Int): Boolean = points.any { (x, y) ->
        r[0] - margin <= x && x < r[2] + margin && r[1] - margin <= y && y < r[3] + margin
    }

    /** The pill's bounds in [corner] on a [screenW] x [screenH] screen: [sideGap] px in from the side, [edgeGap] px
     *  below the status bar or above the navigation bar ([insets]: left, top, right, bottom). Always on screen. */
    fun pillRect(corner: PillCorner, w: Int, h: Int, screenW: Int, screenH: Int, insets: IntArray,
                 sideGap: Int, edgeGap: Int): IntArray {
        val x = sideGap + if (corner.right) insets[2] else insets[0]
        val y = edgeGap + if (corner.top) insets[1] else insets[3]
        val left = (if (corner.right) screenW - x - w else x).coerceIn(0, maxOf(0, screenW - w))
        val top = (if (corner.top) y else screenH - y - h).coerceIn(0, maxOf(0, screenH - h))
        return intArrayOf(left, top, left + w, top + h)
    }

    /** OCR lines that are OpenMob's own Stop pill (their centre inside it): the permission-dialog reader drops them. */
    fun dropOwn(lines: List<GOcrLine>, own: List<IntArray>): List<GOcrLine> {
        if (own.isEmpty()) return lines
        return lines.filter { l ->
            val cx = (l.bounds[0] + l.bounds[2]) / 2.0; val cy = (l.bounds[1] + l.bounds[3]) / 2.0
            own.none { r -> r[0] <= cx && cx < r[2] && r[1] <= cy && cy < r[3] }
        }
    }
}
