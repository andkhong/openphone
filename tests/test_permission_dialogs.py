"""Android's runtime permission dialogs are answered by rule, with no model call (openmob/agent/permission_dialogs.py).

Fixtures: the Google (Pixel) permission controller, Samsung's One UI one, and the older AOSP package installer.
"""

import dataclasses

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent import permission_dialogs as pd
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.permissions import TaskPermissions
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

GOOGLE = "com.google.android.permissioncontroller"
SAMSUNG = "com.samsung.android.permissioncontroller"
AOSP_OLD = "com.android.packageinstaller"
MESSAGES = {
    "notifications": "Allow Chess.com to send you notifications?",
    "camera": "Allow Chess.com to take pictures and record video?",
    "microphone": "Allow Chess.com to record audio?",
    "location": "Allow Chess.com to access this device's location?",
    "contacts": "Allow Chess.com to access your contacts?",
    "photos": "Allow Chess.com to access photos and videos on this device?",
    "phone": "Allow Chess.com to make and manage phone calls?",
    "sms": "Allow Chess.com to send and view SMS messages?",
    "calendar": "Allow Chess.com to access your calendar?",
    "nearby_devices": "Allow Chess.com to find, connect to, and determine the relative position of nearby devices?",
}


def dialog_elements(message, roles=("foreground", "one_time", "deny"), prefix="com.android.permissioncontroller:id/"):
    ids = {"foreground": ("permission_allow_foreground_only_button", "While using the app"),
           "one_time": ("permission_allow_one_time_button", "Only this time"),
           "allow": ("permission_allow_button", "Allow"),
           "always": ("permission_allow_always_button", "Allow all the time"),
           "deny": ("permission_deny_button", "Don't allow")}
    out = [UIElement(0, message, "", prefix + "permission_message", "TextView", False, None, (40, 1250, 1040, 1400))]
    for i, role in enumerate(roles, 1):
        rid, text = ids[role]
        out.append(UIElement(i, text, "", prefix + rid, "Button", True, None, (40, 1400 + 150 * i, 1040, 1520 + 150 * i)))
    return out


@pytest.mark.parametrize("package", [GOOGLE, SAMSUNG, AOSP_OLD])
@pytest.mark.parametrize("kind,message", list(MESSAGES.items()))
def test_each_permission_type_is_recognized(package, kind, message):
    found = pd.detect(package, dialog_elements(message))
    assert found is not None and found.kind == kind and found.app == "Chess.com"


def test_not_a_dialog():
    assert pd.detect("com.chess", [UIElement(0, "Allow", "", "com.chess:id/ok", "Button", True, None, (0, 0, 9, 9))]) is None


def decide(d, mode="auto", app="Chess.com", **prompts):
    return pd.decide(d, mode=mode, prompts=prompts, task_app_label=app)


@pytest.mark.parametrize("kind", list(MESSAGES))
def test_the_default_per_type(kind):
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES[kind]))
    plan = decide(d)  # owner decision: the task's own app gets "Only this time" (notifications: Allow), no prompt
    assert plan.answer == "allow"
    if kind == "notifications":
        assert decide(d, notifications="deny").answer == "deny"
    else:
        assert pd.button(d, plan.answer, plan.grant).text == "Only this time"
        assert pd.question(d).startswith("Allow Chess.com to use your ")


@pytest.mark.parametrize("kind", [k for k in MESSAGES if k != "notifications"])
@pytest.mark.parametrize("setting,answer,tapped", [("deny", "deny", "Don't allow"), ("ask", "ask", None),
                                                   ("once", "allow", "Only this time"),
                                                   ("while_using", "allow", "While using the app")])
def test_each_setting(kind, setting, answer, tapped):
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES[kind]))
    plan = decide(d, **{kind: setting})
    assert plan.answer == answer
    if tapped:
        assert pd.button(d, plan.answer, plan.grant).text == tapped


@pytest.mark.parametrize("kind", list(MESSAGES))
def test_dont_ask_mode_denies_unless_once_or_while_using(kind):
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES[kind]))
    assert decide(d, mode="never").answer == "allow"  # the defaults grant without asking
    if kind != "notifications":
        assert decide(d, mode="never", **{kind: "ask"}).answer == "deny"
        assert decide(d, mode="never", **{kind: "once"}).answer == "allow"
        assert decide(d, mode="never", **{kind: "while_using"}).answer == "allow"


