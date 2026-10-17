package dev.openmob.companion

import dev.openmob.companion.gate.GateScenarioTest
import dev.openmob.companion.gate.ModeChange
import dev.openmob.companion.gate.ScrollDirs
import dev.openmob.companion.gate.SensitiveNumbers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone-side fixes of the 2026-10-17 batched security review (review P) that live outside the gate's shared
 *  scenarios: the shared fixtures (tagged review_2026_10_17) cover H1, M1's duration cap, M3, M4, L1 and L2 in both
 *  engines; these pin down the app code behind M1, M2, L3 and H1's scroll reading. */
class Review20261017Test {
    // --- H1: what counts as scrollable, from accessibility's scroll actions ------------------------------------------

    @Test fun h1OnlyANodeWithAScrollActionLeftCanScroll() {
        assertEquals(emptySet<String>(), ScrollDirs.of(false, setOf("forward", "down"), -1, -1, "android.widget.ScrollView"))
        assertEquals("a ScrollView with nothing to scroll", emptySet<String>(),
                     ScrollDirs.of(true, emptySet(), -1, -1, "android.widget.ScrollView"))
        assertEquals(setOf("down"), ScrollDirs.of(true, setOf("forward"), -1, -1, "android.widget.ScrollView"))
        assertEquals(setOf("up", "down"), ScrollDirs.of(true, setOf("up", "down"), -1, -1, "android.view.View"))
        // A vertical list (many rows, one column) and a sideways one (one row, many columns), by their collection.
        assertEquals(setOf("down", "up"), ScrollDirs.of(true, setOf("forward", "backward"), 40, 1, "androidx.recyclerview.widget.RecyclerView"))
        assertEquals(setOf("right"), ScrollDirs.of(true, setOf("forward"), 1, 12, "androidx.recyclerview.widget.RecyclerView"))
        // A pager without a collection: sideways by its class, so a vertical drag inside it is never a scroll.
        assertEquals(setOf("right", "left"), ScrollDirs.of(true, setOf("forward", "backward"), -1, -1, "androidx.viewpager.widget.ViewPager"))
    }

    // --- M1: the indicator can't fade while an admitted act runs, nor for 1.5 s after it ------------------------------

    @Test fun m1TheIndicatorStaysWhileAnActRunsAndForItsHoldAfter() {
        val p = IndicatorPolicy()
        p.actStart(0)
        p.ended(10) // the server says the task ended while a 60 s drag still runs
        assertTrue(p.agentActive(1_600))
        assertTrue(p.agentActive(59_999))
        assertEquals(1, p.inFlight)
        p.actDone(60_000)
        assertTrue("1.5 s after it completes", p.agentActive(61_499))
        assertFalse(p.agentActive(61_500))
    }

    @Test fun m1TwoActsInFlightBothCount() {
        val p = IndicatorPolicy()
        p.actStart(0); p.actStart(5)
        p.ended(6)
        p.actDone(100)
        assertTrue("the second act still runs", p.agentActive(20_000))
        p.actDone(30_000)
        assertTrue(p.agentActive(31_000))
        assertFalse(p.agentActive(31_500))
    }

    @Test fun m1AStopHidesItAtOnceAndAFinishingActDoesntBringItBack() {
        val p = IndicatorPolicy()
        p.actStart(0)
        p.stop()
        assertFalse(p.agentActive(1))
        p.actDone(500)
        assertFalse(p.agentActive(600))
        assertEquals(0, p.inFlight)
    }

    @Test fun m1WithoutAnEndTheUsualTenSecondHoldCountsFromCompletion() {
        val p = IndicatorPolicy()
        p.actStart(0)
        p.actDone(4_000)
        assertTrue(p.agentActive(13_999))
        assertFalse(p.agentActive(14_000 + 10_000))
    }

    // --- M2: a Faster mode prompt carries nothing else, and only the scope the owner tapped ------------------------

    private fun input(json: String) = "Server's question\n$json"
    private val session = input("{\"faster_mode\": \"session\", \"faster_idle_s\": 3600, \"faster_resume_s\": 300, \"faster_until\": 1900000000}")

    @Test fun m2AFasterModeChangeMixedWithAnythingElseIsDeclined() {
        val tapped = "session" to 1_000L
        assertNull(ModeChange.fasterRefusal(session, tapped, 2_000))
        for (extra in listOf("\"never\": \"24h\"", "\"allow_money\": true", "\"everyday\": true", "\"ceiling\": \"trusted\"",
                             "\"trusted\": [{\"app\": \"com.whatsapp\", \"kinds\": [\"send\"]}]",
                             "\"permission_prompts\": {\"location\": \"allow\"}",
                             "\"faster_drive\": {\"key\": \"k1\", \"on\": true}")) {
            val mixed = input("{\"faster_mode\": \"session\", \"faster_idle_s\": 3600, $extra}")
            assertEquals(extra, "mixed", ModeChange.fasterRefusal(mixed, tapped, 2_000))
        }
        assertEquals("mixed", ModeChange.fasterRefusal(input("{\"faster_drive\": {\"key\": \"k1\", \"on\": true}, \"never\": \"1h\"}"), null, 0))
        assertNull("a key's drive change alone (asked from the dashboard)",
                   ModeChange.fasterRefusal(input("{\"faster_drive\": {\"key\": \"k1\", \"name\": \"driver\", \"on\": true}}"), null, 0))
        assertNull("not a Faster change: not this check's business", ModeChange.fasterRefusal(input("{\"never\": \"24h\"}"), null, 0))
    }

