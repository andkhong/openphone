package dev.openmob.companion.gate

import dev.openmob.companion.ActionDigest

/**
 * A permission change the owner approved on this phone (the server's `mode_change` approval_request: the
 * dashboard's "Don't ask", "Also allow purchases", a higher ceiling, trusted apps). On a gatekeeper phone it
 * becomes signed owner policy (D16, D17, D21) that the phone itself enforces.
 *
 * The phone words the prompt itself from the structured change ([describe]); the server's question is shown
 * only as "Server says". The one screen-lock signature is the server's approval format (so the server's own
 * settings follow too), over the action digest whose `input` carries the change as JSON. On load, each record
 * is re-derived from that signed input and the signature checked again, so nothing else can create one.
 */
object ModeChange {
    private val NEVER_MS = mapOf("1h" to 3_600_000L, "24h" to 86_400_000L, "7d" to 604_800_000L)
    private val CEILINGS = setOf("ask", "plan", "auto", "trusted")
    private val GRANT_KINDS = setOf("send", "post", "delete", "pay", "fill")
    const val PAY_MS = 86_400_000L
    const val GRANT_MS = 604_800_000L
    private val MODE_NAMES = mapOf("ask" to "Ask every step", "plan" to "Plan first", "auto" to "Ask for risky",
                                   "trusted" to "Trusted apps")

    /** The change as JSON, from the signed action's input ("<question>\n<json>"). */
    fun patch(input: String): Map<String, Any?> =
        runCatching { MiniJson.parse(input.substringAfter('\n', "")).obj() }.getOrDefault(emptyMap())

    /** The owner-policy records a change makes. Category entries aren't enforced on the phone (they'd only
     *  stop it asking; it keeps asking). */
    fun records(input: String): List<Map<String, Any?>> {
        val p = patch(input)
        val out = mutableListOf<Map<String, Any?>>()
        (p["ceiling"] as? String)?.takeIf { it in CEILINGS }?.let { out.add(mapOf("type" to "ceiling", "mode" to it)) }
        (p["never"] as? String)?.let { NEVER_MS[it] }?.let {
            out.add(mapOf("type" to "never", "duration_ms" to it, "allow_money" to (p["allow_money"] == true)))
        }
        if (p["everyday"] == true) out.add(mapOf("type" to "everyday", "on" to true))
        for ((perm, value) in p["permission_prompts"].obj()) {
            out.add(mapOf("type" to "permission_default", "perm" to perm, "value" to value.toString()))
        }
        for (e in p["trusted"].list().map { it.obj() }) {
            val app = e["app"] as? String ?: continue
            for (kind in e["kinds"].strings()) if (kind in GRANT_KINDS) {
                out.add(mapOf("type" to "grant", "kind" to kind, "binding" to app,
                              "duration_ms" to if (kind == "pay") PAY_MS else GRANT_MS))
            }
        }
        return out
    }

    /** "never": "off" in the change: a tightening the phone takes from anywhere. */
    fun turnsNeverOff(input: String): Boolean = patch(input)["never"] == "off"

    /** Faster mode's warning, shown when turning it on (docs/safety.md "Faster mode"; the server's FASTER_WARNING). */
    const val FASTER_WARNING = "Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second " +
        "AI service, so it can recognize screens faster. Banking, wallet and password apps, and screens the phone " +
        "blacks out, are never sent. OpenMob blocks banking apps and detected card and ID numbers, but it can't " +
        "recognize every sensitive image. You can turn this off at any time."

    /** The warning's first sentence: what the screen-lock sheet shows ([lockText]). */
    const val FASTER_FIRST = "Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second AI " +
        "service, so it can recognize screens faster."

    /** The keys a Faster mode change may carry (the server's FASTER_PATCH); a drive change carries faster_drive alone. */
    private val FASTER_KEYS = setOf("faster_mode", "faster_until", "faster_idle_s", "faster_resume_s")

    /** How long the owner's tap on "Turn on" stays good for the approval it asks the server for. */
    const val REQUEST_MS = 5 * 60_000L

    /** Why this phone declines a Faster mode change without showing it (review M2, 2026-10-17), else null:
     *  - "mixed": it carries anything besides that one Faster part (Don't ask, purchases, a ceiling, everyday,
     *    permission defaults, trusted apps, or a second Faster part), which one screen lock would also sign;
     *  - "not_requested": it turns Faster mode on in a scope the owner didn't just tap on this phone ([requested]:
     *    the scope and when, in elapsed ms; [now] the same clock).
     *  A key's drive change has no tap here (the dashboard asks), so only "mixed" applies to it. */
    fun fasterRefusal(input: String, requested: Pair<String, Long>?, now: Long): String? {
        val p = patch(input)
        val faster = "faster_mode" in p
        val drive = "faster_drive" in p
        if (!faster && !drive) return null
        if (faster && drive) return "mixed"
        if (faster && !FASTER_KEYS.containsAll(p.keys)) return "mixed"
        if (drive && p.keys != setOf("faster_drive")) return "mixed"
        val scope = fasterScope(input) ?: return null
        if (requested == null || requested.first != scope || now < requested.second || now - requested.second > REQUEST_MS) {
            return "not_requested"
        }
        return null
    }

