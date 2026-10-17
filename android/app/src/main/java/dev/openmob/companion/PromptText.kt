package dev.openmob.companion

/**
 * The screen-lock prompt's description. Android's device-credential screen puts the description above the PIN field
 * in a scroll area; a long one (Faster mode's warning) pushed the PIN field under the keyboard on an Android 15
 * emulator, so the PIN couldn't be typed. The full text is on the approval screen right before, so the system prompt
 * gets its first question (or the start of it).
 */
object PromptText {
    const val MAX = 180

    fun short(text: String, max: Int = MAX): String {
        val t = text.trim()
        if (t.length <= max) return t
        val q = t.indexOf('?')
        if (q in 1 until max) return t.substring(0, q + 1)
        return t.take(max - 1).trimEnd() + "…"
    }
}
