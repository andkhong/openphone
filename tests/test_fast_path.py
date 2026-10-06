"""The fast path: classifier-decided steps on labeled screens, its escalations, and the tap safety checks."""

import time

from conftest import FakeDriver, RecordingIO, ScriptedClient, text_block, tool_use

from openmob.agent import fast
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.jev import JevJudge, ScreenAssessment, StepChoice
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver


def el(i, text, cls="TextView", clickable=True, bounds=None, **kw):
    return UIElement(i, text, "", "", cls, clickable, None, bounds or (0, 100 * i, 1000, 100 * i + 80), **kw)


SETTINGS = [el(0, "Network & internet"), el(1, "Display"), el(2, "Sound"), el(3, "Battery")]
DISPLAY = [el(0, "Brightness level"), el(1, "Dark theme"), el(2, "Screen timeout")]


class Phone(FakeDriver):
    """Screens change on taps: `screens` maps a tapped label to the next element list."""

    def __init__(self, elements, screens=None, **kw):
        super().__init__(elements=list(elements), **kw)
        self.screens = screens or {}

    def tap(self, x, y):
        super().tap(x, y)
        for e in self.elements:
            x1, y1, x2, y2 = e.bounds
            if x1 <= x < x2 and y1 <= y < y2 and e.text in self.screens:
                self.elements = list(self.screens[e.text])
                return


class Judge:
    """Stands in for the classifier: answers next_step from a script of (operation, target, p)."""

    def __init__(self, script, delay=0.0, risk=0.0, requested=0.0):
        self.script = list(script)
        self.delay = delay
        self.risk, self.requested = risk, requested
        self.asked = []  # (screen, recent_actions, targets) per next_step

    def assess_screen(self, request, screen):
        return ScreenAssessment("app_content", 0.9, 0.9, 0.0)

    def assess_tap(self, request, target, screen):
        return self.risk, self.requested

    def next_step(self, request, screen, recent_actions, targets):
        self.asked.append((screen, list(recent_actions), targets))
        time.sleep(self.delay)
        op, target, p = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        heads = {o: (str(target), p, 0.9) for o in ("TAP", "TYPE_TEXT") if target is not None}
        return StepChoice(op, p, 0.9, heads)


def make(responses, driver, judge, tmp_path, io=None, **cfg):
    config = Config(**{"settle_delay": 0, "launch_settle_delay": 0, "fast_path": "on", **cfg})
    client = ScriptedClient(responses)
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    agent = Agent(client, driver, resolver, config, io, judge=judge, use_ocr=False)
    return agent, client, steps


DONE = [tool_use("done", "d", summary="Display settings are open.", success=True)]


def test_a_confident_tap_runs_without_the_main_model(tmp_path):
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    judge = Judge([("TAP", 1, 0.95), ("DONE", None, 0.95)], delay=0.02)
    agent, client, steps = make([DONE], driver, judge, tmp_path)
    result = agent.run("open display settings")

    assert result.success and result.summary == "Display settings are open."
    assert driver.actions[-1] == ("tap", 500, 140)  # [1]'s own center, never model coordinates
    assert len(client.requests) == 1  # only the vision step that confirmed DONE
    first = client.requests[0]["messages"][0]["content"]
    assert any("quick helper" in b.get("text", "") and "tap_element" in b.get("text", "") for b in first)
    tap, done = steps
    assert tap["tool"] == "tap_element" and tap["fast"] is True and done["fast"] is False
    assert tap["jev_ms"] >= 20 and tap["model_ms"] == 0  # a fast tap uses no model at all
    assert tap["variant"] == "adaptive+fast+noshot+1req+launch"
    # The classifier saw an indexed table and only compatible targets per operation.
    screen, _, targets = judge.asked[0]
    assert screen["elements"][1].startswith("[1] TextView 'Display'")
    assert set(targets["TAP"]) == {"0", "1", "2", "3"} and targets["TYPE_TEXT"] == {}


