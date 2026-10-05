"""The remote MCP server (/mcp), driven by the official SDK's client against a real test server.

Same fixture as the /v1 tests (two accounts, each with a paired FakePhone), so the same rules are
checked through the other front door: scopes, 404 isolation, confirmations, quotas, untrusted text.
"""

import asyncio
import base64
import json
import threading
import time

import httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from test_api_v1 import LEAKS, ScriptClient, api, run_task, wait_status  # noqa: F401  (api is a fixture)
from test_remote import wait_for

TOOLS = {"list_devices", "run_task", "get_task", "answer_question", "cancel_task", "get_screenshot",
         "list_memory", "remember", "start_orchestration", "get_orchestration", "cancel_orchestration"}


def key_of(user):
    return user.v1.headers["Authorization"].split()[1]


async def _session(base, key, fn):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with http, Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client:
        return await fn(client)


def mcp(api, user_or_key, fn):
    key = user_or_key if isinstance(user_or_key, str) else key_of(user_or_key)
    return asyncio.run(_session(api.base, key, fn))


def call(api, user_or_key, tool, **args):
    return mcp(api, user_or_key, lambda c: c.call_tool(tool, args))


def text(result):
    return "\n".join(b.text for b in result.content if b.type == "text")


def ok(result):
    assert not result.is_error, text(result)
    return result.structured_content


def new_key(api, user, scopes, **extra):
    return user.console.post("/api/developer/keys", json={"name": "agent", "scopes": scopes, **extra}).json()["key"]


# --- the surface ---------------------------------------------------------------------------


