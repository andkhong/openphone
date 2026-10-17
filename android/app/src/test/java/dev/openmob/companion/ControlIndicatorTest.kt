package dev.openmob.companion

import dev.openmob.companion.gate.GNode
import dev.openmob.companion.gate.GOcrLine
import dev.openmob.companion.gate.GScreen
import dev.openmob.companion.gate.GWindow
import dev.openmob.companion.gate.GateScenarioTest
import dev.openmob.companion.gate.GateScreen
import dev.openmob.companion.gate.Gatekeeper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The agent indicator (docs/design/control-indicator.md): when it shows, the owner's settings, its colours, and
 *  how the gatekeeper treats its windows. */
class ControlIndicatorTest {
    private class MapStore : IndicatorStore {
        val m = HashMap<String, Any>()
        override fun bool(key: String, default: Boolean) = m[key] as? Boolean ?: default
        override fun str(key: String, default: String) = m[key] as? String ?: default
        override fun put(key: String, value: Boolean) { m[key] = value }
        override fun put(key: String, value: String) { m[key] = value }
    }

    // --- show and hide ---------------------------------------------------------------------------------------------

    @Test fun hiddenUntilAnAdmittedAction() {
        val p = IndicatorPolicy()
        assertEquals(IndicatorState.HIDDEN, p.state(1_000))
        p.call(1_000) // reads and waits never start it
        assertEquals(IndicatorState.HIDDEN, p.state(1_001))
        assertNull(p.recheckIn(1_001))
        p.act(2_000)
        assertEquals(IndicatorState.WORKING, p.state(2_000))
    }

    @Test fun staysUpBetweenStepsAndFadesWhenTheSessionGoesQuiet() {
        val p = IndicatorPolicy(actHoldMs = 10_000, callHoldMs = 10_000, sessionMaxMs = 60_000)
        p.act(0)
        assertEquals(IndicatorState.WORKING, p.state(9_999))
        assertEquals(IndicatorState.HIDDEN, p.state(10_000))
        p.call(9_000) // a long wait (screenshots, wait_for) keeps it up
        assertEquals(IndicatorState.WORKING, p.state(18_000))
        assertEquals(IndicatorState.HIDDEN, p.state(19_000))
        for (t in 19_000L..70_000L step 5_000) p.call(t) // only reads: not longer than sessionMax after the last act
        assertEquals(IndicatorState.HIDDEN, p.state(61_000))
    }

    @Test fun theServerSayingEndedOnlyShortensTheHoldAndTheNextActShowsItAgain() {
        val p = IndicatorPolicy(endedHoldMs = 1_500)
        p.act(0)
        p.ended(200) // result/error, or the controller line cleared
        assertEquals(IndicatorState.WORKING, p.state(1_400))
        assertEquals(IndicatorState.HIDDEN, p.state(1_500))
        assertEquals(1_300L, p.recheckIn(200))
        // A compromised server says "ended" and acts anyway: the act shows it again.
        p.ended(5_000)
        p.act(5_001)
        assertEquals(IndicatorState.WORKING, p.state(5_001))
        assertEquals(IndicatorState.WORKING, p.state(14_000))
        // ...even in the same millisecond
        p.ended(20_000); p.act(20_000)
        assertEquals(IndicatorState.WORKING, p.state(25_000))
    }

    @Test fun approvalWinsAndPausedIsDashedOnlyWhileASessionIsActive() {
        val p = IndicatorPolicy()
        p.prompt = true // a task prompt can come before any action
        assertEquals(IndicatorState.APPROVAL, p.state(0))
        p.prompt = false
        p.paused = true
        assertEquals(IndicatorState.HIDDEN, p.state(0)) // locked, but no agent was acting
        p.act(100)
        assertEquals(IndicatorState.PAUSED, p.state(200))
        assertEquals(IndicatorState.PAUSED, p.state(120_000)) // waits for the owner past the act hold
        p.prompt = true
        assertEquals(IndicatorState.APPROVAL, p.state(120_000))
    }

    @Test fun stopTurnsEverythingOffAtOnce() {
        val p = IndicatorPolicy()
        p.act(0); p.prompt = true; p.paused = true
        p.stop()
        assertEquals(IndicatorState.HIDDEN, p.state(1))
        assertFalse(p.agentActive(1))
        assertNull(p.recheckIn(1))
    }

