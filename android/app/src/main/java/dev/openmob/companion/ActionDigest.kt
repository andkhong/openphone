package dev.openmob.companion

import java.security.MessageDigest

/**
 * The action an approval binds (docs/phone-protocol.md, "Approval signing"; the server's
 * phone/approval_keys.py action_digest). The server sends the action's fields with each
 * approval_request; the app shows them, recomputes the digest from exactly those fields and signs it,
 * so a yes can only ever approve the action it was shown.
 *
 * Digest: SHA-256 (hex) of "openmob-action-1" followed, for each field in [FIELDS] order, by
 * `|<UTF-8 byte length>:<value>` (missing fields are empty; bounds as "l,t,r,b").
 */
object ActionDigest {
    const val TAG = "openmob-action-1"
    val FIELDS = listOf("tool", "package", "text", "desc", "resource_id", "bounds", "input")

    fun of(fields: Map<String, String>): String {
        val out = java.io.ByteArrayOutputStream()
        out.write(TAG.toByteArray(Charsets.UTF_8))
        for (name in FIELDS) {
            val raw = (fields[name] ?: "").toByteArray(Charsets.UTF_8)
            out.write("|${raw.size}:".toByteArray(Charsets.UTF_8))
            out.write(raw)
        }
        return MessageDigest.getInstance("SHA-256").digest(out.toByteArray()).joinToString("") { "%02x".format(it) }
    }

    private val ALLOW_MONEY = Regex("\"allow_money\":\\s*true\\b")

    /**
     * A permission change (`mode_change`) that would also let purchases run without asking ("Also allow
     * purchases" with Never ask). Read from the signed action itself (its input is the question, then
     * the change as JSON), never from unsigned fields, so the app always asks the second time for it.
     */
    fun allowsMoney(fields: Map<String, String>): Boolean =
        fields["tool"] == "mode_change" && ALLOW_MONEY.containsMatchIn(fields["input"].orEmpty().substringAfter('\n', ""))

    /** For a `mode_change`: the question as signed (the input's first line), or "" for anything else. */
    fun signedQuestion(fields: Map<String, String>): String =
        if (fields["tool"] == "mode_change") fields["input"].orEmpty().substringBefore('\n').trim() else ""

    /** One line saying what will happen: `tap "Buy now" (id buy_now) in com.amazon… · "typed text"`. */
    fun describe(fields: Map<String, String>): String {
        val tool = fields["tool"].orEmpty()
        val verb = when (tool) {
            "tap", "tap_element" -> "Tap"
            "long_press" -> "Long-press"
            "swipe", "scroll" -> "Swipe"
            "enter" -> "Press Enter in"
            "type_text" -> "Type into"
            "open_url" -> "Open"
            "launch_app" -> "Open the app"
            "plan" -> "Carry out the plan"
            "mode_change" -> "Change how agents ask"
            else -> tool.replace('_', ' ').replaceFirstChar { it.uppercase() }
        }
        val label = listOf(fields["text"], fields["desc"]).firstOrNull { !it.isNullOrBlank() }?.trim()
        val id = fields["resource_id"].orEmpty().trim()
        val target = buildString {
            if (label != null) append("“").append(label.take(80)).append("”")
            if (id.isNotEmpty()) append(if (isEmpty()) "id $id" else " (id $id)")
            if (isEmpty() && fields["bounds"].orEmpty().isNotEmpty() && tool in setOf("tap", "long_press", "swipe")) {
                append("an unlabeled spot at ").append(fields["bounds"])
            }
        }
        val input = fields["input"].orEmpty().trim()
        val app = fields["package"].orEmpty()
        return buildString {
            append(verb)
            if (target.isNotEmpty()) append(' ').append(target)
            if (input.isNotEmpty() && tool !in setOf("plan", "mode_change")) {
                append(" · “").append(input.take(120)).append("”")
            }
            if (app.isNotEmpty()) append(" in ").append(app)
        }
    }
}
