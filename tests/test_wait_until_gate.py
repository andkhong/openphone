"""wait_for under the phone's gatekeeper: a read (never an act), blacked out like ui_elements while a blocked
app is visible, so a wait can't leak a blocked app's text (tests/gatekeeper_ref.py mirrors Gatekeeper.kt)."""

import json
import threading

from gatekeeper_ref import RULES_PATH, Gatekeeper, Screen, load_screens
from test_wait_until import STORE, FakeLink, TimedPhone, el

from openmob.agent import waiting

SCREENS = load_screens()


def test_wait_for_is_a_read_not_an_act():
    methods = json.loads(RULES_PATH.read_text())["methods"]
    assert "wait_for" in methods["read"]
    assert "wait_for" not in methods["act"] and "wait_for" not in methods["meta"]


def test_wait_for_is_allowed_on_an_ordinary_screen_and_blacked_out_over_a_blocked_app():
    gk = Gatekeeper()
    plain = Screen.of(SCREENS["launcher"])
    assert gk.admit("wait_for", {"condition": {"until": "loading_done"}, "timeout_ms": 1000}, plain).decision == "allow"
    bank = Screen.of(SCREENS["chase"])
    assert gk.admit("ui_elements", {}, bank).decision == "redact"  # the read it stands for
    assert gk.admit("wait_for", {"condition": {"until": "text_disappears", "pattern": "Balance"}, "timeout_ms": 1000},
                    bank).decision == "redact"


class RedactingLink(FakeLink):
    """A gatekeeper phone with a protected app on screen: ui_elements is [] and wait_for is withheld."""

    def call(self, method, params):
        self.calls.append(method)
        if method == "wait_for":
            return {"met": False, "redacted": True, "waited_ms": 0, "seen": "", "package": None}
        if method == "ui_elements":
            return []
        return super().call(method, params)


def test_a_withheld_wait_never_reports_a_verdict():
    from openmob.phone.remote import RemoteDriver

    phone = TimedPhone([(0, STORE, [el("Balance")])])
    driver = RemoteDriver(RedactingLink(phone))
    driver.capabilities = {"wait_idle", "wait_for"}
    for until in ("text_disappears", "text_appears", "element_enabled"):
        cond = waiting.parse({"until": until, "target": "Balance", "timeout_s": 1})
        driver.screenshot = lambda *a, **k: TimedPhone([(0, STORE, [])]).screenshot()  # a black-out stand-in
        result = waiting.Waiter(driver, threading.Event()).run(cond)
        assert not result.met and "Balance" not in result.seen  # (the condition itself is the model's own words)