    @Test fun m2OnlyTheScopeTheOwnerJustTappedIsShown() {
        assertEquals("not_requested", ModeChange.fasterRefusal(session, null, 2_000))
        assertEquals("not_requested", ModeChange.fasterRefusal(session, "phone" to 1_000L, 2_000))
        assertEquals("not_requested", ModeChange.fasterRefusal(input("{\"faster_mode\": \"phone\"}"), "session" to 1_000L, 2_000))
        assertEquals("too late", "not_requested", ModeChange.fasterRefusal(session, "session" to 1_000L, 1_000 + ModeChange.REQUEST_MS + 1))
        assertNull(ModeChange.fasterRefusal(input("{\"faster_mode\": \"phone\"}"), "phone" to 1_000L, 2_000))
    }

    @Test fun m2TheScreenLockSheetShowsTheScopeAndTheWholeFirstSentence() {
        for ((change, scope) in listOf(session to "Turn on Faster mode (this session)",
                                       input("{\"faster_mode\": \"phone\"}") to "Turn on Faster mode (always on this phone)")) {
            val text = ModeChange.lockText(change)
            assertTrue(text, text.startsWith(scope))
            assertTrue(text, text.endsWith(ModeChange.FASTER_FIRST))
            assertEquals("short enough that PromptText leaves it whole (the PIN field stays reachable)", text, PromptText.short(text))
            assertTrue(text.length <= PromptText.MAX)
        }
        val drive = ModeChange.lockText(input("{\"faster_drive\": {\"key\": \"k1\", \"name\": \"a very long key name for a robot\", \"on\": true}}"))
        assertTrue(drive, drive.endsWith(ModeChange.FASTER_FIRST) && drive.length <= PromptText.MAX)
        // Any other change: each part's question, as many as fit, then how many more the screen above lists.
        val many = ModeChange.lockText(input("{\"never\": \"24h\", \"everyday\": true, \"ceiling\": \"trusted\", \"permission_prompts\": {\"location\": \"allow\"}}"))
        assertTrue(many, many.startsWith("Turn on Don't ask") && many.contains("more above") && many.length <= PromptText.MAX)
        assertEquals(many, PromptText.short(many))
    }

    // --- L3: the pill can't hop more than once every 2 s, nor more than 10 times a session ---------------------------

    private val rects = mapOf(
        PillCorner.TOP_RIGHT to intArrayOf(797, 149, 1049, 275), PillCorner.TOP_LEFT to intArrayOf(31, 149, 283, 275),
        PillCorner.BOTTOM_RIGHT to intArrayOf(797, 2125, 1049, 2251), PillCorner.BOTTOM_LEFT to intArrayOf(31, 2125, 283, 2251))

    private fun rectOf(c: PillCorner) = rects.getValue(c)

    @Test fun l3ThePillMovesAtMostOnceEveryTwoSecondsAndTenTimesASession() {
        val p = PillPlacement()
        val left = listOf(100.0 to 200.0)
        val right = listOf(980.0 to 200.0)
        assertEquals(PillCorner.TOP_LEFT, p.dodge(PillCorner.TOP_RIGHT, right, 21, ::rectOf, 0))
        assertNull("1 s later: it stays put (the gesture stays refused)", p.dodge(PillCorner.TOP_RIGHT, left, 21, ::rectOf, 1_000))
        var now = 2_000L
        var moves = 1
        while (moves < 10) {
            val points = if (p.corner(PillCorner.TOP_RIGHT) == PillCorner.TOP_LEFT) left else right
            assertTrue(p.dodge(PillCorner.TOP_RIGHT, points, 21, ::rectOf, now) != null)
            moves++
            now += 2_000
        }
        assertEquals(10, p.moves)
        val points = if (p.corner(PillCorner.TOP_RIGHT) == PillCorner.TOP_LEFT) left else right
        assertNull("the eleventh: it stays", p.dodge(PillCorner.TOP_RIGHT, points, 21, ::rectOf, now + 60_000))
        p.reset()
        assertEquals("a new session has a fresh budget", PillCorner.TOP_LEFT, p.dodge(PillCorner.TOP_RIGHT, right, 21, ::rectOf, now + 60_000))
    }

    // --- L1: the detector's new separators, digits and URL tricks (the shared fixture has the full list) -----------

    @Test fun l1TheReviewsBypassesAreCaught() {
        val label = GateScenarioTest.RULES.ssnLabel
        for (t in listOf("4111.1111.1111.1111", "4111/1111/1111/1111", "4111_1111_1111_1111", "4111  1111  1111  1111",
                         "٤١١١١١١١١١١١١١١١")) assertEquals(t, SensitiveNumbers.CARD, SensitiveNumbers.find(t))
        assertEquals(SensitiveNumbers.SSN, SensitiveNumbers.find("123.45.6789"))
        assertEquals(SensitiveNumbers.SSN, SensitiveNumbers.find("123 45 6789"))
        assertEquals(SensitiveNumbers.CARD, SensitiveNumbers.inUrl("https://evil.example/?a=4111111&b=111111111", label))
        assertEquals(SensitiveNumbers.CARD, SensitiveNumbers.inUrl(
            "https://x.example/?n=" + "4111111111111111".map { if (it == '4') "%25252534" else it.toString() }.joinToString(""), label))
        assertNull(SensitiveNumbers.inUrl("https://x.example/?page=12&ts=1760000000000", label))
    }
}
