"""Adversarial suite: a compromised server against a gatekeeper phone (docs/design/phone-gatekeeper.md, A1).

The server is the attacker. It speaks protocol v1 as raw JSON over a socket-like pipe to a *simulated
gatekeeper phone*: the phone side runs the gatekeeper's reference engine (tests/gatekeeper_ref.py) exactly where
the app runs it (ConnectionService.handleCall: admit first, then reverify right before the side effect). The
owner holding the phone is simulated too: they approve only what the phone itself worded, with their screen
lock, when a test says so.

Why a reference engine: the attacks need a phone to attack, and pytest can't run the APK. The policy the phone
enforces is the Kotlin engine, so every attack below is also a fixture (tests/fixtures/gatekeeper/
adversarial.json) that JUnit runs through the Kotlin engine (GateScenarioTest.kt): an attack that's stopped
here and not on the phone (or the other way round) fails one of the two suites.
"""

import json

import pytest
from gatekeeper_ref import Gatekeeper, Screen, TestVerifier, load_fixtures, load_screens, run_scenario

SCREENS = load_screens()
ATTACKS = load_fixtures("adversarial.json")


class SimulatedPhone:
    """The phone end of the socket: every message from the server goes through the gate."""

    def __init__(self, screen: str = "launcher"):
        self.gk = Gatekeeper()
        self.screen = Screen.of(SCREENS[screen])
        self.prompts: list[dict] = []  # what the owner was shown (only ever phone-worded)
        self.sent: list[dict] = []
        self.executed: list[str] = []

    def receive(self, raw: str) -> dict | None:
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "call":
            return self._call(message)
        # S3: a server-worded approval prompt behind the screen lock would be phishing; the phone ignores it.
        # `permissions` can't loosen anything either: the mode lives in the owner's signed policy.
        return None

    def _call(self, message: dict) -> dict:
        method, params = message["method"], message.get("params") or {}
        self.gk.clock.elapsed += 200
        adm = self.gk.admit(method, params, self.screen)
        reply = {"type": "result", "id": message["id"]}
        if adm.decision == "allow":
            adm = self.gk.reverify(adm, self.screen)
        if adm.decision == "allow":
            self.executed.append(method)
            return {**reply, "ok": True, "result": None}
        if adm.decision == "redact":
            self.executed.append(method)
            return {**reply, "ok": True, "result": [] if method == "ui_elements" else {"black": True}}
        if adm.decision == "ask":
            self.prompts.append({"approval_id": adm.approval_id, "wording": adm.wording})
            return {**reply, "ok": False, "code": "gate_needs_approval", "approval_id": adm.approval_id,
                    "error": adm.message, "gate": {"rule": adm.rule, "package": adm.package}}
        return {**reply, "ok": False, "code": adm.code, "error": adm.message,
                "gate": {"rule": adm.rule, "package": adm.package}}

    def owner_approves(self, approval_id: str) -> bool:
        if approval_id not in self.gk.pending:  # nothing on screen to approve
            return False
        message = self.gk.approval_message(approval_id)
        return self.gk.approve(approval_id, message, TestVerifier.sign(message))


class EvilServer:
    def __init__(self, phone: SimulatedPhone):
        self.phone = phone
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        return self.phone.receive(json.dumps({"type": "call", "id": f"c{self.n}", "method": method, "params": params}))

    def send(self, message: dict):
        return self.phone.receive(json.dumps(message))


@pytest.mark.parametrize("attack", ATTACKS, ids=[a["name"] for a in ATTACKS])
def test_attack_is_stopped(attack):
    """Every listed attack is refused or approval-gated on the phone (run through the same steps JUnit runs)."""
    assert run_scenario(attack, SCREENS) == []
    calls = [s for s in attack["steps"] if "call" in s]
    assert calls and all(s["expect"]["decision"] in ("refuse", "ask", "redact") for s in calls[-1:])


