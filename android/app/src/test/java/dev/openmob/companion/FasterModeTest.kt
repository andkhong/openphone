package dev.openmob.companion

import dev.openmob.companion.gate.ModeChange
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Faster mode on the phone: worded here from the signed change, held as the owner's signed grant (with a session's
 *  limits inside it), and shown on only while that grant is good. */
class FasterModeTest {
    private fun input(json: String) = "Server says anything\n$json"
    private val t0 = 1_800_000_000L // the approval's signed time (s)
    private val ms = t0 * 1000

    private fun session(until: Long = t0 + 86_400, idle: Long = 3600, resume: Long = 300) = FasterMode.grantFrom(
        input("{\"faster_mode\": \"session\", \"faster_until\": $until, \"faster_idle_s\": $idle, \"faster_resume_s\": $resume}"),
        "a1", "mode_change:a1", "sig", t0)!!

    @Test fun thePhoneWordsTheWarningItself() {
        val text = ModeChange.describe(input("{\"faster_mode\": \"phone\"}"))
        assertTrue(text.startsWith("Turn on Faster mode on this phone?"))
        assertTrue(text.contains("sends screenshots of what the agent is looking at to TypeSafe"))
        assertTrue(text.contains("it can't recognize every sensitive image"))
        assertTrue(ModeChange.describe(input("{\"faster_mode\": \"session\"}")).startsWith("Turn on Faster mode for this session"))
    }

    @Test fun onlyAnOnChangeHasAScope() {
        assertEquals("phone", ModeChange.fasterScope(input("{\"faster_mode\": \"phone\"}")))
        assertEquals("session", ModeChange.fasterScope(input("{\"faster_mode\": \"session\"}")))
        assertNull(ModeChange.fasterScope(input("{\"faster_mode\": \"off\"}")))
        assertNull(ModeChange.fasterScope(input("{\"never\": \"1h\"}")))
        assertNull(ModeChange.fasterScope("no json"))
    }

    @Test fun itIsNotGatekeeperPolicy() {
        assertTrue(ModeChange.records(input("{\"faster_mode\": \"phone\"}")).isEmpty())
        assertTrue(ModeChange.records(input("{\"faster_drive\": {\"key\": \"k1\", \"name\": \"bot\", \"on\": true}}")).isEmpty())
    }

    @Test fun aKeysDriveSessionsAreWordedPerKey() {
        val text = ModeChange.describe(input("{\"faster_drive\": {\"key\": \"k1\", \"name\": \"bot\", \"on\": true}}"))
        assertTrue(text.startsWith("Let the API key “bot” use Faster mode in its drive sessions on this phone?"))
        assertEquals("k1" to "bot", ModeChange.fasterDriveKey(input("{\"faster_drive\": {\"key\": \"k1\", \"name\": \"bot\", \"on\": true}}")))
        assertNull(ModeChange.fasterDriveKey(input("{\"faster_drive\": {\"key\": \"k1\", \"on\": false}}")))
        assertNull(FasterMode.grantFrom(input("{\"faster_drive\": {\"key\": \"k1\", \"on\": true}}"), "a", "t", "s", t0))
    }

    @Test fun aSessionsLimitsComeFromTheSignedChangeAndAreClamped() {
        val g = session()
        assertEquals("session", g.scope)
        assertEquals(ms + 86_400_000, g.untilMs)
        assertEquals(3_600_000, g.idleMs)
        assertEquals(300_000, g.resumeMs)
        val greedy = session(until = t0 + 10 * 86_400, idle = 99_999, resume = 99_999)
        assertEquals(ms + FasterMode.MAX_SESSION_MS, greedy.untilMs)
        assertEquals(FasterMode.MAX_IDLE_MS, greedy.idleMs)
        assertEquals(FasterMode.MAX_RESUME_MS, greedy.resumeMs)
    }

    @Test fun aBlipKeepsItAndALongAbsenceOrIdleEndsIt() {
        val g = session()
        val now = ms + 600_000
        assertTrue(g.active(now, lastActivity = now - 1000, disconnectedAt = 0))
        assertTrue(g.active(now, now - 1000, disconnectedAt = now - 120_000)) // a 2 min blip
        assertFalse(g.active(now, now - 1000, disconnectedAt = now - 301_000)) // gone over 5 min
        assertFalse(g.active(now, lastActivity = now - 3_601_000, disconnectedAt = 0)) // an hour idle
        assertFalse(g.active(ms + 86_400_001, ms + 86_400_000, 0)) // past the signed expiry
    }

    @Test fun aServerClaimingMoreThanTheOwnersGrantIsTurnedOff() {
        val now = ms + 600_000
        val g = session()
        assertTrue(FasterMode.decide("phone", null, now, now))
        assertTrue(FasterMode.decide("session", null, now, now))
        assertTrue(FasterMode.decide("phone", g, now, now)) // a session approval never becomes "always"
        assertFalse(FasterMode.decide("session", g, now, now))
        assertFalse(FasterMode.decide("off", null, now, now))
        // After expiry (idle, past the signed time, or away too long) a server still claiming "on" is told off.
        assertTrue(FasterMode.decide("session", g, now, lastActivity = now - 3_700_000))
        assertTrue(FasterMode.decide("session", g, ms + 90_000_000, ms + 90_000_000))
        val phone = FasterMode.grantFrom(input("{\"faster_mode\": \"phone\"}"), "a2", "t", "sig", t0)
        assertNotNull(phone)
        assertFalse(FasterMode.decide("phone", phone, now, 0))
    }

    @Test fun theWarningMatchesTheServers() {
        // openmob/permissions.py FASTER_WARNING: the same words on both sides.
        assertEquals(
            "Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second AI service, so it " +
                "can recognize screens faster. Banking, wallet and password apps, and screens the phone blacks out, " +
                "are never sent. OpenMob blocks banking apps and detected card and ID numbers, but it can't " +
                "recognize every sensitive image. You can turn this off at any time.",
            ModeChange.FASTER_WARNING,
        )
    }
}
