"""Settings deep links on a gatekeeper phone (docs/design/deep-links.md §2.3): the phone judges `open_settings` like
opening that Settings page, through the Python reference engine (tests/gatekeeper_ref.py; the Kotlin engine runs the
same shared scenarios), and a refusal reaches the agent with its code."""

from conftest import RecordingIO, ScriptedClient, tool_use
from gatekeeper_ref import Gatekeeper, Screen, load_screens
from test_deep_links import DONE, SETTINGS, Phone, opened, tool_result

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone import settings_pages as sp
from openmob.phone.apps import AppResolver
from openmob.phone.base import GateRefused
from openmob.phone.remote import GATED

LAUNCHER = Screen.of(load_screens()["launcher"])


def admit(gk, page, package=None, mode=None):
    params = {"page": page}
    if package:
        params["package"] = package
    if mode:
        params["mode"] = mode
    return gk.admit("open_settings", params, LAUNCHER)


def test_the_reference_engine_judges_a_deep_link_like_the_page():
    gk = Gatekeeper()
    for page in [p.key for p in sp.PAGES.values() if p.tier == sp.EVERYDAY and not p.needs_package]:
        adm = admit(gk, page)
        assert adm.decision == "allow" and gk.reverify(adm, LAUNCHER).decision == "allow", page
        gk.clock.elapsed += 300
    assert admit(gk, "app_notifications", "com.android.chrome").decision == "allow"
    ask = admit(gk, "date_time")
    assert (ask.decision, ask.kind, ask.level, ask.wording) == ("ask", "settings", "fingerprint",
                                                                  "Allow opening the Date & time Settings page?")
    assert ask.grant_offer is None
    gk.cancel(ask.approval_id)
    assert admit(gk, "app_details", "com.chase.sig.android").code == "gate_blocked_app"
    assert admit(gk, "app_notifications", "dev.openmob.companion").code == "gate_blocked_app"
    assert admit(gk, "app_details").rule == "builtin:settings_package"
    assert admit(gk, "accessibility").code == "gate_denied_page"
    assert admit(gk, "android.settings.ACCESSIBILITY_SETTINGS").code == "gate_denied_page"
    assert "open_settings" in GATED  # the server sends the task's mode, task id and hint with it


def test_a_key_for_a_hard_denied_page_would_be_refused_in_every_mode():
    extra = {"accessibility": sp.SettingsPage("accessibility", "android.settings.ACCESSIBILITY_SETTINGS", "Accessibility"),
             "language": sp.SettingsPage("language", "android.settings.LOCALE_SETTINGS", "Languages"),
             "lock": sp.SettingsPage("lock", "android.settings.LOCK_STUFF", "Screen lock")}
    gk = Gatekeeper(pages=lambda key: extra.get(key) or sp.PAGES.get(key))
    for key in extra:
        assert admit(gk, key).rule == "builtin:settings_security", key
        assert admit(gk, key, mode="never").rule == "builtin:settings_security", key
    assert admit(gk, "display").decision == "allow"


def test_dont_ask_refuses_the_ask_page_and_ask_mode_asks_for_every_page():
    gk = Gatekeeper()
    record = {"type": "never", "duration_ms": 3600000}
    from gatekeeper_ref import policy_canon
    message = "gk1|policy|" + policy_canon(record)
    assert gk.add_policy(record, message, gk.verifier.sign(message))
    assert admit(gk, "display", mode="never").decision == "allow"
    assert admit(gk, "date_time", mode="never").rule == "never:settings_page"
    step = admit(gk, "display", mode="ask")
    assert (step.decision, step.kind, step.wording) == ("ask", "step", "Allow opening the Display Settings page?")


class GatePhone(Phone):
    """A gatekeeper phone whose gate refuses every open_settings call with a stable code."""

    def __init__(self, refusal, **kw):
        super().__init__(**kw)
        self.refusal = refusal

    def open_settings(self, page, package=None):
        raise self.refusal


def test_a_refusal_on_the_phone_reaches_the_model_with_its_code(tmp_path):
    phone = GatePhone(GateRefused("gate_denied_page", "This Settings page is security-critical; agents can't open it.",
                                  "builtin:settings_security"))
    resolver = AppResolver(phone, {"settings": SETTINGS}, label_cache=tmp_path / "l.json")
    io = RecordingIO()
    agent = Agent(ScriptedClient([[tool_use("open_settings_page", "a", page="display")], DONE]), phone, resolver,
                  Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    agent.run("open the display settings")
    assert opened(phone) == []
    text = tool_result(agent.client)
    assert text.startswith("Refused on the phone (gate_denied_page): This Settings page is security-critical")
    assert "Don't retry this" in text
