package dev.openmob.companion.gate

import dev.openmob.companion.gate.GateScenarioTest.Companion.RULES
import dev.openmob.companion.gate.GateScenarioTest.Companion.SCREENS
import dev.openmob.companion.gate.GateScenarioTest.Companion.VERIFIER
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The phone-side fixes of batch security review C (2026-10-09). The shared fixtures (scenarios tagged review_c)
 *  cover the decisions in both engines; these pin down the engine details behind them. */
class ReviewCFixesTest {
    private val clock = GateScenarioTest.FakeClock()
    private val gk = Gatekeeper(RULES, clock, VERIFIER, "dev", tapVerifier = GateScenarioTest.TAP_VERIFIER)

    private fun tap(x: Long, y: Long, screen: String, extra: Map<String, Any?> = emptyMap()): Admission {
        clock.elapsed += 1000
        return gk.admit("tap", mapOf<String, Any?>("x" to x, "y" to y) + extra, SCREENS.getValue(screen))
    }

    private fun dialog(screen: String): List<GNode> =
        gk.ocrPreview(SCREENS.getValue(screen)).windows.first { it.root?.cls == "OCRDialog" }.root!!.children

    @Test fun h1TheTitleIsTheOneAboveTheButtonsAndAppTextOutsideTheCardIsDropped() {
        tap(540, 300, "kalshi_app")
        val honest = dialog("ocr_camera")
        assertEquals("Allow Kalshi to take pictures and record video?", honest.first { it.id == "permission_message" }.text)
        assertFalse(honest.any { it.id == "ocr_unsure" })
        assertFalse("the app's own text behind the scrim is not part of the card", honest.any { it.text == "Markets" })
    }

    @Test fun h1TwoPermissionTitlesOnScreenMakeEveryGrantAskButDenyPasses() {
        tap(540, 300, "kalshi_app")
        assertTrue(dialog("ocr_camera_spoof").any { it.id == "ocr_unsure" })
        val a = tap(540, 1505, "ocr_camera_spoof")
        assertEquals("risk:permission:ocr_unsure", a.rule)
        assertEquals("tap", a.level)
        gk.cancel(a.approvalId)
        clock.elapsed += 2000
        assertTrue(tap(540, 1765, "ocr_camera_spoof").allowed) // Don't allow
    }

    @Test fun m3OnlyTheCardBetweenTitleAndButtonsIsAPreciseApproximateChoice() {
        tap(540, 300, "kalshi_app")
        val nodes = dialog("ocr_location_fake_approx")
        val approx = nodes.filter { it.text == "Approximate" }
        assertEquals("the app-drawn line is dropped, the card's stays", 1, approx.size)
        assertTrue(approx[0].clickable)
        assertTrue(approx[0].bounds[1] < 1400)
    }

    @Test fun m1ThePermitBindsTheQuestion() {
        tap(540, 300, "kalshi_app")
        val a = tap(540, 1505, "ocr_location_other")
        val b = tap(540, 1505, "ocr_contacts_other_swap")
        assertEquals("ask", a.decision)
        assertNotEquals("the same button under another question binds differently", a.binding, b.binding)
    }

    @Test fun m2TypingCarriesTheJudgedFieldAndNoFieldIsRefused() {
        val ok = gk.admit("type_text", mapOf("text" to "hi"), SCREENS.getValue("form_a"))
        assertTrue(ok.allowed)
        assertEquals("1|40,400,1040,500|com.example.app:id/name|android.widget.EditText", ok.field)
        assertEquals(ok.field, GateScreen.fieldRef(1, intArrayOf(40, 400, 1040, 500), "com.example.app:id/name", "android.widget.EditText"))
        val moved = gk.reverify(ok, SCREENS.getValue("form_b"))
        assertEquals("builtin:field_changed", moved.rule)
        for ((m, p) in listOf("type_text" to mapOf("text" to "hi"), "key" to mapOf("name" to "ENTER"), "key" to mapOf("name" to "DELETE"))) {
            assertEquals("builtin:no_field", gk.admit(m, p, SCREENS.getValue("kalshi_app")).rule)
        }
    }