@pytest.mark.parametrize("attack", ATTACKS, ids=[a["name"] for a in ATTACKS])
def test_attack_over_the_socket(attack):
    """The same attacks as raw protocol messages: nothing the owner didn't approve executes, and every refusal
    carries a stable code."""
    phone = SimulatedPhone()
    server = EvilServer(phone)
    approved = False
    for s in attack["steps"]:
        if "call" in s:
            phone.screen = Screen.of(SCREENS[s["screen"]])
            reply = server.call(s["call"]["method"], **(s["call"].get("params") or {}))
            expect = s["expect"]
            if expect["decision"] == "refuse":
                assert reply["ok"] is False and reply["code"] == expect["code"], reply
            elif expect["decision"] == "ask":
                assert reply["ok"] is False and reply["code"] == "gate_needs_approval" and reply["approval_id"]
            elif expect["decision"] == "allow":
                assert reply["ok"] is True and approved or s["call"]["method"] in ("tap", "key"), reply
        elif "approve" in s and s.get("sig") == "valid" and s.get("binding") != "other" and phone.prompts:
            approved = phone.owner_approves(phone.prompts[-1]["approval_id"]) or approved
        elif "deny" in s and phone.prompts:
            phone.gk.deny(phone.prompts[-1]["approval_id"])
        elif "cancel" in s and phone.prompts:
            phone.gk.cancel(phone.prompts[-1]["approval_id"])
        elif "advance_ms" in s:
            phone.gk.clock.elapsed += s["advance_ms"]
        elif "policy" in s and s.get("sig") != "valid":
            # The server has no way to write the owner's policy: an unsigned or forged record is dropped.
            assert not phone.gk.add_policy(s["policy"], "gk1|policy|x", "forged")
        elif "policy" in s:
            from gatekeeper_ref import policy_canon
            message = f"gk1|policy|{policy_canon(s['policy'])}"
            assert phone.gk.add_policy(s["policy"], message, TestVerifier.sign(message))
    for p in phone.prompts:  # every prompt the owner saw was worded by the phone, from what it sees
        assert p["wording"].startswith(("Allow", "Fill", "Share")), p


def test_server_worded_approval_requests_are_ignored():
    phone = SimulatedPhone("amazon_product")
    server = EvilServer(phone)
    assert server.send({"type": "approval_request", "id": "a1", "task_id": "t", "kind": "action",
                        "question": "Confirm your screen lock to keep using OpenMob", "action": {}, "digest": ""}) is None
    assert phone.prompts == []


def test_a_server_claimed_mode_changes_nothing():
    phone = SimulatedPhone("amazon_product")
    server = EvilServer(phone)
    server.send({"type": "permissions", "ceiling": "never", "never_until": 9e12, "never_money": True})
    reply = server.call("tap", x=540, y=1720, mode="never", allow_money=True)
    assert reply["code"] == "gate_needs_approval"


def test_prompt_flood_is_capped():
    """Even a server that waits out each prompt gets at most 6 prompts per 10 minutes."""
    phone = SimulatedPhone("whatsapp_chat")
    server = EvilServer(phone)
    codes = []
    for _ in range(12):
        reply = server.call("tap", x=990, y=2250)
        codes.append(reply["code"])
        if reply.get("approval_id"):
            phone.gk.mark_shown(reply["approval_id"])  # the owner sees it…
            phone.gk.cancel(reply["approval_id"])  # …and ignores it
        phone.gk.clock.elapsed += 5000
    assert codes.count("gate_needs_approval") == 6
    assert codes[6:] == ["gate_rate_limited"] * 6


def test_replaying_an_approval_after_its_use():
    phone = SimulatedPhone("whatsapp_chat")
    server = EvilServer(phone)
    first = server.call("tap", x=990, y=2250)
    assert phone.owner_approves(first["approval_id"])
    assert server.call("tap", x=990, y=2250)["ok"] is True  # the one approved send
    again = server.call("tap", x=990, y=2250)  # replayed: a new prompt, not a second send
    assert again["code"] == "gate_needs_approval"
    assert phone.executed.count("tap") == 1
    assert not phone.owner_approves(first["approval_id"])  # the old approval id is spent


def test_unknown_messages_and_methods_execute_nothing():
    phone = SimulatedPhone()
    server = EvilServer(phone)
    assert server.send({"type": "gate_event", "approval_id": "x", "state": "approved"}) is None
    assert server.send({"type": "policy", "ceiling": "never"}) is None
    assert server.call("shell", cmd="pm grant")["code"] == "gate_unknown_method"
    assert phone.executed == []