    @Test fun previewShowsWorkingForItsTimeAndIsNotAnAgent() {
        val p = IndicatorPolicy()
        p.preview(0, 4_000)
        assertEquals(IndicatorState.WORKING, p.state(3_999))
        assertFalse(p.agentActive(1))
        assertEquals(IndicatorState.HIDDEN, p.state(4_000))
        p.preview(5_000, 4_000); p.endPreview()
        assertEquals(IndicatorState.HIDDEN, p.state(5_001))
    }

    // --- settings ----------------------------------------------------------------------------------------------------

    @Test fun settingsDefaultsAndPersistence() {
        val store = MapStore()
        val a = IndicatorPrefs(store).snapshot()
        assertTrue(a.border && a.pulse && a.pill)
        assertEquals("blue", a.color)
        assertEquals(PillCorner.TOP_RIGHT, a.corner)

        val p = IndicatorPrefs(store)
        p.border = false; p.pulse = false; p.pill = false; p.corner = PillCorner.BOTTOM_LEFT; p.color = "purple"
        val b = IndicatorPrefs(store).snapshot() // a fresh reader over the same storage (an app restart)
        assertFalse(b.border || b.pulse || b.pill)
        assertTrue(b.off)
        assertEquals(PillCorner.BOTTOM_LEFT, b.corner)
        assertEquals("purple", b.color)

        p.color = "#00b8d4"
        assertEquals("#00B8D4", IndicatorPrefs(store).color)
        assertEquals(0xFF00B8D4.toInt(), IndicatorPrefs(store).snapshot().argb)
        p.color = "javascript:alert(1)" // ignored
        p.color = "#12345"
        assertEquals("#00B8D4", IndicatorPrefs(store).color)
        store.m["color"] = "garbage"; store.m["pill_corner"] = "middle"
        assertEquals("blue", IndicatorPrefs(store).color)
        assertEquals(PillCorner.TOP_RIGHT, IndicatorPrefs(store).corner)
    }

    @Test fun thePulseRespectsTheSettingAndRemoveAnimations() {
        val on = IndicatorPrefs.Snapshot(true, "blue", true, true, PillCorner.TOP_RIGHT)
        assertTrue(IndicatorPrefs.pulseRuns(on, animatorsEnabled = true, state = IndicatorState.WORKING))
        assertFalse(IndicatorPrefs.pulseRuns(on, animatorsEnabled = false, state = IndicatorState.WORKING))
        assertFalse(IndicatorPrefs.pulseRuns(on, true, IndicatorState.APPROVAL))
        assertFalse(IndicatorPrefs.pulseRuns(on, true, IndicatorState.PAUSED))
        assertFalse(IndicatorPrefs.pulseRuns(IndicatorPrefs.Snapshot(true, "blue", false, true, PillCorner.TOP_RIGHT), true, IndicatorState.WORKING))
    }

    // --- colours -----------------------------------------------------------------------------------------------------

    @Test fun sixPresetsBlueFirstAndEveryOneVisibleOnLightAndDarkContent() {
        assertEquals(6, IndicatorColors.PRESETS.size)
        assertEquals("blue", IndicatorColors.PRESETS.first().key)
        for (p in IndicatorColors.PRESETS) {
            assertTrue("${p.key}: ${IndicatorColors.worstContrast(p.argb)}", IndicatorColors.worstContrast(p.argb) >= 3.0)
            assertFalse("${p.key} must stay apart from the approval amber", IndicatorColors.nearApproval(p.argb))
        }
        assertTrue(IndicatorColors.outline(0xFFFFFFFF.toInt()) != null) // white gets a dark hairline
        assertTrue(IndicatorColors.nearApproval(0xFFFFC107.toInt())) // a custom amber: drawn outlined
        assertTrue(IndicatorColors.worstContrast(0xFFFFFF00.toInt()) >= 3.0) // a custom yellow still shows
    }

    // --- the gatekeeper and its windows ------------------------------------------------------------------------------

