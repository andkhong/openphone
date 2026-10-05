"""Hardening: question timeouts, run limits, idempotency, locked phones, clean starts, stops from the
phone, approvals on every phone, and billing (UTC reset, no charge for our own faults).

Uses the orchestrator fixture: Alice has a phone (A) and a tablet (B), Bob one phone. Waits are on
conditions, never fixed sleeps.
"""

import asyncio
import threading
import time

import httpx
import pytest
from test_orchestrator import A, B, get, orc, start, step, step_is, wait_status  # noqa: F401  (orc: fixture)
from test_remote import wait_for

from openmob.ledger import Ledger, day_start
from openmob.phone import protocol


def task_status(user, task_id):
    return user.v1.get(f"/v1/tasks/{task_id}").json()


def wait_task(user, task_id, *statuses):
    last = {}

    def reached():
        last["t"] = task_status(user, task_id)
        return last["t"]["status"] in statuses

    assert wait_for(reached, timeout=20), last.get("t")
    return last["t"]


def new_task(user, device, instruction, headers=None):
    return user.v1.post(f"/v1/devices/{device}/tasks", json={"instruction": instruction}, headers=headers or {})


def no_orchestration_threads():
    return not any(t.name == "openmob-orchestration" and t.is_alive() for t in threading.enumerate())


# --- 1. questions nobody answers; runs that go on too long ---------------------------------


def test_unanswered_question_fails_the_task_and_frees_the_slot(orc):
    orc.manager.get(A).coordinator.question_timeout = 0.5
    orc.cfg.api_max_concurrent_tasks = 1
    r = new_task(orc.alice, A, "ask me which one")
    assert r.status_code == 201
    done = wait_task(orc.alice, r.json()["id"], "succeeded", "failed", "cancelled")
    assert done["status"] == "failed" and done["outcome"]["reason"] == "question_timeout"
    assert "nobody answered" in done["outcome"]["summary"]
    assert new_task(orc.alice, A, "open maps").status_code == 201  # the key's only task slot is free again


def test_unanswered_question_in_an_orchestration(orc):
    orc.manager.get(A).coordinator.question_timeout = 0.5
    o = start(orc.alice, [{"id": "a", "device_id": A, "instruction": "ask me first"},
                          {"id": "b", "device_id": B, "instruction": "open maps", "depends_on": ["a"]}])
    done = wait_status(orc.alice, o["id"], "failed")
    assert done["reason"] == "question_timeout" and step(done, "a")["reason"] == "question_timeout"
    assert step(done, "b")["status"] == "skipped"


def test_run_that_takes_too_long_times_out(orc):
    o = start(orc.alice, [{"id": "a", "device_id": A, "instruction": "loop on phone a"},
                          {"id": "b", "device_id": B, "instruction": "open maps", "depends_on": ["a"]}],
              max_duration_seconds=1)
    done = wait_status(orc.alice, o["id"], "failed")
    assert done["reason"] == "timed_out" and [s["status"] for s in done["steps"]] == ["cancelled", "skipped"]
    task = step(done, "a")["task_id"]
    assert wait_for(lambda: orc.manager.get(A).coordinator.task(task)["status"] == "cancelled")
    r = orc.alice.v1.post("/v1/orchestrations", json={"steps": [{"id": "a", "device_id": A, "instruction": "x"}],
                                                      "max_duration_seconds": 7201})
    assert r.status_code == 422


# --- 2. idempotency -------------------------------------------------------------------------


def test_idempotent_task_creation(orc):
    headers = {"Idempotency-Key": "order-42"}
    first = new_task(orc.alice, A, "open maps", headers)
    again = new_task(orc.alice, A, "open maps", headers)
    assert first.status_code == 201 and again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert again.json() == first.json()
    other = new_task(orc.alice, A, "open youtube", headers)
    assert other.status_code == 409 and other.json()["code"] == "idempotency_conflict"
    assert new_task(orc.bob, "app-bob-phone", "open maps", headers).status_code == 201  # per key
    assert len(orc.ledger.recent_tasks(orc.alice.account_id)) == 1


