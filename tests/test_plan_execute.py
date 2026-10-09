"""Plan with the model, execute with the classifier (plan_execute, off by default): one model call lays out
sub-goals with cues, the classifier takes each step toward the current sub-goal, every check still applies,
and the model takes over (told the plan and progress) whenever a screen doesn't fit. Fake phones, a fake
classifier and scripted clients only."""

import time

from conftest import RecordingIO, ScriptedClient, system_text, text_block, tool_use
from test_fast_path import DISPLAY, DONE, SETTINGS, Phone, el
from test_latency_round_d import FakeJev
from test_replay import Settling

from openmob.agent import plan, replay
from openmob.agent.loop import Agent
from openmob.config import Config, load_config
from openmob.jev import JevJudge
from openmob.permissions import TaskPermissions
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver


def title(text):
    return UIElement(0, text, "", "title", "TextView", False, None, (40, 120, 400, 200))


NAV_BAR = el(4, "Navigation bar", bounds=(0, 2200, 1000, 2380))  # a real screen height for the title check
MAIN = SETTINGS  # Network & internet, Display, Sound, Battery: tappable rows
DISPLAY_PAGE = [title("Display"), el(1, "Brightness level"), el(2, "Dark theme"), el(3, "Screen timeout"), NAV_BAR]
DARK_PAGE = [title("Dark theme"), el(1, "Use Dark theme"), el(2, "Schedule"), el(3, "Battery saver"), NAV_BAR]
SCREENS = {"Display": DISPLAY_PAGE, "Dark theme": DARK_PAGE}
TASK1 = "open display settings"
TASK2 = "open the display settings and go to dark theme"
ESCALATE = ("ESCALATE", None, 0.99)


def steps_for(*goals):
    return [tool_use("plan_steps", "p", steps=[{"goal": g, "cue": c} for g, c in goals])]


PLAN1 = steps_for(("Open the Display page", "Display"))
PLAN2 = steps_for(("Open the Display page", "Display"), ("Open Dark theme", "Dark theme"))


def make(responses, driver, jev, tmp_path, io=None, paths=None, **cfg):
    config = Config(**{"settle_delay": 0, "launch_settle_delay": 0, "fast_path": "on", "plan_execute": True, **cfg})
    client = ScriptedClient(responses)
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    agent = Agent(client, driver, resolver, config, io, judge=JevJudge(jev), use_ocr=False, paths=paths)
    return agent, client, steps


def taps(driver):
    return [a for a in driver.actions if a[0] == "tap"]


def asked(jev):
    return [state["user_request"] for state, _ in jev.sent]


def told(client, n):
    """What the model saw at its n-th call."""
    return str(client.requests[n]["messages"])


# --- the planning request ---------------------------------------------------------------------


class SlowClient(ScriptedClient):
    def create(self, **kwargs):
        time.sleep(0.02)
        return super().create(**kwargs)


def test_the_planning_request_is_one_dedicated_call_before_the_step_loop(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.1, 0.95])
    config = Config(settle_delay=0, launch_settle_delay=0, fast_path="on", plan_execute=True)
    client, io, steps = SlowClient([PLAN2]), RecordingIO(), []
    io.record_step = steps.append
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config, io,
                  judge=JevJudge(jev), use_ocr=False)
    result = agent.run(TASK2)
    assert result.success and result.planned == 2 and len(client.requests) == 1
    request = client.requests[0]
    assert [t["name"] for t in request["tools"]] == ["plan_steps"]  # its only tool; no forced choice
    assert "call plan_steps" in system_text(request) and "cue" in system_text(request)
    content = request["messages"][0]["content"]
    assert content[0]["text"] == f"Request: {TASK2}"
    assert any("SCREEN:" in b.get("text", "") for b in content) and any(b.get("type") == "image" for b in content)
    planned = steps[0]
    assert planned["tool"] == "plan_steps" and planned["source"] == "plan" and not planned["is_error"]
    assert planned["fast"] is False and planned["model_ms"] >= 15  # the call's own model time
    assert ("status", "Plan: 2 sub-goals; a quick helper carries them out.") in io.events


