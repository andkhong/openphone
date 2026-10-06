package dev.openmob.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingTest {
    private val code = "a1b2c3d4" + "Zx9_-Qw8Er7Ty6Ui5Op4As" // 8-char id + 128-bit secret

    @Test
    fun accountLinkCarriesServerAndCode() {
        val link = Pairing.parse("openmob://connect?server=https%3A%2F%2Fopenmob.fly.dev%2F&code=$code")
        assertEquals(Pairing.Link.Account("https://openmob.fly.dev", code), link)
        val local = Pairing.parse("openmob://connect?server=http%3A%2F%2Fmac-mini.local%3A8765&code=$code")
        assertEquals(Pairing.Link.Account("http://mac-mini.local:8765", code), local)
    }

    @Test
    fun plainHttpToAPublicHostIsRefused() {
        assertEquals(Pairing.Link.Invalid, Pairing.parse("openmob://connect?server=http%3A%2F%2Fattacker.example&code=$code"))
        assertEquals(Pairing.Link.Invalid, Pairing.parse("openmob://connect?server=https%3A%2F%2Fu%3Ap%40x.example&code=$code"))
    }

    @Test
    fun tokenLinksAreRefusedAndJunkIsInvalid() {
        assertEquals(Pairing.Link.TokenRefused, Pairing.parse("openmob://connect?server=https%3A%2F%2Fx.example&t=secret"))
        assertEquals(Pairing.Link.Invalid, Pairing.parse("openmob://connect?server=https%3A%2F%2Fx.example"))
        assertEquals(Pairing.Link.Invalid, Pairing.parse("openmob://connect?server=https%3A%2F%2Fx.example&code=short"))
        assertEquals(Pairing.Link.Invalid, Pairing.parse("openmob://connect?server=https%3A%2F%2Fx.example&code=bad%20code%3Cscript%3E1234"))
        assertNull(Pairing.parse("https://openmob.fly.dev/pair?code=$code"))
        assertNull(Pairing.parse("openmob://other?code=$code"))
        assertNull(Pairing.parse(null))
    }

    @Test
    fun batteryGuideFollowsTheMaker() {
        assertEquals(Pairing.BatteryGuide.SAMSUNG, Pairing.batteryGuide("samsung"))
        assertEquals(Pairing.BatteryGuide.SAMSUNG, Pairing.batteryGuide(" Samsung "))
        assertEquals(Pairing.BatteryGuide.STANDARD, Pairing.batteryGuide("Google"))
        assertEquals(Pairing.BatteryGuide.STANDARD, Pairing.batteryGuide(null))
    }
}
