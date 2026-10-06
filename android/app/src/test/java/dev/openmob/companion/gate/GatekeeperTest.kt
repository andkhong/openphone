package dev.openmob.companion.gate

import dev.openmob.companion.gate.GateScenarioTest.Companion.RULES
import dev.openmob.companion.gate.GateScenarioTest.Companion.SCREENS
import dev.openmob.companion.gate.GateScenarioTest.Companion.VERIFIER
import dev.openmob.companion.gate.GateScenarioTest.Companion.sign
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Engine details the scenario fixtures don't pin down directly. */
class GatekeeperTest {
    private val clock = GateScenarioTest.FakeClock()
    private val gk = Gatekeeper(RULES, clock, VERIFIER, "dev")
    private val buy = mapOf<String, Any?>("x" to 540L, "y" to 1720L)
    private val send = mapOf<String, Any?>("x" to 990L, "y" to 2250L)

    private fun policy(record: Map<String, Any?>): Boolean {
        val m = "gk1|policy|" + Gatekeeper.canon(record)
        return gk.addPolicy(record, PolicyProof(m, sign(m)))
    }

    @Test fun wordsAreNormalized() {
        assertEquals(listOf("cafe", "pay", "now"), GateText.tokens("CAFÉ — Pay-now!"))
        assertEquals(listOf("buy", "now", "button"), GateText.idTokens("com.x:id/buyNowButton"))
        assertTrue(GateText.hasPhrase(GateText.tokens("Place your ORDER"), "place your order"))
        assertEquals(listOf("2", "items"), GateText.stripPhrases(GateText.tokens("Proceed to checkout 2 items"),
            RULES.w.getValue("pre_commit")))
    }

    @Test fun schemesAreCleanedLikeBrowsersDo() {
        assertEquals("javascript", GateText.urlScheme("  java\tscript:alert(1)"))
        assertNull(GateText.urlScheme("example.com/page"))
        assertTrue(GateText.looksLikeUrl("www.chase.com"))
    }

    @Test fun modeIsTheMinimumOfRequestAndCeiling() {
        assertEquals("auto", gk.mode("never"))
        assertEquals("auto", gk.mode("trusted"))
        assertEquals("ask", gk.mode("ask"))
        assertEquals("auto", gk.mode(null))
        assertTrue(policy(mapOf("type" to "ceiling", "mode" to "trusted")))
        assertEquals("trusted", gk.mode("never"))
        assertEquals("auto", gk.mode("plan")) // plan is enforced as auto on the phone in Phase 1
        assertTrue(policy(mapOf("type" to "never", "duration_ms" to 3_600_000L)))
        assertEquals("never", gk.mode("never"))
        assertEquals("never", gk.mode(null))
        assertEquals("ask", gk.mode("ask")) // tightening is accepted from anywhere
    }

    @Test fun anUnsignedOrForgedPolicyIsDropped() {
        val record = mapOf<String, Any?>("type" to "ceiling", "mode" to "trusted")
        val m = "gk1|policy|" + Gatekeeper.canon(record)
        assertFalse(gk.addPolicy(record, PolicyProof(m, "")))
        assertFalse(gk.addPolicy(record, PolicyProof(m, sign("gk1|policy|type=ceiling;mode=never"))))
        assertFalse(gk.addPolicy(mapOf("type" to "ceiling", "mode" to "never"), PolicyProof(m, sign(m))))
        assertEquals("auto", gk.mode("trusted"))
    }

    @Test fun neverWithoutAllowMoneyRefusesPurchasesWithIt() {
        assertTrue(policy(mapOf("type" to "never", "duration_ms" to 3_600_000L)))
        assertEquals("never:money", gk.admit("tap", buy + ("mode" to "never"), SCREENS.getValue("amazon_product")).rule)
        assertTrue(policy(mapOf("type" to "never", "duration_ms" to 3_600_000L, "allow_money" to true)))
        assertTrue(gk.admit("tap", buy + ("mode" to "never"), SCREENS.getValue("amazon_product")).allowed)
        assertTrue(gk.neverMoney)
        gk.revokeAll() // Stop
        assertFalse(gk.neverActive)
    }

    @Test fun aPermitIsBoundToTheExactAction() {
        val screen = SCREENS.getValue("whatsapp_chat")
        val ask = gk.admit("tap", send, screen)
        assertEquals("ask", ask.decision)
        val m = gk.approvalMessage(ask.approvalId)!!
        assertFalse(gk.approve(ask.approvalId, m, sign(m + "x")))
        assertFalse(gk.approve(ask.approvalId, m.replace(ask.binding, "0".repeat(64)), sign(m.replace(ask.binding, "0".repeat(64)))))
        assertTrue(gk.approve(ask.approvalId, m, sign(m)))
        assertFalse(gk.approve(ask.approvalId, m, sign(m))) // one answer per prompt
        clock.elapsed += 1000
        // A different point on the same button is a different action: the permit doesn't cover it.
        assertEquals("ask", gk.admit("tap", mapOf("x" to 991L, "y" to 2250L), screen).decision)
    }

