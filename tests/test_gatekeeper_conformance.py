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
    classified = set(rules["meta"]) | set(rules["read"]) | set(rules["act"]) | set(rules["task"])
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


def test_permission_defaults_tighten_freely_and_loosen_only_signed():
    gk = Gatekeeper()
    assert (gk.perm_setting("camera"), gk.perm_setting("notifications"), gk.perm_setting("location_accuracy")) == \
        ("once", "allow", "approximate")
    assert not gk.tighten_permission("camera", "while_using")
    assert gk.tighten_permission("camera", "deny") and gk.perm_setting("camera") == "deny"
    assert not gk.tighten_permission("camera", "always")


def test_no_gesture_lands_while_a_prompt_is_up_or_just_closed():
    """B1: an action admitted just before a prompt appeared can't land on its Allow."""
    from gatekeeper_ref import Screen

    gk = Gatekeeper()
    shop, chat = Screen.of(SCREENS["amazon_product"]), Screen.of(SCREENS["whatsapp_chat"])
    early = gk.admit("tap", {"x": 540, "y": 1560}, shop)  # Add to cart: allowed
    gk.clock.elapsed += 300
    ask = gk.admit("tap", {"x": 990, "y": 2250}, chat)  # a prompt opens
    assert early.decision == "allow" and ask.decision == "ask"
    assert gk.reverify(early, shop).rule == "builtin:prompt_cooldown"
    gk.cancel(ask.approval_id)
    assert gk.reverify(early, shop).rule == "builtin:prompt_cooldown"  # just closed
    gk.clock.elapsed += 1100
    assert gk.reverify(early, shop).decision == "allow"


def test_the_scenarios_cover_every_area():
    tags = {t for s in SCENARIOS for t in s["tags"]}
    assert {"friction", "permits", "grants", "clock", "modes", "hard_deny", "settings", "risk", "limits",
            "toctou"} <= tags
    assert Gatekeeper().mode("never") == "auto"  # an unsigned claim gets the ceiling


def test_the_pill_dodges_only_for_a_gesture_the_gate_would_accept():
    """GateHost moves the Stop pill only when the gesture is acceptable without it (Gatekeeper.admitsWithoutPill)."""
    from gatekeeper_ref import Screen
    screens = load_screens()
    gk = Gatekeeper()
    shop, bank = Screen.of(screens["indicator_over_shop"]), Screen.of(screens["indicator_over_bank"])
    assert gk.admit("tap", {"x": 980, "y": 200}, shop).rule == "builtin:own_indicator"
    assert gk.admits_without_pill("tap", {"x": 980, "y": 200}, shop)  # the shop's Menu under the pill
    assert not gk.admits_without_pill("tap", {"x": 980, "y": 200}, bank)  # a bank under it: refused anyway
    assert not gk.admits_without_pill("type_text", {"text": "x"}, shop)
