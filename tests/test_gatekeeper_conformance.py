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


def test_the_scenarios_cover_every_area():
    tags = {t for s in SCENARIOS for t in s["tags"]}
    assert {"friction", "permits", "grants", "clock", "modes", "hard_deny", "settings", "risk", "limits",
            "toctou"} <= tags
    assert Gatekeeper().mode("never") == "auto"  # an unsigned claim gets the ceiling
