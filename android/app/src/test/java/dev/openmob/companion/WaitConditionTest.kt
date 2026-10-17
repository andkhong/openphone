package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** wait_for's conditions match like the server's openmob/agent/waiting.py (tests/test_wait_until.py). */
class WaitConditionTest {
    private fun el(text: String = "", cls: String = "TextView", rid: String = "", enabled: Boolean = true,
                   covered: Boolean = false) = WaitElement(text, "", rid, cls, null, enabled, covered)

    private fun cond(until: String, pattern: String = "", pkg: String = "") = WaitCondition(until, pattern, pkg, 500L)

    @Test fun wholeLabelsOrWholeWords() {
        val c = cond("text_appears", "Open|Play")
        assertTrue(c.matches("Open")); assertTrue(c.matches("Play now"))
        assertFalse(c.matches("Opening")); assertFalse(c.matches("Display"))
        assertFalse(c.matches("Google Play", exact = true))
        assertTrue(cond("text_appears", "(unbalanced").matches("an (unbalanced label")) // literal fallback
    }

    @Test fun textAppearsAndDisappears() {
        val screen = listOf(el("Cancel"), el("Open", enabled = false))
        assertNotNull(cond("text_appears", "Open").check("p", screen, null, 0))
        assertNull(cond("text_disappears", "Open").check("p", screen, null, 0))
        assertNotNull(cond("text_disappears", "Open").check("p", listOf(el("Uninstall")), null, 0))
        assertNull(cond("text_disappears", "Open").check("p", emptyList(), null, 0)) // nothing readable: can't tell
    }

    @Test fun elementEnabledWantsTheButtonUsable() {
        val c = cond("element_enabled", "Open|Play")
        assertNull(c.check("p", listOf(el("Open", enabled = false)), null, 0))
        assertNull(c.check("p", listOf(el("Open", covered = true)), null, 0))
        assertEquals("'Open' is enabled", c.check("p", listOf(el("Open")), null, 0))
    }

    @Test fun screenChangesIgnoresNumbersAndPositions() {
        val base = WaitCondition.signature("p", listOf(el("10:41")))
        val c = cond("screen_changes")
        assertNull(c.check("p", listOf(el("10:42")), base, 0))
        assertNotNull(c.check("p", listOf(el("10:42"), el("New message")), base, 0))
        assertNotNull(c.check("q", listOf(el("10:42")), base, 0))
    }

    @Test fun loadingDoneNeedsNoSpinnerAndQuiet() {
        val c = cond("loading_done")
        assertNull(c.check("p", listOf(el(cls = "ProgressBar"), el("Chess")), null, 2_000))
        assertNull(c.check("p", listOf(el("Loading…")), null, 2_000))
        assertNull(c.check("p", listOf(el("Play Bots")), null, 100)) // not quiet long enough
        assertNotNull(c.check("p", listOf(el("Play Bots")), null, 600))
    }

    @Test fun appInFront() {
        assertNotNull(cond("app_in_front", pkg = "com.chess").check("com.chess", emptyList(), null, 0))
        assertNull(cond("app_in_front", pkg = "com.chess").check("com.android.vending", emptyList(), null, 0))
    }

    @Test(expected = IllegalArgumentException::class) fun unknownConditionsAreRefused() {
        cond("tap_when_ready", "x")
    }

    @Test fun loadingDoneEndsOnALabeledScreenThatKeepsMoving() {
        // Maps keeps changing (tiles, pins), so it is never quiet: nothing loading for the grace time ends the wait.
        val c = cond("loading_done")
        val maps = listOf(el("Results"), el("Cafe de Casa"))
        assertNull(c.check("maps", maps, null, quietFor = 0L, unloadedFor = WaitCondition.MOVING_GRACE_MS - 1))
        assertEquals("nothing is loading (the screen keeps moving)",
            c.check("maps", maps, null, quietFor = 0L, unloadedFor = WaitCondition.MOVING_GRACE_MS))
        // Something still loading keeps waiting, however long.
        assertNull(c.check("maps", maps + el("Loading…"), null, quietFor = 0L, unloadedFor = 60_000L))
    }

    // --- the still exit (still_ms), in step with waiting.py's still exit (tests/test_game_waits.py) ---

    private fun sig(vararg labels: String) = WaitCondition.signature("com.n3twork.tetris", labels.map { el(it) })

    @Test fun aStillScreenEndsTheWaitAfterStillMs() {
        val still = StillClock(3_500L, start = 0L)
        for (t in 0L..3_400L step 200L) {
            still.update(t, sig("Game view"), loading = false)
            assertFalse("not yet at $t ms", still.still(t))
        }
        still.update(3_500L, sig("Game view"), loading = false)
        assertTrue(still.still(3_500L))
        assertEquals(3_500L, still.stillFor(3_500L))
    }

    @Test fun aChangeOrSomethingLoadingRestartsTheClock() {
        val still = StillClock(3_500L, start = 0L)
        still.update(0L, sig("Your move"), loading = false)
        still.update(2_000L, sig("Thinking…"), loading = false) // the labels changed: moving
        assertFalse(still.still(5_000L))
        assertTrue(still.still(5_500L))
        val loading = StillClock(3_500L, start = 0L)
        loading.update(0L, sig("Installing…"), loading = true)
        loading.update(4_000L, sig("Installing…"), loading = true)
        assertFalse(loading.still(4_000L)) // something loading never counts as still
        // A clock's digits are blanked in the signature, so a ticking clock is still.
        val clock = StillClock(3_500L, start = 0L)
        clock.update(0L, sig("10:41"), loading = false)
        clock.update(1_000L, sig("10:42"), loading = false)
        assertTrue(clock.still(3_500L))
    }

    @Test fun noStillMsMeansNoStillExit() {
        val off = StillClock(0L, start = 0L)
        off.update(0L, sig("Game view"), loading = false)
        assertFalse(off.still(60_000L))
        assertEquals(0L, cond("screen_changes").stillMs) // older servers send no still_ms: off
        assertEquals(3_500L, WaitCondition("screen_changes", "", "", 500L, 3_500L).stillMs)
    }

    @Test fun aDropdownSpinnerIsNotLoading() {
        assertFalse(WaitCondition.loading(el(cls = "android.widget.Spinner")))
        assertTrue(WaitCondition.loading(el(cls = "android.widget.ProgressBar")))
        assertTrue(WaitCondition.loading(el(cls = "com.app.LoadingSpinnerView")))
    }
}
