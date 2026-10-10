"""Typing on a screen whose tree wasn't read: ui_tree "auto" stops reading an app's tree after two label-less looks,
so the field is read right before typing (security review 2026-10-09: a login form shown after a game's splash was
typed into unseen), and nothing is typed when the field can't be seen at all."""

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

PW = UIElement(0, "", "", "pw", "android.widget.EditText", True, None, (0, 300, 1080, 400),
               editable=True, password=True, focused=True, hint="Password")


class SplashThenLogin(FakeDriver):
    def __init__(self):
        super().__init__(elements=[])
        self.package = "com.example.game"
        self.reads = 0

    def ui_elements(self):
        self.reads += 1
        return [PW] if self.reads > 2 else []  # two canvas frames (splash), then the login form


def test_a_password_field_after_two_treeless_looks_is_still_refused(tmp_path):
    config = Config(settle_delay=0, launch_settle_delay=0)
    assert config.ui_tree == "auto"
    driver = SplashThenLogin()
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    replies = [[tool_use("wait", "w1", seconds=0.5)], [tool_use("wait", "w2", seconds=0.5)],
               [tool_use("type_text", "t", text="hunter2")], [tool_use("done", "z", summary="ok", success=True)]]
    agent = Agent(ScriptedClient(replies), driver, resolver, config, RecordingIO(), use_ocr=False)
    agent.run("log in with password hunter2")
    assert ("type", "hunter2") not in driver.actions, driver.actions
    assert driver.reads >= 3  # read again right before typing, and kept being read after


def test_nothing_is_typed_into_a_field_nobody_can_see(tmp_path):
    driver = FakeDriver(elements=[])
    driver.package = "com.example.game"
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    replies = [[tool_use("type_text", "t", text="hello")], [tool_use("done", "z", summary="ok", success=True)]]
    client = ScriptedClient(replies)
    agent = Agent(client, driver, resolver, Config(settle_delay=0, launch_settle_delay=0), RecordingIO(), use_ocr=False)
    agent.run("type hello")
    assert not any(a[0] == "type" for a in driver.actions)
    assert "can't be read" in str(client.requests[1]["messages"][-1]["content"])


def test_control_always_reads_and_refuses(tmp_path):
    config = Config(settle_delay=0, launch_settle_delay=0, ui_tree="always")
    driver = SplashThenLogin()
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    replies = [[tool_use("wait", "w1", seconds=0.5)], [tool_use("wait", "w2", seconds=0.5)],
               [tool_use("type_text", "t", text="hunter2")], [tool_use("done", "z", summary="ok", success=True)]]
    agent = Agent(ScriptedClient(replies), driver, resolver, config, RecordingIO(), use_ocr=False)
    agent.run("log in with password hunter2")
    assert ("type", "hunter2") not in driver.actions
