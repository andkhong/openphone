"""Developer API (/v1): keys, scopes, limits, tenant isolation, the task lifecycle, safety, kill
switches, audit, webhooks — and that nothing internal (models, providers, tokens) leaks out.

Runs a real server with two accounts, each with a paired FakePhone. The phones' agents use a fake
model client that follows a script picked by the instruction's wording.
"""

import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeDriver, text_block, tool_use
from test_hosted import STORES, _up, pair, signup
from test_remote import FakePhone, free_port, new_signer, wait_for

from openmob.accounts import AccountStore
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.history import History
from openmob.ledger import Ledger
from openmob.phone.apps import AppResolver
from openmob.runner import Coordinator
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker

LEAKS = re.compile(r"claude|anthropic|sonnet|opus|haiku|fable|jev|typesafe|tokens|by_model", re.IGNORECASE)
BANK = "com.chase.sig.android"


class ScriptClient:
    """Fake model client: picks a script from the request's wording; step = assistant turns so far.
    Every response claims to come from an internal model, so leaks would be visible."""

    # (model id, how a summary names it): swapped to test other providers' names
    brand = ("claude-sonnet-5-5", "Claude Sonnet via Anthropic")

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        msgs = kw["messages"]
        first = msgs[0]["content"][0]["text"]
        request = first.rsplit("Request: ", 1)[-1].lower()
        step = sum(1 for m in msgs if m["role"] == "assistant")
        model, named = self.brand
        done = [text_block(f"{model} thinks this is done"),
                tool_use("done", f"d{step}", summary=f"Finished with {named}", success=True)]
        if "ask me" in request:
            script = [[tool_use("ask_user", "q", question="Which account, Claude?")], done]
        elif "cart" in request:
            script = [[tool_use("tap", "t", x=10, y=10, target="Buy now")], done]
        elif "wait" in request:
            script = [[tool_use("wait", f"w{step}", seconds=30)]] * 40
        elif "bank" in request:
            script = [[tool_use("launch_app", "l", app=BANK)], done]
        else:
            script = [done]
        content = script[min(step, len(script) - 1)]
        return SimpleNamespace(content=content, stop_reason="tool_use", model=model,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=50, cache_read_input_tokens=0))


@pytest.fixture
def api(tmp_path):
    def build_agent(cfg, io, driver, label_cache):
        return Agent(ScriptClient(), driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, io,
                     use_ocr=False)

    store = AccountStore(tmp_path / "accounts.json")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    cfg = Config(settle_delay=0, launch_settle_delay=0)
    manager = DeviceManager(cfg, build_agent, Speaker(enabled=False), adb_discovery=False,
                            history=History(tmp_path / "history.sqlite"))
    port = free_port()
    serve_in_background(create_app(manager, store=store, ledger=ledger, webhook_allow_private=True), "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    assert wait_for(lambda: _up(base))
    STORES[base] = store
    ws = base.replace("http", "ws")

    users = {}
    for name in ("alice", "bob"):
        client, session = signup(base, f"{name}@example.com")
        device_key = pair(base, client, session, device_id=f"{name}-phone")
        driver = FakeDriver(packages=["com.android.settings", "com.google.android.youtube", BANK])
        # Its app registers a screen-lock approval key on connect, and signs its approvals with it.
        phone = FakePhone(ws, driver, device_id=f"{name}-phone", key=device_key, signer=new_signer())
        assert phone.welcome.wait(5)
        key = client.post("/api/developer/keys", json={
            "name": "test", "scopes": ["devices:read", "tasks:run", "tasks:read", "screen:read", "history:read"],
        }).json()
        users[name] = SimpleNamespace(
            console=client, phone=phone, driver=driver, device=f"app-{name}-phone", key_id=key["id"],
            v1=httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key['key']}"}, timeout=10),
        )
    yield SimpleNamespace(base=base, store=store, ledger=ledger, manager=manager, cfg=cfg, tmp=tmp_path, **users)
    for u in users.values():
        try:
            u.phone.disconnect()
        except Exception:
            pass


