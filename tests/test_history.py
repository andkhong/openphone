"""Saved chats: recording, reopening, feedback, and account isolation."""

import time

import httpx
from conftest import FakeDriver
from test_hosted import hosted, pair, signup  # noqa: F401
from test_remote import FakePhone, wait_for
from test_runner_server import ScriptedAgent, wait_finals

from openmob.history import History
from openmob.runner import Coordinator
from openmob.voice.tts import Speaker


def coordinator(tmp_path, account="acct-1"):
    c = Coordinator(Speaker(enabled=False), history=History(tmp_path / "h.sqlite"))
    c.account_id, c.device_id, c.device_model = account, "app-phone", "Pixel"
    c.agent = ScriptedAgent(c)
    c.agent.recent = []  # stands in for the agent's memory of earlier requests
    c.start()
    return c


def test_tasks_are_saved_as_structured_turns(tmp_path):
    c = coordinator(tmp_path)
    c.submit("open maps", "dashboard")
    wait_finals(c, 1)
    c.submit("open mail", "dashboard")
    wait_finals(c, 2)
    chats = c.history.list_chats("acct-1")
    assert len(chats) == 1 and chats[0]["title"] == "open maps" and chats[0]["turns"] == 2
    turns = c.history.turns("acct-1", chats[0]["id"])
    assert [t["request"] for t in turns] == ["open maps", "open mail"]
    assert turns[0]["success"] == 1 and turns[0]["summary"] == "done open maps" and turns[0]["steps"] == 1
    events = c.history.events("acct-1", chats[0]["id"])
    kinds = [e["kind"] for e in events if e["task"] == turns[0]["id"]]
    assert kinds[0] == "heard" and "action" in kinds and "result" in kinds


def test_new_chat_starts_a_new_saved_chat_and_old_ones_reopen(tmp_path):
    c = coordinator(tmp_path)
    c.submit("open maps", "dashboard")
    wait_finals(c, 1)
    first = c.chat_id
    c.clear_log()  # New chat
    assert c.chat_id is None and not c.log
    c.submit("open mail", "dashboard")
    deadline = time.time() + 3
    while len(c.history.list_chats("acct-1")) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert c.chat_id != first and len(c.history.list_chats("acct-1")) == 2
    wait_finals(c, 1)

    assert c.open_chat(first)
    assert c.chat_id == first and any("open maps" in e["message"] for e in c.log)
    assert list(c.agent.recent) == [("open maps", "done open maps")]  # memory restored
    assert not c.open_chat("not-a-chat")


def test_history_survives_a_restart_and_is_owner_scoped(tmp_path):
    c = coordinator(tmp_path)
    c.submit("open maps", "dashboard")
    wait_finals(c, 1)
    reopened = History(tmp_path / "h.sqlite")
    assert reopened.list_chats("acct-1")[0]["title"] == "open maps"
    assert reopened.list_chats("someone-else") == []
    assert reopened.events("someone-else", c.chat_id) is None
    turn = reopened.turns("acct-1", c.chat_id)[0]["id"]
    assert not reopened.set_feedback("someone-else", turn, 1)
    assert reopened.set_feedback("acct-1", turn, -1, "tapped the wrong button")
    t = reopened.turns("acct-1", c.chat_id)[0]
    assert (t["feedback"], t["feedback_note"]) == (-1, "tapped the wrong button")
    assert reopened.delete_chat("acct-1", c.chat_id) and reopened.list_chats("acct-1") == []
    assert reopened.events("acct-1", c.chat_id) is None


def test_chat_api_is_owner_only(hosted):  # noqa: F811
    base, manager, _ = hosted
    ws = base.replace("http", "ws")
    alice, alice_session = signup(base, "alice@example.com")
    bob, bob_session = signup(base, "bob@example.com")
    alice_phone = FakePhone(ws, FakeDriver(), device_id="a1", key=pair(base, alice, alice_session, "a1"))
    bob_phone = FakePhone(ws, FakeDriver(), device_id="b1", key=pair(base, bob, bob_session, "b1"))
    assert alice_phone.welcome.wait(5) and bob_phone.welcome.wait(5)

    alice.post("/api/devices/app-a1/command", json={"text": "open maps"})
    assert wait_for(lambda: alice.get("/api/chats").json() and alice.get("/api/chats").json()[0]["turns"] >= 1)
    chat = alice.get("/api/chats").json()[0]
    assert chat["title"] == "open maps" and chat["device_id"] == "app-a1"
    assert wait_for(lambda: alice.get(f"/api/chats/{chat['id']}").json()["turns"][0]["finished"])
    turn = alice.get(f"/api/chats/{chat['id']}").json()["turns"][0]["id"]

    # Bob can't see, read, open, rename, delete or rate Alice's chat.
    assert bob.get("/api/chats").json() == []
    assert bob.get(f"/api/chats/{chat['id']}").status_code == 404
    assert bob.post("/api/devices/app-b1/chat/open", json={"chat_id": chat["id"]}).status_code == 404
    assert bob.patch(f"/api/chats/{chat['id']}", json={"title": "mine"}).status_code == 404
    assert bob.delete(f"/api/chats/{chat['id']}").status_code == 404
    assert bob.post(f"/api/turns/{turn}/feedback", json={"value": 1}).status_code == 404
    assert httpx.get(f"{base}/api/chats").status_code == 401

    # Alice: feedback, rename, new chat, reopen on the same phone, delete.
    assert alice.post(f"/api/turns/{turn}/feedback", json={"value": 1}).json() == {"ok": True}
    assert alice.post(f"/api/turns/{turn}/feedback", json={"value": 5}).status_code == 400
    assert alice.patch(f"/api/chats/{chat['id']}", json={"title": "Maps run"}).json() == {"ok": True}
    assert alice.post("/api/devices/app-a1/chat/new").json() == {"ok": True}
    assert alice.get("/api/devices/app-a1/status").json()["log"] == []
    assert alice.post("/api/devices/app-a1/chat/open", json={"chat_id": chat["id"]}).json()["ok"]
    status = alice.get("/api/devices/app-a1/status").json()
    assert status["chat_id"] == chat["id"] and status["feedback"] == {turn: 1}
    assert any("open maps" in e["message"] for e in status["log"])
    assert alice.get("/api/chats").json()[0]["title"] == "Maps run"
    assert alice.delete(f"/api/chats/{chat['id']}").json() == {"ok": True}
    assert alice.get("/api/chats").json() == []
    alice_phone.disconnect()
    bob_phone.disconnect()
