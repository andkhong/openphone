"""One action per turn (the default): a reply holding several tool calls runs only the first; the rest were decided
on a screen it may have changed. Security review 2026-10-09: tap, type, TAB and type in one reply typed a password
into a field the password check never saw."""

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver


USER = UIElement(0, "", "", "com.example.login:id/user", "android.widget.EditText", True, None, (0, 100, 1080, 200),
                 editable=True, hint="Username")
PW = UIElement(1, "", "", "com.example.login:id/pw", "android.widget.EditText", True, None, (0, 300, 1080, 400),
               editable=True, password=True, hint="Password")
BTN = UIElement(2, "Sign in", "", "", "android.widget.Button", True, None, (0, 500, 1080, 600))


class LoginDriver(FakeDriver):
    """A login form; TAB moves input focus from the username field to the password field (as Android does)."""

    def __init__(self):
        super().__init__(elements=[USER, PW, BTN])
        self.package = "com.example.login"
        self.focus = None
        self.typed_into = []

    def ui_elements(self):
        from dataclasses import replace
        return [replace(e, focused=(e.index == self.focus)) for e in self.elements]

    def tap(self, x, y):
        super().tap(x, y)
        for e in self.elements:
            x1, y1, x2, y2 = e.bounds
            if x1 <= x < x2 and y1 <= y < y2 and e.editable:
                self.focus = e.index

    def key(self, name):
        super().key(name)
        if name == "TAB" and self.focus == 0:
            self.focus = 1

    def type_text(self, text, replace=False):
        super().type_text(text, replace)
        self.typed_into.append((self.focus, text))


def run(replies, tmp_path, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = LoginDriver()
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    client = ScriptedClient(replies)
    agent = Agent(client, driver, resolver, config, RecordingIO(), use_ocr=False)
    result = agent.run("log in as bob with password hunter2")
    return result, client, driver


def test_a_reply_with_several_calls_runs_only_the_first(tmp_path):
    assert Config().max_actions_per_turn == 1  # the default
    reply = [tool_use("tap_element", "a", index=0), tool_use("type_text", "b", text="bob"),
             tool_use("press_key", "c", key="TAB"), tool_use("type_text", "d", text="hunter2")]
    result, client, driver = run([reply, [tool_use("done", "z", summary="ok", success=True)]], tmp_path)
    assert driver.typed_into == [] and driver.focus == 0  # only the tap ran
    skipped = [b for b in client.requests[1]["messages"][-1]["content"] if b.get("type") == "tool_result"][1:]
    assert len(skipped) == 3 and all(b["is_error"] and "one action runs per turn" in str(b["content"]) for b in skipped)


def test_control_password_refused_when_looked_at(tmp_path):
    """The same sequence one call per reply: the look after TAB sees the focused password field and refuses."""
    replies = [[tool_use("tap_element", "a", index=0)], [tool_use("type_text", "b", text="bob")],
               [tool_use("press_key", "c", key="TAB")], [tool_use("type_text", "d", text="hunter2")],
               [tool_use("done", "z", summary="ok", success=True)]]
    result, client, driver = run(replies, tmp_path)
    assert (1, "hunter2") not in driver.typed_into, driver.typed_into
