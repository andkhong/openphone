/*
 * WaitCondition: the conditions of the protocol's wait_for, evaluated on the phone's own element list
 * (docs/phone-protocol.md §4.16). Pure Kotlin (no Android types), so it's unit-tested on the JVM, and it
 * matches exactly like the server's openmob/agent/waiting.py: a pattern is a case-insensitive regex (text
 * that isn't one is matched literally) that is either the whole label or stands as whole words in it.
 *
 * Read-only by construction: it only looks at elements PhoneActions already read.
 */
package dev.openmob.companion

import org.json.JSONArray
import org.json.JSONObject

/** One element of a ui_elements result, as far as a wait needs it. */
data class WaitElement(
    val text: String,
    val desc: String,
    val resourceId: String,
    val cls: String,
    val checked: Boolean?,
    val enabled: Boolean,
    val covered: Boolean,
) {
    val labeled: Boolean get() = text.isNotEmpty() || desc.isNotEmpty()

    companion object {
        fun parse(array: JSONArray): List<WaitElement> = (0 until array.length()).mapNotNull { i ->
            val o = array.optJSONObject(i) ?: return@mapNotNull null
            WaitElement(
                o.optString("text"), o.optString("desc"), o.optString("resource_id"), o.optString("cls"),
                if (o.isNull("checked") || !o.has("checked")) null else o.optBoolean("checked"),
                o.optBoolean("enabled", true), o.optBoolean("covered", false),
            )
        }
    }
}

class WaitCondition(val until: String, pattern: String, val pkg: String, val quietMs: Long) {
    private val regex: Regex? = if (pattern.isEmpty()) null else
        runCatching { Regex(pattern, RegexOption.IGNORE_CASE) }.getOrElse { Regex(Regex.escape(pattern), RegexOption.IGNORE_CASE) }
    private val words: Regex? = regex?.let { runCatching { Regex("(?<!\\w)(?:${it.pattern})(?!\\w)", RegexOption.IGNORE_CASE) }.getOrNull() }

    init {
        require(until in CONDITIONS) { "Unknown wait condition $until" }
        require(until !in NEEDS_PATTERN || regex != null) { "$until needs a pattern" }
        require(until != "app_in_front" || pkg.isNotEmpty()) { "app_in_front needs a package" }
    }

    fun matches(value: String, exact: Boolean = false): Boolean {
        val v = value.trim()
        val r = regex ?: return false
        if (v.isEmpty()) return false
        if (r.matches(v)) return true
        return !exact && words?.containsMatchIn(v) == true
    }

    /**
     * What satisfies the condition, or null. [baseline]: the signature when the wait began (screen_changes);
     * [quietFor]: ms since the last window or content change (loading_done); [unloadedFor]: ms a labeled screen has
     * shown nothing loading (loading_done on a screen that never goes quiet: a map's moving tiles and pins).
     */
    fun check(pkgNow: String?, els: List<WaitElement>, baseline: Set<String>?, quietFor: Long,
              unloadedFor: Long = 0L): String? = when (until) {
        "app_in_front" -> if (pkgNow == pkg) "$pkg is in front" else null
        "text_appears" -> els.firstOrNull { matches(it.text) || matches(it.desc) }?.let { "'${label(it)}' is on screen" }
        "text_disappears" -> if (els.any { it.labeled } && els.none { matches(it.text) || matches(it.desc) })
            "no match on screen" else null
        "element_enabled" -> els.firstOrNull { it.enabled && !it.covered && (matches(it.text, true) || matches(it.desc, true)) }
            ?.let { "'${label(it)}' is enabled" }
        "screen_changes" -> if (baseline != null && signature(pkgNow, els) != baseline) "the screen changed" else null
        "loading_done" -> if (els.any { it.labeled } && els.none { loading(it) } && quietFor >= quietMs)
            "nothing is loading and the screen is still"
        else if (els.any { it.labeled } && els.none { loading(it) } && unloadedFor >= MOVING_GRACE_MS)
            "nothing is loading (the screen keeps moving)" else null
        else -> null
    }

    companion object {
        /** loading_done ends this long after a labeled screen last showed something loading, even if it never goes
         *  quiet (owner's Samsung, 2026-10-17: Maps waited its ~9.4 s cap with results up after ~2.5 s). The server
         *  mirrors it (waiting.LOADING_MOVING_GRACE_S). */
        const val MOVING_GRACE_MS = 2_500L
        val CONDITIONS = setOf("screen_changes", "text_appears", "text_disappears", "element_enabled", "app_in_front", "loading_done")
        private val NEEDS_PATTERN = setOf("text_appears", "text_disappears", "element_enabled")
        private val LOADING_CLASS = Regex("progress|spinner|loading|shimmer", RegexOption.IGNORE_CASE)
        private val LOADING_ID = Regex("progress|spinner|loading|shimmer|skeleton", RegexOption.IGNORE_CASE)
        private val LOADING_TEXT = Regex("^\\s*(loading|please wait|connecting|starting|signing in|downloading|installing|" +
            "pending|preparing|updating)\\b|\\bloading\\b\\W*$", RegexOption.IGNORE_CASE) // no bare "83%": see waiting.py
        private val DIGITS = Regex("\\d+")

        fun from(json: JSONObject) = WaitCondition(
            json.getString("until"), json.optString("pattern", ""), json.optString("package", ""),
            json.optLong("quiet_ms", 500L).coerceIn(50L, 8_000L),
        )

        fun loading(e: WaitElement): Boolean = LOADING_CLASS.containsMatchIn(e.cls) || LOADING_ID.containsMatchIn(e.resourceId) ||
            listOf(e.text, e.desc).any { it.isNotEmpty() && LOADING_TEXT.containsMatchIn(it) }

        /** Labels and ids present, numbers blanked (a clock, a percentage), positions and OCR text left out. */
        fun signature(pkg: String?, els: List<WaitElement>): Set<String> =
            els.filter { it.cls != "OCRText" }.map {
                "${DIGITS.replace(it.text, "#")}\u0000${DIGITS.replace(it.desc, "#")}\u0000${it.resourceId}\u0000${it.cls}\u0000${it.checked}"
            }.toSet() + "\u0001${pkg ?: ""}"

        private fun label(e: WaitElement) = (e.text.ifEmpty { e.desc }).take(60)
    }
}