def run_task(user, instruction, **extra):
    r = user.v1.post(f"/v1/devices/{user.device}/tasks", json={"instruction": instruction, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def wait_status(user, task_id, *statuses, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = user.v1.get(f"/v1/tasks/{task_id}").json()
        if task["status"] in statuses:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task stayed {task['status']}, wanted {statuses}")


# --- keys -------------------------------------------------------------------------------


def test_key_is_shown_once_and_stored_only_as_a_hash(api):
    r = api.alice.console.post("/api/developer/keys", json={"name": "ci"}).json()
    key = r["key"]
    assert key.startswith("om_live_" + r["id"] + "_") and r["prefix"] == "om_live_" + r["id"]
    assert sorted(r["scopes"]) == ["devices:read", "tasks:read", "tasks:run"]  # screen:read is opt-in
    listed = api.alice.console.get("/api/developer/keys").json()
    assert all("key" not in k and "hash" not in k for k in listed)
    assert key not in (api.tmp / "accounts.json").read_text()
    assert hashlib.sha256(key.encode()).hexdigest() in (api.tmp / "accounts.json").read_text()


def test_missing_bad_and_revoked_keys_are_refused(api):
    base = api.base
    assert httpx.get(f"{base}/v1/devices").status_code == 401
    assert httpx.get(f"{base}/v1/devices", headers={"Authorization": "Bearer nope"}).status_code == 401
    real = api.alice.v1.headers["Authorization"].split()[1]
    forged = real[:-4] + "AAAA"
    assert httpx.get(f"{base}/v1/devices", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    # A session cookie is not an API key.
    assert api.alice.console.get("/v1/devices").status_code == 401

    assert api.alice.v1.get("/v1/devices").status_code == 200
    assert api.alice.console.post(f"/api/developer/keys/{api.alice.key_id}/revoke").json() == {"ok": True}
    assert api.alice.v1.get("/v1/devices").status_code == 401


def test_rotate_replaces_the_key(api):
    new = api.alice.console.post(f"/api/developer/keys/{api.alice.key_id}/rotate").json()
    assert api.alice.v1.get("/v1/devices").status_code == 401
    r = httpx.get(f"{api.base}/v1/devices", headers={"Authorization": f"Bearer {new['key']}"})
    assert r.status_code == 200 and sorted(new["scopes"]) == sorted(
        ["devices:read", "tasks:run", "tasks:read", "screen:read", "history:read"])


def test_only_the_owner_can_revoke_or_rotate_a_key(api):
    assert api.bob.console.post(f"/api/developer/keys/{api.alice.key_id}/revoke").status_code == 404
    assert api.bob.console.post(f"/api/developer/keys/{api.alice.key_id}/rotate").status_code == 404
    assert api.alice.v1.get("/v1/devices").status_code == 200
    assert httpx.get(f"{api.base}/api/developer/keys").status_code == 401


def test_each_scope_is_enforced(api):
    key = api.alice.console.post("/api/developer/keys", json={"scopes": ["devices:read"]}).json()["key"]
    c = httpx.Client(base_url=api.base, headers={"Authorization": f"Bearer {key}"})
    assert c.get("/v1/devices").status_code == 200
    assert c.post(f"/v1/devices/{api.alice.device}/tasks", json={"instruction": "open maps"}).status_code == 403
    assert c.get("/v1/tasks").status_code == 403
    assert c.get(f"/v1/devices/{api.alice.device}/screenshot").status_code == 403
    assert c.get("/v1/history").status_code == 403
    bad = api.alice.console.post("/api/developer/keys", json={"scopes": ["admin"]})
    assert bad.status_code == 400


# --- isolation ----------------------------------------------------------------------------


def test_devices_never_mix(api):
    assert [d["id"] for d in api.alice.v1.get("/v1/devices").json()["devices"]] == [api.alice.device]
    assert [d["id"] for d in api.bob.v1.get("/v1/devices").json()["devices"]] == [api.bob.device]


def test_cannot_reach_another_accounts_phone_or_tasks(api):
    task = run_task(api.alice, "please wait here")
    victim = api.alice.device
    for method, path, body in [
        ("POST", f"/v1/devices/{victim}/tasks", {"instruction": "open maps"}),
        ("GET", f"/v1/devices/{victim}/screenshot", None),
        ("POST", f"/v1/devices/no-such-phone/tasks", {"instruction": "x"}),
        ("GET", f"/v1/tasks/{task['id']}", None),
        ("GET", f"/v1/tasks/{task['id']}/events", None),
        ("POST", f"/v1/tasks/{task['id']}/answer", {"answer": "yes"}),
        ("POST", f"/v1/tasks/{task['id']}/cancel", None),
    ]:
        r = api.bob.v1.request(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    assert task["id"] not in [t["id"] for t in api.bob.v1.get("/v1/tasks").json()["tasks"]]
    assert api.alice.v1.get(f"/v1/tasks/{task['id']}").json()["status"] in ("queued", "running")
    api.alice.v1.post(f"/v1/tasks/{task['id']}/cancel")


# --- task lifecycle -------------------------------------------------------------------------


def test_run_ask_answer_finish_with_events(api):
    task = run_task(api.alice, "ask me something", tier="fast")
    assert task["status"] in ("queued", "running", "needs_input") and task["tier"] == "fast"
    asking = wait_status(api.alice, task["id"], "needs_input")
    assert asking["question"] == "Which account, OpenMob?"
    # A confirmation-style reply is refused for an ordinary question, and vice versa.
    assert api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={}).status_code == 400
    assert api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "work"}).json() == {"ok": True}
    done = wait_status(api.alice, task["id"], "succeeded")
    assert done["outcome"]["success"] is True and done["steps"] == 2
    assert done["credits"] == 2 * 1  # fast tier: 1 credit per step

    with api.alice.v1.stream("GET", f"/v1/tasks/{task['id']}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    types = re.findall(r"^event: (\w+)", body, re.M)
    assert "question" in types and "outcome" in types and types[-1] == "done"
    # Resuming after the last seen event skips what was already delivered.
    last = max(int(i) for i in re.findall(r"^id: (\d+)", body, re.M))
    with api.alice.v1.stream("GET", f"/v1/tasks/{task['id']}/events", headers={"Last-Event-ID": str(last)}) as r:
        assert re.findall(r"^event: (\w+)", "".join(r.iter_text()), re.M) == ["done"]
    # The tier's model ran the task (internally).
    assert api.ledger.task(api.store.account_id_for("alice@example.com"), task["id"])["status"] == "succeeded"


def test_live_events_stream_until_done(api):
    task = run_task(api.alice, "ask me later")
    seen = []

    def answer_when_asked():
        wait_status(api.alice, task["id"], "needs_input")
        api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "ok"})

    threading.Thread(target=answer_when_asked, daemon=True).start()
    with api.alice.v1.stream("GET", f"/v1/tasks/{task['id']}/events") as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                seen.append(line[7:])
    assert seen[-1] == "done" and "question" in seen


def test_risky_tap_becomes_needs_confirmation(api):
    task = run_task(api.alice, "check the cart")
    pending = wait_status(api.alice, task["id"], "needs_confirmation")
    assert "Buy now" in pending["question"]
    assert ("tap", 10, 10) not in api.alice.driver.actions  # nothing tapped before approval
    recent = api.alice.console.get("/api/developer/overview").json()["recent_tasks"]
    assert next(t for t in recent if t["id"] == task["id"])["status"] == "needs_confirmation"
    r = api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "yes"})
    assert r.status_code == 400 and "approve" in r.json()["detail"]
    # The key can't approve its own risky action: the owner is asked on the phone instead.
    r = api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": True})
    assert r.status_code == 403 and r.json()["code"] == "confirmation_requires_owner"
    assert api.bob.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": True}).status_code == 404
    assert wait_for(lambda: api.alice.phone.approval_requests)
    request = api.alice.phone.approval_requests[-1]
    assert request["task_id"] == task["id"] and request["requester"] == "test" and "Buy now" in request["question"]
    assert not any(a[0] == "tap" for a in api.alice.driver.actions)
    status = api.alice.console.get(f"/api/devices/{api.alice.device}/status").json()
    assert status["confirm_on_phone"] is True  # the dashboard hides its own Approve button
    api.alice.phone.send({"type": "approval", "id": request["id"], "approved": True})  # unsigned: ignored
    assert wait_for(lambda: any("screen lock" in m for k, m in api.alice.phone.events if k == "status"))
    assert not any(a[0] == "tap" for a in api.alice.driver.actions)
    api.alice.phone.send(api.alice.phone.approval(request["id"], request["task_id"], True))  # screen lock
    wait_status(api.alice, task["id"], "succeeded")
    assert any(a[0] == "tap" for a in api.alice.driver.actions)


