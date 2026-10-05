"""Mid-task corrections (steer) and the dashboard command mode."""

import threading
import time

from conftest import FakeDriver, ScriptedClient, tool_use
from fastapi.testclient import TestClient

from openmob.agent.loop import Agent, AgentResult
from openmob.config import Config
from openmob.history import History
from openmob.phone.apps import AppResolver
from openmob.runner import Coordinator
from openmob.server import create_app
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


# --- steer -------------------------------------------------------------------------


class SlowAgent:
    def __init__(self):
        self.started = threading.Event()

    def run(self, text, cancel, go_home=False):
        self.started.set()
        cancel.wait(5)
        return AgentResult(False, "Stopped.", 1)


def test_while_a_task_runs_utterances_steer_it_unless_queued_or_stop():
    c = Coordinator(Speaker(enabled=False))
    c.agent = SlowAgent()
    assert c.steer("anything") is False  # nothing running
    c.start()
    assert c.submit("open gmail", "dashboard") == "queued"
    assert c.agent.started.wait(2)
    assert c.submit("no, the archive icon is the box", "phone") == "steered"
    assert c.take_corrections() == ["no, the archive icon is the box"]
    correction = [e for e in c.snapshot()["log"] if e["kind"] == "correction"][0]
    assert correction["message"] == "[phone] no, the archive icon is the box" and correction["task"] == c.current_task
    assert c.submit("then open maps", "dashboard", mode="queue") == "queued" and c.tasks.qsize() == 1
    assert c.submit("stop", "dashboard") == "stopped"


def test_a_pending_question_still_gets_the_answer_not_a_correction():
    c = Coordinator(Speaker(enabled=False))
    c.current_task = "t1"
    c.current = "buy it"
    answer = {}
    t = threading.Thread(target=lambda: answer.setdefault("a", c.ask("Buy it?")))
    t.start()
    wait_until(lambda: c.question is not None)
    assert c.steer("yes") is False
    assert c.submit("yes", "phone") == "answered"
    t.join(2)
    assert answer["a"] == "yes"


def test_the_agent_reads_corrections_at_its_next_step(tmp_path):
    c, client, _ = make(tmp_path, [[tool_use("wait", "a", seconds=0.5)],
                                   [tool_use("done", "b", summary="ok", success=True)]])
    original = client.create

    def create(**kwargs):
        response = original(**kwargs)
        if len(client.requests) == 1:
            assert c.steer("no, the other button", "dashboard")  # said while the model was thinking
        return response

    client.create = create
    task = c.enqueue("tap the button", "dashboard")
    wait_until(lambda: finished(c, task))
    assert c.task(task)["status"] == "succeeded"
    last_user = client.requests[1]["messages"][-1]["content"]
    assert last_user[-1] == {"type": "text", "text": "User correction: no, the other button"}
    # The correction is in the saved chat too.
    events = c.history.events(None, c.chat_id)
    assert any(e["kind"] == "correction" and e["message"].endswith("no, the other button") for e in events)


# --- dashboard API -----------------------------------------------------------------


class FakeManager:
    def __init__(self, coordinator, cfg):
        from types import SimpleNamespace

        self.cfg = cfg
        self.session = SimpleNamespace(coordinator=coordinator, driver=FakeDriver(),
                                       describe=lambda: {"id": "abc", "model": "Pixel", "online": True})
        self.history = None

    def get(self, device_id):
        return self.session if device_id == "abc" else None

    def sessions_for(self, account_id):
        return [self.session]


def test_command_mode_and_model_listing(tmp_path):
    c = Coordinator(Speaker(enabled=False))
    c.agent = SlowAgent()
    cfg = Config(models=[{"id": "claude-sonnet-5-5", "label": "Sonnet", "provider": "anthropic"},
                         {"id": "gpt-5.5", "label": "GPT", "provider": "openai"}])
    cfg.api_key = "sk-ant-test"
    app = TestClient(create_app(FakeManager(c, cfg), token="t"))
    auth = {"X-OpenMob-Token": "t"}

    assert app.get("/api/models", headers=auth).json() == {"default": "claude-sonnet-5-5", "models": [
        {"id": "claude-sonnet-5-5", "label": "Sonnet", "provider": "anthropic", "available": True},
        {"id": "gpt-5.5", "label": "GPT", "provider": "openai", "available": False},
    ]}

    c.start()
    assert app.post("/api/devices/abc/command", json={"text": "open gmail"}, headers=auth).json() == {"result": "queued"}
    assert c.agent.started.wait(2)
    assert app.post("/api/devices/abc/command", json={"text": "the other one"}, headers=auth).json() == {"result": "steered"}
    assert app.post("/api/devices/abc/command", json={"text": "then maps", "mode": "queue"}, headers=auth).json() == {"result": "queued"}
    assert app.post("/api/devices/abc/command", json={"text": "x", "mode": "later"}, headers=auth).status_code == 422
    c.stop()


def test_selecting_a_model_without_a_provider_key_is_refused(tmp_path):
    c, _, _ = make(tmp_path, [], history=False)
    cfg = Config(models=[{"id": "claude-sonnet-5-5", "label": "Sonnet"}, {"id": "gpt-5.5", "label": "GPT"}])
    c.agent.config = cfg
    app = TestClient(create_app(FakeManager(c, cfg), token="t"))
    auth = {"X-OpenMob-Token": "t"}
    r = app.post("/api/devices/abc/model", json={"model": "gpt-5.5"}, headers=auth)
    assert r.status_code == 400 and "isn't available" in r.json()["detail"]
    assert app.post("/api/devices/abc/model", json={"model": "claude-sonnet-5-5"}, headers=auth).status_code == 200
