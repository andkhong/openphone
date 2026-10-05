"""Memory over HTTP: the dashboard's "What I've learned" endpoints and /v1/memory, each account
seeing only its own notes; API tasks use the owner's notes unless they ask for memory off."""

import httpx
import pytest
from conftest import FakeDriver, ScriptedClient, tool_use
from test_hosted import _up, pair, signup
from test_remote import FakePhone, free_port, wait_for

from openmob.accounts import AccountStore
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.history import History
from openmob.ledger import Ledger
from openmob.memory import Memory
from openmob.phone.apps import AppResolver
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker


@pytest.fixture
def server(tmp_path):
    def build_agent(cfg, io, driver, label_cache):
        client = ScriptedClient([[tool_use("done", "a", summary="ok", success=True)]] * 10)
        return Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, io, use_ocr=False)

    store = AccountStore(tmp_path / "accounts.json")
    memory = Memory(tmp_path / "memory.sqlite")
    manager = DeviceManager(Config(settle_delay=0, launch_settle_delay=0), build_agent, Speaker(enabled=False),
                            adb_discovery=False, history=History(tmp_path / "h.sqlite"), memory=memory)
    port = free_port()
    serve_in_background(create_app(manager, store=store, ledger=Ledger(tmp_path / "ledger.sqlite")), "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    assert wait_for(lambda: _up(base))
    alice, alice_session = signup(base, "alice@example.com")
    bob, _ = signup(base, "bob@example.com")
    yield Server(base, manager, memory, alice, alice_session, bob, store)


class Server(tuple):
    """The fixture's values, unpackable as before, plus the account store."""

    def __new__(cls, *values):
        self = super().__new__(cls, values[:6])
        self.store = values[6]
        return self


def test_dashboard_notes_crud_and_isolation(server):
    base, _, _, alice, _, bob = server
    note = alice.post("/api/memory", json={"text": "I prefer dark mode"}).json()
    app_note = alice.post("/api/memory", json={"text": "Library is bottom right", "package": "com.google.android.youtube"}).json()
    assert note["status"] == "active" and app_note["package"] == "com.google.android.youtube"
    assert [n["text"] for n in alice.get("/api/memory").json()["notes"]] == ["I prefer dark mode", "Library is bottom right"]
    assert bob.get("/api/memory").json()["notes"] == []
    for method, path, body in [("PATCH", f"/api/memory/{note['id']}", {"text": "x"}), ("DELETE", f"/api/memory/{note['id']}", None)]:
        assert bob.request(method, path, json=body).status_code == 404  # someone else's: doesn't exist
    assert alice.patch(f"/api/memory/{note['id']}", json={"text": "I prefer light mode"}).json()["text"] == "I prefer light mode"
    assert alice.post("/api/memory", json={"text": "   "}).status_code == 400
    assert httpx.get(f"{base}/api/memory").status_code == 401
    assert alice.delete("/api/memory").status_code == 400  # needs ?confirm=true
    assert alice.delete("/api/memory?confirm=true").json() == {"deleted": 2}


def test_approving_and_dismissing_proposals(server):
    _, _, memory, alice, _, _ = server
    alice_id = server.store.account_id_for("alice@example.com")
    proposal = memory.add(alice_id, "Likes lo-fi music", status="proposed", source="agent")
    assert alice.patch(f"/api/memory/{proposal['id']}", json={"status": "active"}).json()["status"] == "active"
    other = memory.add(alice_id, "Wrong guess", status="proposed", source="agent")
    assert alice.delete(f"/api/memory/{other['id']}").json() == {"ok": True}  # dismissed
    assert [n["text"] for n in alice.get("/api/memory").json()["notes"]] == ["Likes lo-fi music"]


def test_v1_memory_scopes_and_isolation(server):
    base, _, _, alice, _, bob = server
    read_only = alice.post("/api/developer/keys", json={"scopes": ["memory:read"]}).json()["key"]
    writer = alice.post("/api/developer/keys", json={"scopes": ["memory:read", "memory:write"]}).json()["key"]
    bob_key = bob.post("/api/developer/keys", json={"scopes": ["memory:read", "memory:write"]}).json()["key"]
    as_ = lambda key: httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}"})  # noqa: E731
    assert as_(read_only).post("/v1/memory", json={"text": "x"}).status_code == 403
    note = as_(writer).post("/v1/memory", json={"text": "Use my second game account"}).json()
    assert note["status"] == "active"
    assert [n["text"] for n in as_(read_only).get("/v1/memory").json()["notes"]] == ["Use my second game account"]
    assert as_(bob_key).get("/v1/memory").json()["notes"] == []
    assert as_(bob_key).delete(f"/v1/memory/{note['id']}").status_code == 404
    default_key = alice.post("/api/developer/keys", json={}).json()["key"]  # memory scopes are opt-in
    assert as_(default_key).get("/v1/memory").status_code == 403
    assert as_(writer).delete(f"/v1/memory/{note['id']}").json() == {"ok": True}


def test_api_tasks_use_the_owners_notes_unless_memory_is_off(server):
    base, manager, memory, alice, session, _ = server
    key = pair(base, alice, session, device_id="alice-phone")
    phone = FakePhone(base.replace("http", "ws"), FakeDriver(), device_id="alice-phone", key=key)
    assert phone.welcome.wait(5)
    alice.post("/api/memory", json={"text": "Always use my second game account"})
    api_key = alice.post("/api/developer/keys", json={}).json()["key"]
    v1 = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {api_key}"})
    client = manager.get("app-alice-phone").coordinator.agent.client

    for mode, expected in (("on", True), ("off", False)):
        task = v1.post("/v1/devices/app-alice-phone/tasks", json={"instruction": "open the game", "memory": mode}).json()
        assert wait_for(lambda: v1.get(f"/v1/tasks/{task['id']}").json()["status"] == "succeeded")
        assert ("Always use my second game account" in client.requests[-1]["system"]) is expected
    assert v1.post("/v1/devices/app-alice-phone/tasks", json={"instruction": "x", "memory": "sometimes"}).status_code == 422
    phone.disconnect()
