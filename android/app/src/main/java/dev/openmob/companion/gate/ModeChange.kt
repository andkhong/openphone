package dev.openmob.companion.gate

import dev.openmob.companion.ActionDigest

/**
 * A permission change the owner approved on this phone (the server's `mode_change` approval_request: the
 * dashboard's "Never ask", "Also allow purchases", a higher ceiling, trusted apps). On a gatekeeper phone it
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

    /** The phone's own wording of the change. */
    fun describe(input: String): String {
        val parts = mutableListOf<String>()
        for (r in records(input)) when (r["type"]) {
            "never" -> {
                val hours = (r["duration_ms"] as Long) / 3_600_000L
                val span = if (hours >= 24) "24 hours" else "$hours hour${if (hours == 1L) "" else "s"}"
                parts.add(if (r["allow_money"] == true) "Let agents act on this phone without asking, INCLUDING PURCHASES, for $span?"
                          else "Let agents act on this phone without asking for $span? Purchases stay off.")
            }
            "ceiling" -> parts.add("Let agents ask less on this phone (up to “${r["mode"]}”)?")
            "grant" -> parts.add("Let agents ${r["kind"]} in ${r["binding"]} without asking for ${
                if (r["kind"] == "pay") "24 hours" else "7 days"}?")
        }
        return parts.distinct().joinToString(" ").ifEmpty { "Change how agents ask on this phone?" }
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
