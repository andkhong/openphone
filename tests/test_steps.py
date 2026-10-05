"""Structured step logging and per-model success stats."""

import time

from conftest import FakeDriver, ScriptedClient, tool_use

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.history import History
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver
from openmob.runner import Coordinator
from openmob.voice.tts import Speaker


def wait_until(check, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def make(tmp_path, responses, elements=None, history=True):
    c = Coordinator(Speaker(enabled=False), history=History(tmp_path / "history.sqlite") if history else None)
    c.device_id = "phone-1"
    driver = FakeDriver(elements=elements, packages=["com.android.settings"])
    client = ScriptedClient(responses)
    cfg = Config(settle_delay=0, launch_settle_delay=0)
    c.agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, c, use_ocr=False)
    c.start()
    return c, client, driver


def finished(c, task_id):
    return (c.task(task_id) or {}).get("status") in ("succeeded", "failed", "cancelled")


# --- steps -------------------------------------------------------------------------


def test_steps_are_recorded_structured_and_redacted(tmp_path):
    el = UIElement(0, "Wi-Fi", "", "android:id/title", "TextView", True, None, (100, 200, 300, 260))
    c, _, _ = make(tmp_path, [
        [tool_use("tap_element", "a", index=0, target="Wi-Fi")],
        [tool_use("type_text", "b", text="hunter2")],
        [tool_use("swipe", "c", x1=270, y1=900, x2=270, y2=300)],
        [tool_use("done", "d", summary="Done.", success=True)],
    ], elements=[el])
    c.agent.driver.package = "com.android.settings"
    task = c.enqueue("connect to wifi", "dashboard")
    wait_until(lambda: finished(c, task))

    steps = c.history.steps(None, task)
    assert [s["tool"] for s in steps] == ["tap_element", "type_text", "swipe", "done"]
    assert [s["idx"] for s in steps] == [1, 2, 3, 4]
    tap = steps[0]
    assert tap["anchor"] == {"text": "Wi-Fi", "desc": "", "resource_id": "android:id/title", "class": "TextView",
                             "bounds": [100, 200, 300, 260], "index": 0}
    assert tap["norm_xy"] == [round(200 / 1080, 4), round(230 / 2400, 4)]
    assert tap["package"] == "com.android.settings" and (tap["screen_w"], tap["screen_h"]) == (1080, 2400)
    assert tap["args"] == {"index": 0, "target": "Wi-Fi"} and tap["is_error"] is False and tap["latency_ms"] >= 0
    typed = steps[1]
    assert typed["args"]["text"] == "<redacted>" and "hunter2" not in typed["outcome"]
    assert steps[2]["norm_xy"] == [0.5, 0.75, 0.5, 0.25]
    turn = c.history.turn(None, task)
    assert turn["package"] == "com.android.settings"

    # Deleting the chat deletes its steps; the per-model outcome (no content) stays.
    assert c.history.delete_chat(None, c.chat_id)
    assert c.history.steps(None, task) == []
    assert c.history.model_stats()[0]["tasks"] == 1


def test_steps_are_account_scoped(tmp_path):
    h = History(tmp_path / "h.sqlite")
    chat = h.create_chat("alice", "p", "Pixel", "hi")
    h.record(chat, {"kind": "heard", "message": "[dashboard] hi", "task": "t1", "time": 1})
    h.record_step(chat, "t1", {"idx": 1, "tool": "wait", "args": {"seconds": 1}, "is_error": False}, account_id="alice")
    assert len(h.steps("alice", "t1")) == 1 and h.steps("bob", "t1") == []


def test_old_history_databases_gain_the_package_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE turns (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, request TEXT NOT NULL, model TEXT, "
               "started REAL NOT NULL, finished REAL, success INTEGER, summary TEXT, steps INTEGER, usage TEXT, "
               "feedback INTEGER, feedback_note TEXT)")
    db.commit()
    db.close()
    h = History(path)
    assert "package" in {r["name"] for r in h._q("PRAGMA table_info(turns)")}


# --- per-model stats -------------------------------------------------------------


def test_model_stats_per_finished_task(tmp_path):
    h = History(tmp_path / "h.sqlite")
    h.record_model_run("alice", "t1", "claude-sonnet-5-5", True, 4)
    h.record_model_run("alice", "t2", "claude-sonnet-5-5", False, 10)
    h.record_model_run("alice", "t3", "gpt-5.5", True, 6)
    h.record_model_run("bob", "t4", "gpt-5.5", False, 2)
    assert h.model_stats("alice") == [
        {"model": "claude-sonnet-5-5", "tasks": 2, "succeeded": 1, "success_rate": 0.5, "avg_steps": 7.0},
        {"model": "gpt-5.5", "tasks": 1, "succeeded": 1, "success_rate": 1.0, "avg_steps": 6.0},
    ]
    assert [s["tasks"] for s in h.model_stats()] == [2, 2]


def test_coordinator_records_the_model_that_ran_each_task(tmp_path):
    c, _, _ = make(tmp_path, [[tool_use("done", "a", summary="ok", success=True)]])
    task = c.enqueue("check", "dashboard", options={"model": "claude-opus-5-5"})
    wait_until(lambda: finished(c, task))
    wait_until(lambda: c.history.model_stats())
    assert c.history.model_stats() == [{"model": "claude-opus-5-5", "tasks": 1, "succeeded": 1, "success_rate": 1.0, "avg_steps": 1.0}]