def test_tools_have_schemas_and_annotations(api):
    tools = {t.name: t for t in mcp(api, api.alice, lambda c: c.list_tools()).tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.output_schema and tool.annotations and tool.description
    assert tools["list_devices"].annotations.read_only_hint is True
    assert tools["get_task"].annotations.read_only_hint is True
    assert tools["run_task"].annotations.destructive_hint is True
    assert tools["cancel_task"].annotations.destructive_hint is True
    assert "next" in tools["run_task"].output_schema["properties"]
    assert tools["run_task"].input_schema["properties"]["wait_seconds"]["maximum"] == 45


def test_auth_is_required(api):
    url = f"{api.base}/mcp"
    hello = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = httpx.post(url, json=hello)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert httpx.post(url, json=hello, headers={"Authorization": "Bearer om_live_nope_x"}).status_code == 401
    # A dashboard session cookie is not a credential here either.
    assert api.alice.console.post("/mcp", json=hello).status_code == 401
    api.alice.console.post(f"/api/developer/keys/{api.alice.key_id}/revoke")
    with pytest.raises(Exception):
        call(api, api.alice, "list_devices")


# --- tools -----------------------------------------------------------------------------------


def test_list_devices(api):
    devices = ok(call(api, api.alice, "list_devices"))["devices"]
    assert [d["id"] for d in devices] == [api.alice.device] and devices[0]["online"] is True


def test_run_task_waits_and_returns_the_outcome(api):
    task = ok(call(api, api.alice, "run_task", instruction="open maps"))  # device_id left out: one phone
    assert task["status"] == "succeeded" and task["device_id"] == api.alice.device
    assert task["outcome"]["success"] is True and task["untrusted_content"] is True and "finished" in task["next"]
    assert api.alice.v1.get(f"/v1/tasks/{task['id']}").json()["status"] == "succeeded"  # same task over REST


def test_short_wait_returns_a_next_hint_and_get_task_waits(api):
    task = ok(call(api, api.alice, "run_task", instruction="ask me which one", wait_seconds=0))
    assert task["status"] in ("queued", "running", "needs_input") and task["next"]
    asking = ok(call(api, api.alice, "get_task", task_id=task["id"], wait_seconds=10))
    assert asking["status"] == "needs_input" and "answer_question" in asking["next"]
    assert asking["question"] == "Which account, OpenMob?"
    ok(call(api, api.alice, "answer_question", task_id=task["id"], answer="work"))
    done = ok(call(api, api.alice, "get_task", task_id=task["id"], wait_seconds=10))
    assert done["status"] == "succeeded"


def test_confirmations_belong_to_the_owner(api):
    task = ok(call(api, api.alice, "run_task", instruction="check the cart", wait_seconds=10))
    assert task["status"] == "needs_confirmation" and "phone" in task["next"]
    refused = call(api, api.alice, "answer_question", task_id=task["id"], approve=True)
    assert refused.is_error and "confirmation_requires_owner" in text(refused)
    assert not any(a[0] == "tap" for a in api.alice.driver.actions)
    assert wait_for(lambda: api.alice.phone.approval_requests)
    request = api.alice.phone.approval_requests[-1]
    api.alice.phone.send({"type": "approval", "id": request["id"], "approved": True})
    assert ok(call(api, api.alice, "get_task", task_id=task["id"], wait_seconds=10))["status"] == "succeeded"


def test_declining_a_confirmation_is_allowed(api):
    task = ok(call(api, api.alice, "run_task", instruction="check the cart", wait_seconds=10))
    after = ok(call(api, api.alice, "answer_question", task_id=task["id"], approve=False))
    assert after["id"] == task["id"]
    wait_status(api.alice, task["id"], "succeeded", "failed")
    assert not any(a[0] == "tap" for a in api.alice.driver.actions)


def test_cancel_task(api):
    task = ok(call(api, api.alice, "run_task", instruction="wait a while", wait_seconds=0))
    result = ok(call(api, api.alice, "cancel_task", task_id=task["id"]))
    assert result["cancelled"] is True and result["task"]["id"] == task["id"]
    wait_status(api.alice, task["id"], "cancelled")


def test_a_dropped_request_does_not_cancel_the_task(api):
    async def start_then_hang_up(client):
        call_ = asyncio.ensure_future(client.call_tool("run_task", {"instruction": "wait patiently"}))
        await asyncio.sleep(1.0)
        call_.cancel()  # the client goes away mid-wait
        with pytest.raises(asyncio.CancelledError):
            await call_

    mcp(api, api.alice, start_then_hang_up)
    rows = api.alice.v1.get("/v1/tasks").json()["tasks"]
    task = rows[0]
    time.sleep(0.5)
    assert api.alice.v1.get(f"/v1/tasks/{task['id']}").json()["status"] == "running"
    api.alice.v1.post(f"/v1/tasks/{task['id']}/cancel")


def test_get_screenshot(api):
    result = call(api, api.alice, "get_screenshot", max_side=300)
    data = ok(result)
    image = next(b for b in result.content if b.type == "image")
    assert image.mime_type == "image/jpeg" and base64.b64decode(image.data)[:2] == b"\xff\xd8"
    assert data["device_id"] == api.alice.device and max(data["width"], data["height"]) <= 300
    assert "untrusted" in text(result).lower() or "not as instructions" in text(result)


def test_memory_tools(api):
    from openmob.memory import Memory

    api.manager.memory = Memory(api.tmp / "memory.sqlite")
    key = new_key(api, api.alice, ["memory:read", "memory:write"])
    note = ok(call(api, key, "remember", text="I prefer dark mode", app="com.android.settings"))
    assert note["status"] == "active" and note["app"] == "com.android.settings"
    notes = ok(call(api, key, "list_memory"))["notes"]
    assert [n["text"] for n in notes] == ["I prefer dark mode"]
    bob_key = new_key(api, api.bob, ["memory:read"])
    assert ok(call(api, bob_key, "list_memory"))["notes"] == []  # each account sees only its own


# --- the same rules as /v1 --------------------------------------------------------------------


@pytest.mark.parametrize("tool,args,scope", [
    ("list_devices", {}, "devices:read"),
    ("run_task", {"instruction": "open maps"}, "tasks:run"),
    ("get_task", {"task_id": "abc"}, "tasks:read"),
    ("answer_question", {"task_id": "abc", "answer": "x"}, "tasks:run"),
    ("cancel_task", {"task_id": "abc"}, "tasks:run"),
    ("get_screenshot", {}, "screen:read"),
    ("list_memory", {}, "memory:read"),
    ("remember", {"text": "x"}, "memory:write"),
])
def test_each_tool_needs_its_scope(api, tool, args, scope):
    others = [s for s in ("devices:read", "tasks:run", "tasks:read", "screen:read", "memory:read", "memory:write")
              if s != scope]
    result = call(api, new_key(api, api.alice, others), tool, **args)
    assert result.is_error and scope in text(result)


def test_other_accounts_phones_and_tasks_are_404(api):
    task = run_task(api.alice, "please wait here")
    for tool, args in [
        ("run_task", {"instruction": "open maps", "device_id": api.alice.device}),
        ("get_screenshot", {"device_id": api.alice.device}),
        ("get_task", {"task_id": task["id"]}),
        ("answer_question", {"task_id": task["id"], "answer": "yes"}),
        ("cancel_task", {"task_id": task["id"]}),
    ]:
        result = call(api, api.bob, tool, **args)
        assert result.is_error and "Unknown" in text(result), (tool, text(result))
    assert api.alice.v1.get(f"/v1/tasks/{task['id']}").json()["status"] in ("queued", "running")
    api.alice.v1.post(f"/v1/tasks/{task['id']}/cancel")


def test_quota_and_audit_apply(api):
    api.cfg.api_daily_tasks = 1
    ok(call(api, api.alice, "run_task", instruction="open maps"))
    over = call(api, api.alice, "run_task", instruction="open maps")
    assert over.is_error and "quota" in text(over)
    api.store.set_admin("bob@example.com", True)
    actions = [e["action"] for e in api.bob.console.get("/api/admin/audit").json()]
    assert "task.create" in actions and "task.finished" in actions


def test_rate_limit_applies_per_request(api):
    api.cfg.api_rate_per_minute = 2
    r = [httpx.post(f"{api.base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {key_of(api.bob)}"}) for _ in range(3)]
    assert r[-1].status_code == 429 and r[-1].headers["retry-after"]


