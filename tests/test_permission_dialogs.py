"""Android's runtime permission dialogs are answered by rule, with no model call (openmob/agent/permission_dialogs.py).

Fixtures: the Google (Pixel) permission controller, Samsung's One UI one, and the older AOSP package installer.
"""

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


@pytest.mark.parametrize("kind", list(MESSAGES))
def test_the_decision_per_type(kind):
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES[kind]))
    expected = "deny" if kind == "notifications" else "ask"
    assert pd.decide(d, mode="auto", notifications="deny", task_app_label="Chess.com") == expected
    if kind == "notifications":
        assert pd.decide(d, mode="auto", notifications="allow", task_app_label="Chess.com") == "allow"
    else:
        assert pd.question(d).startswith("Allow Chess.com to use your ")


@pytest.mark.parametrize("kind", list(MESSAGES))
def test_dont_ask_mode_denies_everything_but_the_notification_setting(kind):
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES[kind]))
    got = pd.decide(d, mode="never", notifications="allow", task_app_label="Chess.com")
    assert got == ("allow" if kind == "notifications" else "deny")


def test_another_apps_request_asks():
    d = pd.detect(GOOGLE, dialog_elements("Allow Spyware to send you notifications?"))
    assert pd.decide(d, mode="auto", notifications="allow", task_app_label="Chess.com") == "ask"
    assert pd.decide(d, mode="auto", notifications="deny", task_app_label="Chess.com") == "deny"  # deny never asks
    d = pd.detect(GOOGLE, dialog_elements("Allow Spyware to access your contacts?"))
    assert pd.decide(d, mode="auto", notifications="deny", task_app_label="Chess.com") == "ask"


def test_a_yes_taps_the_narrowest_grant():
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["camera"], roles=("always", "foreground", "one_time", "deny")))
    assert pd.button(d, "allow").text == "Only this time"
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["location"], roles=("always", "foreground", "deny")))
    assert pd.button(d, "allow").text == "While using the app"
    d = pd.detect(GOOGLE, dialog_elements(MESSAGES["location"], roles=("always", "deny")))
    assert pd.button(d, "allow") is None  # never "Allow all the time"
    assert pd.button(d, "deny").text == "Don't allow"


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


class StepIO(RecordingIO):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.steps = []

    def record_step(self, row):
        self.steps.append(row)


def run(tmp_path, message, answers=None, mode="auto", notifications="deny", phone=None):
    driver = (phone or DialogPhone)(message)
    client = ScriptedClient([[tool_use("done", "d", summary="ok", success=True)]])
    io = StepIO(answers)
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    perms = TaskPermissions(mode, lambda: {"ceiling": "trusted", "default_mode": mode,
                                           "notifications_permission": notifications,
                                           **({"never_until": 9e12} if mode == "never" else {})})
    agent.run("play chess", permissions=perms)
    return driver, client, io


def test_notifications_are_denied_without_a_model_call(tmp_path):
    driver, client, io = run(tmp_path, MESSAGES["notifications"])
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1 and taps[0][2] > 1850  # "Don't allow"
    assert len(client.requests) == 1  # only the step after the dialog (the model's "done")
    row = next(r for r in io.steps if r.get("handled_by") == "permission_rule")
    assert row["fast"] is True and row["tool"] == "tap_element"
    assert any("Permission dialog (notifications" in m for k, m in io.events)
    assert io.questions == []


def test_camera_asks_the_owner_and_prefers_one_time(tmp_path):
    driver, client, io = run(tmp_path, MESSAGES["camera"], answers=["yes"])
    assert io.questions and "Chess.com" in io.questions[0] and "camera" in io.questions[0]
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1 and 1700 < taps[0][2] < 1820  # "Only this time" (second button)


def test_camera_denied_by_the_owner(tmp_path):
    driver, client, io = run(tmp_path, MESSAGES["camera"], answers=["no"])
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1 and taps[0][2] > 1850  # "Don't allow"


class GatekeeperDialogPhone(DialogPhone):
    """A gatekeeper phone: the server doesn't prompt; its hint makes the phone ask with the dialog's own words."""
    gatekeeper = True

    def __init__(self, message):
        super().__init__(message)
        self.hints = []

    def gate_hint(self, text):
        self.hints.append(text)


def test_on_a_gatekeeper_phone_the_phone_asks(tmp_path):
    driver, client, io = run(tmp_path, MESSAGES["camera"], phone=GatekeeperDialogPhone)
    assert io.questions == []  # no server-side prompt: one prompt, on the phone
    assert driver.hints and "camera" in driver.hints[0]
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1 and 1700 < taps[0][2] < 1820  # "Only this time"


def test_dont_ask_mode_denies_the_camera_without_asking(tmp_path):
    driver, client, io = run(tmp_path, MESSAGES["camera"], mode="never")
    assert io.questions == []
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1 and taps[0][2] > 1850