def test_low_confidence_goes_to_the_vision_step(tmp_path):
    driver = Phone(SETTINGS)
    judge = Judge([("TAP", 1, 0.4)])
    agent, client, steps = make([DONE], driver, judge, tmp_path)
    agent.run("open display settings")
    assert len(client.requests) == 1 and not any(a[0] == "tap" for a in driver.actions)
    assert steps[0]["fast"] is False and judge.asked


def test_escalate_goes_to_the_vision_step(tmp_path):
    judge = Judge([("ESCALATE", None, 0.99)])
    agent, client, _ = make([DONE], Phone(SETTINGS), judge, tmp_path)
    agent.run("open display settings")
    assert len(client.requests) == 1 and judge.asked


def test_screens_without_a_usable_element_list_use_vision(tmp_path):
    judge = Judge([("TAP", 0, 0.99)])
    game = Phone([el(0, "", cls="SurfaceView")])  # a game: one unlabeled view
    agent, client, _ = make([DONE], game, judge, tmp_path)
    agent.run("open the menu")
    assert judge.asked == [] and len(client.requests) == 1
    ocr_only = Phone([el(i, w, cls="OCRText") for i, w in enumerate(["Play", "Shop", "Menu", "Quit"])])
    agent, client, _ = make([DONE], ocr_only, Judge([("TAP", 0, 0.99)]), tmp_path)
    agent.run("open the menu")
    assert agent.judge.asked == [] and len(client.requests) == 1


def test_no_progress_hands_the_next_step_to_vision(tmp_path):
    driver = Phone(SETTINGS)  # tapping changes nothing
    judge = Judge([("TAP", 2, 0.95)])
    agent, client, steps = make([DONE], driver, judge, tmp_path)
    agent.run("open sound settings")
    assert [s["fast"] for s in steps] == [True, False]  # one fast tap, then the vision model
    assert len(client.requests) == 1
    assert judge.asked[0][1] == []  # nothing done yet when it was first asked


def test_a_repeated_action_goes_to_vision(tmp_path):
    driver = Phone(SETTINGS, {"Sound": SETTINGS[:3] + [el(3, "Battery saver")]})  # changes once
    judge = Judge([("TAP", 2, 0.95)])
    agent, client, steps = make([[tool_use("press_key", "b", key="BACK")], DONE], driver, judge, tmp_path)
    agent.run("open sound settings")
    # It changed the screen, but tapping 'Sound' again right away is the vision model's call; after
    # that (here BACK) once more is fine, a third time within four actions isn't.
    assert [s["fast"] for s in steps] == [True, False, True, False]
    assert judge.asked[1][1][-1] == {"action": "tap [2] 'Sound'", "screen_changed": True}
    assert steps[1]["tool"] == "press_key" and not steps[1]["fast"]


def test_done_needs_the_vision_model(tmp_path):
    judge = Judge([("DONE", None, 0.99)])
    agent, client, _ = make([[tool_use("press_key", "a", key="BACK")],
                             [tool_use("done", "b", summary="Not yet.", success=False)]], Phone(SETTINGS), judge, tmp_path)
    result = agent.run("open display settings")
    assert len(client.requests) == 2  # the classifier's DONE finished nothing
    assert not result.success and result.summary == "Not yet."


def test_type_text_asks_the_small_text_model(tmp_path):
    search = el(0, "", cls="EditText", editable=True, bounds=(0, 0, 1000, 80))
    search.resource_id = "search_src_text"
    screen = [search, el(1, "Home"), el(2, "Shorts"), el(3, "Subscriptions")]
    driver = Phone(screen)
    judge = Judge([("TYPE_TEXT", 0, 0.95), ("DONE", None, 0.95)])
    io = RecordingIO()
    agent, client, steps = make([[text_block('{"text": "lofi beats"}')], DONE], driver, judge, tmp_path, io=io)
    agent.run("search youtube for lofi beats")

    assert client.requests[0]["model"] == agent.config.resolver_model  # the small model wrote the text
    assert client.requests[0].get("tools") is None and "lofi" in client.requests[0]["messages"][0]["content"]
    assert ("tap", 500, 40) in driver.actions and ("type", "lofi beats") in driver.actions
    tap, typed = steps[0], steps[1]
    assert tap["fast"] and typed["fast"] and typed["args"]["text"] == "<redacted>"
    assert tap["model_ms"] >= 0 and typed["tool"] == "type_text"
    assert "lofi beats" not in str(io.events)  # typed text isn't shown in the fast step's events
    assert set(judge.asked[0][2]["TYPE_TEXT"]) == {"0"}