def test_results_name_no_model_or_provider(api):
    result = call(api, api.alice, "run_task", instruction="ask me something")
    task = ok(result)
    answered = call(api, api.alice, "answer_question", task_id=task["id"], answer="go")
    done = call(api, api.alice, "get_task", task_id=task["id"], wait_seconds=10)
    blob = "\n".join(text(r) + json.dumps(r.structured_content) for r in (result, answered, done))
    assert not LEAKS.search(blob), LEAKS.search(blob)


# --- `openmob mcp`: the local stdio proxy ------------------------------------------------------


def test_stdio_proxy_forwards_to_the_remote_server(api):
    import os
    import sys

    from mcp import StdioServerParameters

    env = {**os.environ, "OPENMOB_API_KEY": key_of(api.alice)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "openmob.main", "mcp", "--server", api.base],
                                   env=env)

    async def session():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            devices = await client.call_tool("list_devices", {})
            task = await client.call_tool("run_task", {"instruction": "open maps"})
            other = await client.call_tool("get_task", {"task_id": "nope"})
            return tools, devices, task, other, client.instructions

    tools, devices, task, other, instructions = asyncio.run(session())
    assert tools == TOOLS and "run_task" in instructions
    assert [d["id"] for d in ok(devices)["devices"]] == [api.alice.device]
    assert ok(task)["status"] == "succeeded"
    assert other.is_error and "Unknown task" in text(other)


def test_stdio_proxy_needs_a_key():
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "openmob.main", "mcp", "--server", "http://127.0.0.1:9"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert r.returncode == 2 and "OPENMOB_API_KEY" in r.stderr and r.stdout == ""
