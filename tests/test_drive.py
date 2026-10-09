"""The drive level's loop (docs/design/mcp-drive.md): an outside agent's commands run through Agent._execute,
the same path as the model's tool calls, with the same refusals and approvals.

A fake phone and a recording IO stand in; the session runs on a thread, as it does on a phone's Coordinator,
and the test plays the outside agent on the DriveChannel."""

import threading
import time

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient

from openmob import permissions as perm
from openmob.agent import drive
from openmob.agent.loop import BLOCKED_APP_MESSAGE, Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

YT = "com.google.android.youtube"
SHOP = "com.amazon.mShop.android.shopping"
BANK = "com.chase.sig.android"
PERMISSIONS = "com.google.android.permissioncontroller"


def button(i, text="", bounds=(0, 1000, 1000, 1100), desc="", rid="", cls="Button", clickable=True, **kw):
    return UIElement(i, text, desc, rid, cls, clickable, None, bounds, **kw)


def field(i, text="", bounds=(0, 400, 1000, 500), rid="", hint="", focused=True, password=False):
    el = UIElement(i, text, "", rid, "EditText", True, None, bounds, editable=True, password=password)
    el.focused, el.hint = focused, hint
    return el


class DriveIO(RecordingIO):
    """RecordingIO plus what the Coordinator offers the loop: step records and the owner's mid-task words."""

    def __init__(self, answers=()):
        super().__init__(answers)
        self.steps = []
        self.corrections = []

    def record_step(self, step):
        self.steps.append(step)

    def take_corrections(self):
        out, self.corrections = self.corrections, []
        return out


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def agent(tmp_path, answers=(), package="com.android.launcher3", elements=(), driver=None):
    driver = driver or FakeDriver(packages=[YT, SHOP, BANK], labels={YT: "YouTube", SHOP: "Amazon", BANK: "Chase"})
    driver.elements = list(elements)
    driver.package = package
    io = DriveIO(list(answers))
    resolver = AppResolver(driver, {"youtube": YT, "amazon": SHOP, "chase": BANK}, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    a = Agent(ScriptedClient([]), driver, resolver, Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    return a, driver, io


def session(a, goal="look around", mode="auto", max_steps=40, idle=5.0, duration=60.0, blocked=None, cancel=None):
    """Run a drive session on a thread (as the Coordinator would) and return its channel and result holder."""
    settings = perm.default_settings()
    if mode == "never":  # Don't ask is a window the owner turned on; otherwise the ceiling tightens it to auto
        settings["never_until"] = time.time() + 3600
    store = {"s": perm.normalize(settings)}
    perms = perm.TaskPermissions(mode, lambda: store["s"])
    channel = drive.DriveChannel(idle, duration, max_steps, "Claude Code")
    done = {}

    def run():
        done["result"] = a.run(goal, cancel=cancel, permissions=perms, drive=channel, blocked=blocked,
                               isolated=True, max_steps=max_steps)

    threading.Thread(target=run, daemon=True).start()
    assert wait_for(lambda: channel.active.is_set() or channel.ended.is_set())
    return channel, done


def act(channel, name, timeout=5.0, **args):
    command = channel.submit(drive.Command(op="act", name=name, args=args))
    assert command.wait(timeout), f"{name} never came back"
    return command


def look(channel, screenshot=False):
    command = channel.submit(drive.Command(op="observe", screenshot=screenshot))
    assert command.wait(5)
    return command


def end(channel, summary="done", success=True):
    command = channel.submit(drive.Command(op="end", name="done", args={"summary": summary, "success": success}))
    assert command.wait(5)
    return command


def did(driver, kind):
    return [x for x in driver.actions if x[0] == kind]


# --- every act goes through the loop's one path ---------------------------------------------------


def test_each_act_reaches_the_phone_and_returns_the_screen_after_it(tmp_path):
    a, driver, io = agent(tmp_path, elements=[button(0, "Search", rid="search_button")])
    channel, done = session(a)
    assert channel.view["package"] == "com.android.launcher3" and channel.view["elements"][0]["text"] == "Search"

    tapped = act(channel, "tap_element", index=0, target="Search")
    assert not tapped.outcome["is_error"] and did(driver, "tap") == [("tap", 500, 1050)]
    assert tapped.view["elements"] and tapped.text.startswith("SCREEN:") and "Search" in tapped.text

    assert not act(channel, "tap", x=100, y=100, target="a spot").outcome["is_error"]
    driver.elements = [field(0, hint="Search")]
    assert not act(channel, "type_text", text="lofi").outcome["is_error"] and did(driver, "type") == [("type", "lofi")]
    assert not act(channel, "scroll", direction="down").outcome["is_error"] and did(driver, "swipe")
    assert not act(channel, "swipe", x1=10, y1=10, x2=200, y2=400).outcome["is_error"]
    assert not act(channel, "press_key", key="BACK").outcome["is_error"] and ("key", "BACK") in driver.actions
    launched = act(channel, "launch_app", app="youtube")
    assert not launched.outcome["is_error"] and did(driver, "launch") == [("launch", YT)]
    assert launched.view["package"] == YT and launched.view["app"] == "YouTube"
    assert not act(channel, "open_url", url="https://example.com").outcome["is_error"] and did(driver, "url")
    waited = act(channel, "wait_until", until="screen_changes", timeout_s=1)
    assert not waited.outcome["is_error"]
    apps = act(channel, "list_apps", query="you")
    assert YT in apps.outcome["text"]
    assert channel.steps == 10
    ended = end(channel, "Looked around.")
    assert wait_for(lambda: "result" in done)
    assert done["result"].success and done["result"].summary == "Looked around." and done["result"].steps == 11
    assert channel.ended.is_set() and channel.reason == "ended" and channel.result["success"]
    # The feed saw every act as the loop reports its own, and every step was recorded with the drive source.
    kinds = [k for k, _ in io.events]
    assert kinds.count("action") == 11 and all(s["source"] == "drive" for s in io.steps) and len(io.steps) == 11
    assert all("total_ms" in s and "action_ms" in s for s in io.steps)
    assert io.steps[2]["args"]["text"] == "<redacted>"  # typed text never in the record


def test_every_act_goes_through_execute(tmp_path, monkeypatch):
    """No path from a drive command to the phone skips Agent._execute (and so _dispatch and its checks)."""
    a, driver, io = agent(tmp_path, elements=[button(0, "Go")])
    seen = []
    original = Agent._execute

    def spy(self, name, args, obs, state, *rest, **kw):
        seen.append(name)
        return original(self, name, args, obs, state, *rest, **kw)

    monkeypatch.setattr(Agent, "_execute", spy)
    channel, done = session(a)
    act(channel, "tap_element", index=0, target="Go")
    act(channel, "press_key", key="HOME")
    look(channel)
    end(channel)
    assert wait_for(lambda: "result" in done)
    assert seen == ["tap_element", "press_key", "done"]
    assert [x[0] for x in driver.actions if x[0] != "wake"] == ["tap", "key"]


# --- the same refusals as the loop ---------------------------------------------------------------


def test_blocked_apps_are_refused_and_their_screens_read_black(tmp_path):
    blocked = lambda package: package == BANK
    a, driver, io = agent(tmp_path, elements=[button(0, "Balance")])
    channel, done = session(a, blocked=blocked)
    refused = act(channel, "launch_app", app="chase")
    assert refused.outcome["is_error"] and BLOCKED_APP_MESSAGE in refused.outcome["text"] and not did(driver, "launch")
    # The owner opens the bank by hand: the session goes dark and only BACK / HOME are accepted.
    driver.package = BANK
    seen = look(channel)
    assert seen.view["blocked_app"] is True and seen.view["elements"] == [] and "blocked" in seen.text
    tap = act(channel, "tap_element", index=0, target="Balance")
    assert tap.outcome["is_error"] and BLOCKED_APP_MESSAGE in tap.outcome["text"] and not did(driver, "tap")
    typed = act(channel, "type_text", text="1000")
    assert typed.outcome["is_error"] and not did(driver, "type")
    home = act(channel, "press_key", key="HOME")
    assert not home.outcome["is_error"] and ("key", "HOME") in driver.actions
    driver.package = "com.android.launcher3"
    assert look(channel).view["blocked_app"] is False
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_password_and_one_time_code_fields_are_never_typed(tmp_path):
    # A label beside the field, as login screens have: a tree with no text at all counts as a game's after two
    # looks (ui_tree auto) and isn't read any more, which is the loop's rule, not this test's subject.
    label = button(1, "Sign in", cls="TextView", clickable=False, bounds=(0, 200, 1000, 300))
    a, driver, io = agent(tmp_path, package=SHOP, elements=[field(0, password=True), label])
    channel, done = session(a)
    refused = act(channel, "type_text", text="hunter2")
    assert refused.outcome["refused"] and "password" in refused.outcome["text"] and not did(driver, "type")
    driver.elements = [field(0, hint="Enter the 6-digit verification code", rid="otp_input"), label]
    look(channel)  # the screen changed: the agent looks before it acts (an act is judged on the screen it saw)
    refused = act(channel, "type_text", text="123456")
    assert refused.outcome["refused"] and "verification codes" in refused.outcome["text"] and not did(driver, "type")
    assert any("one-time-code field" in m for k, m in io.events if k == "status")  # the feed names the field
    driver.elements = [field(0, hint="Search"), label]
    look(channel)
    assert not act(channel, "type_text", text="cables").outcome["is_error"] and did(driver, "type") == [("type", "cables")]
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_the_loop_itself_still_types_login_codes(tmp_path):
    """The OTP refusal is the drive level's (state.refuse_otp); the agent loop is unchanged."""
    from conftest import tool_use

    driver = FakeDriver(packages=[SHOP])
    driver.package = SHOP
    driver.elements = [field(0, hint="Verification code", rid="otp_input")]
    io = RecordingIO()
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    steps = [[tool_use("type_text", "a", text="123456")], [tool_use("done", "z", summary="ok", success=True)]]
    a = Agent(ScriptedClient(steps), driver, resolver, Config(settle_delay=0, launch_settle_delay=0), io, use_ocr=False)
    a.run("enter the code 123456")
    assert did(driver, "type") == [("type", "123456")]


def test_dangerous_links_are_refused(tmp_path):
    a, driver, io = agent(tmp_path)
    channel, done = session(a)
    for url in ("javascript:alert(1)", "intent://x#Intent;end", "file:///etc/passwd", "spotify:track:1"):
        refused = act(channel, "open_url", url=url)
        assert refused.outcome["refused"], url
    assert not did(driver, "url")
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_a_risky_tap_asks_the_owner_and_a_no_means_no_tap(tmp_path):
    a, driver, io = agent(tmp_path, ["no", "yes"], package=SHOP, elements=[button(0, "Buy now")])
    channel, done = session(a, goal="look at the cart")
    declined = act(channel, "tap_element", index=0, target="the product")
    assert declined.outcome["is_error"] and "did not approve" in declined.outcome["text"] and not did(driver, "tap")
    assert len(io.questions) == 1 and "Buy now" in io.questions[0]
    approved = act(channel, "tap_element", index=0, target="the product")
    assert not approved.outcome["is_error"] and did(driver, "tap") == [("tap", 500, 1050)]
    assert len(io.questions) == 2
    end(channel)
    assert wait_for(lambda: "result" in done)
    assert done["result"].prompts == 2


def test_the_agents_target_words_never_make_a_tap_safe_or_requested(tmp_path):
    """A model lying about what it taps ("Display settings") is judged by the real element, as in the loop;
    a goal that says "send" makes one Send requested (the same rule as run_task's instruction)."""
    a, driver, io = agent(tmp_path, ["no"], package=SHOP, elements=[button(0, "Buy now")])
    channel, done = session(a, goal="open the display settings")
    lie = act(channel, "tap", x=250, y=525, target="Display settings")  # (250,525) on the half-size shot hits y 1050
    assert lie.outcome["is_error"] and not did(driver, "tap") and "Buy now" in io.questions[0]
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_ask_every_step_asks_before_each_act(tmp_path):
    a, driver, io = agent(tmp_path, ["no", "yes"])
    channel, done = session(a, mode="ask")
    refused = act(channel, "launch_app", app="youtube")
    assert refused.outcome["is_error"] and not did(driver, "launch") and io.questions[0].startswith("Next step: open")
    assert not act(channel, "launch_app", app="youtube").outcome["is_error"] and did(driver, "launch")
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_plan_first_needs_a_plan_and_a_rejected_plan_ends_the_session(tmp_path):
    a, driver, io = agent(tmp_path, ["no"])
    channel, done = session(a, mode="plan")
    blocked = act(channel, "launch_app", app="youtube")
    assert blocked.outcome["is_error"] and "Plan first" in blocked.outcome["text"] and not did(driver, "launch")
    rejected = act(channel, "propose_plan", steps=[{"text": "open YouTube"}])
    assert wait_for(lambda: "result" in done)
    assert "rejected" in rejected.outcome["text"] and not done["result"].success and channel.reason == "ended"


def test_never_mode_refuses_purchases_instead_of_asking(tmp_path):
    a, driver, io = agent(tmp_path, package=SHOP, elements=[button(0, "Buy now")])
    channel, done = session(a, mode="never")
    refused = act(channel, "tap_element", index=0, target="buy")
    assert refused.outcome["refused"] and "purchases are off" in refused.outcome["text"].lower() and not did(driver, "tap")
    assert not io.questions
    end(channel)
    assert wait_for(lambda: "result" in done)


# --- the loop's own rules between steps ----------------------------------------------------------


def test_permission_dialogs_are_dismissed_by_rule_and_noted(tmp_path):
    dialog = [UIElement(0, "Allow YouTube to send you notifications?", "", "com.android.permissioncontroller:id/permission_message",
                        "TextView", False, None, (0, 500, 1000, 600)),
              button(1, "Allow", rid="com.android.permissioncontroller:id/permission_allow_button"),
              button(2, "Don't allow", rid="com.android.permissioncontroller:id/permission_deny_button",
                     bounds=(0, 1100, 1000, 1200))]
    a, driver, io = agent(tmp_path, package=YT)
    channel, done = session(a)
    original_key = driver.key

    def key(name):  # BACK closes the dialog
        original_key(name)
        if name == "BACK":
            driver.package, driver.elements = YT, [button(0, "Subscribe")]

    driver.key = key
    driver.package, driver.elements = PERMISSIONS, dialog
    seen = look(channel)
    assert ("key", "BACK") in driver.actions and seen.view["package"] == YT
    assert any("Dismissed" in note and "notifications" in note for note in seen.view["notes"])
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_the_owners_words_reach_the_agent_and_count_as_the_request(tmp_path):
    a, driver, io = agent(tmp_path, package=SHOP, elements=[button(0, "Send")])
    channel, done = session(a, goal="look at the chat")
    io.corrections.append("send it")
    seen = look(channel)
    assert seen.view["owner_messages"] == ["send it"]
    # The owner asked for the send: one Send in the task's app runs without a prompt, as in the loop.
    sent = act(channel, "tap_element", index=0, target="Send")
    assert not sent.outcome["is_error"] and did(driver, "tap") and not io.questions
    end(channel)
    assert wait_for(lambda: "result" in done)


# --- limits and stopping ---------------------------------------------------------------------------


def test_the_step_cap_ends_the_session_after_the_last_act(tmp_path):
    a, driver, io = agent(tmp_path, elements=[button(0, "Go")])
    channel, done = session(a, max_steps=2)
    act(channel, "tap_element", index=0, target="Go")
    last = act(channel, "tap_element", index=0, target="Go")
    assert not last.outcome["is_error"] and last.view is not None  # the act ran and its screen came back
    assert wait_for(lambda: "result" in done)
    assert channel.reason == "step_cap" and "step cap" in done["result"].summary and not done["result"].success
    with pytest.raises(drive.DriveEnded):
        channel.submit(drive.Command(op="act", name="press_key", args={"key": "HOME"}))


def test_idle_and_duration_limits_end_the_session(tmp_path):
    a, driver, io = agent(tmp_path)
    channel, done = session(a, idle=0.4)
    assert wait_for(lambda: "result" in done)
    assert channel.reason == "idle_timeout" and "no command" in done["result"].summary
    a, driver, io = agent(tmp_path)
    channel, done = session(a, idle=10, duration=0.4)
    assert wait_for(lambda: "result" in done)
    assert channel.reason == "max_duration"


def test_a_stop_ends_the_session_and_answers_the_waiting_agent(tmp_path):
    a, driver, io = agent(tmp_path, elements=[button(0, "Go")])
    cancel = threading.Event()
    channel, done = session(a, cancel=cancel)
    cancel.set()  # the owner's Stop, cancel_task, a revoked key
    assert wait_for(lambda: "result" in done)
    assert done["result"].summary == "Stopped." and channel.reason == "stopped"
    with pytest.raises(drive.DriveEnded):
        channel.submit(drive.Command(op="act", name="tap_element", args={"index": 0, "target": "Go"}))


def test_one_command_at_a_time(tmp_path):
    a, driver, io = agent(tmp_path)
    channel, done = session(a)
    slow = channel.submit(drive.Command(op="act", name="wait_until", args={"until": "screen_changes", "timeout_s": 2}))
    with pytest.raises(drive.DriveBusy):
        channel.submit(drive.Command(op="act", name="press_key", args={"key": "HOME"}))
    assert slow.wait(5)
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_screenshots_come_only_when_asked_and_never_for_a_blocked_app(tmp_path):
    a, driver, io = agent(tmp_path, elements=[button(0, "Go")])
    channel, done = session(a, blocked=lambda p: p == BANK)
    assert look(channel).take_jpeg() is None
    shot = look(channel, screenshot=True)
    jpeg = shot.take_jpeg()
    assert jpeg is not None and jpeg[:2] == b"\xff\xd8" and shot.take_jpeg() is None  # handed over once
    assert shot.view["screenshot"]["device_width"] == 1080
    driver.package = BANK
    assert look(channel, screenshot=True).take_jpeg() is None
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_a_password_fields_text_is_redacted_in_the_view(tmp_path):
    a, driver, io = agent(tmp_path, package=SHOP, elements=[field(0, text="hunter2", password=True), button(1, "Log in")])
    channel, done = session(a)
    seen = channel.view
    assert seen["elements"][0]["text"] == "<redacted>" and seen["elements"][0]["password"] is True
    assert "hunter2" not in channel.view_text and "Log in" in channel.view_text
    end(channel)
    assert wait_for(lambda: "result" in done)


def test_a_login_form_without_labels_stays_readable_so_its_password_is_never_typed(tmp_path):
    # Fields with no text, no description and no hint used to count as a game's treeless screen after two looks
    # (ui_tree auto), and the tree stopped being read, so the password check couldn't see the field.
    a, driver, io = agent(tmp_path, package=SHOP, elements=[field(0, password=True)])
    channel, done = session(a)
    for _ in range(3):
        look(channel)
    refused = act(channel, "type_text", text="hunter2")
    assert refused.outcome["refused"] and "password" in refused.outcome["text"] and not did(driver, "type")
    end(channel)
    assert wait_for(lambda: "result" in done)