def test_a_text_model_without_an_answer_escalates(tmp_path):
    field = el(0, "Search", cls="EditText", editable=True)
    driver = Phone([field, el(1, "A"), el(2, "B"), el(3, "C")])
    judge = Judge([("TYPE_TEXT", 0, 0.95)])
    agent, client, _ = make([[text_block('{"text": null}')], DONE], driver, judge, tmp_path)
    agent.run("search for something")
    assert not any(a[0] == "type" for a in driver.actions) and len(client.requests) == 2


def test_risky_fast_taps_still_need_approval(tmp_path):
    screen = [el(0, "Message"), el(1, "Send", cls="Button"), el(2, "Attach"), el(3, "Emoji")]
    driver = Phone(screen)
    io = RecordingIO(answers=["no"])
    judge = Judge([("TAP", 1, 0.99)])
    agent, client, steps = make([DONE], driver, judge, tmp_path, io=io)
    agent.run("open the chat")
    assert io.questions and "Send" in io.questions[0]
    assert not any(a[0] == "tap" for a in driver.actions)  # declined: never tapped
    assert steps[0]["fast"] and steps[0]["is_error"] and len(client.requests) == 1


def test_the_classifier_can_veto_requested_but_not_grant_it(tmp_path):
    screen = [el(0, "Message"), el(1, "Buy now", cls="Button"), el(2, "Attach"), el(3, "Emoji")]
    io = RecordingIO(answers=["no"])
    judge = Judge([("TAP", 1, 0.99)], risk=0.0, requested=1.0)
    agent, _, _ = make([DONE], Phone(screen), judge, tmp_path, io=io)
    agent.run("open the shop")  # "buy" isn't in the request: the classifier's "requested" doesn't count
    assert io.questions


def test_fast_path_needs_the_classifier_and_the_setting(tmp_path):
    judge = Judge([("TAP", 1, 0.99)])
    agent, client, _ = make([DONE], Phone(SETTINGS), judge, tmp_path, fast_path="off")
    agent.run("open display settings")
    assert judge.asked == []
    agent, client, _ = make([DONE], Phone(SETTINGS), None, tmp_path)
    agent.run("open display settings")
    assert len(client.requests) == 1
    tier = Judge([("TAP", 1, 0.99)])
    agent, _, _ = make([DONE, DONE], Phone(SETTINGS), tier, tmp_path, fast_path="fast_tier")
    agent.run("open display settings")  # the default model isn't the fast tier's
    assert tier.asked == []
    agent.run("open display settings", model=agent.config.api_tiers["fast"]["model"])
    assert tier.asked


# --- safety checks for every tier -------------------------------------------------------------


class Shifty(Phone):
    """A phone whose list is quick to read (like the app) and whose screen moves before the tap."""

    fast_ui_tree = True

    def __init__(self, elements, later):
        super().__init__(elements)
        self.later = later
        self.reads = 0

    def ui_elements(self):
        self.reads += 1
        return list(self.elements if self.reads == 1 else self.later)


def test_a_target_that_moved_is_not_tapped(tmp_path):
    later = [el(0, "Promo banner"), el(1, "Network & internet"), el(2, "Display"), el(3, "Sound")]
    driver = Shifty(SETTINGS, later)
    agent, _, steps = make([[tool_use("tap_element", "a", index=1, target="Display")], DONE], driver, None, tmp_path)
    agent.run("open display settings")
    assert not any(a[0] == "tap" for a in driver.actions)
    assert steps[0]["is_error"] and "no longer where it was" in steps[0]["outcome"]

    same = Shifty(SETTINGS, SETTINGS)
    agent, _, _ = make([[tool_use("tap_element", "a", index=1, target="Display")], DONE], same, None, tmp_path)
    agent.run("open display settings")
    assert ("tap", 500, 140) in same.actions and same.reads >= 2  # re-checked, then tapped


