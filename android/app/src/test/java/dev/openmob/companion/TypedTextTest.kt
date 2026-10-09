package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Test

class TypedTextTest {
    @Test
    fun replaceLeavesOnlyTheNewText() {
        assertEquals("chess games" to 11, TypedText.result("Spotify", 7 to 7, "chess games", replace = true))
    }

    @Test
    fun insertGoesAtTheCursorAndReplacesASelection() {
        assertEquals("Dear Sam, hi" to 12, TypedText.result("Dear Sam,", null, " hi", replace = false))
        assertEquals("Dear Ann," to 8, TypedText.result("Dear Sam,", 5 to 8, "Ann", replace = false))
    }
}
