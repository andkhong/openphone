package dev.openmob.companion.gate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The sensitive-number detector against the shared fixture (tests/fixtures/gatekeeper/sensitive_numbers.json), which
 *  the server's detector (openmob/agent/sensitive.py) and the Python reference engine also pass. */
class SensitiveNumbersTest {
    private val fixture = GateScenarioTest.fixture("sensitive_numbers.json").obj()
    private val ssnLabel = GateScenarioTest.RULES.ssnLabel

    @Test fun texts() {
        val cases = fixture["texts"].list().map { it.obj() }
        assertTrue(cases.size > 30)
        val wrong = cases.mapNotNull { c ->
            val got = SensitiveNumbers.find(c.str("text"), c.bool("ssn_context"), c.bool("card_field"))
            if (got != c["expect"]) "${c["text"]} (ssn ${c["ssn_context"]}, card ${c["card_field"]}): expected ${c["expect"]}, got $got" else null
        }
        assertEquals(wrong.joinToString("\n"), 0, wrong.size)
    }

    @Test fun urls() {
        val cases = fixture["urls"].list().map { it.obj() }
        assertTrue(cases.size > 15)
        val wrong = cases.mapNotNull { c ->
            val got = SensitiveNumbers.inUrl(c.str("url"), ssnLabel)
            if (got != c["expect"]) "${c["url"]}: expected ${c["expect"]}, got $got" else null
        }
        assertEquals(wrong.joinToString("\n"), 0, wrong.size)
    }

    @Test fun luhn() {
        assertTrue(SensitiveNumbers.luhn("4111111111111111"))
        assertTrue(!SensitiveNumbers.luhn("4111111111111112"))
    }
}
