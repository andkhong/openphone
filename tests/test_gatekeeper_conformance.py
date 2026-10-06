"""The gatekeeper's shared scenarios, run through the Python reference engine (tests/gatekeeper_ref.py).

The same fixture files run through the phone's Kotlin engine in JUnit (GateScenarioTest.kt): both must pass,
so the reference the adversarial suite relies on is the policy the phone enforces.
"""

import json
import subprocess
import sys

import pytest
from gatekeeper_ref import FIXTURES, RULES_PATH, Gatekeeper, load_fixtures, load_screens, run_scenario

from openmob.phone.protocol import METHODS

SCREENS = load_screens()
SCENARIOS = load_fixtures("scenarios.json")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scenario(scenario):
    assert run_scenario(scenario, SCREENS) == []


def test_every_protocol_method_has_a_gate_class():
    """D1 parity: a new method in protocol.py must be classified on the phone, or the phone refuses it."""
    rules = json.loads(RULES_PATH.read_text())["methods"]
    classified = set(rules["meta"]) | set(rules["read"]) | set(rules["act"])
    assert set(METHODS) <= classified, set(METHODS) - classified


def test_fixtures_match_build_py(tmp_path):
    out = subprocess.run([sys.executable, "-c", (
        "import importlib.util, json, sys;"
        f"spec = importlib.util.spec_from_file_location('b', {str(FIXTURES / 'build.py')!r});"
        "b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b);"
        "print(json.dumps([b.SCREENS, b.SCENARIOS, b.ADVERSARIAL]))")], capture_output=True, text=True, check=True)
    screens, scenarios, adversarial = json.loads(out.stdout)
    assert screens == load_screens() and scenarios == SCENARIOS and adversarial == load_fixtures("adversarial.json"), \
        "run tests/fixtures/gatekeeper/build.py and commit the JSON"


def test_every_refusal_code_is_documented_in_the_protocol():
    import re

    from openmob.phone.protocol import GATE_CODES

    kotlin = (FIXTURES.parents[2] / "android/app/src/main/java/dev/openmob/companion/gate/Gatekeeper.kt").read_text()
    used = set(re.findall(r'"((?:gate_[a-z_]+)|phone_locked)"', kotlin)) - {"gate_event"}
    assert used <= set(GATE_CODES), used - set(GATE_CODES)
    docs = (FIXTURES.parents[2] / "docs/phone-protocol.md").read_text()
    assert all(code in docs for code in GATE_CODES)


def test_fuzz_fixtures_match_the_reference_engine():
    """fuzz.json holds the reference engine's answers to random screens; JUnit checks the Kotlin engine gives the
    same ones (the differential test). Regenerated here, so a change to either engine shows up."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_fuzz", FIXTURES / "build_fuzz.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    screens, cases = module.build()
    saved = json.loads((FIXTURES / "fuzz.json").read_text())
    assert saved == json.loads(json.dumps({"screens": screens, "cases": cases})), \
        "run tests/fixtures/gatekeeper/build_fuzz.py and commit fuzz.json"


def test_gate_prompt_ids_are_namespaced():
    from gatekeeper_ref import Screen

    gk = Gatekeeper()
    adm = gk.admit("tap", {"x": 990, "y": 2250}, Screen.of(SCREENS["whatsapp_chat"]))
    assert adm.decision == "ask" and adm.approval_id.startswith("gk")


def test_the_scenarios_cover_every_area():
    tags = {t for s in SCENARIOS for t in s["tags"]}
    assert {"friction", "permits", "grants", "clock", "modes", "hard_deny", "settings", "risk", "limits",
            "toctou"} <= tags
    assert Gatekeeper().mode("never") == "auto"  # an unsigned claim gets the ceiling
