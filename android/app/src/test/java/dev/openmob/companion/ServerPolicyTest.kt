package dev.openmob.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerPolicyTest {
    @Test
    fun httpsIsAllowedAnywhere() {
        assertTrue(ServerPolicy.isAllowed("https://openmob.example.com"))
        assertTrue(ServerPolicy.isAllowed("https://203.0.113.9:8443/"))
    }

    @Test
    fun plainHttpOnlyForLocalAddresses() {
        for (ok in listOf(
            "http://localhost:8765", "http://127.0.0.1:8765", "http://10.0.0.5:8765", "http://172.16.4.2",
            "http://172.31.255.1", "http://192.168.1.20:8765", "http://169.254.10.1", "http://mac-mini.local:8765",
            "http://[::1]:8765", "http://[fe80::1]", "http://[fd12::3]",
        )) assertTrue(ok, ServerPolicy.isAllowed(ok))
        for (bad in listOf(
            "http://attacker.example", "http://8.8.8.8", "http://172.32.0.1", "http://192.169.1.1",
            "http://localhost.attacker.example", "http://[2001:db8::1]",
        )) assertFalse(bad, ServerPolicy.isAllowed(bad))
    }

    @Test
    fun otherSchemesAndJunkAreRefused() {
        for (bad in listOf("", "openmob.example.com", "ftp://x.example", "javascript:alert(1)", "https://",
            "https://user:pass@openmob.example.com", "http://127.0.0.1.attacker.example")) {
            assertFalse(bad, ServerPolicy.isAllowed(bad))
        }
    }
}