def test_a_reply_without_a_plan_carries_on_as_usual(tmp_path):
    driver = Phone(MAIN, SCREENS)
    agent, client, steps = make([[text_block("Nothing to plan here.")],
                                 [tool_use("tap_element", "a", index=1, target="Display")], DONE],
                                driver, FakeJev([ESCALATE]), tmp_path)
    result = agent.run(TASK1)
    assert result.success and result.planned == 0 and len(client.requests) == 3
    assert steps[0]["tool"] == "plan_steps" and steps[0]["source"] == "plan" and steps[0]["is_error"]
    assert steps[0]["outcome"] == plan.NO_PLAN and ("thought", "Nothing to plan here.") in agent.io.events
    assert len(client.requests[1]["tools"]) > 1  # the first vision step: every tool, and not a second planning call
    assert "source" not in steps[1] and steps[1]["tool"] == "tap_element"


def test_a_failed_planning_call_carries_on_as_usual(tmp_path):
    class FailingFirst(ScriptedClient):
        def create(self, **kwargs):
            if not self.requests:
                self.requests.append(kwargs)
                raise RuntimeError("provider down")
            return super().create(**kwargs)

    driver = Phone(MAIN, SCREENS)
    config = Config(settle_delay=0, launch_settle_delay=0, fast_path="on", plan_execute=True)
    client, io, steps = FailingFirst([[tool_use("tap_element", "a", index=1, target="Display")], DONE]), RecordingIO(), []
    io.record_step = steps.append
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config, io,
                  judge=JevJudge(FakeJev([ESCALATE])), use_ocr=False)
    result = agent.run(TASK1)
    assert result.success and result.planned == 0 and len(client.requests) == 3
    assert steps[0]["tool"] == "plan_steps" and steps[0]["is_error"] and steps[0]["outcome"] == plan.NO_PLAN
    assert any(kind == "error" and "No plan" in message for kind, message in io.events)


# --- the plan runs ----------------------------------------------------------------------------


def test_the_plan_runs_with_one_model_call_on_a_multi_screen_flow(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.1, 0.95])
    agent, client, steps = make([PLAN2], driver, jev, tmp_path)
    result = agent.run(TASK2)
    assert result.success and result.done_via == "classifier" and len(client.requests) == 1  # the plan call only
    assert result.planned == 2 and result.fast_steps == 3
    assert taps(driver) == [("tap", 500, 140), ("tap", 500, 240)]  # the elements' own centers
    assert [s["tool"] for s in steps] == ["plan_steps", "tap_element", "tap_element", "done"]
    assert [s.get("source") for s in steps] == ["plan", "plan", "plan", None]
    assert steps[1]["fast"] is True and steps[1]["model_ms"] == 0 and steps[1]["variant"].endswith("+plan+links")
    # The classifier was asked about the sub-goal in hand, not the whole request, while the helper worked.
    requests = asked(jev)
    assert requests[0] == TASK2  # the first look, before any plan
    assert requests[1].startswith("Open the Display page") and "'Display'" in requests[1]
    assert requests[2].startswith("Open Dark theme")
    assert requests[3] == TASK2  # the plan was complete: the usual path finished the task
    first = client.requests[0]
    assert "plan_steps" in {t["name"] for t in first["tools"]} and "plan_steps" in system_text(first)


def test_a_cue_that_never_appears_hands_over_with_the_plan(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95)], done=0.1)
    agent, client, steps = make([steps_for(("Open the Display page", "Wallpaper")), DONE], driver, jev, tmp_path,
                                plan_max_steps_per_goal=1)
    result = agent.run(TASK2)
    assert result.success and result.planned == 1 and len(client.requests) == 2
    note = told(client, 1)
    assert "Plan so far" in note and "-> 1. Open the Display page (cue 'Wallpaper')" in note
    assert "didn't appear after 1 step" in note and "call plan_steps again" in note


def test_an_unsure_classifier_hands_over(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.4), ESCALATE])
    agent, client, steps = make([PLAN1, [tool_use("tap_element", "a", index=1, target="Display")], DONE],
                                driver, jev, tmp_path)
    result = agent.run(TASK1)
    assert result.success and result.planned == 0 and len(client.requests) == 3
    assert "wasn't sure" in told(client, 1) and "source" not in steps[1]  # the model's tap


