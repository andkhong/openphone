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
}