    /** What the screen-lock sheet says (it must stay short: a long description pushes the PIN field under the
     *  keyboard, PromptText): for Faster mode the scope and the warning's whole first sentence; for other changes each
     *  part's first sentence, as many as fit, then how many more the screen above lists. */
    fun lockText(input: String, max: Int = dev.openmob.companion.PromptText.MAX): String {
        when (fasterScope(input)) {
            "session" -> return "Turn on Faster mode (this session): $FASTER_FIRST"
            "phone" -> return "Turn on Faster mode (always on this phone): $FASTER_FIRST"
        }
        fasterDriveKey(input)?.let { (key, name) -> return "API key “${name.ifEmpty { key }.take(20)}”: $FASTER_FIRST" }
        val parts = describeParts(input).map { p -> p.indexOf('?').let { q -> if (q > 0) p.substring(0, q + 1) else p } }
        if (parts.isEmpty()) return dev.openmob.companion.PromptText.short(describe(input), max)
        val shown = mutableListOf<String>()
        for ((i, part) in parts.withIndex()) {
            val more = parts.size - i - 1
            val tail = if (more > 0) " +$more more above." else ""
            val candidate = (shown + part).joinToString(" ")
            if ((candidate + tail).length > max) break
            shown.add(part)
        }
        if (shown.isEmpty()) return dev.openmob.companion.PromptText.short(parts[0], max)
        val left = parts.size - shown.size
        return shown.joinToString(" ") + if (left > 0) " +$left more above." else ""
    }

    /** A change that turns Faster mode on: its scope ("phone" or "session"), else null. Not gatekeeper policy (the
     *  server sends the images; the phone keeps the owner's choice in [dev.openmob.companion.FasterMode]). */
    fun fasterScope(input: String): String? = (patch(input)["faster_mode"] as? String)?.takeIf { it == "phone" || it == "session" }

    /** A change that lets one API key's drive sessions use Faster mode: (key id, name), else null. */
    fun fasterDriveKey(input: String): Pair<String, String>? {
        val d = patch(input)["faster_drive"].obj()
        val key = d["key"] as? String ?: return null
        return if (d["on"] == true) key to (d["name"] as? String).orEmpty().take(60) else null
    }

    /** The phone's own wording of the change. */
    fun describe(input: String): String =
        describeParts(input).joinToString(" ").ifEmpty { "Change how agents ask on this phone?" }

    /** One wording per part of the change, in order. */
    fun describeParts(input: String): List<String> {
        val parts = mutableListOf<String>()
        when (fasterScope(input)) {
            "phone" -> parts.add("Turn on Faster mode on this phone? $FASTER_WARNING")
            "session" -> parts.add("Turn on Faster mode for this session (until you disconnect, or an hour without " +
                                   "agent activity)? $FASTER_WARNING")
        }
        fasterDriveKey(input)?.let { (key, name) ->
            parts.add("Let the API key “${name.ifEmpty { key }}” use Faster mode in its drive sessions on this phone? " +
                      FASTER_WARNING)
        }
        // The loosest first (Don't ask), so a short summary (lockText) never leaves it out.
        for (r in records(input).sortedBy { if (it["type"] == "never") 0 else 1 }) when (r["type"]) {
            "never" -> {
                val hours = (r["duration_ms"] as Long) / 3_600_000L
                val span = if (hours >= 24) "24 hours" else "$hours hour${if (hours == 1L) "" else "s"}"
                parts.add(if (r["allow_money"] == true) "Turn on Don't ask: agents act on this phone without asking, INCLUDING PURCHASES, for $span?"
                          else "Turn on Don't ask: agents act on this phone without asking for $span? Purchases stay off.")
            }
            "ceiling" -> parts.add("Let agents ask less on this phone (up to “${MODE_NAMES[r["mode"]] ?: r["mode"]}”)?")
            "everyday" -> parts.add("Stop confirming everyday actions (messages, shares, posts, deletes)? Purchases and dangerous actions still need your fingerprint.")
            "permission_default" -> {
                val what = r["perm"].toString().replace('_', ' ')
                parts.add(when (r["value"]) {
                    "precise" -> "Let agents give apps your precise location without asking?"
                    "once", "while_using", "allow" -> "Let agents allow $what (${r["value"]}) in permission dialogs without asking?"
                    else -> "Ask before agents allow $what?"
                })
            }
            "grant" -> parts.add("Let agents ${r["kind"]} in ${r["binding"]} without asking for ${
                if (r["kind"] == "pay") "24 hours" else "7 days"}?")
        }
        return parts.distinct()
    }

    fun allowsMoney(input: String) = records(input).any { it["type"] == "never" && it["allow_money"] == true }

    /** The proof stored with each record: the signed approval's fields. */
    fun proof(approvalId: String, taskId: String, action: Map<String, String>, timestamp: Long, always: Boolean,
              signature: String): PolicyProof {
        val extra = mutableMapOf("kind" to "mode_change", "approval_id" to approvalId, "task_id" to taskId,
                                 "timestamp" to timestamp.toString(), "always" to if (always) "1" else "0")
        for ((k, v) in action) extra["a:$k"] = v
        return PolicyProof(message(approvalId, taskId, ActionDigest.of(action), timestamp, always), signature, extra)
    }

    /** The server's approval message format (ApprovalKeys.message). */
    fun message(approvalId: String, taskId: String, digest: String, timestamp: Long, always: Boolean) =
        "$approvalId|$taskId|$digest|true|$timestamp" + if (always) "|always" else ""

    /** Gatekeeper's extra proof check for these records. */
    fun verifier(check: GateVerifier): (String, Map<String, Any?>, PolicyProof) -> Boolean = { canon, _, proof ->
        val x = proof.extra
        if (x["kind"] != "mode_change") false else {
            val action = x.filterKeys { it.startsWith("a:") }.mapKeys { it.key.removePrefix("a:") }
            val expected = message(x["approval_id"].orEmpty(), x["task_id"].orEmpty(), ActionDigest.of(action),
                                   x["timestamp"]?.toLongOrNull() ?: -1, x["always"] == "1")
            action["tool"] == "mode_change" && proof.message == expected && check.verify(proof.message, proof.signature) &&
                records(action["input"].orEmpty()).any { Gatekeeper.canon(it) == canon }
        }
    }
}
