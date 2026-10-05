"""Per-step timing: what each phase covers, persistence, the report, and the Jev on/off harness."""

import sqlite3
import time
from pathlib import Path

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob import bench
from openmob.agent.loop import Agent
from openmob.agent.timing import StepTimer
from openmob.config import Config, load_config
from openmob.history import History
from openmob.jev import ScreenAssessment
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver
from openmob.platform.service import public_event
from openmob.runner import Coordinator
from openmob.timing_report import format_report, parse_since, percentile, summarize
from openmob.voice.tts import Speaker

TASKS = Path(__file__).parent / "fixtures" / "bench_tasks.yaml"
PHASE_KEYS = ("observe_ms", "jev_ms", "wait_ms", "model_ms", "action_ms", "settle_ms", "total_ms")


class SlowClient(ScriptedClient):
    """A model that takes `delay` seconds per call."""

    def __init__(self, responses, delay=0.03):
        super().__init__(responses)
        self.delay = delay

    def create(self, **kwargs):
        time.sleep(self.delay)
        return super().create(**kwargs)


class SlowJudge:
    """Stands in for Jev: answers after `delay` seconds; never says loading or risky."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.calls = 0

    def assess_screen(self, request, screen):
        self.calls += 1
        time.sleep(self.delay)
        return ScreenAssessment("app_content", 0.9, 0.9, 0.0)

    def assess_tap(self, request, target, screen):
        self.calls += 1
        time.sleep(self.delay)
        return 0.0, 0.0


class SlowAnswerIO(RecordingIO):
    """A person who takes `delay` seconds to answer."""

    def __init__(self, answers, delay=0.06):
        super().__init__(answers)
        self.delay = delay

    def ask(self, question):
        time.sleep(self.delay)
        return super().ask(question)


def wait_until(check, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def make_agent(responses, tmp_path, judge=None, io=None, driver=None, delay=0.03, **cfg):
    driver = driver or FakeDriver(packages=["com.android.settings", "com.google.android.youtube"])
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    return Agent(SlowClient(responses, delay), driver, resolver, config, io or RecordingIO(), judge=judge, use_ocr=False)


# --- the timer ---------------------------------------------------------------------


def test_nested_phases_are_exclusive():
    t = StepTimer()
    with t.phase("observe"):
        time.sleep(0.02)
        t.timed("jev", time.sleep, 0.03)
    with t.phase("action"):
        t.add("jev", 0.05)  # e.g. app picking reported by a hook
    step = t.take()
    assert 15 <= step["observe_ms"] < 30  # the classifier's 30 ms are not observe time
    assert step["jev_ms"] >= 80 and step["action_ms"] < 10
    assert step["total_ms"] >= step["observe_ms"] + 30
    assert t.take()["jev_ms"] == 0  # the next step starts from zero
    assert t.task_totals()["jev_ms"] >= 80


def test_a_failing_phase_still_counts_and_closes():
    t = StepTimer()
    with pytest.raises(RuntimeError):
        with t.phase("model"):
            time.sleep(0.01)
            raise RuntimeError("provider down")
    with t.phase("action"):
        pass
    step = t.take()
    assert step["model_ms"] >= 10 and step["action_ms"] < 10  # the stack was unwound


# --- in the agent ------------------------------------------------------------------


def test_phases_with_jev_on(tmp_path):
    send = UIElement(0, "Send", "", "", "Button", True, None, (100, 200, 300, 260))
    driver = FakeDriver(elements=[send])
    io = SlowAnswerIO(["yes"], delay=0.06)
    judge = SlowJudge(0.02)
    agent = make_agent([
        [tool_use("tap_element", "a", index=0, target="Send")],  # a risky tap: the classifier, then approval
        [tool_use("done", "b", summary="Sent.", success=True)],
    ], tmp_path, judge=judge, io=io, driver=driver)
    recorded = []
    io.record_step = recorded.append
    result = agent.run("open the chat")

    assert result.success and io.questions  # it asked to approve the Send tap
    tap, done = recorded
    assert tap["jev"] is True and tap["model"] == agent.task_model
    assert tap["model_ms"] >= 30  # the first model call
    assert tap["jev_ms"] >= 40  # assess_screen + assess_tap
    assert tap["wait_ms"] >= 60  # the approval, human time
    assert tap["action_ms"] < 60  # neither the approval nor the classifier counts as the phone action
    assert tap["latency_ms"] >= 60  # latency_ms keeps its meaning: the whole tool call, approval included
    assert sum(tap[k] for k in PHASE_KEYS[:-1]) <= tap["total_ms"] + 2
    assert done["jev_ms"] >= 20 and done["model_ms"] >= 30  # the next observation's classifier call, the next decision
    assert result.timings["wait_ms"] >= 60 and result.timings["total_ms"] >= tap["total_ms"] + done["total_ms"]


def test_phases_with_jev_off(tmp_path):
    agent = make_agent([
        [tool_use("launch_app", "a", app="settings")],
        [tool_use("wait", "b", seconds=0.5)],
        [tool_use("done", "c", summary="Done.", success=True)],
    ], tmp_path)
    io = agent.io
    recorded = []
    io.record_step = recorded.append
    agent.run("open settings")
    assert [s["jev"] for s in recorded] == [False] * 3
    assert all(s["jev_ms"] == 0 and s["wait_ms"] == 0 for s in recorded)
    # The wait tool's sleep is settle time, not the phone acting.
    assert recorded[1]["settle_ms"] >= 450 and recorded[1]["action_ms"] < 100


def test_app_picking_by_the_classifier_counts_as_jev(tmp_path):
    from openmob.jev import JevJudge

    class Client:
        on_usage = None

        def ask(self, state, questions):
            time.sleep(0.03)
            return {"app": {"choice": "none", "probabilities": {"none": 1.0}, "confidence": 1.0}}

    judge = JevJudge(Client())
    seen = []
    judge.on_timing = seen.append
    assert judge.pick_package("x", {"a.b": "a"})[0] is None and seen[0] >= 0.03
    agent = make_agent([], tmp_path, judge=judge)
    with agent.timer.phase("action"):
        judge.pick_package("x", {"a.b": "a"})
    step = agent.timer.take()
    assert step["jev_ms"] >= 30 and step["action_ms"] < 30


def test_timings_are_persisted_and_in_the_step_event(tmp_path):
    c = Coordinator(Speaker(enabled=False), history=History(tmp_path / "h.sqlite"))
    c.device_id = "phone-1"
    c.agent = make_agent([
        [tool_use("press_key", "a", key="HOME")],
        [tool_use("done", "b", summary="Done.", success=True)],
    ], tmp_path, judge=SlowJudge(0.01), io=c)
    c.start()
    task = c.enqueue("go home", "dashboard")
    wait_until(lambda: (c.task(task) or {}).get("status") in ("succeeded", "failed"))

    steps = c.history.steps(None, task)
    assert [s["tool"] for s in steps] == ["press_key", "done"]
    for s in steps:
        assert all(s[k] is not None for k in PHASE_KEYS) and s["jev"] is True and s["model"]
    assert steps[0]["jev_ms"] >= 10 and steps[0]["model_ms"] >= 30
    action = next(e for e in c.history.events(None, c.chat_id) if e["kind"] == "action")
    assert action["data"]["jev"] is True and action["data"]["timings"]["model_ms"] == steps[0]["model_ms"]


def test_public_events_carry_timings_without_service_names():
    entry = {"seq": 3, "time": 1.0, "kind": "action", "message": "tap: Tapped.",
             "data": {"timings": {"observe_ms": 5, "jev_ms": 7, "wait_ms": 0, "model_ms": 900, "action_ms": 40,
                                  "settle_ms": 1000, "total_ms": 1960}, "jev": True}}
    event = public_event(entry)
    assert event["timings"] == {"observe_ms": 5, "classifier_ms": 7, "wait_ms": 0, "model_ms": 900, "action_ms": 40,
                                "settle_ms": 1000, "total_ms": 1960, "classifier": True}
    assert "jev" not in str(event).lower()
    assert "timings" not in public_event({"seq": 4, "time": 1.0, "kind": "action", "message": "x"})


# --- persistence -------------------------------------------------------------------


def test_old_steps_tables_gain_the_timing_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE steps (turn_id TEXT NOT NULL, idx INTEGER NOT NULL, chat_id TEXT NOT NULL,
        account_id TEXT NOT NULL, device_id TEXT, t REAL NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
        norm_xy TEXT, package TEXT, screen_w INTEGER, screen_h INTEGER, anchor TEXT, outcome TEXT,
        is_error INTEGER NOT NULL, latency_ms INTEGER, source TEXT NOT NULL DEFAULT 'agent', PRIMARY KEY (turn_id, idx))""")
    db.execute("INSERT INTO steps (turn_id, idx, chat_id, account_id, t, tool, args, is_error, latency_ms) "
               "VALUES ('t0', 1, 'c', 'local', 1, 'tap', '{}', 0, 12)")
    db.commit()
    db.close()

    h = History(path)
    columns = {r["name"] for r in h._q("PRAGMA table_info(steps)")}
    assert set(PHASE_KEYS) | {"jev", "model"} <= columns
    old = h.steps(None, "t0")[0]
    assert old["latency_ms"] == 12 and old["total_ms"] is None and old["jev"] is None
    assert h.step_timings() == []  # untimed rows stay out of the report
    History(path)  # opening again is a no-op

    chat = h.create_chat(None, "p", "Pixel", "hi")
    h.record(chat, {"kind": "heard", "message": "hi", "task": "t1", "time": 2})
    h.record_step(chat, "t1", {"idx": 1, "tool": "tap", "is_error": False, "jev": False, "model": "m",
                               **dict.fromkeys(PHASE_KEYS, 5)})
    assert h.steps(None, "t1")[0]["jev"] is False
    assert h.step_timings()[0]["model_ms"] == 5


