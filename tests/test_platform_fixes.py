"""Fixes from the docs review: per-key isolation, key rotation, account-wide caps, error codes,
webhook secret rotation and dead deliveries, pagination, unpairing mid-task, retention, input checks.

Alice has a phone (A) and a tablet (B), Bob one phone. Waits are on conditions, never fixed sleeps.
"""

import asyncio
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest
from test_orchestrator import A, B, orc, start, wait_status  # noqa: F401  (orc: fixture)
from test_remote import wait_for

from openmob.config import Config
from openmob.history import History
from openmob.ledger import Ledger, day_start
from openmob.platform.webhooks import WebhookOutbox
from openmob.server import purge_task_text


def client(orc, key):
    return httpx.Client(base_url=orc.base, headers={"Authorization": f"Bearer {key}"}, timeout=10)


def second_key(orc, user, scopes=("devices:read", "tasks:run", "tasks:read", "history:read", "screen:read")):
    key = user.console.post("/api/developer/keys", json={"name": "second", "scopes": list(scopes)}).json()
    return key["id"], client(orc, key["key"])


def run(c, device, instruction, **extra):
    r = c.post(f"/v1/devices/{device}/tasks", json={"instruction": instruction, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def wait_task(c, task_id, *statuses):
    last = {}

    def reached():
        last["t"] = c.get(f"/v1/tasks/{task_id}").json()
        return last["t"].get("status") in statuses

    assert wait_for(reached, timeout=20), last.get("t")
    return last["t"]


class Receiver:
    """A local webhook endpoint answering each request with the next status in `codes` (then 204)."""

    def __init__(self, codes=()):
        self.codes, self.received = list(codes), []
        outer = self

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.received.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(outer.codes.pop(0) if outer.codes else 204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"


# --- per-key isolation --------------------------------------------------------------------


def test_a_key_sees_only_its_own_tasks(orc):
    _, other = second_key(orc, orc.alice)
    task = run(orc.alice.v1, A, "ask me something")
    wait_task(orc.alice.v1, task["id"], "needs_input")
    for method, path, body in [
        ("GET", f"/v1/tasks/{task['id']}", None),
        ("GET", f"/v1/tasks/{task['id']}/events", None),
        ("POST", f"/v1/tasks/{task['id']}/answer", {"answer": "x"}),
        ("POST", f"/v1/tasks/{task['id']}/cancel", None),
    ]:
        assert other.request(method, path, json=body).status_code == 404, (method, path)
    assert other.get("/v1/tasks").json()["tasks"] == []
    assert other.get("/v1/history").json()["turns"] == []
    mine = run(other, B, "open maps")
    assert [t["id"] for t in other.get("/v1/tasks").json()["tasks"]] == [mine["id"]]
    assert [t["id"] for t in orc.alice.v1.get("/v1/tasks").json()["tasks"]] == [task["id"]]
    # The owner's console still sees every key's tasks.
    recent = {t["id"] for t in orc.alice.console.get("/api/developer/overview").json()["recent_tasks"]}
    assert {task["id"], mine["id"]} <= recent
    orc.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "go"})


def test_history_lists_only_this_keys_api_tasks(orc):
    _, other = second_key(orc, orc.alice)
    orc.alice.phones["alice-phone"].send({"type": "command", "text": "open settings from the phone"})
    task = run(other, A, "open maps")
    wait_task(other, task["id"], "succeeded")
    turns = other.get("/v1/history").json()["turns"]
    assert [(t["task_id"], t["instruction"], t["success"]) for t in turns] == [(task["id"], "open maps", True)]


def test_a_key_sees_only_its_own_orchestrations(orc):
    _, other = second_key(orc, orc.alice)
    o = start(orc.alice, [{"id": "a", "device_id": A, "instruction": "loop on phone a"}])
    assert other.get(f"/v1/orchestrations/{o['id']}").status_code == 404
    assert other.post(f"/v1/orchestrations/{o['id']}/cancel").status_code == 404
    assert other.get("/v1/orchestrations").json()["orchestrations"] == []
    orc.alice.v1.post(f"/v1/orchestrations/{o['id']}/cancel")
    wait_status(orc.alice, o["id"], "cancelled")


def test_mcp_tools_see_only_their_keys_tasks(orc):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    task = run(orc.alice.v1, A, "please wait here")
    key = orc.alice.console.post("/api/developer/keys", json={"scopes": ["tasks:run", "tasks:read"]}).json()["key"]

    async def session():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
        async with http, Client(streamable_http_client(f"{orc.base}/mcp", http_client=http)) as c:
            return [await c.call_tool(name, {"task_id": task["id"]}) for name in ("get_task", "cancel_task")]

    for result in asyncio.run(session()):
        assert result.is_error and "Unknown task" in result.content[0].text
    orc.alice.v1.post(f"/v1/tasks/{task['id']}/cancel")


# --- key rotation keeps the key ---------------------------------------------------------------


def test_rotation_keeps_limits_tasks_and_running_work(orc):
    orc.cfg.api_daily_tasks = 2
    done = run(orc.alice.v1, A, "open maps")
    running = start(orc.alice, [{"id": "a", "device_id": B, "instruction": "loop on the tablet"}])
    rotated = orc.alice.console.post(f"/api/developer/keys/{orc.alice.key_id}/rotate").json()
    assert rotated["id"] == orc.alice.key_id and rotated["prefix"] == f"om_live_{orc.alice.key_id}"
    assert orc.alice.v1.get("/v1/devices").status_code == 401  # the old secret is dead
    new = client(orc, rotated["key"])
    # Same key: its tasks and runs are still its own, and the run keeps going.
    assert new.get(f"/v1/tasks/{done['id']}").status_code == 200
    assert new.get(f"/v1/orchestrations/{running['id']}").json()["status"] == "running"
    assert new.get(f"/v1/orchestrations/{running['id']}").json()["status"] == "running"
    # The daily quota carries on: one task and one orchestration step used two of two.
    r = new.post(f"/v1/devices/{A}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 429 and r.json()["code"] == "daily_quota"
    new.post(f"/v1/orchestrations/{running['id']}/cancel")


# --- account-wide daily caps -----------------------------------------------------------------


def test_account_daily_caps_span_all_keys(orc):
    orc.cfg.api_account_daily_tasks = 1
    _, other = second_key(orc, orc.alice)
    run(orc.alice.v1, A, "open maps")
    r = other.post(f"/v1/devices/{A}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 429 and r.json()["code"] == "account_daily_quota"
    assert r.json()["resets_at"] == day_start() + 86400
    run(orc.bob.v1, "app-bob-phone", "open maps")  # another account: unaffected
    limits = other.get("/v1/limits").json()["account"]
    assert limits["tasks_per_day"] == {"limit": 1, "used": 1}
    orc.cfg.api_account_daily_tasks, orc.cfg.api_account_daily_credits = 100, 1
    t = orc.alice.v1.get("/v1/tasks").json()["tasks"][0]
    wait_task(orc.alice.v1, t["id"], "succeeded")
    r = other.post(f"/v1/devices/{A}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 429 and r.json()["code"] == "account_credits_exhausted"
    overview = orc.alice.console.get("/api/developer/overview").json()["limits"]
    assert overview["account_tasks_per_day"] == 100 and overview["account_credits_per_day"] == 1


# --- error codes ------------------------------------------------------------------------------


def test_mcp_429_says_rate_limited():
    from openmob.platform.mcp import HTTP_ERRORS

    assert HTTP_ERRORS[429] == "rate_limited" and HTTP_ERRORS[403] == "forbidden"


def test_mcp_rate_limit_body(orc):
    orc.cfg.api_rate_per_minute = 1
    hello = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": f"Bearer {orc.bob.key}"}
    httpx.post(f"{orc.base}/mcp", json=hello, headers=headers)
    r = httpx.post(f"{orc.base}/mcp", json=hello, headers=headers)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_conflicts_carry_codes(orc):
    task = run(orc.alice.v1, A, "open maps")
    wait_task(orc.alice.v1, task["id"], "succeeded")
    r = orc.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "x"})
    assert r.status_code == 409 and r.json()["code"] == "not_waiting"
    _, viewer = second_key(orc, orc.alice)
    orc.alice.phones["alice-phone"].disconnect()
    orc.alice.phones["alice-tablet"].disconnect()
    assert wait_for(lambda: not orc.manager.get(A).online and not orc.manager.get(B).online)
    r = viewer.get(f"/v1/devices/{A}/screenshot")
    assert r.status_code == 409 and r.json()["code"] == "phone_offline"
    r = viewer.post(f"/v1/devices/{A}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 409 and r.json()["code"] == "phone_offline"
    with pytest.raises(Exception) as err:  # only_device: none of the account's phones is online
        from openmob.platform.principal import Principal

        orc.service.only_device(Principal("k", orc.alice.account_id, frozenset({"tasks:run"}), "k"))
    assert err.value.code == "no_device_online"


def test_whitespace_instruction_is_422(orc):
    r = orc.alice.v1.post(f"/v1/devices/{A}/tasks", json={"instruction": "   \n "})
    assert r.status_code == 422
    r = orc.alice.v1.post("/v1/orchestrations", json={"steps": [{"id": "a", "device_id": A, "instruction": "  "}]})
    assert r.status_code == 422


# --- webhooks: secret rotation and dead deliveries ------------------------------------------------


def verify(secret, header, body):
    parts = header.split(",")
    t = parts[0].split("=", 1)[1]
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(p.split("=", 1)[1], expected) for p in parts[1:] if p.startswith("v1="))


def test_new_webhook_secret_overlaps_the_old_one_for_a_day(orc):
    receiver = Receiver()
    try:
        old = orc.alice.console.post("/api/developer/webhook", json={"url": receiver.url}).json()["secret"]
        new = orc.alice.console.post("/api/developer/webhook", json={"url": receiver.url}).json()["secret"]
        task = run(orc.alice.v1, A, "open maps")
        assert wait_for(lambda: receiver.received)
        headers, body = receiver.received[-1]
        signature = headers["X-OpenMob-Signature"]
        assert signature.count("v1=") == 2 and verify(new, signature, body) and verify(old, signature, body)
        assert json.loads(body)["task"]["id"] == task["id"]
        # After the overlap only the current secret signs.
        account = orc.store._data["accounts"][orc.alice.account_id]["webhook"]
        account["previous_until"] = time.time() - 1
        orc.store._save()
        assert orc.store.webhook_secrets(orc.alice.account_id) == [new]
    finally:
        receiver.server.shutdown()


def test_4xx_deliveries_are_dead_except_408_409_429(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    store = SimpleNamespace(webhook=lambda a: (receiver.url, "whsec_x"), webhook_secrets=lambda a: ["whsec_x"])
    from openmob.platform.service import sign_webhook

    receiver = Receiver([410, 429, 408, 409, 204])
    box = WebhookOutbox(ledger, store, lambda u: True, sign_webhook, backoff=(0.05,) * 5)
    box.start()
    try:
        gone = box.enqueue("acct", "task.finished", "t1", "{}")
        assert wait_for(lambda: ledger.webhook(gone)["status"] == "dead")
        row = ledger.webhook(gone)
        assert row["attempts"] == 1 and row["last_error"] == "HTTP 410"
        retried = box.enqueue("acct", "task.finished", "t2", "{}")
        assert wait_for(lambda: ledger.webhook(retried)["status"] == "delivered")
        assert ledger.webhook(retried)["attempts"] == 4  # 429, 408, 409, then 204
        assert any(e["action"] == "webhook.dead" for e in ledger.audit_log())
    finally:
        box.stop()
        receiver.server.shutdown()


# --- pagination ------------------------------------------------------------------------------------


def test_cursor_pagination(orc):
    orc.cfg.api_max_concurrent_tasks = 10
    _, c = second_key(orc, orc.alice)  # with history:read
    ids = [run(c, A, f"open app {i}")["id"] for i in range(3)]
    for task_id in ids:
        wait_task(c, task_id, "succeeded")
    page = c.get("/v1/tasks?limit=2").json()
    assert [t["id"] for t in page["tasks"]] == ids[::-1][:2] and page["next_cursor"]
    rest = c.get(f"/v1/tasks?limit=2&cursor={page['next_cursor']}").json()
    assert [t["id"] for t in rest["tasks"]] == [ids[0]] and rest["next_cursor"] is None
    history = c.get("/v1/history?limit=2").json()
    assert len(history["turns"]) == 2 and history["next_cursor"]
    more = c.get(f"/v1/history?limit=2&cursor={history['next_cursor']}").json()
    assert [t["task_id"] for t in more["turns"]] == [ids[0]]
    r = c.get("/v1/tasks?cursor=%%%")
    assert r.status_code == 400 and r.json()["code"] == "invalid_cursor"
    runs = [c.post("/v1/orchestrations", json={"steps": [{"id": "a", "device_id": B, "instruction": "open maps"}]}).json()["id"]
            for _ in range(2)]
    page = c.get("/v1/orchestrations?limit=1").json()
    assert [o["id"] for o in page["orchestrations"]] == [runs[1]]
    page = c.get(f"/v1/orchestrations?limit=1&cursor={page['next_cursor']}").json()
    assert [o["id"] for o in page["orchestrations"]] == [runs[0]]


# --- unpairing a phone mid-task ----------------------------------------------------------------------


def test_unpairing_cancels_running_and_queued_tasks_as_device_revoked(orc):
    receiver = Receiver()
    try:
        orc.alice.console.post("/api/developer/webhook", json={"url": receiver.url})
        running = run(orc.alice.v1, A, "loop on phone a")
        wait_task(orc.alice.v1, running["id"], "running")
        queued = run(orc.alice.v1, A, "open maps")
        assert orc.alice.console.post(f"/api/devices/{A}/revoke").json() == {"ok": True}
        for task in (running, queued):
            done = wait_task(orc.alice.v1, task["id"], "cancelled")
            assert done["outcome"]["reason"] == "device_revoked", done
        finished = lambda: {json.loads(b)["task"]["id"] for h, b in receiver.received
                            if h["X-OpenMob-Event"] == "task.finished"}
        assert wait_for(lambda: {running["id"], queued["id"]} <= finished())
    finally:
        receiver.server.shutdown()


# --- retention ---------------------------------------------------------------------------------------


def test_retention_defaults_to_30_days():
    assert Config().history_retention_days == 30


def test_one_job_purges_every_copy_of_task_text(tmp_path):
    history, ledger = History(tmp_path / "h.sqlite"), Ledger(tmp_path / "l.sqlite")
    chat = history.create_chat("acct", "app-x", "Pixel", "open my bank")
    history.record(chat, {"seq": 1, "time": time.time(), "kind": "heard", "message": "[api] open my bank", "task": "t1"})
    ledger.start_task("t1", "acct", "k", "x", "fast", "open my bank")
    ledger.finish_task("t1", "succeeded", 2, 2)
    ledger.save_orchestration({"id": "orc_1", "account_id": "acct", "key_id": "k", "status": "succeeded",
                               "created": time.time(), "steps": [{"id": "a", "instruction": "open my bank",
                                                                   "summary": "balance $12"}]})
    ledger.add_webhook("dlv_old", "acct", "task.finished", "t1", '{"task": "open my bank"}')
    ledger.add_webhook("dlv_pending", "acct", "task.finished", "t1", '{"task": "open my bank"}')
    ledger._q("UPDATE webhook_outbox SET status = 'delivered' WHERE id = 'dlv_old'")
    old = time.time() - 31 * 86400
    history._db.execute("UPDATE chats SET updated = ?", (old,))
    ledger._q("UPDATE orchestrations SET created = ?", (old,))
    ledger._q("UPDATE webhook_outbox SET created = ?", (old,))

    assert purge_task_text(0, history, ledger) == {"chats": 0}  # 0 keeps everything
    out = purge_task_text(30, history, ledger)
    assert out["chats"] == 1 and out["webhooks"] == 1 and out["orchestrations"] == 1
    assert history.list_chats("acct") == []
    data = ledger.orchestration("acct", "orc_1")
    assert data["steps"][0]["instruction"] is None and data["steps"][0]["summary"] is None
    assert ledger.webhook("dlv_old") is None and ledger.webhook("dlv_pending") is not None  # not yet sent: kept
    row = ledger.task("acct", "t1")
    assert row["credits"] == 2 and row["steps"] == 2  # billing totals stay
    assert "open my bank" not in json.dumps([dict(r) for r in ledger._q("SELECT * FROM api_tasks")])