def test_progress_by_the_model_hands_the_plan_back_to_the_helper(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.4), ("TAP", 2, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.1, 0.95])
    agent, client, steps = make([PLAN2, [tool_use("tap_element", "a", index=1, target="Display")], DONE],
                                driver, jev, tmp_path)
    result = agent.run(TASK2)
    assert result.success and result.planned == 1 and len(client.requests) == 3
    assert [s["tool"] for s in steps] == ["plan_steps", "tap_element", "tap_element", "done"]
    assert "source" not in steps[1] and steps[2]["source"] == "plan"  # the model opened Display; the helper went on
    assert taps(driver) == [("tap", 500, 140), ("tap", 500, 240)]


def test_the_model_can_re_plan_after_a_handover(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95), ("TAP", 2, 0.95), ("DONE", None, 0.95)],
                  done=[0.1, 0.1, 0.1, 0.1, 0.95])
    agent, client, steps = make([steps_for(("Open the Display page", "Wallpaper")),
                                 steps_for(("Open Dark theme", "Dark theme"))], driver, jev, tmp_path,
                                plan_max_steps_per_goal=1)
    result = agent.run(TASK2)
    assert result.success and result.planned == 2 and len(client.requests) == 2
    assert [s["tool"] for s in steps] == ["plan_steps", "tap_element", "plan_steps", "tap_element", "done"]
    assert result.done_via == "classifier"  # replies that only planned aren't escalations


def test_a_refused_tap_gets_one_more_look_without_the_model(tmp_path):
    moved = [el(0, "Promo"), el(1, "Network & internet"), el(2, "Display"), el(3, "Sound"), el(4, "Battery")]
    settled = [el(i, text, bounds=(0, 100 * i + 100, 1000, 100 * i + 180))
               for i, text in enumerate(["Promo", "Network & internet", "Display", "Sound", "Battery"])]
    # Reads: the screen decided on, the re-check (moved), the quiet check (moved again: refused), the next looks.
    driver = Settling(SETTINGS, moved, settled, settled, settled)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.1, 0.95])
    agent, client, steps = make([PLAN1, DONE], driver, jev, tmp_path)
    result = agent.run(TASK1)
    assert result.success and result.planned == 2 and len(client.requests) == 2  # the plan, then the done
    _, refused, tapped, _ = steps
    assert refused["source"] == "plan" and refused["is_error"] and replay.NOT_RUN in refused["outcome"]
    assert tapped["source"] == "plan" and not tapped["is_error"] and taps(driver) == [("tap", 500, 340)]


# --- every check still applies ------------------------------------------------------------------


def test_a_risky_tap_still_asks_and_a_no_stands(tmp_path):
    trash = [el(0, "Inbox"), el(1, "Delete all", cls="Button"), el(2, "Spam"), el(3, "Drafts")]
    driver = Phone(trash)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.99)])
    io = RecordingIO(answers=["no"])
    agent, client, steps = make([steps_for(("Empty the trash", "No messages")),
                                 [tool_use("done", "d", summary="Left it.", success=False)]], driver, jev, tmp_path, io=io)
    result = agent.run("empty the trash")
    assert io.questions and "Delete all" in io.questions[0] and not taps(driver)
    assert steps[1]["source"] == "plan" and steps[1]["is_error"] and "did not approve" in steps[1]["outcome"]
    assert result.planned == 1 and len(client.requests) == 2 and "the last step failed" in told(client, 1)


def test_ask_mode_asks_before_each_step_the_helper_takes(tmp_path):
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.95])
    io = RecordingIO(answers=["yes"])
    agent, client, _ = make([PLAN1], driver, jev, tmp_path, io=io)
    result = agent.run(TASK1, permissions=TaskPermissions("ask"))
    assert io.questions == ["Next step: tap 'Display'. Should I go ahead?"]
    assert result.success and result.planned == 1 and result.prompts == 1 and taps(driver) == [("tap", 500, 140)]


