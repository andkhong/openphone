package dev.openmob.companion.gate

import dev.openmob.companion.KeepAwakePolicy
import dev.openmob.companion.StreamGate
import dev.openmob.companion.gate.GateScenarioTest.Companion.RULES
import dev.openmob.companion.gate.GateScenarioTest.Companion.VERIFIER
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The pure pieces of the Android glue around the engine (review B1, B2, B3). */
class AndroidGlueTest {
    /** A fake accessibility node. `gone`: getChild returns null for it (it vanished mid-read). */
    class F(val text: String = "", val b: IntArray, val kids: List<F> = emptyList(), val gone: Boolean = false,
            val clickable: Boolean = false)

    object Fake : TreeWalk.Adapter<F> {
        override fun bounds(n: F) = n.b
        override fun visible(n: F) = true
        override fun childCount(n: F) = n.kids.size
        override fun child(n: F, i: Int) = n.kids[i].takeIf { !it.gone }
        override fun leaf(n: F, children: List<GNode>, truncated: Boolean) =
            GNode(text = n.text, bounds = n.b, clickable = n.clickable, children = children, truncated = truncated)
    }

    private fun deep(levels: Int, leaf: F): F {
        var n = leaf
        repeat(levels) { n = F(b = intArrayOf(0, 0, 1080, 2400), kids = listOf(n)) }
        return n
    }

    private fun screenOf(root: GNode, pkg: String = "com.example.news") =
        GScreen(listOf(GWindow(1, "application", 1, pkg, "", intArrayOf(0, 0, 1080, 2400), root)))

    @Test fun theTargetIsReadPastTheBudgetAndDepth() {
        val button = F("Place order", intArrayOf(0, 1600, 1080, 1700), clickable = true)
        val noise = (0 until 3000).map { F("row $it", intArrayOf(0, it, 1080, it + 1)) }
        val root = F(b = intArrayOf(0, 0, 1080, 2400), kids = noise + deep(120, button))
        val tree = TreeWalk(Fake, listOf(540.0 to 1650.0)).read(root)!!
        val t = GateScreen.hitTest(RULES, screenOf(tree), 540.0, 1650.0)!!
        assertEquals("Place order", t.hit!!.text)
        assertTrue(walk(tree, limit = 100_000).size < 1700) // the context stays within the budget
        // Without the point, the same tree is cut at depth 60 and the button is never seen.
        val blind = TreeWalk(Fake, emptyList()).read(root)!!
        assertTrue(walk(blind, limit = 100_000).none { it.text == "Place order" })
    }