def test_owner_can_let_a_key_approve_risky_actions(api):
    keys = api.alice.console.get("/api/developer/keys").json()
    assert next(k for k in keys if k["id"] == api.alice.key_id)["confirm_approve"] is False
    assert api.bob.console.patch(f"/api/developer/keys/{api.alice.key_id}", json={"confirm_approve": True}).status_code == 404
    r = api.alice.console.patch(f"/api/developer/keys/{api.alice.key_id}", json={"confirm_approve": True})
    assert r.json()["confirm_approve"] is True
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "needs_confirmation")
    # The dashboard never approves on an app phone (only the screen-lock prompt, or this key, can).
    assert api.alice.console.get(f"/api/devices/{api.alice.device}/status").json()["confirm_on_phone"] is True
    assert api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": True}).json() == {"ok": True}
    wait_status(api.alice, task["id"], "succeeded")
    assert not api.alice.phone.approval_requests  # nobody needed to be asked on the phone
    created = api.alice.console.post("/api/developer/keys", json={"name": "ci", "confirm_approve": True}).json()
    assert created["confirm_approve"] is True


def test_phone_shows_who_is_using_it(api):
    task = run_task(api.alice, "open maps")
    wait_status(api.alice, task["id"], "succeeded")
    assert wait_for(lambda: ("controller", "") in api.alice.phone.events)
    controller = [m for k, m in api.alice.phone.events if k == "controller"]
    assert controller == ["test", ""]