    private val own = "dev.openmob.companion"
    private val full = intArrayOf(0, 0, 1080, 2400)
    private val pillBounds = intArrayOf(900, 140, 1060, 272)
    private fun app() = GWindow(1, "application", 1, "com.example.shop", "", full,
        GNode(cls = "android.widget.FrameLayout", bounds = full, children = listOf(
            GNode(text = "Search", id = "com.example.shop:id/search", bounds = intArrayOf(0, 1000, 1080, 1100), clickable = true),
            GNode(text = "Menu", id = "com.example.shop:id/menu", bounds = intArrayOf(940, 150, 1060, 260), clickable = true))))
    // As the phone's own tree reads (GateScreens.capture turns it into GateScreen.ownOverlayView).
    private fun border() = GWindow(20, "accessibility_overlay", 30, own, "OpenMob indicator", full, GNode(cls = "android.view.View", bounds = full))
    private fun pill() = GWindow(21, "accessibility_overlay", 31, own, "OpenMob Stop", pillBounds,
        GNode(text = "■ Stop", desc = "Stop the agent", cls = "android.widget.TextView", bounds = pillBounds, clickable = true))
    private fun screen() = GScreen(listOf(app(), GateScreen.ownOverlayView(border()), GateScreen.ownOverlayView(pill())),
        labels = mapOf("com.example.shop" to "Shop"))

    @Test fun ownOverlayViewKeepsOnlyWhetherItCatchesTouches() {
        val p = GateScreen.ownOverlayView(pill())
        assertTrue(GateScreen.catchesTouches(p))
        assertEquals("", p.root!!.text + p.root!!.desc) // "Stop" never counts as screen words
        assertFalse(GateScreen.catchesTouches(GateScreen.ownOverlayView(border())))
    }

