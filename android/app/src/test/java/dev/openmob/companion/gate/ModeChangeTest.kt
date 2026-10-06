package dev.openmob.companion.gate

import dev.openmob.companion.ActionDigest
import dev.openmob.companion.gate.GateScenarioTest.Companion.RULES
import dev.openmob.companion.gate.GateScenarioTest.Companion.SCREENS
import dev.openmob.companion.gate.GateScenarioTest.Companion.VERIFIER
import dev.openmob.companion.gate.GateScenarioTest.Companion.sign
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** A permission change approved on the phone becomes its own signed policy (and nothing else can make one). */
class ModeChangeTest {
    private val clock = GateScenarioTest.FakeClock()
    private val gk = Gatekeeper(RULES, clock, VERIFIER, "dev", ModeChange.verifier(VERIFIER))
    private val buy = mapOf<String, Any?>("x" to 540L, "y" to 1720L, "mode" to "never")

    private fun action(question: String, json: String) =
        mapOf("tool" to "mode_change", "package" to "", "text" to "", "desc" to "", "resource_id" to "", "bounds" to "",
              "input" to "$question\n$json")

    private fun approve(action: Map<String, String>, sigOk: Boolean = true): Int {
        val message = ModeChange.message("a1", "mode_change:a1", ActionDigest.of(action), 1_800_000_000, false)
        val proof = ModeChange.proof("a1", "mode_change:a1", action, 1_800_000_000, false, if (sigOk) sign(message) else "x")
        return ModeChange.records(action.getValue("input")).count { gk.addPolicy(it, proof) }
    }

    @Test fun theServersQuestionIsNotTheWording() {
        val a = action("Approve to keep your account safe", "{\"allow_money\": true, \"never\": \"1h\"}")
        assertEquals("Turn on Don't ask: agents act on this phone without asking, INCLUDING PURCHASES, for 1 hour?",
                     ModeChange.describe(a.getValue("input")))
        assertTrue(ModeChange.allowsMoney(a.getValue("input")))
    }

    @Test fun anApprovedNeverWithPurchasesIsEnforced() {
        assertEquals("ask", gk.admit("tap", buy, SCREENS.getValue("amazon_product")).decision)
        gk.cancel(gk.admit("key", mapOf("name" to "BACK"), SCREENS.getValue("amazon_product")).approvalId)
        assertEquals(0, approve(action("q", "{\"allow_money\": true, \"never\": \"1h\"}"), sigOk = false))
        clock.elapsed += 200_000 // let the earlier prompt expire
        assertEquals(1, approve(action("q", "{\"allow_money\": true, \"never\": \"1h\"}")))
        assertTrue(gk.admit("tap", buy, SCREENS.getValue("amazon_product")).allowed)
        // Saved and re-verified on load; a record the signed change doesn't contain is dropped.
        val (entries, hwm) = gk.persist()
        val fresh = Gatekeeper(RULES, clock, VERIFIER, "dev", ModeChange.verifier(VERIFIER))
        assertEquals(1, fresh.restore(entries, hwm))
        val forged = entries.map { PolicyEntry(it.record + ("duration_ms" to 86_400_000L), it.proof, it.issued, it.remaining) }
        assertEquals(0, Gatekeeper(RULES, clock, VERIFIER, "dev", ModeChange.verifier(VERIFIER)).restore(forged, hwm))
    }

    @Test fun ceilingAndTrustedApps() {
        assertEquals(2, approve(action("q", "{\"ceiling\": \"trusted\", \"trusted\": [{\"app\": \"com.whatsapp\", " +
            "\"kinds\": [\"send\"], \"expires_at\": 1}, {\"category\": \"social\", \"kinds\": [\"post\"]}]}")))
        assertEquals("trusted", gk.mode("never"))
        assertEquals("grant", gk.admit("tap", mapOf("x" to 990L, "y" to 2250L), SCREENS.getValue("whatsapp_chat")).rule)
    }

    @Test fun approvedPermissionDefaultsBecomeSignedPolicy() {
        val a = action("q", "{\"permission_prompts\": {\"location\": \"while_using\", \"location_accuracy\": \"precise\"}}")
        assertEquals("Let agents allow location (while_using) in permission dialogs without asking? " +
                     "Let agents give apps your precise location without asking?", ModeChange.describe(a.getValue("input")))
        assertEquals(2, approve(a))
        assertEquals("while_using", gk.permSetting("location"))
        assertEquals("precise", gk.permSetting("location_accuracy"))
        assertEquals(0, approve(action("q", "{\"permission_prompts\": {\"location\": \"always\"}}")))
    }

    @Test fun neverOffIsATightening() {
        assertTrue(ModeChange.turnsNeverOff(action("q", "{\"never\": \"off\"}").getValue("input")))
        assertFalse(ModeChange.turnsNeverOff(action("q", "{\"never\": \"1h\"}").getValue("input")))
    }
}
