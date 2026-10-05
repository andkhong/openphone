import threading

from conftest import FakeDriver, RecordingIO, ScriptedClient, text_block, tool_use

from openmob.agent.loop import OMITTED, Agent, is_yes
from openmob.config import Config
from openmob.jev import ScreenAssessment
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver


def make_agent(responses, driver=None, io=None, judge=None, tmp_path=None, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver or FakeDriver(packages=["com.netease.wwm", "com.android.settings"])
    resolver = AppResolver(driver, {"where winds meet": "com.netease.wwm"}, label_cache=tmp_path / "l.json")
    client = ScriptedClient(responses)
    agent = Agent(client, driver, resolver, config, io or RecordingIO(), judge=judge, use_ocr=False)
    return agent, client, driver


def test_open_game_and_menu_flow(tmp_path):
    agent, client, driver = make_agent([
        [text_block("Opening the game."), tool_use("launch_app", "a", app="Where Winds Meet")],
        [tool_use("tap", "b", x=500, y=50, target="Menu icon top-right")],
        [tool_use("done", "c", summary="The menu is open.", success=True)],
    ], tmp_path=tmp_path)
    result = agent.run("Open where winds meet, and open the menu")

    assert result.success and result.summary == "The menu is open." and result.steps == 3
    # 540x1200 screenshot on a 1080x2400 device: model coords double.
    assert driver.actions == [("wake",), ("launch", "com.netease.wwm"), ("tap", 1000, 100)]
    first = client.requests[0]["messages"][0]["content"]
    assert first[0]["text"] == "Request: Open where winds meet, and open the menu"
    assert first[2]["type"] == "image"


def test_tool_results_carry_new_screenshot_and_history_is_trimmed(tmp_path):
    steps = [[tool_use("wait", f"w{i}", seconds=0.5)] for i in range(4)]
    agent, client, _ = make_agent(steps + [[tool_use("done", "d", summary="ok", success=True)]], tmp_path=tmp_path)
    agent.run("wait around")

    last = client.requests[-1]["messages"]
    images = [b for m in last if m["role"] == "user" and isinstance(m["content"], list)
              for blk in m["content"] for b in ([blk] + (blk.get("content") or [] if blk.get("type") == "tool_result" else []))
              if b.get("type") == "image"]
    assert len(images) == 2  # keep_screenshots default
    flat = str(last)
    assert OMITTED in flat


def test_tap_element_uses_device_center(tmp_path):
    el = UIElement(0, "Dark theme", "", "switch", "Switch", True, False, (880, 600, 1032, 700))
    driver = FakeDriver(elements=[el])
    agent, _, driver = make_agent([
        [tool_use("tap_element", "a", index=0, target="dark theme switch")],
        [tool_use("done", "b", summary="Dark theme on.", success=True)],
    ], driver=driver, tmp_path=tmp_path)
    agent.run("turn on dark mode")
    assert ("tap", 956, 650) in driver.actions


def test_risky_tap_needs_confirmation(tmp_path):
    io = RecordingIO(answers=["no thanks"])
    agent, client, driver = make_agent([
        [tool_use("tap", "a", x=10, y=10, target="Buy 100 gems button")],
        [tool_use("done", "b", summary="Cancelled.", success=False)],
    ], io=io, tmp_path=tmp_path)
    agent.run("open the shop")  # purchase not requested, so it must ask
    assert io.questions and "Buy 100 gems" in io.questions[0]
    assert not any(a[0] == "tap" for a in driver.actions)
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "did not approve" in result["content"][0]["text"]


def test_ask_user_yes_pre_approves_next_risky_tap(tmp_path):
    io = RecordingIO(answers=["yes please"])
    agent, _, driver = make_agent([
        [tool_use("ask_user", "a", question="Send the message?")],
        [tool_use("tap", "b", x=10, y=10, target="Send button")],
        [tool_use("done", "c", summary="Sent.", success=True)],
    ], io=io, tmp_path=tmp_path)
    agent.run("text mom hi")
    assert io.questions == ["Send the message?"]
    assert ("tap", 20, 20) in driver.actions


def test_unknown_app_tells_model_to_use_drawer(tmp_path):
    agent, client, _ = make_agent([
        [tool_use("launch_app", "a", app="Banana Phone")],
        [tool_use("done", "b", summary="Not installed.", success=False)],
    ], tmp_path=tmp_path)
    agent.run("open banana phone")
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "app drawer" in result["content"][0]["text"]


def test_text_only_reply_is_nudged_then_ends(tmp_path):
    agent, client, _ = make_agent([[text_block("Hmm.")], [text_block("I can't.")]], tmp_path=tmp_path)
    result = agent.run("do something")
    assert not result.success and result.summary == "I can't."
    assert "calling a tool" in client.requests[1]["messages"][-1]["content"]


def test_cancel_stops_before_next_model_call(tmp_path):
    cancel = threading.Event()
    cancel.set()
    agent, client, _ = make_agent([], tmp_path=tmp_path)
    result = agent.run("anything", cancel)
    assert result.summary == "Stopped." and client.requests == []


def test_max_steps(tmp_path):
    agent, _, _ = make_agent([[tool_use("press_key", f"k{i}", key="BACK")] for i in range(3)],
                             tmp_path=tmp_path, max_steps=3)
    result = agent.run("loop forever")
    assert not result.success and "3 steps" in result.summary


class FakeJudge:
    def __init__(self, states, risk=0.0, requested=0.0):
        self.states = list(states)
        self.risk = risk
        self.requested = requested
        self.assessed = 0

    def assess_screen(self, request, screen):
        self.assessed += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return ScreenAssessment(state, 0.95, 0.9, 0.1)

    def assess_tap(self, request, target, screen):
        return self.risk, self.requested


def test_jev_loading_screens_are_waited_out_without_model_calls(tmp_path, monkeypatch):
    judge = FakeJudge(["loading", "loading", "app_content"])
    agent, client, _ = make_agent([[tool_use("done", "a", summary="ok", success=True)]],
                                  judge=judge, tmp_path=tmp_path)
    monkeypatch.setattr("openmob.agent.loop.LOADING_POLL_SECONDS", 0.01)
    agent.run("open the menu")
    assert judge.assessed == 3 and len(client.requests) == 1
    assert "Jev screen state: app_content" in str(client.requests[0]["messages"][0])


def test_jev_risk_triggers_confirmation_without_keyword(tmp_path):
    io = RecordingIO(answers=["no"])
    agent, _, driver = make_agent([
        [tool_use("tap", "a", x=10, y=10, target="Gold diamond icon")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], io=io, judge=FakeJudge(["app_content"], risk=0.9), tmp_path=tmp_path)
    agent.run("open the shop")
    assert io.questions and not any(a[0] == "tap" for a in driver.actions)


def test_is_yes():
    assert is_yes("Yes, go ahead") and is_yes("ok") and is_yes("Yeah.")
    assert not is_yes("no") and not is_yes("") and not is_yes("not yet")


def test_unknown_app_suggests_closest_installed_apps(tmp_path):
    driver = FakeDriver(packages=["com.facebook.orca", "com.android.settings"])
    agent, client, _ = make_agent([
        [tool_use("launch_app", "a", app="Facebook Messenger app")],
        [tool_use("launch_app", "b", app="com.facebook.orca")],
        [tool_use("done", "c", summary="Messenger is open.", success=True)],
    ], driver=driver, tmp_path=tmp_path)
    agent.resolver.llm_picker = lambda *a: None
    agent.run("open messenger")
    hint = client.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]
    assert "facebook orca (com.facebook.orca)" in hint
    assert ("launch", "com.facebook.orca") in driver.actions


def test_list_apps_and_open_url(tmp_path):
    driver = FakeDriver(packages=["com.spotify.music", "com.android.settings"], labels={"com.spotify.music": "Spotify"})
    agent, client, driver = make_agent([
        [tool_use("list_apps", "a", query="spotify")],
        [tool_use("open_url", "b", url="https://en.wikipedia.org")],
        [tool_use("done", "c", summary="ok", success=True)],
    ], driver=driver, tmp_path=tmp_path)
    agent.resolver.ensure_labels()
    agent.run("check apps")
    listing = client.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]
    assert listing.splitlines()[0] == "Spotify (com.spotify.music)"
    assert ("url", "https://en.wikipedia.org") in driver.actions