def test_covered_elements_are_not_tapped(tmp_path):
    screen = [el(0, "Network"), el(1, "Display", covered=True), el(2, "Sound")]
    driver = Phone(screen)
    agent, _, steps = make([[tool_use("tap_element", "a", index=1, target="Display")], DONE], driver, None, tmp_path)
    agent.run("open display settings")
    assert not any(a[0] == "tap" for a in driver.actions) and "covered" in steps[0]["outcome"]
    assert "1" not in fast.targets(screen)["TAP"]  # nor offered to the classifier


def test_typing_into_a_search_box_waits_longer_for_suggestions(tmp_path):
    class Quiet(Phone):
        def __init__(self, elements):
            super().__init__(elements)
            self.quiet = []

        def wait_idle(self, quiet_ms, max_ms):
            self.quiet.append(quiet_ms)
            return {"waited_ms": quiet_ms, "idle": True}

    box = el(0, "Search settings", cls="EditText", editable=True)
    driver = Quiet([box, el(1, "Display")])
    agent, _, _ = make([[tool_use("type_text", "a", text="dark")], DONE], driver, None, tmp_path,
                       settle_delay=0.5, settle_quiet_ms=300)
    agent.run("search settings for dark")
    assert driver.quiet == [500]


# --- the classifier request -----------------------------------------------------------------


def test_next_step_asks_one_request_with_compatible_target_heads():
    class Client:
        on_usage = None

        def __init__(self):
            self.sent = []

        def ask(self, state, questions):
            self.sent.append((state, questions))
            return {"operation": {"choice": "TAP", "probabilities": {"TAP": 0.9}, "confidence": 0.8},
                    "tap_target": {"choice": "2", "probabilities": {"2": 0.85}, "confidence": 0.7}}

    client = Client()
    choice = JevJudge(client).next_step("open sound", {"foreground_app": "x", "elements": []},
                                        [{"action": "a", "screen_changed": True}] * 7,
                                        {"TAP": {"1": "[1] a", "2": "[2] b"}, "TYPE_TEXT": {}})
    assert (choice.operation, choice.probability, choice.confidence) == ("TAP", 0.9, 0.8)
    assert choice.targets == {"TAP": ("2", 0.85, 0.7)}
    state, questions = client.sent[0]
    assert len(client.sent) == 1 and set(questions) == {"operation", "tap_target"}
    assert "TYPE_TEXT" not in questions["operation"]["criteria"]  # no text field: not an option
    assert {"DONE", "ESCALATE", "LAUNCH_APP"} <= set(questions["operation"]["criteria"])
    assert len(state["recent_actions"]) == 5


def test_fast_step_events_name_no_service_publicly():
    from openmob.platform.service import public_event

    for message in ("Fast step skipped: Jev request failed: timeout", "tap_element: Tapped [1] 'Display'."):
        event = public_event({"seq": 1, "time": 1.0, "kind": "action", "message": message,
                              "data": {"timings": {"jev_ms": 140, "model_ms": 0, "total_ms": 500}, "jev": True, "fast": True}})
        text = str(event).lower()
        assert "jev" not in text and "typesafe" not in text and event["timings"]["classifier_ms"] == 140


def test_text_model_replies_are_validated():
    assert fast.parse_value('{"text": "hi"}', "text") == "hi"
    assert fast.parse_value('Sure! {"app": "YouTube"}', "app") == "YouTube"
    for bad in ("", "hi", '{"text": null}', '{"text": ""}', '{"text": 3}', '{"other": "x"}',
                '{"text": "' + "x" * 600 + '"}'):
        assert fast.parse_value(bad, "text") is None
