import threading
import time

from fastapi.testclient import TestClient

from openmob.agent.loop import AgentResult
from openmob.runner import Coordinator, is_stop
from openmob.server import create_app
from openmob.voice.tts import Speaker


class SlowAgent:
    def __init__(self):
        self.started = threading.Event()
        self.runs = []

    def run(self, text, cancel, go_home=False):
        self.runs.append((text, go_home))
        self.started.set()
        cancel.wait(5)
        return AgentResult(False, "Stopped.", 1)


def coordinator():
    c = Coordinator(Speaker(enabled=False))
    c.agent = SlowAgent()
    return c


def test_is_stop():
    assert is_stop("Stop.") and is_stop("never mind") and not is_stop("stop the music app")


def test_commands_queue_run_and_stop():
    c = coordinator()
    c.start()
    assert c.submit("open youtube", "mic") == "queued"
    assert c.agent.started.wait(2)
    assert c.snapshot()["state"] == "running"
    assert c.submit("stop", "mic") == "stopped"
    deadline = time.time() + 2
    while c.snapshot()["state"] != "idle" and time.time() < deadline:
        time.sleep(0.01)
    assert c.snapshot()["state"] == "idle"
    assert c.agent.runs == [("open youtube", False)]


def test_answers_go_to_pending_question():
    c = coordinator()
    answer = {}
    t = threading.Thread(target=lambda: answer.setdefault("a", c.ask("Buy it?")))
    t.start()
    while c.snapshot()["state"] != "asking":
        time.sleep(0.01)
    assert c.submit("yes", "phone") == "answered"
    t.join(2)
    assert answer["a"] == "yes" and c.tasks.empty()


class FakeManager:
    def __init__(self, coordinator):
        from types import SimpleNamespace

        from conftest import FakeDriver

        self.session = SimpleNamespace(
            coordinator=coordinator,
            driver=FakeDriver(),
            describe=lambda: {"id": "abc", "model": "Samsung SM-S948U1", "connection": "wifi", "online": True},
        )
        self.sessions = {"abc": self.session}
        self.refreshed = 0
        self.history = None

    def refresh(self):
        self.refreshed += 1

    def get(self, device_id):
        return self.sessions.get(device_id)

    def sessions_for(self, account_id):
        return list(self.sessions.values())


def test_dashboard_api_requires_token_and_routes_commands():
    c = coordinator()
    manager = FakeManager(c)
    client = TestClient(create_app(manager, token="secret"))
    auth = {"X-OpenMob-Token": "secret"}
    assert client.get("/").status_code == 200
    assert client.get("/api/devices").status_code == 401
    assert client.get("/api/devices?rescan=true", headers=auth).json()[0]["model"] == "Samsung SM-S948U1"
    assert manager.refreshed == 1
    r = client.post("/api/devices/abc/command", json={"text": "open maps"}, headers=auth)
    assert r.json() == {"result": "queued"}
    assert c.tasks.get_nowait()[:2] == ("open maps", "dashboard")
    assert client.get("/api/devices/abc/status?t=secret").json()["state"] == "idle"
    assert client.get("/api/devices/nope/status", headers=auth).status_code == 404
    shot = client.get("/api/devices/abc/screen.jpg?t=secret")
    assert shot.headers["content-type"] == "image/jpeg" and shot.content[:2] == b"\xff\xd8"


def test_pairing_info_uses_public_address_and_requires_token():
    client = TestClient(create_app(FakeManager(coordinator()), token="se cret"))
    assert client.get("/connect").status_code == 200 and client.get("/pair").status_code == 200
    assert client.get("/api/pairing").status_code == 401
    p = client.get(
        "/api/pairing",
        headers={"X-OpenMob-Token": "se cret", "x-forwarded-proto": "https", "host": "abc.trycloudflare.com"},
    ).json()
    assert p["server"] == "https://abc.trycloudflare.com"
    assert p["pair_url"] == "https://abc.trycloudflare.com/pair?t=se%20cret"
    assert p["app_link"] == "openmob://connect?server=https%3A%2F%2Fabc.trycloudflare.com&t=se%20cret"
    assert p["local"] is False


class ScriptedAgent:
    """Runs instantly, logging one step; asks a question when told to."""

    def __init__(self, coordinator, ask=False):
        self.c = coordinator
        self.ask_question = ask

    def run(self, text, cancel, go_home=False):
        self.c.event("action", f"tap: did {text}")
        if self.ask_question:
            self.ask_question = False  # only the first task asks
            self.c.event("question", "Go ahead?")
            self.c.ask("Go ahead?")
        return AgentResult(True, f"done {text}", 1)


def wait_finals(c, count, timeout=3):
    """Wait until `count` tasks have logged their final outcome (no idle-looking race)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(1 for e in c.snapshot()["log"] if (e.get("data") or {}).get("final")) >= count:
            return
        time.sleep(0.01)
    raise AssertionError("tasks did not finish")


def test_events_are_grouped_by_task_for_the_chat():
    c = Coordinator(Speaker(enabled=False))
    c.agent = ScriptedAgent(c)
    c.start()
    c.submit("open maps", "dashboard")
    wait_finals(c, 1)
    c.submit("open mail", "dashboard")
    wait_finals(c, 2)
    log = c.snapshot()["log"]
    tasks = list(dict.fromkeys(e["task"] for e in log if e["task"] is not None))  # in order of arrival
    assert len(tasks) == 2
    first = [e for e in log if e["task"] == tasks[0]]
    assert [e["kind"] for e in first][0] == "heard" and "open maps" in first[0]["message"]
    final = [e for e in first if (e.get("data") or {}).get("final")]
    assert len(final) == 1 and final[0]["data"] == {"final": True, "success": True, "steps": 1}
    assert all(e["task"] != tasks[0] for e in log if "open mail" in e["message"])
    seqs = [e["seq"] for e in log]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_answers_join_the_running_task():
    c = Coordinator(Speaker(enabled=False))
    c.agent = ScriptedAgent(c, ask=True)
    c.start()
    c.submit("send hi", "dashboard")
    deadline = time.time() + 3
    while c.snapshot()["state"] != "asking" and time.time() < deadline:
        time.sleep(0.01)
    running = c.snapshot()["task_id"]
    # While the agent waits for an answer, whatever you send is that answer, in the same turn.
    assert c.submit("yes", "dashboard") == "answered"
    wait_finals(c, 1)
    answer = [e for e in c.snapshot()["log"] if e["kind"] == "heard" and e["message"].endswith("yes")][0]
    assert answer["task"] == running


def test_stop_cancels_queued_tasks_in_their_own_turns():
    c = coordinator()  # SlowAgent: runs until stopped
    c.start()
    c.submit("first", "dashboard")
    assert c.agent.started.wait(2)
    assert c.submit("second", "dashboard") == "queued"
    queued = [e for e in c.snapshot()["log"] if e["message"].endswith("second")][0]["task"]
    assert c.submit("stop", "dashboard") == "stopped"
    wait_finals(c, 2)  # the cancelled queued task and the stopped running one
    cancelled = [e for e in c.snapshot()["log"] if e["task"] == queued and (e.get("data") or {}).get("final")]
    assert cancelled and cancelled[0]["message"] == "Cancelled before it started."