def test_another_apps_request_asks():
    d = pd.detect(GOOGLE, dialog_elements("Allow Spyware to send you notifications?"))
    assert decide(d, notifications="allow").answer == "ask"
    assert decide(d, notifications="deny").answer == "deny"  # deny never asks
    d = pd.detect(GOOGLE, dialog_elements("Allow Spyware to access your contacts?"))
    assert decide(d, contacts="while_using").answer == "ask"  # the owner's default is for the task's own app


def test_a_yes_taps_the_narrowest_grant():
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["camera"], roles=("always", "foreground", "one_time", "deny")))
    assert pd.button(d, "allow", "once", asked=True).text == "Only this time"
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["location"], roles=("always", "foreground", "deny")))
    assert pd.button(d, "allow", "once", asked=True).text == "While using the app"
    assert pd.button(d, "deny").text == "Don't allow"


def test_never_allow_all_the_time_unless_the_owner_says_so():
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["location"], roles=("always", "deny")))
    for setting in ("once", "while_using", "ask"):
        assert decide(d, location=setting).answer == "deny"  # the only grant is background location: never
        assert decide(d, mode="never", location=setting).answer == "deny"
    assert pd.button(d, "allow", "while_using") is None
    assert pd.button(d, "allow", "once", asked=True) is None  # not even after a yes


def location_elements(prefix="com.android.permissioncontroller:id/", precise_checked=True, ids=True):
    els = dialog_elements(MESSAGES["location"], prefix=prefix)
    n = len(els)
    els.append(UIElement(n, "Precise", "", prefix + "permission_location_accuracy_radio_fine" if ids else "",
                         "RadioButton", True, precise_checked, (40, 1100, 520, 1240)))
    els.append(UIElement(n + 1, "Approximate", "", prefix + "permission_location_accuracy_radio_coarse" if ids else "",
                         "RadioButton", True, not precise_checked, (560, 1100, 1040, 1240)))
    return els


@pytest.mark.parametrize("package,prefix,ids", [
    (GOOGLE, "com.android.permissioncontroller:id/", True),
    (SAMSUNG, "com.samsung.android.permissioncontroller:id/", True),
    (SAMSUNG, "com.samsung.android.permissioncontroller:id/", False)])  # One UI text-only cards
def test_approximate_is_chosen_unless_the_owner_says_precise(package, prefix, ids):
    d = pd.detect(package, location_elements(prefix, ids=ids))
    plan = decide(d, location="while_using")
    assert plan.accuracy == "approximate" and pd.accuracy_card(d, plan.accuracy).text == "Approximate"
    plan = decide(d, location="while_using", location_accuracy="precise")
    assert pd.accuracy_card(d, plan.accuracy) is None  # Precise is already selected
    d = pd.detect(package, location_elements(prefix, precise_checked=False, ids=ids))
    assert pd.accuracy_card(d, "approximate") is None and pd.accuracy_card(d, "precise").text == "Precise"


class DialogPhone(FakeDriver):
    """Chess.com in front with a permission dialog over it; a tap answers it and it goes away."""

    def __init__(self, message):
        super().__init__(packages=["com.chess"], labels={"com.chess": "Chess.com"})
        self.package = "com.chess"
        self.dialog = dialog_elements(message, prefix="com.android.permissioncontroller:id/")
        self.elements = list(self.dialog)

    def current_app(self):
        return GOOGLE if self.elements else "com.chess"

    def tap(self, x, y):
        super().tap(x, y)
        self.elements = []

    def later(self):
        """The dialog comes up only after the task has been in Chess.com (BACK shows it): the task's own app."""
        self.held, self.elements = self.elements, []
        return self

    def key(self, name):
        super().key(name)
        self.elements = getattr(self, "held", self.elements)


class StepIO(RecordingIO):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.steps = []

    def record_step(self, row):
        self.steps.append(row)