    @Test fun bindingIgnoresHintAndModeButNotTheTarget() {
        val a = GateScreen.binding("tap", mapOf("x" to 1L, "y" to 2L, "hint" to "x", "mode" to "ask"), "fp")
        assertEquals(a, GateScreen.binding("tap", mapOf("x" to 1.0, "y" to 2.0), "fp"))
        assertNotEquals(a, GateScreen.binding("tap", mapOf("x" to 1L, "y" to 2L), "fp2"))
    }

    @Test fun grantsCountDownOnTheMonotonicClockAcrossAReboot() {
        val screen = SCREENS.getValue("whatsapp_chat")
        val ask = gk.admit("tap", send, screen)
        val m = gk.approvalMessage(ask.approvalId, grant = true)!!
        assertTrue(m.contains("|grant:binding=com.whatsapp;duration_ms=604800000;kind=send;type=grant"))
        assertTrue(gk.approve(ask.approvalId, m, sign(m)))
        clock.elapsed += 3 * 86_400_000L
        val (entries, hwm) = gk.persist()
        assertEquals(4 * 86_400_000L, entries.single().remaining)
        // Same boot (the app restarted): the issue time still holds.
        val again = Gatekeeper(RULES, clock, VERIFIER, "dev")
        assertEquals(1, again.restore(entries, hwm))
        // Rebooted: elapsed restarted. 4 days remain, whatever the wall clock says.
        val boot = GateScenarioTest.FakeClock(60_000)
        val after = Gatekeeper(RULES, boot, VERIFIER, "dev")
        assertEquals(1, after.restore(entries, hwm))
        boot.elapsed += 4 * 86_400_000L - 120_000
        assertEquals(1, after.grantsSummary().size)
        boot.elapsed += 120_000
        assertEquals(0, after.grantsSummary().size)
        assertEquals(0, after.persist().first.size)
        // A grant whose remaining time wasn't saved expires at a reboot (fail closed).
        val lost = Gatekeeper(RULES, GateScenarioTest.FakeClock(5_000), VERIFIER, "dev")
        assertEquals(0, lost.restore(entries.map { PolicyEntry(it.record, it.proof, it.issued, null) }, hwm))
    }

    @Test fun aTamperedSavedGrantIsDroppedOnLoad() {
        val ask = gk.admit("tap", send, SCREENS.getValue("whatsapp_chat"))
        val m = gk.approvalMessage(ask.approvalId, grant = true)!!
        gk.approve(ask.approvalId, m, sign(m))
        val (entries, hwm) = gk.persist()
        val e = entries.single()
        val forged = PolicyEntry(e.record + ("binding" to "com.chase.sig.android"), e.proof, e.issued, e.remaining)
        assertEquals(0, Gatekeeper(RULES, clock, VERIFIER, "dev").restore(listOf(forged), hwm))
        val longer = PolicyEntry(e.record + ("duration_ms" to 30 * 86_400_000L), e.proof, e.issued, e.remaining)
        assertEquals(0, Gatekeeper(RULES, clock, VERIFIER, "dev").restore(listOf(longer), hwm))
    }

    @Test fun requestedWordsAreOneShotAndNeverMoney() {
        gk.noteInstruction("send hi to Mom and buy flowers")
        val screen = SCREENS.getValue("whatsapp_chat")
        assertEquals("requested", gk.admit("tap", send, screen).rule)
        clock.elapsed += 500
        assertEquals("ask", gk.admit("tap", buy, SCREENS.getValue("amazon_product")).decision)
    }

    @Test fun actRateLimitRefillsAtFivePerSecond() {
        val screen = SCREENS.getValue("amazon_product")
        repeat(15) { assertTrue(gk.admit("key", mapOf("name" to "BACK"), screen).allowed) }
        assertEquals("gate_rate_limited", gk.admit("key", mapOf("name" to "BACK"), screen).code)
        clock.elapsed += 200
        assertTrue(gk.admit("key", mapOf("name" to "BACK"), screen).allowed)
        assertEquals("gate_rate_limited", gk.admit("key", mapOf("name" to "BACK"), screen).code)
    }

    @Test fun theRunnerReportsAWrongExpectation() {
        val scenario = mapOf<String, Any?>("name" to "x", "steps" to listOf(mapOf(
            "call" to mapOf("method" to "tap", "params" to buy), "screen" to "amazon_product",
            "expect" to mapOf("decision" to "allow"))))
        assertEquals(1, GateScenarioTest.run(scenario).size)
    }

    @Test fun everyRuleRegexCompilesAndMethodsAreClassified() {
        val classified = RULES.meta + RULES.read + RULES.act
        for (m in listOf("screenshot", "ui_elements", "current_app", "launchable_packages", "app_labels", "launch_app",
            "tap", "long_press", "swipe", "type_text", "key", "open_url", "resolve_url", "wake_unlock", "device_state",
            "wait_idle", "stream_start", "stream_stop", "stream_keyframe")) assertTrue(m, m in classified)
        assertTrue(RULES.blocked("com.chase.sig.android"))
        assertTrue(RULES.blocked("com.x8bit.bitwarden"))
        assertFalse(RULES.blocked("com.amazon.mShop.android.shopping"))
        assertFalse(RULES.blocked("com.google.android.apps.photos"))
    }
}
