"""Play Store installs (owner, 2026-10-09): a free install runs unasked only when the request names the app on the
store page; any other install asks, and is refused under Don't ask (security review 2026-10-09: a screen, an ad or a
guessed package id could pick what got installed)."""

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent import gate
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.permissions import TaskPermissions
from openmob.phone.adb import UIElement as U
from openmob.phone.apps import AppResolver

PAGE = [U(0, "Chess - Play and Learn", "", "", "android.widget.TextView", False, None, (40, 300, 900, 380)),
        U(1, "Chess.com", "", "", "android.widget.TextView", False, None, (40, 390, 600, 440)),
        U(2, "4.7★", "", "", "android.widget.TextView", False, None, (40, 500, 200, 560)),
        U(3, "Install", "", "com.android.vending:id/install", "android.widget.Button", True, None, (40, 700, 1040, 800))]


def run(task, mode="auto", answers=("no",), **cfg):
    driver = FakeDriver(elements=PAGE)
    driver.package = "com.android.vending"
    io = RecordingIO(answers=list(answers))
    import tempfile, pathlib
    resolver = AppResolver(driver, {}, label_cache=pathlib.Path(tempfile.mkdtemp()) / "l.json")
    replies = [[tool_use("tap_element", "a", index=3)], [tool_use("done", "z", summary="ok", success=True)]]
    agent = Agent(ScriptedClient(replies), driver, resolver, Config(settle_delay=0, launch_settle_delay=0, **cfg), io,
                  use_ocr=False)
    agent.run(task, permissions=TaskPermissions(mode), isolated=True)
    return driver, io


def tapped(driver):
    return ("tap", 540, 750) in driver.actions


def test_the_store_title_and_naming():
    assert gate.store_title(PAGE, PAGE[3]) == "Chess - Play and Learn"
    assert gate.names_app("install chess and play a game", "Chess - Play and Learn")
    assert not gate.names_app("open the link in the message", "Chess - Play and Learn")
    assert not gate.names_app("install the app", "Free App")  # generic words name nothing


def test_an_install_the_request_names_runs_unasked():
    driver, io = run("install chess and play 5 moves against a bot")
    assert tapped(driver) and not io.questions


def test_an_install_the_request_doesnt_name_asks_and_no_means_no():
    driver, io = run("open the link in my latest message")
    assert io.questions and "Install" in io.questions[0] and not tapped(driver)


def test_an_unnamed_install_is_refused_under_dont_ask():
    driver, io = run("open the link in my latest message", confirm_actions="never")  # nobody can be asked
    assert not tapped(driver) and not io.questions
