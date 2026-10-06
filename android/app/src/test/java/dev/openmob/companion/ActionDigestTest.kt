package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/** The app's digest must equal the server's (openmob/phone/approval_keys.py action_digest). */
class ActionDigestTest {
    @Test
    fun matchesTheServersDigest() {
        // Vectors computed with the server's action_digest.
        assertEquals(
            "c0005f3e100b901404d3b3e306768b977b6d0bc3e1b3d15a5fae573b60b45f9b",
            ActionDigest.of(mapOf(
                "tool" to "tap", "package" to "com.amazon.mShop.android.shopping", "text" to "Buy now",
                "resource_id" to "buy_now", "bounds" to "0,100,500,200",
            )),
        )
        assertEquals(
            "4de00d96d0739154fdb337a80718ac2826c8e55ee4209c5bdac0b9700e170922",
            ActionDigest.of(mapOf("tool" to "type_text", "text" to "Café ✓", "input" to "héllo|x")),
        )
    }

    @Test
    fun fieldsCantRunIntoEachOther() {
        assertNotEquals(
            ActionDigest.of(mapOf("text" to "a|1:b")),
            ActionDigest.of(mapOf("text" to "a", "desc" to "b")),
        )
    }

    @Test
    fun describesWhatWillHappen() {
        assertEquals(
            "Tap “Buy now” (id buy_now) in com.amazon",
            ActionDigest.describe(mapOf("tool" to "tap", "text" to "Buy now", "resource_id" to "buy_now",
                                        "package" to "com.amazon")),
        )
    }
}