# --- the report --------------------------------------------------------------------


def row(turn, jev, model="m1", **ms):
    base = dict.fromkeys(PHASE_KEYS, 0)
    base.update(ms)
    return {"turn_id": turn, "idx": 1, "jev": jev, "model": model, **base}


def test_report_math():
    assert percentile([], 0.5) is None
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.9) == pytest.approx(91.0)
    assert parse_since("2d", now=1_000_000) == 1_000_000 - 2 * 86400
    assert parse_since("12h", now=100_000) == 100_000 - 43200 and parse_since("all") == 0
    with pytest.raises(ValueError):
        parse_since("soon")

    rows = [
        row("a", 1, model_ms=1000, jev_ms=100, observe_ms=200, wait_ms=5000, total_ms=6400),
        row("a", 1, model_ms=2000, jev_ms=300, observe_ms=200, total_ms=2600),
        row("b", 1, model_ms=3000, jev_ms=200, observe_ms=200, total_ms=3500),
        row("c", 0, model_ms=1500, jev_ms=None, observe_ms=100, total_ms=1700),
        row("d", 0, model="m2", model_ms=500, observe_ms=100, total_ms=600),
    ]
    groups = {(g["model"], g["jev"]): g for g in summarize(rows)}
    assert set(groups) == {("m1", True), ("m1", False), ("m2", False), ("(all models)", True), ("(all models)", False)}
    on = groups[("m1", True)]
    assert (on["tasks"], on["steps"]) == (2, 3)
    assert on["steps_per_task"]["mean"] == 1.5 and on["steps_per_task"]["median"] == 1.5
    assert on["phases"]["model_ms"]["median"] == 2000 and on["phases"]["model_ms"]["mean"] == 2000
    assert on["phases"]["jev_ms"]["p90"] == pytest.approx(280)
    # Agent time leaves out the 5 s human wait; other = total − every phase.
    assert on["phases"]["agent_ms"]["mean"] == pytest.approx((1400 + 2600 + 3500) / 3)
    assert on["phases"]["wait_ms"]["mean"] == pytest.approx(5000 / 3)
    assert on["phases"]["other_ms"]["mean"] == pytest.approx((100 + 100 + 100) / 3)
    assert groups[("m1", False)]["phases"]["jev_ms"]["mean"] == 0  # classifier off counts as 0, not missing
    assert groups[("(all models)", False)]["steps"] == 2

    text = format_report(summarize(rows))
    assert "m1 · jev on · 2 tasks · 3 steps" in text and "agent time" in text and "wait (human)" in text
    assert format_report([]).startswith("No timed steps")


