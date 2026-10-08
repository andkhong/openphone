"""Several actions from one model reply (max_actions_per_turn): they run in order with a look in
between, and the rest of the turn is skipped as soon as anything doesn't go as the model predicted."""

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent.loop import Agent
from openmob.agent.tools import MULTI_CALL_RULE, ONE_CALL_RULE, system_prompt
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver


class CountingDriver(FakeDriver):
    """Counts looks at the phone; a tap can open another app."""

    def __init__(self, *args, open_on_tap=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.looks = 0
        self.open_on_tap = open_on_tap

    def current_app(self):
        self.looks += 1
        return self.package

    def tap(self, x, y):
        super().tap(x, y)
        if self.open_on_tap:
            self.package = self.open_on_tap


def run(replies, tmp_path, driver=None, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver or CountingDriver()
    driver.package = "com.chess"
    resolver = AppResolver(driver, {}, label_cache=tmp_path / "l.json")
    client = ScriptedClient(replies)
    agent = Agent(client, driver, resolver, config, RecordingIO(), use_ocr=False)
    return agent.run("move the knight"), client, driver


def tool_results(request, turn=1):
    """The tool results the model got back after its `turn`-th reply."""
    return [b for b in request["messages"][2 * turn]["content"] if b.get("type") == "tool_result"]


def done(tool_id="d"):
    return tool_use("done", tool_id, summary="ok", success=True)


def test_one_action_per_turn_by_default(tmp_path):
    assert ONE_CALL_RULE in system_prompt("android") and ONE_CALL_RULE in system_prompt("android", 1)
    prompt = system_prompt("android", 3)
    assert ONE_CALL_RULE not in prompt and MULTI_CALL_RULE.format(n=3) in prompt
    result, client, _ = run([[tool_use("tap", "a", x=10, y=10)], [done()]], tmp_path)
    assert result.success and ONE_CALL_RULE in client.requests[0]["system"][0]["text"]


def test_two_taps_from_one_reply_run_with_a_look_between(tmp_path):
    result, client, driver = run([
        [tool_use("tap", "a", x=100, y=200), tool_use("tap", "b", x=300, y=400)],
        [done()],
    ], tmp_path, max_actions_per_turn=3)
    assert result.success and len(client.requests) == 2  # one model call for both taps
    # Screenshot coordinates scale to the device (540x1200 shown, 1080x2400 real).
    assert [a for a in driver.actions if a[0] == "tap"] == [("tap", 200, 400), ("tap", 600, 800)]
    assert MULTI_CALL_RULE.format(n=3) in client.requests[0]["system"][0]["text"]
    assert not any(r.get("is_error") for r in tool_results(client.requests[1]))


def test_done_is_never_batched_with_actions(tmp_path):
    result, client, driver = run([
        [tool_use("tap", "a", x=100, y=200), done("d1")],
        [done("d2")],
    ], tmp_path, max_actions_per_turn=3)
    assert result.success and len(client.requests) == 2  # the first done didn't run: the model looked again
    skipped = tool_results(client.requests[1])[1]
    assert skipped["is_error"] and "call done alone" in skipped["content"][0]["text"]


def test_actions_beyond_the_limit_are_skipped(tmp_path):
    taps = [tool_use("tap", f"t{i}", x=10 * i, y=10) for i in range(1, 5)]
    result, client, driver = run([taps, [done()]], tmp_path, max_actions_per_turn=2)
    assert len([a for a in driver.actions if a[0] == "tap"]) == 2
    texts = [r["content"][0]["text"] for r in tool_results(client.requests[1])]
    assert all("at most 2 actions" in t for t in texts[2:])


def test_a_failed_action_skips_the_rest_of_the_turn(tmp_path):
    result, client, driver = run([
        [tool_use("tap_element", "a", index=99), tool_use("tap", "b", x=300, y=400)],
        [done()],
    ], tmp_path, max_actions_per_turn=3)
    assert not [a for a in driver.actions if a[0] == "tap"]
    second = tool_results(client.requests[1])[1]
    assert second["is_error"] and "previous action failed" in second["content"][0]["text"]


def test_another_app_in_front_stops_the_batch(tmp_path):
    driver = CountingDriver(open_on_tap="com.android.vending")
    result, client, _ = run([
        [tool_use("tap", "a", x=100, y=200), tool_use("tap", "b", x=300, y=400)],
        [done()],
    ], tmp_path, driver=driver, max_actions_per_turn=3)
    assert [a for a in driver.actions if a[0] == "tap"] == [("tap", 200, 400)]
    assert "opened another app" in tool_results(client.requests[1])[1]["content"][0]["text"]


def test_a_labeled_screen_that_didnt_change_stops_the_batch(tmp_path):
    driver = CountingDriver(elements=[UIElement(0, "Play", "", "", "android.widget.Button", True, None, (0, 0, 200, 100)),
                                      UIElement(1, "Settings", "", "", "android.widget.Button", True, None, (0, 200, 200, 300))])
    result, client, _ = run([
        [tool_use("tap", "a", x=100, y=50), tool_use("tap", "b", x=100, y=250)],
        [done()],
    ], tmp_path, driver=driver, max_actions_per_turn=3)
    assert [a for a in driver.actions if a[0] == "tap"] == [("tap", 200, 100)]
    assert "didn't change the screen" in tool_results(client.requests[1])[1]["content"][0]["text"]
