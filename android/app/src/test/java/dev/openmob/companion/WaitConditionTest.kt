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

    @Test fun aDropdownSpinnerIsNotLoading() {
        assertFalse(WaitCondition.loading(el(cls = "android.widget.Spinner")))
        assertTrue(WaitCondition.loading(el(cls = "android.widget.ProgressBar")))
        assertTrue(WaitCondition.loading(el(cls = "com.app.LoadingSpinnerView")))
    }
}