def test_idempotent_orchestrations_and_failed_first_attempts(orc):
    body = {"steps": [{"id": "a", "device_id": A, "instruction": "open maps"}]}
    headers = {"Idempotency-Key": "plan-7"}
    first = orc.alice.v1.post("/v1/orchestrations", json=body, headers=headers)
    again = orc.alice.v1.post("/v1/orchestrations", json=body, headers=headers)
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
    changed = orc.alice.v1.post("/v1/orchestrations", json={**body, "on_failure": "continue"}, headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    # A first attempt that fails doesn't use up the key.
    bad = {"steps": [{"id": "a", "device_id": "app-nope", "instruction": "x"}]}
    assert orc.alice.v1.post("/v1/orchestrations", json=bad, headers={"Idempotency-Key": "k2"}).status_code == 404
    good = orc.alice.v1.post("/v1/orchestrations", json=body, headers={"Idempotency-Key": "k2"})
    assert good.status_code == 201
    assert len(orc.ledger.recent_orchestrations(orc.alice.account_id)) == 2


def test_idempotency_keys_expire_after_a_day(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    assert ledger.idempotency_reserve("k", "x", "h1") is None
    ledger.idempotency_store("k", "x", {"id": "t1"})
    assert ledger.idempotency_reserve("k", "x", "h1") == {"request_hash": "h1", "response": {"id": "t1"}}
    ledger._q("UPDATE idempotency SET created = created - 86401")
    assert ledger.idempotency_reserve("k", "x", "h2") is None  # forgotten: a new request


# --- 3. caps on runs in progress ------------------------------------------------------------


def test_running_orchestration_caps(orc):
    orc.cfg.api_max_running_orchestrations = 1
    plan = [{"id": "a", "device_id": A, "instruction": "loop on phone a"}]
    first = start(orc.alice, plan)
    r = orc.alice.v1.post("/v1/orchestrations", json={"steps": plan})
    assert r.status_code == 429 and r.json()["code"] == "too_many_orchestrations"
    orc.cfg.api_max_running_orchestrations, orc.cfg.api_max_running_orchestrations_per_account = 5, 1
    other_key = orc.alice.console.post("/api/developer/keys", json={"scopes": ["tasks:run", "tasks:read"]}).json()["key"]
    c = httpx.Client(base_url=orc.base, headers={"Authorization": f"Bearer {other_key}"})
    r = c.post("/v1/orchestrations", json={"steps": [{"id": "b", "device_id": B, "instruction": "open maps"}]})
    assert r.status_code == 429 and r.json()["code"] == "too_many_orchestrations"  # per account
    orc.alice.v1.post(f"/v1/orchestrations/{first['id']}/cancel")
    wait_status(orc.alice, first["id"], "cancelled")
    orchestrator = orc.service.orchestrator
    assert wait_for(lambda: not orchestrator.runs and no_orchestration_threads())  # watcher let go
    assert c.post("/v1/orchestrations", json={"steps": [{"id": "b", "device_id": B, "instruction": "open maps"}]}).status_code == 201


# --- 4. locked and unresponsive phones ------------------------------------------------------


def lock(user, device, locked=True, interactive=True):
    user.drivers[device].device_state = lambda: {"interactive": interactive, "locked": locked}


def test_locked_phone_refuses_a_standalone_task(orc):
    lock(orc.alice, "alice-phone")
    r = new_task(orc.alice, A, "open maps")
    assert r.status_code == 409 and r.json()["code"] == "phone_locked" and "Unlock" in r.json()["detail"]
    lock(orc.alice, "alice-phone", locked=False, interactive=False)  # screen off counts too
    assert new_task(orc.alice, A, "open maps").json()["code"] == "phone_locked"
    lock(orc.alice, "alice-phone", locked=False)
    assert new_task(orc.alice, A, "open maps").status_code == 201
    assert ("device_state", {}) in orc.alice.phones["alice-phone"].calls


def test_locked_phone_step_waits_then_runs_once_unlocked(orc):
    lock(orc.alice, "alice-tablet")
    o = start(orc.alice, [{"id": "b", "device_id": B, "instruction": "open maps"}], wait_for_phone_seconds=30)
    assert wait_for(lambda: step(get(orc.alice, o["id"]), "b")["reason"] == "phone_locked")
    assert step(get(orc.alice, o["id"]), "b")["status"] == "waiting_for_phone"
    lock(orc.alice, "alice-tablet", locked=False)
    assert wait_status(orc.alice, o["id"], "succeeded")["status"] == "succeeded"


def test_locked_phone_step_fails_after_the_wait(orc):
    lock(orc.alice, "alice-tablet")
    o = start(orc.alice, [{"id": "b", "device_id": B, "instruction": "open maps"}], wait_for_phone_seconds=1)
    done = wait_status(orc.alice, o["id"], "failed")
    assert done["reason"] == "phone_locked" and step(done, "b")["task_id"] is None


def test_unresponsive_phone(orc, monkeypatch):
    for method in ("device_state", "wake_unlock"):
        _, result, _ = protocol.METHODS[method]
        monkeypatch.setitem(protocol.METHODS, method, ({}, result, 0.3))

    def stuck(*args):
        time.sleep(0.8)  # the phone is asleep and doesn't answer in time
        return {"interactive": True, "locked": False}

    orc.alice.drivers["alice-tablet"].device_state = stuck
    r = new_task(orc.alice, B, "open maps")
    assert r.status_code == 409 and r.json()["code"] == "phone_unresponsive"
    o = start(orc.alice, [{"id": "b", "device_id": B, "instruction": "open maps"}])
    assert wait_status(orc.alice, o["id"], "failed")["reason"] == "phone_unresponsive"
    # An older app without device_state: the check is skipped; a first command that times out says so.
    del orc.alice.drivers["alice-tablet"].device_state
    orc.alice.drivers["alice-tablet"].wake_unlock = stuck
    r = new_task(orc.alice, B, "open maps")
    assert r.status_code == 201
    done = wait_task(orc.alice, r.json()["id"], "failed")
    assert done["outcome"]["reason"] == "phone_unresponsive"


# --- 5. clean start ---------------------------------------------------------------------------


def test_orchestrated_steps_start_from_home_standalone_tasks_dont(orc):
    driver = orc.alice.drivers["alice-tablet"]
    r = new_task(orc.alice, B, "open maps")
    wait_task(orc.alice, r.json()["id"], "succeeded")
    assert ("key", "HOME") not in driver.actions
    o = start(orc.alice, [{"id": "b", "device_id": B, "instruction": "open maps"}])
    wait_status(orc.alice, o["id"], "succeeded")
    assert ("key", "HOME") in driver.actions


# --- 6. the owner stops a step from the phone --------------------------------------------------


@pytest.mark.parametrize("how", ["say_stop", "stop_button"])
def test_stop_on_the_phone_cancels_the_whole_run(orc, how):
    o = start(orc.alice, [{"id": "a", "device_id": A, "instruction": "loop on phone a"},
                          {"id": "b", "device_id": B, "instruction": "loop on the tablet"}], on_failure="continue")
    assert wait_for(step_is(orc.alice, o["id"], "a", "running"))
    assert wait_for(lambda: orc.manager.get(A).coordinator.current_task is not None)
    phone = orc.alice.phones["alice-phone"]
    if how == "say_stop":
        phone.send({"type": "command", "text": "stop"})
    else:
        phone.disconnect()  # the notification's Stop closes the connection normally
    done = wait_status(orc.alice, o["id"], "cancelled")
    assert done["reason"] == "stopped_on_phone" and step(done, "b")["status"] == "cancelled"


def test_denying_on_the_phone_cancels_the_whole_run(orc):
    o = start(orc.alice, [{"id": "a", "device_id": A, "instruction": "check the cart"},
                          {"id": "b", "device_id": B, "instruction": "open maps", "depends_on": ["a"]}],
              on_failure="continue")
    phone = orc.alice.phones["alice-phone"]
    assert wait_for(lambda: phone.approval_requests)
    phone.send({"type": "approval", "id": phone.approval_requests[-1]["id"], "approved": False})
    done = wait_status(orc.alice, o["id"], "cancelled")
    assert done["reason"] == "stopped_on_phone" and step(done, "b")["status"] == "skipped"
    assert not any(a[0] == "tap" for a in orc.alice.drivers["alice-phone"].actions)


# --- 7. approvals on every phone ---------------------------------------------------------------


def test_approval_goes_to_every_phone_and_the_first_answer_wins(orc):
    phone, tablet = orc.alice.phones["alice-phone"], orc.alice.phones["alice-tablet"]
    r = new_task(orc.alice, A, "check the cart")
    assert wait_for(lambda: phone.approval_requests and tablet.approval_requests)
    on_phone, on_tablet = phone.approval_requests[-1], tablet.approval_requests[-1]
    assert on_phone["id"] == on_tablet["id"] and on_tablet["device_name"] == "Google Pixel 9"
    assert on_tablet["device_id"] == A and on_tablet["task_id"] == r.json()["id"]
    assert not orc.bob.phones["bob-phone"].approval_requests  # never another account's phones
    tablet.send({"type": "approval", "id": on_tablet["id"], "approved": True})  # answered on the tablet
    wait_task(orc.alice, r.json()["id"], "succeeded")
    assert any(a[0] == "tap" for a in orc.alice.drivers["alice-phone"].actions)
    assert wait_for(lambda: on_phone["id"] in phone.approval_cancels)  # the phone drops its prompt
    assert on_tablet["id"] not in tablet.approval_cancels
    phone.send({"type": "approval", "id": on_phone["id"], "approved": False})  # too late: ignored
    assert task_status(orc.alice, r.json()["id"])["status"] == "succeeded"


def test_approval_answered_elsewhere_is_withdrawn_from_every_phone(orc):
    orc.alice.console.patch(f"/api/developer/keys/{orc.alice.key_id}", json={"confirm_approve": False})
    r = new_task(orc.alice, A, "check the cart")
    phone, tablet = orc.alice.phones["alice-phone"], orc.alice.phones["alice-tablet"]
    assert wait_for(lambda: tablet.approval_requests)
    approval = tablet.approval_requests[-1]["id"]
    assert orc.alice.v1.post(f"/v1/tasks/{r.json()['id']}/answer", json={"approve": False}).json() == {"ok": True}
    assert wait_for(lambda: approval in phone.approval_cancels and approval in tablet.approval_cancels)


# --- billing ---------------------------------------------------------------------------------------


def test_daily_limits_reset_at_utc_midnight(orc):
    now = time.time()
    resets = day_start(now) + 86400
    assert resets % 86400 == 0 and now < resets <= now + 86400
    limits = orc.alice.v1.get("/v1/limits").json()
    assert limits["resets_at"] == resets and limits["tasks_per_day"]["limit"] == orc.cfg.api_daily_tasks
    assert orc.alice.console.get("/api/developer/overview").json()["limits"]["resets_at"] == resets
    orc.cfg.api_daily_tasks = 0
    r = new_task(orc.alice, A, "open maps")
    assert r.status_code == 429 and r.json()["code"] == "daily_quota" and r.json()["resets_at"] == resets
    assert abs(int(r.headers["retry-after"]) - (resets - time.time())) < 5


def test_our_own_failures_are_not_charged(orc):
    r = new_task(orc.alice, A, "crash after a step")
    done = wait_task(orc.alice, r.json()["id"], "failed")
    assert done["outcome"]["reason"] == "internal_error"
    assert wait_for(lambda: task_status(orc.alice, r.json()["id"])["credits"] == 0)
    row = orc.ledger.task(orc.alice.account_id, r.json()["id"])
    assert row["credits"] == 0 and row["credits_waived"] == 3 and row["reason"] == "internal_error"  # 1 step × 3
    usage = orc.ledger.usage(orc.alice.account_id, 0)
    assert usage["credits"] == 0 and usage["credits_waived"] == 3
    orc.cfg.api_daily_credits = 1
    assert new_task(orc.alice, A, "open maps").status_code == 201  # waived credits don't count


def test_tasks_cut_off_by_a_restart_are_failed_and_free(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite")
    ledger.start_task("t1", "acct", "k", "dev", "best", "open maps")
    ledger.set_steps("t1", 4)
    assert ledger.fail_interrupted_tasks(lambda tier: 10) == 1
    row = ledger.task("acct", "t1")
    assert (row["status"], row["reason"], row["credits"], row["credits_waived"]) == ("failed", "server_restarted", 0, 40)


# --- MCP: every scope checked before anything moves --------------------------------------------


def test_mcp_tools_check_all_scopes_before_acting(orc):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    run_only = orc.alice.console.post("/api/developer/keys", json={"scopes": ["tasks:run"]}).json()["key"]
    pending = new_task(orc.alice, A, "ask me something")
    wait_task(orc.alice, pending.json()["id"], "needs_input")

    async def calls(client):
        return [
            await client.call_tool("run_task", {"instruction": "open maps", "device_id": B}),
            await client.call_tool("answer_question", {"task_id": pending.json()["id"], "answer": "the first"}),
            await client.call_tool("start_orchestration", {"steps": [
                {"id": "a", "device_id": B, "instruction": "open maps"}]}),
        ]

    async def session():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {run_only}"})
        async with http, Client(streamable_http_client(f"{orc.base}/mcp", http_client=http)) as client:
            return await calls(client)

    for result in asyncio.run(session()):
        assert result.is_error and "tasks:read" in result.content[0].text
    assert [t["id"] for t in orc.ledger.recent_tasks(orc.alice.account_id)] == [pending.json()["id"]]
    assert not orc.ledger.recent_orchestrations(orc.alice.account_id)
    assert not orc.alice.drivers["alice-tablet"].actions
    assert task_status(orc.alice, pending.json()["id"])["status"] == "needs_input"  # not answered


def test_mcp_idempotency_key(orc):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def session():
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {orc.alice.key}"})
        async with http, Client(streamable_http_client(f"{orc.base}/mcp", http_client=http)) as client:
            args = {"instruction": "open maps", "device_id": A, "idempotency_key": "once"}
            first = await client.call_tool("run_task", args)
            again = await client.call_tool("run_task", args)
            plan = {"steps": [{"id": "a", "device_id": B, "instruction": "open maps"}], "idempotency_key": "plan"}
            o1 = await client.call_tool("start_orchestration", plan)
            o2 = await client.call_tool("start_orchestration", plan)
            clash = await client.call_tool("run_task", {**args, "instruction": "open youtube"})
            return first, again, o1, o2, clash

    first, again, o1, o2, clash = asyncio.run(session())
    assert first.structured_content["id"] == again.structured_content["id"]
    assert o1.structured_content["id"] == o2.structured_content["id"]
    assert clash.is_error and "idempotency_conflict" in clash.content[0].text
    assert len(orc.ledger.recent_tasks(orc.alice.account_id)) == 2  # one task, one orchestration step