    @Test fun h2CallsAndCodes() {
        assertTrue(gk.mmi("**21*5550100#") && gk.mmi("tel:%2321%23") && !gk.mmi("+1 900-555-0100"))
        assertTrue(gk.callApp("com.samsung.android.dialer") && !gk.callApp("com.whatsapp"))
        val call = tap(540, 2075, "dialer_google")
        assertEquals("call", call.kind)
        assertTrue(call.wording, "+1 900-555-0100" in call.wording)
        assertEquals("tap", gk.level(Risk("call", "call:dial")))
        assertEquals("fingerprint", gk.level(Risk("call_code", "call:mmi")))
    }

    @Test fun m4DeviceCareIsASettingsPackage() {
        for (p in listOf("com.samsung.android.lool", "com.samsung.android.sm", "com.samsung.android.sm_cn", "com.android.settings")) {
            assertTrue(p, RULES.settingsPkg.containsMatchIn(p))
        }
        assertFalse(RULES.settingsPkg.containsMatchIn("com.samsung.android.loolx"))
        assertFalse(RULES.settingsPkg.containsMatchIn("com.evil.settings"))
    }

    @Test fun m5NoTaskPromptWhilePaused() {
        repeat(3) { i ->
            val a = gk.admit("task_grant", mapOf("task" to "t$i", "text" to "share video $i"), GScreen(emptyList()))
            assertTrue(gk.deny(a.approvalId))
        }
        assertTrue(gk.suspended)
        assertEquals("builtin:suspended", gk.admit("task_grant", mapOf("task" to "t9", "text" to "x"), GScreen(emptyList())).rule)
        assertFalse(gk.promptOpen)
    }

    @Test fun installsAskUnlessTheTaskNamesTheApp() {
        val page = SCREENS.getValue("play_store_flashlight_get").windows[0]
        val get = walk(page.root).first { it.text == "Get" }
        assertEquals("rating, badge and count lines are skipped", "Torchlight: Flashlight Pro", gk.storeTitle(page, get))
        assertTrue(gk.namesApp("get the Torchlight flashlight", "Torchlight: Flashlight Pro"))
        assertFalse("generic words don't name an app", gk.namesApp("install a pro app", "Torchlight: Flashlight Pro"))
        assertFalse("whole words only", gk.namesApp("chessboard", "Chess - Play and Learn"))
        assertTrue(gk.namesApp("install chess and play 5 moves", "Chess - Play and Learn"))
        val unnamed = tap(540, 860, "play_store_chess", mapOf("task" to "t1", "task_text" to "install a flashlight"))
        assertEquals("install", unnamed.kind)
        assertEquals("tap", unnamed.level)
        gk.cancel(unnamed.approvalId)
        clock.elapsed += 2000
        // Named by the server's task text alone, it asks once, as the task (review L2, 2026-10-17)...
        val claimed = tap(540, 860, "play_store_chess", mapOf("task" to "t1", "task_text" to "install chess"))
        assertEquals("risk:task:install:unattested", claimed.rule)
        gk.cancel(claimed.approvalId)
        clock.elapsed += 2000
        // ...and runs unasked when the phone holds the request (typed here, or the task approved here).
        gk.noteInstruction("install chess")
        assertTrue(tap(540, 860, "play_store_chess", mapOf("task" to "t1", "task_text" to "install chess")).allowed)
    }

    @Test fun phase5OwnWindowWhileAPromptWaitsSaysWait() {
        assertEquals("builtin:own_window", tap(540, 1875, "own_prompt").rule)
        assertEquals("ask", tap(990, 2250, "whatsapp_chat").decision)
        val a = tap(540, 1875, "own_prompt")
        assertEquals("gate_prompt_open", a.code)
    }
}