def test_timings_command(tmp_path, monkeypatch, capsys):
    import openmob.history as history

    monkeypatch.setattr(history, "DEFAULT_PATH", tmp_path / "h.sqlite")
    monkeypatch.chdir(tmp_path)
    h = History(tmp_path / "h.sqlite")
    chat = h.create_chat(None, "p", "Pixel", "hi")
    h.record_step(chat, "t1", {"idx": 1, "tool": "tap", "is_error": False, "jev": True, "model": "m9",
                               **dict.fromkeys(PHASE_KEYS, 10)})
    from openmob.main import main

    monkeypatch.setenv("OPENMOB_JEV", "")
    main(["timings", "--since", "1d", "--model", "m9"])
    assert "m9 · jev on · 1 tasks · 1 steps" in capsys.readouterr().out


# --- forcing Jev off, and the comparison harness ---------------------------------


def test_jev_can_be_forced_off_without_unsetting_the_key(monkeypatch, tmp_path):
    from openmob.main import build_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("OPENMOB_JEV", raising=False)
    cfg = Config()
    cfg.api_key, cfg.jev_api_key = "a", "k"
    driver = FakeDriver()
    assert build_agent(cfg, RecordingIO(), driver=driver).judge is not None
    cfg.jev_enabled = False
    assert build_agent(cfg, RecordingIO(), driver=driver).judge is None

    (tmp_path / "c.yaml").write_text("model: claude-sonnet-5-5\n")
    assert load_config(tmp_path / "c.yaml").jev_enabled is True
    monkeypatch.setenv("OPENMOB_JEV", "off")
    loaded = load_config(tmp_path / "c.yaml")
    assert loaded.jev_enabled is False and loaded.jev_api_key == "k"


