package dev.openmob.companion.gate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The shared gatekeeper fixtures (tests/fixtures/gatekeeper: scenarios.json and the adversarial suite's
 * adversarial.json) run through the phone's own engine. pytest runs the same files through the Python
 * reference (tests/test_gatekeeper_conformance.py, tests/test_gatekeeper_adversarial.py), and a differential fuzz fixture compares the two on random screens.
 */
class GateScenarioTest {
    class FakeClock(var elapsed: Long = 10_000) : MonoClock { override fun elapsed() = elapsed }

    companion object {
        fun sign(message: String) = GateText.sha256("test-key:$message")
        val VERIFIER = GateVerifier { m, s -> s.isNotEmpty() && s == sign(m) }
        fun tapSign(message: String) = GateText.sha256("test-tap-key:$message")
        val TAP_VERIFIER = GateVerifier { m, s -> s.isNotEmpty() && s == tapSign(m) }
        val RULES: GateRules by lazy { GateRules.builtIn() }

        fun fixture(name: String): Any? = GateScenarioTest::class.java.getResourceAsStream("/$name")!!
            .bufferedReader(Charsets.UTF_8).use { MiniJson.parse(it.readText()) }

        val SCREENS: Map<String, GScreen> by lazy { fixture("screens.json").obj().mapValues { GScreen.of(it.value.obj()) } }

        fun newGate(clock: FakeClock) = Gatekeeper(RULES, clock, VERIFIER, "dev", tapVerifier = TAP_VERIFIER)

        /** Runs one scenario; returns the problems (empty: it passed). */
        fun run(scenario: Map<String, Any?>, screens: Map<String, GScreen> = SCREENS): List<String> {
            val clock = FakeClock()
            var gk = newGate(clock)
            val callMs = scenario.long("call_ms", 1000)
            var last = ""
            var saved: Pair<List<PolicyEntry>, Long> = gk.persist()
            val problems = mutableListOf<String>()
            for ((i, raw) in scenario["steps"].list().withIndex()) {
                val s = raw.obj()
                fun fail(msg: String) = problems.add("step $i: $msg")
                when {
                    "call" in s -> {
                        clock.elapsed += callMs
                        val call = s["call"].obj()
                        var adm = gk.admit(call.str("method"), call["params"].obj(), screens.getValue(s.str("screen")))
                        if (adm.decision == "ask") last = adm.approvalId
                        if (adm.allowed) adm = gk.reverify(adm, screens.getValue(s.str("reverify_screen", s.str("screen"))))
                        saved = gk.persist()
                        val e = s["expect"].obj()
                        for ((k, v) in listOf("decision" to adm.decision, "code" to adm.code, "kind" to adm.kind, "money" to adm.money, "level" to adm.level)) {
                            if (k in e && e[k] != v) fail("$k: expected ${e[k]}, got $v (${adm.rule})")
                        }
                        if ("rule" in e && !adm.rule.startsWith(e.str("rule"))) fail("rule: expected ${e["rule"]}, got ${adm.rule}")
                    }
                    "approve" in s -> {
                        var accepted = false
                        val p = gk.pending(last)
                        if (p != null) {
                            var m = gk.approvalMessage(last, s.bool("grant"))!!
                            if (s["binding"] == "other") m = m.replace(p.binding, GateText.sha256("another action"))
                            val tap = p.level == "tap"
                            val sig = when (s["sig"]) {
                                "valid" -> if (tap) tapSign(m) else sign(m)
                                "other_key" -> if (tap) sign(m) else tapSign(m)
                                else -> "00"
                            }
                            accepted = gk.approve(last, m, sig)
                        }
                        val want = s["expect_accepted"] as? Boolean ?: true
                        if (accepted != want) fail("approve: expected accepted=$want, got $accepted")
                    }
                    "deny" in s -> gk.deny(last)
                    "cancel" in s -> gk.cancel(last)
                    "shown" in s -> gk.markShown(last)
                    "advance_ms" in s -> { clock.elapsed += s.long("advance_ms"); saved = gk.persist() }
                    "set_wall_ms" in s -> Unit // the engine never reads the wall clock
                    "reboot" in s -> {
                        val r = s["reboot"].obj()
                        val entries = if (r.bool("saved")) saved.first
                        else saved.first.map { PolicyEntry(it.record, it.proof, it.issued, null) }
                        clock.elapsed = r.long("elapsed_after")
                        gk = newGate(clock)
                        gk.restore(entries, saved.second)
                    }
                    "policy" in s -> {
                        val record = s["policy"].obj()
                        val m = "gk1|policy|" + Gatekeeper.canon(record)
                        val sig = when (s["sig"]) { "valid" -> sign(m); "bad" -> sign(m + "x"); else -> "" }
                        val accepted = gk.addPolicy(record, PolicyProof(m, sig))
                        val want = s["expect_accepted"] as? Boolean ?: true
                        if (accepted != want) fail("policy: expected accepted=$want, got $accepted")
                    }
                    "instruction" in s -> gk.noteInstruction(s.str("instruction"))
                    "stop" in s -> gk.revokeAll()
                    "resume" in s -> gk.resume()
                    else -> fail("unknown step $s")
                }
            }
            return problems
        }
    }

    private fun runAll(file: String) {
        val failures = fixture(file).list().map { it.obj() }.mapNotNull { sc ->
            run(sc).takeIf { it.isNotEmpty() }?.let { "${sc["name"]}: $it" }
        }
        assertEquals(failures.joinToString("\n"), 0, failures.size)
    }

    @Test fun scenarios() = runAll("scenarios.json")

    @Test fun adversarialSuite() = runAll("adversarial.json")

    /** Differential: random screens and calls (tests/fixtures/gatekeeper/build_fuzz.py) must get exactly the Python
     *  reference engine's answers. */
    @Test fun fuzzMatchesTheReferenceEngine() {
        val fuzz = fixture("fuzz.json").obj()
        val screens = fuzz["screens"].obj().mapValues { GScreen.of(it.value.obj()) }
        val cases = fuzz["cases"].list().map { it.obj() }
        assertTrue(cases.size >= 300)
        val failures = cases.mapNotNull { sc -> run(sc, screens).takeIf { it.isNotEmpty() }?.let { "${sc["name"]}: $it" } }
        assertEquals(failures.take(20).joinToString("\n"), 0, failures.size)
    }

    @Test fun fixturesAreThere() {
        assertTrue(fixture("scenarios.json").list().size > 40)
        assertTrue(fixture("adversarial.json").list().size > 25)
    }
}
