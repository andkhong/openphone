package dev.openmob.companion

import dev.openmob.companion.gate.ModeChange
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Faster mode on the phone: worded here from the signed change, and shown on only as the owner approved it. */
class FasterModeTest {
    private fun input(json: String) = "Server says anything\n$json"

    @Test fun thePhoneWordsTheWarningItself() {
        val text = ModeChange.describe(input("{\"faster_mode\": \"phone\"}"))
        assertTrue(text.startsWith("Turn on Faster mode on this phone?"))
        assertTrue(text.contains("sends screenshots of what the agent is looking at to TypeSafe"))
        assertTrue(text.contains("Banking, wallet and password apps, and screens the phone blacks out, are never sent."))
        assertTrue(ModeChange.describe(input("{\"faster_mode\": \"session\"}")).startsWith("Turn on Faster mode for this session?"))
    }

    @Test fun onlyAnOnChangeHasAScope() {
        assertEquals("phone", ModeChange.fasterScope(input("{\"faster_mode\": \"phone\"}")))
        assertEquals("session", ModeChange.fasterScope(input("{\"faster_mode\": \"session\"}")))
        assertNull(ModeChange.fasterScope(input("{\"faster_mode\": \"off\"}")))
        assertNull(ModeChange.fasterScope(input("{\"never\": \"1h\"}")))
        assertNull(ModeChange.fasterScope("no json"))
    }

    @Test fun itIsNotGatekeeperPolicy() {
        // Faster mode changes nothing the gate enforces: no records, so it can't loosen a rule.
        assertTrue(ModeChange.records(input("{\"faster_mode\": \"phone\"}")).isEmpty())
    }

    @Test fun aServerClaimingMoreThanTheOwnerApprovedIsTurnedOff() {
        assertTrue(FasterMode.decide("phone", null))
        assertTrue(FasterMode.decide("session", null))
        assertTrue(FasterMode.decide("phone", "session")) // a session approval never becomes "always"
        assertFalse(FasterMode.decide("phone", "phone"))
        assertFalse(FasterMode.decide("session", "session"))
        assertFalse(FasterMode.decide("off", null))
    }

    @Test fun theWarningMatchesTheServers() {
        // openmob/permissions.py FASTER_WARNING: the same words on both sides.
        assertEquals(
            "Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second AI service, so it " +
                "can recognize screens faster. Banking, wallet and password apps, and screens the phone blacks out, " +
                "are never sent. You can turn this off at any time.",
            ModeChange.FASTER_WARNING,
        )
    }
}