def test_declined_confirmation_does_not_tap(api):
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "needs_confirmation")
    api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": False})
    wait_status(api.alice, task["id"], "succeeded", "failed")
    assert not any(a[0] == "tap" for a in api.alice.driver.actions)


def test_cancel_running_and_queued_tasks(api):
    first = run_task(api.alice, "wait a while")
    wait_status(api.alice, first["id"], "running")
    second = run_task(api.alice, "wait again")
    assert api.alice.v1.get(f"/v1/tasks/{second['id']}").json()["status"] == "queued"
    assert api.alice.v1.post(f"/v1/tasks/{second['id']}/cancel").json() == {"ok": True, "cancelled": True}
    assert wait_status(api.alice, second["id"], "cancelled")["credits"] == 0
    assert api.alice.v1.post(f"/v1/tasks/{first['id']}/cancel").json()["cancelled"] is True
    wait_status(api.alice, first["id"], "cancelled")
    assert api.alice.v1.post(f"/v1/tasks/{first['id']}/cancel").json()["cancelled"] is False


def test_api_instruction_is_never_read_as_stop_or_answer(api):
    c = Coordinator(Speaker(enabled=False))
    task_id = c.enqueue("stop", "api")
    assert c.task(task_id)["status"] == "queued" and c.tasks.qsize() == 1


def test_unknown_tier_offline_phone_and_bad_input(api):
    d = api.alice.device
    assert api.alice.v1.post(f"/v1/devices/{d}/tasks", json={"instruction": "x", "tier": "turbo"}).status_code == 400
    assert api.alice.v1.post(f"/v1/devices/{d}/tasks", json={"instruction": ""}).status_code == 422
    assert api.alice.v1.post(f"/v1/devices/{d}/tasks", json={"instruction": "x" * 5000}).status_code == 422
    api.alice.phone.disconnect()
    assert wait_for(lambda: not api.manager.get(d).online)
    assert api.alice.v1.post(f"/v1/devices/{d}/tasks", json={"instruction": "open maps"}).status_code == 409
    assert api.alice.v1.get("/v1/devices").json()["devices"][0]["online"] is False