    @Test fun aBranchThatCantBeReadAsks() {
        val root = F(b = intArrayOf(0, 0, 1080, 2400), kids = listOf(
            F(b = intArrayOf(0, 1500, 1080, 1800), kids = listOf(F("Pay", intArrayOf(0, 1500, 1080, 1800), gone = true)))))
        val tree = TreeWalk(Fake, listOf(540.0 to 1650.0)).read(root)!!
        val gk = Gatekeeper(RULES, GateScenarioTest.FakeClock(), VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val adm = gk.admit("tap", mapOf("x" to 540L, "y" to 1650L), screenOf(tree))
        assertEquals("ask", adm.decision)
        assertEquals("risk:truncated", adm.rule)
        val capped = TreeWalk(Fake, listOf(540.0 to 1650.0), hardDepth = 10).read(deep(50, F("x", intArrayOf(0, 0, 1080, 2400))))!!
        assertTrue(walk(capped, limit = 1000).last().truncated)
    }

    @Test fun anUnreadableWindowIsRefusedAndBlackedOut() {
        val gk = Gatekeeper(RULES, GateScenarioTest.FakeClock(), VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val screen = GScreen(listOf(GWindow(3, "application", 3, "", "", intArrayOf(0, 0, 1080, 2400), null)))
        assertEquals("gate_screen_unstable", gk.admit("tap", mapOf("x" to 5L, "y" to 5L), screen).code)
        assertEquals("redact", gk.admit("screenshot", emptyMap(), screen).decision)
        assertEquals("gate_blocked_app", gk.admit("stream_start", emptyMap(), screen).code)
        assertEquals("builtin:unknown_window", gk.hidden(screen))
    }

    @Test fun theLiveViewNeverSendsAProtectedFrame() {
        val g = StreamGate<String>(holdMs = 250)
        assertEquals(listOf("sps"), g.offer(0, "sps", key = false, config = true, protectedNow = false))
        assertEquals(emptyList<String>(), g.offer(0, "k0", key = true, config = false, protectedNow = false))
        assertEquals(emptyList<String>(), g.offer(100, "p1", key = false, config = false, protectedNow = false))
        assertEquals(listOf("k0"), g.flush(260, false)) // held 250 ms, then sent
        // The bank app is noticed: p1 (encoded just before) is dropped with everything after.
        assertEquals(emptyList<String>(), g.flush(300, true))
        assertEquals(emptyList<String>(), g.offer(320, "bank", key = true, config = false, protectedNow = true))
        assertTrue(g.needKey)
        // Gone again: only from a fresh keyframe on.
        assertEquals(emptyList<String>(), g.offer(400, "p2", key = false, config = false, protectedNow = false))
        assertEquals(emptyList<String>(), g.offer(450, "k1", key = true, config = false, protectedNow = false))
        assertFalse(g.needKey)
        assertEquals(listOf("k1"), g.flush(700, false))
    }

    @Test fun keepAwakeOnlyWhileWorkingAndAtMostTenMinutesAfterTheOwner() {
        val p = KeepAwakePolicy(idleMs = 20_000, capMs = 600_000)
        assertFalse(p.hold(0))
        p.act(0)
        assertFalse(p.hold(1)) // no owner activity yet: a server alone can't hold the screen on
        p.owner(0)
        assertTrue(p.hold(1))
        assertFalse(p.hold(21_000)) // idle
        var t = 0L
        while (t < 700_000) { p.act(t); t += 5_000 } // a server polling actions every 5 s
        assertTrue(p.hold(595_000))
        assertFalse(p.hold(600_001)) // the cap: normal locking resumes
        p.owner(650_000)
        p.act(650_000)
        assertTrue(p.hold(650_001))
        p.stop()
        assertFalse(p.hold(650_002))
    }

    @Test fun onlyTheOwnersOwnCancelIsADenial() {
        val p = dev.openmob.companion.PromptOutcome
        assertEquals(dev.openmob.companion.PromptOutcome.Kind.DECLINED, p.classify(p.ERROR_USER_CANCELED))
        assertEquals(dev.openmob.companion.PromptOutcome.Kind.DECLINED, p.classify(p.ERROR_NEGATIVE_BUTTON))
        for (code in listOf(p.ERROR_CANCELED, p.ERROR_TIMEOUT, p.ERROR_LOCKOUT, p.ERROR_LOCKOUT_PERMANENT, 1, 2, 11, 12)) {
            assertEquals("code $code", dev.openmob.companion.PromptOutcome.Kind.INTERRUPTED, p.classify(code))
        }
        // An interrupted prompt that times out is "expired" (no answer), and doesn't count toward the suspension.
        val gk = Gatekeeper(RULES, GateScenarioTest.FakeClock(), VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val events = mutableListOf<String>()
        gk.onEvent = { _, state -> events.add(state) }
        repeat(3) {
            val ask = gk.admit("tap", mapOf("x" to 990L, "y" to 2250L), GateScenarioTest.SCREENS.getValue("whatsapp_chat"))
            gk.markShown(ask.approvalId)
            gk.cancel(ask.approvalId)
        }
        assertFalse(gk.suspended)
        assertEquals(listOf("pending", "expired", "pending", "expired", "pending", "expired"), events)
    }

    @Test fun noGestureLandsWhileAPromptIsUpOrJustClosed() {
        val clock = GateScenarioTest.FakeClock()
        val gk = Gatekeeper(RULES, clock, VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val shop = GateScenarioTest.SCREENS.getValue("amazon_product")
        val chat = GateScenarioTest.SCREENS.getValue("whatsapp_chat")
        val early = gk.admit("tap", mapOf("x" to 540L, "y" to 1560L), shop)
        clock.elapsed += 300
        val ask = gk.admit("tap", mapOf("x" to 990L, "y" to 2250L), chat)
        assertTrue(early.allowed)
        assertEquals("ask", ask.decision)
        assertEquals("builtin:prompt_cooldown", gk.reverify(early, shop).rule)
        gk.cancel(ask.approvalId)
        assertEquals("builtin:prompt_cooldown", gk.reverify(early, shop).rule)
        clock.elapsed += 1100
        assertTrue(gk.reverify(early, shop).allowed)
    }

    @Test fun permitsAreVoidedByAnyOtherAction() {
        val gk = Gatekeeper(RULES, GateScenarioTest.FakeClock(), VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val chat = GateScenarioTest.SCREENS.getValue("whatsapp_chat")
        val send = mapOf<String, Any?>("x" to 990L, "y" to 2250L)
        val ask = gk.admit("tap", send, chat)
        val m = gk.approvalMessage(ask.approvalId)!!
        assertTrue(ask.approvalId.startsWith("gk"))
        assertTrue(gk.approve(ask.approvalId, m, GateScenarioTest.tapSign(m)))
        assertTrue(gk.admit("key", mapOf("name" to "DELETE"), chat).allowed)
        assertEquals("ask", gk.admit("tap", send, chat).decision)
    }
}
