package dev.openmob.companion.gate

import dev.openmob.companion.gate.GateScenarioTest.Companion.RULES
import dev.openmob.companion.gate.GateScenarioTest.Companion.SCREENS
import dev.openmob.companion.gate.GateScenarioTest.Companion.VERIFIER
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** A tap is judged by every clickable or labeled node under the point, not only the hit path (2026-10-10). */
class HitTestTest {
    private val clock = GateScenarioTest.FakeClock()
    private val gk = Gatekeeper(RULES, clock, VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)

    private fun tap(screen: String, x: Long, y: Long, extra: Map<String, Any?> = emptyMap()): Admission {
        clock.elapsed += 2000
        return gk.admit("tap", mapOf<String, Any?>("x" to x, "y" to y) + extra, SCREENS.getValue(screen)).also {
            if (it.decision == "ask") gk.cancel(it.approvalId)
        }
    }

    @Test fun theHitPathAloneMissesTheButtonButEveryNodeUnderThePointCounts() {
        val screen = SCREENS.getValue("hit_buy_layered")
        val path = GateScreen.hitTest(RULES, screen, 540.0, 1720.0)!!
        assertFalse("the hit path ends in the later layer", path.labelTokens().contains("buy"))
        val under = GateScreen.targetsAt(screen.windows[0], 540.0, 1720.0)
        assertTrue(under.any { it.pressed?.text == "Buy now" })
        assertEquals("pay", tap("hit_buy_layered", 540, 1720).kind)
    }

    @Test fun everyRiskKindIsJudgedThroughLayers() {
        assertEquals("pay", tap("hit_place_order_layered", 540, 1660).kind)
        assertEquals("send", tap("hit_send_toolbar", 980, 2250).kind)
        assertEquals("settings", tap("hit_settings_layered", 540, 480).kind)
        assertEquals("delete", tap("hit_delete_layered", 900, 2200).kind)
        assertEquals("install", tap("hit_install_layered", 540, 860, mapOf("task" to "t", "task_text" to "weather")).kind)
        assertEquals("gate_blocked_app", tap("hit_overlay_over_bank", 540, 1750).code)
    }

    @Test fun ordinaryTapsOnLayeredScreensDontAsk() {
        for ((x, y) in listOf(540L to 1050L, 540L to 1560L, 540L to 340L)) assertTrue(tap("hit_benign_layered", x, y).allowed)
        assertTrue(tap("hit_send_toolbar", 400, 2250).allowed)
    }

    @Test fun mmiAsksOnlyWhenDialing() {
        assertTrue(gk.completesCode("**21*5550100#") && gk.completesCode("*#06#"))
        assertFalse(gk.completesCode("+1 900-555-0100#") || gk.completesCode("**21*5550100"))
        assertTrue(tap("dialer_google", 870, 1775).allowed) // # after a plain number
        assertEquals("call_code", tap("dialer_google_code", 870, 1775).kind)
    }
}