def test_screenshot_needs_scope_and_is_not_cached(api):
    r = api.alice.v1.get(f"/v1/devices/{api.alice.device}/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"


# --- limits ---------------------------------------------------------------------------------


def test_concurrent_task_cap(api):
    api.cfg.api_max_concurrent_tasks = 1
    first = run_task(api.alice, "wait a bit")
    r = api.alice.v1.post(f"/v1/devices/{api.alice.device}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 429
    api.alice.v1.post(f"/v1/tasks/{first['id']}/cancel")
    wait_status(api.alice, first["id"], "cancelled")
    run_task(api.alice, "open maps")


def test_daily_quota(api):
    api.cfg.api_daily_tasks = 1
    run_task(api.alice, "open maps")
    r = api.alice.v1.post(f"/v1/devices/{api.alice.device}/tasks", json={"instruction": "open maps"})
    assert r.status_code == 429 and "quota" in r.json()["detail"] and int(r.headers["retry-after"]) > 0
    run_task(api.bob, "open maps")  # per key, not global


def test_rate_limit(api):
    api.cfg.api_rate_per_minute = 3
    codes = [api.alice.v1.get("/v1/devices").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert api.bob.v1.get("/v1/devices").status_code == 200


# --- provider confidentiality ---------------------------------------------------------------


def test_public_responses_name_no_model_or_provider(api):
    dumps = []
    task = run_task(api.alice, "ask me first", tier="best")
    dumps.append(json.dumps(task))
    asking = wait_status(api.alice, task["id"], "needs_input")
    dumps.append(json.dumps(asking))
    api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"answer": "go"})
    wait_status(api.alice, task["id"], "succeeded")
    dumps.append(api.alice.v1.get(f"/v1/tasks/{task['id']}").text)
    dumps.append(api.alice.v1.get("/v1/tasks").text)
    dumps.append(api.alice.v1.get("/v1/devices").text)
    dumps.append(api.alice.v1.get("/v1/history").text)
    with api.alice.v1.stream("GET", f"/v1/tasks/{task['id']}/events") as r:
        dumps.append("".join(r.iter_text()))
    blob = "\n".join(dumps)
    assert not LEAKS.search(blob), LEAKS.search(blob)
    # ...while internally the tokens and the model were recorded for billing reconciliation.
    row = api.ledger.task(api.store.account_id_for("alice@example.com"), task["id"])
    assert "claude-sonnet-5-5" in row["internal_usage"] and row["credits"] == 2 * 10


def test_other_providers_model_names_never_leak(api, monkeypatch):
    monkeypatch.setattr(ScriptClient, "brand", ("gemini-3-pro", "Gemini 3 Pro on Google AI (via OpenAI-compatible gpt-5.1 and o4-mini)"))
    gemini = re.compile(r"gemini|google|openai|gpt|o4-mini", re.IGNORECASE)
    task = run_task(api.alice, "open maps", tier="best")
    wait_status(api.alice, task["id"], "succeeded")
    dumps = [api.alice.v1.get(f"/v1/tasks/{task['id']}").text, api.alice.v1.get("/v1/history").text]
    with api.alice.v1.stream("GET", f"/v1/tasks/{task['id']}/events") as r:
        dumps.append("".join(r.iter_text()))
    blob = "\n".join(dumps)
    assert "Finished with OpenMob" in blob and not gemini.search(blob), gemini.search(blob)


def test_configured_model_ids_are_scrubbed():
    from openmob.platform.service import clean_text, register_model_names

    register_model_names(["llama-3.3-70b-local"])
    try:
        assert clean_text("llama-3.3-70b-local opened Google Maps") == "OpenMob opened Google Maps"
    finally:
        register_model_names(m["id"] for m in Config().models)


def test_upstream_error_text_is_reworded():
    from openmob.api_v1 import clean_text, public_event

    assert clean_text("APIStatusError: anthropic overloaded") == "The task hit an internal error."
    assert clean_text("Launched com.x (matched by llm).") == "Launched com.x."
    assert public_event({"seq": 1, "time": 0, "kind": "usage", "message": "Tokens: 5", "data": {}}) is None
    assert public_event({"seq": 2, "time": 0, "kind": "heard", "message": "[api] hi"}) is None


# --- safety -----------------------------------------------------------------------------------


def test_sensitive_apps_are_blocked_unless_the_owner_allows(api):
    task = run_task(api.alice, "open my bank")
    done = wait_status(api.alice, task["id"], "succeeded", "failed")
    assert ("launch", BANK) not in api.alice.driver.actions
    events = api.alice.manager_events = api.manager.get(api.alice.device).coordinator.task(task["id"])["events"]
    assert any("blocked for API tasks" in e["message"] for e in events), done

    s = api.alice.console.post("/api/developer/settings", json={"allow_sensitive_apps": True}).json()
    assert s["allow_sensitive_apps"] is True
    task = run_task(api.alice, "open my bank")
    wait_status(api.alice, task["id"], "succeeded")
    assert ("launch", BANK) in api.alice.driver.actions


def test_a_blocked_app_in_front_stops_the_task(api):
    api.alice.driver.package = BANK  # the owner left their banking app open
    task = run_task(api.alice, "open youtube")
    done = wait_status(api.alice, task["id"], "failed")
    assert "blocked" in done["outcome"]["summary"]


def make_admin(api):
    api.store.set_admin("bob@example.com", True)
    return api.bob.console


def test_admin_kill_switch_for_a_key_cancels_its_tasks(api):
    admin = make_admin(api)
    task = run_task(api.alice, "wait forever")
    wait_status(api.alice, task["id"], "running")
    assert api.alice.console.post(f"/api/admin/api-keys/{api.alice.key_id}/disable",
                                  json={"disabled": True}).status_code == 403  # not an admin
    r = admin.post(f"/api/admin/api-keys/{api.alice.key_id}/disable", json={"disabled": True}).json()
    assert r == {"ok": True, "cancelled_tasks": 1}
    assert api.alice.v1.get("/v1/devices").status_code == 403
    assert api.manager.get(api.alice.device).coordinator.task(task["id"])["status"] in ("running", "cancelled")
    assert wait_for(lambda: api.manager.get(api.alice.device).coordinator.task(task["id"])["status"] == "cancelled")
    admin.post(f"/api/admin/api-keys/{api.alice.key_id}/disable", json={"disabled": False})
    assert api.alice.v1.get("/v1/devices").status_code == 200


def test_admin_kill_switch_for_an_account(api):
    admin = make_admin(api)
    alice_id = api.store.account_id_for("alice@example.com")
    assert admin.post(f"/api/admin/users/{alice_id}/api", json={"disabled": True}).json()["ok"] is True
    assert api.alice.v1.get("/v1/devices").status_code == 403
    assert api.bob.v1.get("/v1/devices").status_code == 200
    keys = {k["id"]: k for k in admin.get("/api/admin/api-keys").json()}
    assert api.alice.key_id in keys and "hash" not in keys[api.alice.key_id]


def test_audit_log_records_actions_without_instructions(api):
    admin = make_admin(api)
    task = run_task(api.alice, "open maps secretly")
    wait_status(api.alice, task["id"], "succeeded")
    api.alice.v1.get(f"/v1/devices/{api.alice.device}/screenshot")
    assert api.alice.console.get("/api/admin/audit").status_code == 403
    log = admin.get("/api/admin/audit").json()
    actions = [e["action"] for e in log]
    assert {"key.create", "task.create", "task.finished", "screen.read"} <= set(actions)
    create = next(e for e in log if e["action"] == "task.create")
    assert create["task_id"] == task["id"] and create["key_id"] == api.alice.key_id
    assert create["instruction_hash"] == hashlib.sha256(b"open maps secretly").hexdigest()
    assert "secretly" not in json.dumps(log)


# --- webhooks -------------------------------------------------------------------------------------


def test_webhook_is_signed_and_verifiable(api):
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        secret = api.alice.console.post("/api/developer/webhook", json={"url": url}).json()["secret"]
        task = run_task(api.alice, "open maps")
        assert wait_for(lambda: any(h["X-OpenMob-Event"] == "task.finished" for h, _ in received), timeout=5)
        headers, body = next((h, b) for h, b in received if h["X-OpenMob-Event"] == "task.finished")
        t, v1 = dict(part.split("=", 1) for part in headers["X-OpenMob-Signature"].split(",")).values()
        assert abs(int(t) - time.time()) < 60  # receivers reject old timestamps to stop replays
        assert hmac.compare_digest(v1, hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest())
        payload = json.loads(body)
        assert payload["task"]["id"] == task["id"] and payload["task"]["status"] == "succeeded"
        assert not LEAKS.search(body.decode())
    finally:
        server.shutdown()


def test_webhook_refuses_private_addresses_in_production():
    from openmob.api_v1 import DeveloperApi

    api = DeveloperApi.__new__(DeveloperApi)
    api.webhook_allow_private = False
    assert not api._webhook_url_ok("http://example.com/hook")  # https only
    assert not api._webhook_url_ok("https://127.0.0.1/hook")
    assert not api._webhook_url_ok("https://169.254.169.254/latest")
    assert not api._webhook_url_ok("https://10.0.0.5/hook")
    assert not api._webhook_url_ok("file:///etc/passwd")