def test_typing_goes_to_the_model(tmp_path):
    search = [el(0, "Search settings", cls="EditText", editable=True), el(1, "Display"), el(2, "Sound"), el(3, "Battery")]
    driver = Phone(search, SCREENS)
    jev = FakeJev([ESCALATE, ("TYPE_TEXT", 0, 0.95), ESCALATE])
    agent, client, steps = make([steps_for(("Search settings for dark", "Dark theme")),
                                 [tool_use("tap_element", "a", index=0, target="Search")],
                                 [tool_use("type_text", "b", text="dark")], DONE], driver, jev, tmp_path)
    result = agent.run("search settings for dark theme")
    assert result.success and result.planned == 0 and ("type", "dark") in driver.actions
    assert [s["tool"] for s in steps] == ["plan_steps", "tap_element", "type_text", "done"]
    assert "source" not in steps[2] and "the next step is typing" in told(client, 1)


def test_a_popup_or_a_correction_hands_over(tmp_path):
    class PopupJev(FakeJev):
        def ask(self, state, questions):
            answers = super().ask(state, questions)
            if "state" in answers:
                answers["state"] = {"choice": "popup", "probabilities": {"popup": 0.9}, "confidence": 0.9}
            return answers

    driver = Phone(MAIN, SCREENS)
    agent, client, _ = make([PLAN1, DONE], driver, PopupJev([ESCALATE, ("TAP", 1, 0.95)]), tmp_path)
    result = agent.run(TASK1)
    assert result.planned == 0 and len(client.requests) == 2 and "a popup screen came up" in told(client, 1)

    class SteeringIO(RecordingIO):
        def __init__(self):
            super().__init__()
            self.pending = ["no, the sound settings"]

        def take_corrections(self):
            out, self.pending = self.pending, []
            return out

    driver = Phone(MAIN, SCREENS)
    agent, client, _ = make([PLAN1, DONE], driver, FakeJev([ESCALATE, ("TAP", 1, 0.95)]), tmp_path, io=SteeringIO())
    result = agent.run(TASK1)
    assert result.planned == 0 and len(client.requests) == 2 and "corrected the request" in told(client, 1)


# --- the switch and the plan's shape --------------------------------------------------------------


def test_nothing_changes_when_the_switch_is_off(tmp_path):
    assert Config().plan_execute is False
    driver = Phone(MAIN, SCREENS)
    agent, client, steps = make([PLAN1, [tool_use("tap_element", "a", index=1, target="Display")], DONE],
                                driver, FakeJev([ESCALATE]), tmp_path, plan_execute=False)
    result = agent.run(TASK1)
    first = client.requests[0]
    assert "plan_steps" not in {t["name"] for t in first["tools"]} and "plan_steps" not in system_text(first)
    assert steps[0]["is_error"] and "Unknown tool" in steps[0]["outcome"]  # not offered: not run
    assert result.planned == 0 and "+plan" not in steps[1]["variant"] and len(client.requests) == 3
    assert asked(FakeJev([ESCALATE])) == [] and all(r == TASK1 for r in asked(agent.judge.client))