    @Test fun theGateLooksThroughTheBorderButRefusesThePill() {
        val rules = GateScenarioTest.RULES
        val s = screen()
        assertEquals("com.example.shop", GateScreen.hitTest(rules, s, 540.0, 1050.0)!!.window.pkg)
        assertEquals("com.example.shop", GateScreen.hitTest(rules, s, 2.0, 1050.0)!!.window.pkg) // the very edge
        assertEquals(21, GateScreen.hitTest(rules, s, 980.0, 200.0)!!.window.id)

        val clock = GateScenarioTest.FakeClock()
        val gk = Gatekeeper(rules, clock, GateScenarioTest.VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        fun tap(x: Long, y: Long) = gk.admit("tap", mapOf("x" to x, "y" to y), s).also { clock.elapsed += 2000 }
        assertTrue(tap(540, 1050).allowed)
        assertTrue(tap(3, 1050).allowed)
        val refused = tap(980, 200)
        assertEquals("refuse", refused.decision)
        assertEquals("builtin:own_indicator", refused.rule)
        assertEquals("allow", gk.admit("screenshot", emptyMap(), s).decision) // not an unknown window: no black frame
        assertEquals("", gk.hidden(s))
        assertNull(gk.ocrCandidate(s)) // and no permission-dialog OCR because of it
    }

    @Test fun ocrDropsThePillsText() {
        val lines = listOf(GOcrLine("■ Stop", intArrayOf(920, 180, 1040, 230)), GOcrLine("Allow Shop to send you notifications?", intArrayOf(100, 1000, 980, 1060)))
        assertEquals(listOf("Allow Shop to send you notifications?"), IndicatorGeometry.dropOwn(lines, listOf(pillBounds)).map { it.text })
        assertEquals(2, IndicatorGeometry.dropOwn(lines, emptyList()).size)
    }

    // --- the pill dodges (owner decision 2026-10-17) --------------------------------------------------------------------

    private val insets = intArrayOf(0, 128, 0, 63) // status bar, navigation bar (the emulator's, px)
    private fun rectOf(c: PillCorner) = IndicatorGeometry.pillRect(c, 252, 126, 1080, 2400, insets, 31, 21)

    @Test fun pillRectsMatchTheLayoutAndStayOnScreen() {
        assertEquals(listOf(797, 149, 1049, 275), rectOf(PillCorner.TOP_RIGHT).toList()) // as measured on gk_b7
        for (c in PillCorner.values()) {
            val r = rectOf(c)
            assertTrue(c.name, r[0] >= 0 && r[1] >= 0 && r[2] <= 1080 && r[3] <= 2400 && r[2] - r[0] == 252)
        }
        assertEquals(listOf(PillCorner.TOP_LEFT, PillCorner.BOTTOM_RIGHT, PillCorner.BOTTOM_LEFT), IndicatorGeometry.order(PillCorner.TOP_RIGHT))
        assertEquals(listOf(PillCorner.BOTTOM_RIGHT, PillCorner.TOP_LEFT, PillCorner.TOP_RIGHT), IndicatorGeometry.order(PillCorner.BOTTOM_LEFT))
    }

    @Test fun thePillDodgesToTheOtherCornerOfTheSameEdge() {
        val p = PillPlacement()
        assertNull(p.dodge(PillCorner.TOP_RIGHT, listOf(540.0 to 1200.0), 21, ::rectOf)) // not in the way: stays
        assertEquals(PillCorner.TOP_RIGHT, p.corner(PillCorner.TOP_RIGHT))
        assertEquals(PillCorner.TOP_LEFT, p.dodge(PillCorner.TOP_RIGHT, listOf(980.0 to 200.0), 21, ::rectOf))
        assertEquals(PillCorner.TOP_LEFT, p.corner(PillCorner.TOP_RIGHT))
        // within the margin counts as in the way
        assertEquals(PillCorner.TOP_LEFT, PillPlacement().dodge(PillCorner.TOP_RIGHT, listOf(785.0 to 200.0), 21, ::rectOf))
    }

    @Test fun aRepeatDodgeMovesOnAndASwipeAcrossTheTopGoesToTheBottom() {
        val p = PillPlacement()
        p.dodge(PillCorner.TOP_RIGHT, listOf(980.0 to 200.0), 21, ::rectOf)
        // the agent then needs something under the top-left position: back to the top right (free now)
        assertEquals(PillCorner.TOP_RIGHT, p.dodge(PillCorner.TOP_RIGHT, listOf(100.0 to 200.0), 21, ::rectOf))
        // a swipe along the whole top edge covers both top corners: a bottom corner
        val q = PillPlacement()
        assertEquals(PillCorner.BOTTOM_RIGHT, q.dodge(PillCorner.TOP_RIGHT, listOf(980.0 to 200.0, 100.0 to 200.0), 21, ::rectOf))
    }

    @Test fun everyCornerCoveredKeepsThePillWhereItIs() {
        val p = PillPlacement()
        val all = listOf(980.0 to 200.0, 100.0 to 200.0, 980.0 to 2250.0, 100.0 to 2250.0)
        assertNull(p.dodge(PillCorner.TOP_RIGHT, all, 21, ::rectOf))
        assertEquals(PillCorner.TOP_RIGHT, p.corner(PillCorner.TOP_RIGHT))
        assertNull(p.dodged)
    }

    @Test fun thePillReturnsToTheOwnersCornerForTheNextTask() {
        val p = PillPlacement()
        p.dodge(PillCorner.TOP_RIGHT, listOf(980.0 to 200.0), 21, ::rectOf)
        p.reset() // the indicator went (ControlIndicator.apply: HIDDEN)
        assertEquals(PillCorner.TOP_RIGHT, p.corner(PillCorner.TOP_RIGHT))
        assertEquals(PillCorner.BOTTOM_LEFT, p.corner(PillCorner.BOTTOM_LEFT)) // the owner's choice, whatever it is
    }

    private fun screenWithPillAt(r: IntArray) = GScreen(listOf(app(), GateScreen.ownOverlayView(border()),
        GateScreen.ownOverlayView(GWindow(21, "accessibility_overlay", 31, own, "OpenMob Stop", r,
            GNode(cls = "android.widget.TextView", bounds = r, clickable = true)))), labels = mapOf("com.example.shop" to "Shop"))

    @Test fun theGateDodgesOnlyForAGestureItWouldAcceptAndTheMovedPillIsStillRefused() {
        val clock = GateScenarioTest.FakeClock()
        val gk = Gatekeeper(GateScenarioTest.RULES, clock, GateScenarioTest.VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val before = screenWithPillAt(rectOf(PillCorner.TOP_RIGHT))
        val menu = mapOf<String, Any?>("x" to 1000L, "y" to 200L)
        assertEquals("builtin:own_indicator", gk.admit("tap", menu, before).rule)
        assertTrue(gk.admitsWithoutPill("tap", menu, before)) // the shop's Menu under it: fine to move for
        assertFalse(gk.admitsWithoutPill("type_text", mapOf("text" to "x"), before))
        clock.elapsed += 2000
        val after = screenWithPillAt(rectOf(PillCorner.TOP_LEFT))
        assertTrue(gk.admit("tap", menu, after).allowed) // judged on the real Menu now
        clock.elapsed += 2000
        val onMoved = gk.admit("tap", mapOf("x" to 150L, "y" to 200L), after)
        assertEquals("builtin:own_indicator", onMoved.rule) // the pill in its new corner is still refused
        // A bank under the pill: refused without it too, so the pill doesn't move for it.
        val bank = GScreen(listOf(GWindow(1, "application", 1, "com.chase.sig.android", "", full,
            GNode(cls = "android.widget.FrameLayout", bounds = full, children = listOf(
                GNode(text = "Transfer", bounds = intArrayOf(900, 150, 1060, 260), clickable = true)))),
            GateScreen.ownOverlayView(pill())))
        assertFalse(gk.admitsWithoutPill("tap", menu, bank))
    }

    // --- the pill stops the task and stays connected (owner decision 2026-10-17) ---------------------------------------

    private class Recorder : PillStop.Effects {
        val calls = mutableListOf<String>()
        var connected = true
        override fun withdrawPrompts() { calls += "withdraw" }
        override fun holdActs() { calls += "hold" }
        override fun sendTaskStop(): Boolean { calls += "task_stop"; return connected }
        override fun hideIndicator() { calls += "hide" }
        override fun note(sent: Boolean) { calls += "note:$sent" }
    }

    @Test fun thePillSendsATaskStopAndNeverDisconnects() {
        val r = Recorder()
        assertTrue(PillStop.run(r))
        // Prompts are withdrawn and acts held before the server hears it; nothing closes the connection.
        assertEquals(listOf("withdraw", "hold", "task_stop", "hide", "note:true"), r.calls)
        assertFalse(r.calls.any { "disconnect" in it || "close" in it })
        val offline = Recorder().apply { connected = false }
        assertFalse(PillStop.run(offline))
        assertTrue("acts are held even when the stop can't be sent", "hold" in offline.calls)
    }

    @Test fun actsAfterThePillAreRefusedUntilTheTaskEndsOrTheTimeout() {
        val hold = OwnerStopHold(holdMs = 10_000)
        assertFalse(hold.blocks(0))
        hold.stop(1_000)
        assertTrue(hold.blocks(1_000))
        assertTrue(hold.blocks(10_999)) // a server that ignores the stop: still refused
        assertFalse(hold.blocks(11_000)) // the timeout
        hold.stop(20_000)
        hold.ended() // the task's result/error event arrived (or the owner asked for something new)
        assertFalse(hold.blocks(20_001))
    }

    @Test fun aNewTaskWorksAfterThePill() {
        // GateHost refuses acts while the hold blocks; the gate itself is untouched by the stop.
        val hold = OwnerStopHold()
        val clock = GateScenarioTest.FakeClock()
        val gk = Gatekeeper(GateScenarioTest.RULES, clock, GateScenarioTest.VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val s = screen()
        hold.stop(0)
        assertTrue(hold.blocks(5))
        hold.ended() // the stopped task's final event
        assertFalse(hold.blocks(6))
        assertTrue(gk.admit("tap", mapOf("x" to 540L, "y" to 1050L), s).allowed) // the next task acts as usual
    }

    @Test fun thePillsStopDropsTheTasksPermitsButNotTheOwnersPolicy() {
        val clock = GateScenarioTest.FakeClock()
        val gk = Gatekeeper(GateScenarioTest.RULES, clock, GateScenarioTest.VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)
        val chat = GateScenarioTest.SCREENS.getValue("whatsapp_chat")
        val send = mapOf<String, Any?>("x" to 990L, "y" to 2250L)
        val ask = gk.admit("tap", send, chat)
        val m = gk.approvalMessage(ask.approvalId)!!
        assertTrue(gk.approve(ask.approvalId, m, GateScenarioTest.tapSign(m))) // the owner approved this send
        gk.stopTasks() // then tapped Stop on the pill
        clock.elapsed += 2000
        assertEquals("ask", gk.admit("tap", send, chat).decision) // the stopped task's permit is gone
        gk.stopTasks() // and an open prompt closes
        assertFalse(gk.promptOpen)
    }
}