def test_follow_up_requests_see_earlier_ones(tmp_path):
    agent, client, _ = make_agent([
        [tool_use("done", "a", summary="YouTube is open.", success=True)],
        [tool_use("done", "b", summary="Scrolled.", success=True)],
    ], tmp_path=tmp_path)
    agent.run("open youtube")
    agent.run("now scroll down")
    first_text = client.requests[1]["messages"][0]["content"][0]["text"]
    assert "'open youtube' -> YouTube is open." in first_text
    assert first_text.endswith("Request: now scroll down")


def test_failed_action_result_stays_text_only(tmp_path):
    agent, client, _ = make_agent([
        [tool_use("tap_element", "a", index=99, target="missing")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path=tmp_path)
    agent.run("tap something")
    turn = client.requests[1]["messages"][-1]["content"]
    error = turn[0]
    assert error["type"] == "tool_result" and error["is_error"]
    assert all(block["type"] == "text" for block in error["content"])
    assert any(block["type"] == "image" for block in turn[1:])  # new screen follows the results


def test_requested_risky_action_runs_without_asking(tmp_path):
    io = RecordingIO()
    agent, _, driver = make_agent([
        [tool_use("tap", "a", x=10, y=10, target="Send button")],
        [tool_use("done", "b", summary="Sent.", success=True)],
    ], io=io, tmp_path=tmp_path)
    agent.run("send hello world to myself")  # no Jev: the keyword "send" is in the request
    assert io.questions == [] and ("tap", 20, 20) in driver.actions


def test_jev_decides_whether_risky_tap_was_requested(tmp_path):
    io = RecordingIO()
    agent, _, driver = make_agent([
        [tool_use("tap", "a", x=10, y=10, target="Compose")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], io=io, judge=FakeJudge(["app_content"], risk=0.7, requested=0.9), tmp_path=tmp_path)
    agent.run("email myself hello world")
    assert io.questions == [] and ("tap", 20, 20) in driver.actions


def test_confirm_policies(tmp_path):
    steps = lambda: [[tool_use("tap", "a", x=10, y=10, target="Send button")],  # noqa: E731
                     [tool_use("done", "b", summary="ok", success=True)]]
    io = RecordingIO(answers=["yes"])
    agent, _, _ = make_agent(steps(), io=io, tmp_path=tmp_path, confirm_actions="always")
    agent.run("send hello")
    assert len(io.questions) == 1
    io = RecordingIO()
    agent, _, driver = make_agent(steps(), io=io, tmp_path=tmp_path, confirm_actions="never")
    agent.run("open my email")
    assert io.questions == [] and ("tap", 20, 20) in driver.actions


def test_agent_counts_claude_tokens_per_task(tmp_path):
    from types import SimpleNamespace

    agent, client, _ = make_agent([
        [tool_use("wait", "a", seconds=0.5)],
        [tool_use("done", "b", summary="ok", success=True)],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path=tmp_path)
    original = client.create

    def with_usage(**kwargs):
        response = original(**kwargs)
        response.usage = SimpleNamespace(input_tokens=1000, output_tokens=50, cache_read_input_tokens=0)
        return response

    client.create = with_usage
    result = agent.run("wait then finish")
    assert (result.usage.claude_calls, result.usage.claude_input, result.usage.claude_output) == (2, 2000, 100)
    second = agent.run("again")  # each task starts counting from zero
    assert (second.usage.claude_calls, second.usage.claude_input) == (1, 1000)


def test_model_switch_applies_from_the_next_task(tmp_path):
    agent, client, _ = make_agent([
        [tool_use("wait", "a", seconds=0.5)],
        [tool_use("done", "b", summary="ok", success=True)],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path=tmp_path)
    original = client.create

    def switch_mid_task(**kwargs):
        response = original(**kwargs)
        agent.model = "claude-opus-5-5"  # the user picks a new model while the task runs
        return response

    client.create = switch_mid_task
    agent.run("first task")
    agent.run("second task")
    models = [r["model"] for r in client.requests]
    assert models == ["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


def test_new_chat_forgets_earlier_requests(tmp_path):
    from openmob.runner import Coordinator
    from openmob.voice.tts import Speaker

    agent, client, _ = make_agent([
        [tool_use("done", "a", summary="YouTube is open.", success=True)],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path=tmp_path)
    coordinator = Coordinator(Speaker(enabled=False))
    coordinator.agent = agent
    agent.run("open youtube")
    coordinator.clear_log()  # what the dashboard's "New chat" calls
    agent.run("scroll down")
    assert client.requests[1]["messages"][0]["content"][0]["text"] == "Request: scroll down"


def test_tokens_are_counted_under_the_model_the_api_reports(tmp_path):
    from types import SimpleNamespace

    agent, client, _ = make_agent([[tool_use("done", "a", summary="ok", success=True)]], tmp_path=tmp_path)
    original = client.create

    def served(**kwargs):
        response = original(**kwargs)
        response.usage = SimpleNamespace(input_tokens=500, output_tokens=20, cache_read_input_tokens=0)
        response.model = "claude-sonnet-5-5"  # what the API says it used
        return response

    client.create = served
    result = agent.run("check")
    assert client.requests[0]["model"] == "claude-sonnet-5-5"
    assert result.usage.by_model == {"claude-sonnet-5-5": {"input": 500, "output": 20, "calls": 1}}


def test_iphone_gets_ios_instructions(tmp_path):
    agent, client, _ = make_agent([[tool_use("done", "a", summary="ok", success=True)],
                                   [tool_use("done", "b", summary="ok", success=True)]], tmp_path=tmp_path)
    agent.run("open settings")
    assert "Android" in client.requests[0]["system"] and "no BACK key" not in client.requests[0]["system"]
    agent.platform = "ios"
    agent.run("open settings")
    ios_prompt = client.requests[1]["system"]
    assert "no BACK key" in ios_prompt and "Spotlight" in ios_prompt and "Android phone" not in ios_prompt