def test_the_environment_can_turn_the_switch_on_or_off(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: claude-sonnet-5-5\n")
    monkeypatch.delenv("OPENMOB_PLAN_EXECUTE", raising=False)
    assert load_config(cfg_file).plan_execute is False
    monkeypatch.setenv("OPENMOB_PLAN_EXECUTE", "on")
    assert load_config(cfg_file).plan_execute is True
    cfg_file.write_text("plan_execute: true\n")
    monkeypatch.setenv("OPENMOB_PLAN_EXECUTE", "off")
    assert load_config(cfg_file).plan_execute is False


def test_a_plan_is_validated_data(tmp_path):
    goals = plan.parse({"steps": [{"goal": "  Open   Settings ", "cue": "Network & internet"}]})
    assert goals == [plan.Goal("Open Settings", "Network & internet")]
    assert len(plan.parse({"steps": [{"goal": "x" * 500, "cue": "y"}]})[0].text) == plan.MAX_CHARS
    bad = [None, {}, {"steps": []}, {"steps": [{"goal": "a", "cue": "b"}] * 9}, {"steps": "tap"},
           {"steps": [{"goal": "a"}]}, {"steps": [{"goal": "a", "cue": ""}]}, {"steps": [{"goal": 3, "cue": "b"}]},
           {"steps": ["open settings"]}]
    for args in bad:
        try:
            plan.parse(args)
        except ValueError:
            continue
        raise AssertionError(f"accepted {args!r}")
    driver = Phone(MAIN, SCREENS)
    agent, client, steps = make([[tool_use("plan_steps", "p", steps=[])],
                                 [tool_use("tap_element", "a", index=1, target="Display")], DONE],
                                driver, FakeJev([ESCALATE]), tmp_path)
    result = agent.run(TASK1)
    assert steps[0]["is_error"] and "1 to 8 steps" in steps[0]["outcome"] and result.planned == 0


def test_the_progress_note_and_the_classifiers_request():
    p = plan.Plan(plan.parse({"steps": [{"goal": "Open Settings", "cue": "Connections"},
                                        {"goal": "Open Display", "cue": "Brightness"}]}))
    assert p.request() == "Open Settings (once done, the screen shows 'Connections')"
    p.advance()
    p.suspended = "the helper wasn't sure what to do on this screen"
    note = p.note()
    assert "[x] 1. Open Settings (cue 'Connections')" in note and "-> 2. Open Display (cue 'Brightness')" in note
    assert "wasn't sure" in note and "plan_steps" in note
    p.advance()
    assert p.complete() and p.request() == ""


def test_a_cue_is_the_place_itself_not_the_way_there():
    assert plan.cue_visible(DISPLAY_PAGE, "Display")  # the page's title
    assert not plan.cue_visible(MAIN, "Display")  # a tappable row named after it
    assert not plan.cue_visible(DISPLAY_PAGE, "Dark theme") and plan.cue_visible(DARK_PAGE, "dark theme")
    caption = UIElement(5, "Brightness level", "", "", "TextView", False, None, (40, 1500, 400, 1560))
    assert plan.cue_visible(DISPLAY_PAGE + [caption], "Brightness")  # a non-tappable label anywhere, whole words
    row = UIElement(5, "", "", "", "LinearLayout", True, None, (0, 1480, 1000, 1580))  # the same label inside a row
    assert not plan.cue_visible([row, caption] + MAIN, "Brightness")
    assert not plan.cue_visible([UIElement(0, "Brightness", "", "", "OCRText", True, None, (0, 0, 100, 50))], "Brightness")
    tab = UIElement(0, "Library", "", "bottom_nav_library", "TabView", True, True, (0, 2200, 300, 2380))
    assert plan.cue_visible([tab] + MAIN, "Library")  # a selected tab


# --- with the replay cache ----------------------------------------------------------------------


def test_a_cold_run_plans_and_stores_a_path_and_a_warm_run_replays_it(tmp_path):
    store = replay.PathStore()
    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ("TAP", 1, 0.95), ("TAP", 2, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.1, 0.95])
    agent, client, _ = make([PLAN2], driver, jev, tmp_path, paths=store, replay_paths=True)
    cold = agent.run(TASK2)
    assert cold.success and cold.planned == 2 and len(client.requests) == 1
    path = store.get(replay.goal_key(TASK2))
    assert [s["target"]["text"] for s in path] == ["Display", "Dark theme"]  # the plan call itself isn't a step

    driver = Phone(MAIN, SCREENS)
    jev = FakeJev([ESCALATE, ESCALATE, ("DONE", None, 0.95)], done=[0.1, 0.1, 0.95])
    agent, client, steps = make([], driver, jev, tmp_path, paths=store, replay_paths=True)  # no model reply to give
    warm = agent.run(TASK2)
    assert warm.success and warm.done_via == "classifier" and len(client.requests) == 0
    assert warm.replayed == 2 and warm.planned == 0 and taps(driver) == [("tap", 500, 140), ("tap", 500, 240)]
    assert [s.get("source") for s in steps] == ["replay", "replay", None]
    assert steps[0]["variant"].endswith("+replay+plan+links")