def run(tmp_path, message, answers=None, mode="auto", phone=None, in_app_first=False, **prompts):
    driver = (phone or DialogPhone)(message)
    # A dialog on the very first screen has no task app yet: it counts as another app's (it asks). in_app_first: the
    # task is in Chess.com first (one model step), then the dialog comes up.
    client = ScriptedClient(([[tool_use("press_key", "k", key="BACK")]] if in_app_first else [])
                            + [[tool_use("done", "d", summary="ok", success=True)]])
    if in_app_first:
        driver.later()
    io = StepIO(answers)
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    perms = TaskPermissions(mode, lambda: {"ceiling": "trusted", "default_mode": mode,
                                           "permission_prompts": prompts,
                                           **({"never_until": 9e12} if mode == "never" else {})})
    agent.run("play chess", permissions=perms)
    return driver, client, io








class GatekeeperDialogPhone(DialogPhone):
    """A gatekeeper phone: the server doesn't prompt; its hint makes the phone ask with the dialog's own words."""
    gatekeeper = True

    def __init__(self, message):
        super().__init__(message)
        self.hints = []

    def gate_hint(self, text):
        self.hints.append(text)






def test_a_preselected_precise_without_an_approximate_card_asks():
    els = [e for e in location_elements() if e.text != "Approximate"]
    d = pd.detect(GOOGLE, els)
    assert decide(d, location="while_using").answer == "ask"
    assert decide(d, location="while_using", location_accuracy="precise").answer == "allow"


class LocationPhone(DialogPhone):
    """Kalshi asks for location with Precise/Approximate cards; tapping a card selects it, a button answers."""

    def __init__(self, message):
        super().__init__(message)
        self.elements = location_elements()

    def tap(self, x, y):
        FakeDriver.tap(self, x, y)
        hit = next(e for e in self.elements if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3])
        if hit.cls == "RadioButton":
            self.elements = [dataclasses.replace(e, checked=(e is hit)) if e.cls == "RadioButton" else e
                             for e in self.elements]
        else:
            self.answered = hit.text
            self.elements = []










def test_an_ocr_read_dialog_is_recognized_by_its_text():
    """Android 14+: the phone reads the dialog with OCR (class OCRText, no resource ids)."""
    els = [UIElement(0, "Allow Kalshi to access this device's location?", "", "ocr_permission_message", "OCRText",
                     False, None, (0, 0, 1, 1)),
           UIElement(1, "Precise", "", "", "OCRText", True, True, (184, 1244, 376, 1326)),
           UIElement(2, "Approximate", "", "", "OCRText", True, False, (674, 1244, 926, 1326)),
           UIElement(3, "While using the app", "", "", "OCRText", True, None, (284, 1464, 796, 1546)),
           UIElement(4, "Only this time", "", "", "OCRText", True, None, (284, 1594, 796, 1676)),
           UIElement(5, "Don't allow", "", "", "OCRText", True, None, (284, 1724, 796, 1806))]
    d = pd.detect(GOOGLE, els)
    assert d is not None and d.kind == "location" and d.app == "Kalshi"
    plan = decide(d, app="Kalshi")
    assert plan.answer == "allow" and pd.accuracy_card(d, plan.accuracy).text == "Approximate"
    assert pd.button(d, plan.answer, plan.grant).text == "Only this time"


# The loop no longer taps these dialogs (Android drops accessibility taps on them): it dismisses them with BACK, in
# every mode and whatever the settings (tests/test_tight_loop.py has the rest). The policy above is the phone gate's.

class BackPhone(DialogPhone):
    def key(self, name):
        super().key(name)
        if name == "BACK":
            self.elements = []


@pytest.mark.parametrize("mode,prompts", [("auto", {}), ("auto", {"camera": "ask"}), ("never", {"camera": "ask"}),
                                          ("auto", {"notifications": "deny"})])
def test_every_dialog_is_dismissed_with_back_without_a_model_call(tmp_path, mode, prompts):
    driver, client, io = run(tmp_path, MESSAGES["camera"], mode=mode, phone=BackPhone, **prompts)
    assert not [a for a in driver.actions if a[0] == "tap"] and ("key", "BACK") in driver.actions
    assert io.questions == [] and len(client.requests) == 1
    row = next(r for r in io.steps if r.get("source") == "auto_dialog")
    assert row["tool"] == "press_key" and row["handled_by"] == "permission_rule"