def test_harness_compares_jev_on_and_off_on_a_fake_phone(tmp_path):
    tasks = bench.load_tasks(TASKS)
    assert [t.name for t in tasks] == ["wifi settings", "youtube"] and tasks[0].max_steps == 8
    script = {
        "Open Settings and show the Wi-Fi page": [
            [tool_use("launch_app", "a", app="settings")],
            [tool_use("done", "b", summary="Wi-Fi page open.", success=True)],
        ],
        "Open YouTube": [[tool_use("launch_app", "a", app="youtube")],
                         [tool_use("done", "b", summary="Can't.", success=False)]],
    }
    driver = FakeDriver(packages=["com.android.settings", "com.google.android.youtube"])
    order = iter(t for t in tasks for _ in (True, False))
    made = []

    def make_agent(jev):
        responses = script[next(order).instruction]
        agent = make_agent_for_bench(responses, tmp_path, driver, SlowJudge(0.01) if jev else None)
        made.append((jev, agent))
        return agent

    runs = bench.run_bench(tasks, make_agent, credits_per_step=3)
    assert [(r.task.name, r.jev) for r in runs] == [("wifi settings", True), ("wifi settings", False),
                                                    ("youtube", True), ("youtube", False)]
    assert [r.success for r in runs] == [True, True, False, False]
    assert all(r.steps == 2 and r.credits == 6 for r in runs)
    assert all(r.timings["model_ms"] >= 60 for r in runs)
    on, off = runs[0], runs[1]
    assert on.timings["jev_ms"] >= 20 and off.timings["jev_ms"] == 0
    assert ("key", "HOME") in driver.actions  # each run starts from the home screen

    s = bench.summarize(runs)
    assert s[True]["succeeded"] == 1 and s[False]["credits"] == 12
    text = bench.format_comparison(runs)
    assert "jev on" in text and "jev off" in text and "Δ off−on" in text and "youtube" in text


def make_agent_for_bench(responses, tmp_path, driver, judge):
    return make_agent(responses, tmp_path, judge=judge, driver=driver, delay=0.03)


def test_harness_keeps_going_when_a_run_breaks(tmp_path):
    task = bench.BenchTask("anything", "t")

    def make_agent(jev):
        return make_agent_for_bench([], tmp_path, FakeDriver(), None)  # no responses: the model call fails

    runs = bench.run_bench([task], make_agent)
    assert len(runs) == 2 and all(r.error and not r.success for r in runs)
    assert "ERR" in bench.format_comparison(runs)
