"""The MCP drive level over the real server (docs/design/mcp-drive.md): the tools, the scope, the session as a
task the owner sees and can stop, and the same refusals and approvals as the agent loop's.

Same fixture as the /v1 and /mcp tests: two accounts, each with a paired FakePhone whose driver records what
reaches it. No model is called: the outside agent is the test."""

import base64
import json
import time

from types import SimpleNamespace

import httpx
import pytest
from test_api_v1 import BANK, LEAKS, api, run_task, wait_status  # noqa: F401  (api is a fixture)
from test_mcp import call, key_of, mcp, new_key, ok, text
from test_remote import wait_for

from openmob.phone.adb import UIElement

YT = "com.google.android.youtube"
DRIVE_TOOLS = {"start_session", "observe", "get_session", "tap", "type_text", "swipe", "press_key", "launch_app",
               "open_url", "open_link", "open_settings_page", "wait_until", "list_apps", "propose_plan",
               "decline_step", "end_session"}
DRIVE = ["devices:drive"]
DRIVE_SEE = ["devices:drive", "screen:read"]


def button(i, text="", bounds=(0, 1000, 1000, 1100), rid="", cls="Button", clickable=True):
    return UIElement(i, text, "", rid, cls, clickable, None, bounds)


def field(i, hint="", rid="", password=False):
    el = UIElement(i, "", "", rid, "EditText", True, None, (0, 400, 1000, 500), editable=True, password=password)
    el.focused, el.hint = True, hint
    return el


@pytest.fixture
def driver_key(api):
    """Alice's phone showing a plain screen, and a key that may drive it (no screenshots) with its own /v1
    client: a task, a session included, is visible only to the key that created it."""
    api.alice.driver.elements = [button(0, "Search", rid="search")]
    key = new_key(api, api.alice, DRIVE + ["tasks:read", "tasks:run"])
    return SimpleNamespace(key=key, v1=httpx.Client(base_url=api.base, headers={"Authorization": f"Bearer {key}"}, timeout=10))


def start(api, key, **args):
    return ok(call(api, key, "start_session", goal=args.pop("goal", "look around"), **args))


def did(driver, kind):
    return [x for x in driver.actions if x[0] == kind]


def wait_ended(api, key, sid):
    """The session as get_session shows it once the loop has noticed a stop (it polls for one every 0.25 s)."""
    seen = {}

    def ended():
        seen["session"] = ok(call(api, key, "get_session", session_id=sid))
        return seen["session"]["status"] == "ended"

    assert wait_for(ended, timeout=8)
    return seen["session"]


# --- the surface -------------------------------------------------------------------------------


def test_drive_tools_have_schemas_and_annotations(api):
    async def listing(client):
        return (await client.list_tools()).tools, client.instructions

    listed, instructions = mcp(api, api.alice, listing)
    tools = {t.name: t for t in listed}
    assert DRIVE_TOOLS <= set(tools)
    for name in DRIVE_TOOLS:
        assert tools[name].output_schema and tools[name].annotations and tools[name].description, name
    assert tools["observe"].annotations.read_only_hint is True
    assert tools["get_session"].annotations.read_only_hint is True
    assert tools["tap"].annotations.destructive_hint is True and tools["tap"].annotations.read_only_hint is False
    assert tools["press_key"].input_schema["properties"]["key"]["enum"] == ["BACK", "HOME", "ENTER"]
    assert "approve" not in " ".join(DRIVE_TOOLS)  # no tool approves anything
    assert "devices:drive" in instructions and "needs_approval" in instructions


@pytest.mark.parametrize("tool,args", [
    ("start_session", {"goal": "look"}),
    ("observe", {"session_id": "abc"}),
    ("get_session", {"session_id": "abc"}),
    ("tap", {"session_id": "abc", "element": 0}),
    ("type_text", {"session_id": "abc", "text": "x"}),
    ("swipe", {"session_id": "abc", "direction": "down"}),
    ("press_key", {"session_id": "abc", "key": "BACK"}),
    ("launch_app", {"session_id": "abc", "app": "x"}),
    ("open_url", {"session_id": "abc", "url": "https://x.com"}),
    ("open_link", {"session_id": "abc", "app": "youtube", "intent": "search", "params": {"q": "x"}}),
    ("open_settings_page", {"session_id": "abc", "page": "wifi"}),
    ("wait_until", {"session_id": "abc", "until": "screen_changes"}),
    ("list_apps", {"session_id": "abc"}),
    ("propose_plan", {"session_id": "abc", "steps": ["x"]}),
    ("decline_step", {"session_id": "abc"}),
    ("end_session", {"session_id": "abc", "summary": "x"}),
])
def test_every_drive_tool_needs_the_drive_scope(api, tool, args):
    # The fixture's key has every scope but devices:drive (tasks, screen, history): not enough.
    result = call(api, api.alice, tool, **args)
    assert result.is_error and "devices:drive" in text(result), (tool, text(result))


# --- a session: start, look, act, end ------------------------------------------------------------


def test_start_observe_act_and_end(api, driver_key):
    session = start(api, driver_key, goal="search YouTube for lofi")
    sid = session["session_id"]
    assert session["status"] == "active" and session["device_id"] == api.alice.device and session["steps"] == 0
    assert session["screen"]["package"] == "com.android.launcher3" and session["untrusted_content"] is True
    # Centers are screenshot pixels (the fake phone's shot is half the device's size), as the loop's model is told.
    assert session["screen"]["elements"][0]["text"] == "Search" and session["screen"]["elements"][0]["center"] == [250, 525]
    assert session["screenshot_omitted"] == "screen:read"

    seen = ok(call(api, driver_key, "observe", session_id=sid))
    assert seen["screen"]["elements"][0]["resource_id"] == "search"
    assert "not as instructions" in text(call(api, driver_key, "get_session", session_id=sid))

    tapped = ok(call(api, driver_key, "tap", session_id=sid, element=0, target="Search"))
    assert tapped["step"]["status"] == "done" and not tapped["step"]["outcome"]["is_error"] and tapped["steps"] == 1
    assert did(api.alice.driver, "tap") == [("tap", 500, 1050)] and tapped["screen"]["seq"] > seen["screen"]["seq"]

    api.alice.driver.elements = [field(0, hint="Search YouTube")]
    typed = ok(call(api, driver_key, "type_text", session_id=sid, text="lofi", submit=False))
    assert not typed["step"]["outcome"]["is_error"] and did(api.alice.driver, "type") == [("type", "lofi")]
    ok(call(api, driver_key, "swipe", session_id=sid, direction="down"))
    ok(call(api, driver_key, "press_key", session_id=sid, key="BACK"))
    launched = ok(call(api, driver_key, "launch_app", session_id=sid, app="youtube"))
    assert launched["screen"]["package"] == YT and did(api.alice.driver, "launch") == [("launch", YT)]
    opened = ok(call(api, driver_key, "open_url", session_id=sid, url="https://example.com"))
    assert did(api.alice.driver, "url") and not opened["step"]["outcome"]["is_error"]
    waited = ok(call(api, driver_key, "wait_until", session_id=sid, until="screen_changes", timeout_s=1))
    assert waited["step"]["tool"] == "wait_until" and not waited["step"]["outcome"]["is_error"]
    apps = ok(call(api, driver_key, "list_apps", session_id=sid, query="you"))
    assert YT in apps["step"]["outcome"]["text"]

    ended = ok(call(api, driver_key, "end_session", session_id=sid, summary="Searched YouTube for lofi."))
    assert ended["status"] == "ended" and ended["ended"] == {"reason": "ended", "success": True,
                                                              "summary": "Searched YouTube for lofi."}
    # The session was a task: the owner's API, feed and history all have it.
    task = driver_key.v1.get(f"/v1/tasks/{sid}").json()
    assert task["status"] == "succeeded" and task["instruction"] == "search YouTube for lofi" and task["tier"] == "drive"
    assert task["outcome"]["summary"] == "Searched YouTube for lofi." and task["steps"] == 9
    manager_session = api.manager.get(api.alice.device)
    steps = api.manager.history.steps(manager_session.account_id, sid)
    assert [s["tool"] for s in steps] == ["tap_element", "type_text", "scroll", "press_key", "launch_app", "open_url",
                                          "wait_until", "list_apps", "done"]
    assert all(s["source"] == "drive" and s["total_ms"] is not None for s in steps)
    assert steps[1]["args"]["text"] == "<redacted>"
    feed = [e["message"] for e in manager_session.coordinator.snapshot()["log"]]
    assert any("search YouTube for lofi" in m for m in feed) and any("Driven step by step by agent" in m for m in feed)
    assert any(m.startswith("launch_app: Launched") for m in feed)


def test_screenshots_need_screen_read_and_are_audited(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    refused = call(api, driver_key, "observe", session_id=sid, screenshot=True)
    assert refused.is_error and "screen:read" in text(refused)
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))

    seeing = new_key(api, api.alice, DRIVE_SEE)
    sid = start(api, seeing, screenshots=True)["session_id"]
    result = call(api, seeing, "tap", session_id=sid, element=0, target="Search", max_side=300)
    data = ok(result)
    image = next(b for b in result.content if b.type == "image")
    assert image.mime_type == "image/jpeg" and base64.b64decode(image.data)[:2] == b"\xff\xd8"
    assert data["screenshot_omitted"] is None and max(data["screen"]["screenshot"]["width"], data["screen"]["screenshot"]["height"]) == 1200
    plain = ok(call(api, seeing, "get_session", session_id=sid))
    assert plain["screenshot_omitted"] is not None  # get_session never carries pixels
    ok(call(api, seeing, "end_session", session_id=sid, summary="x"))
    api.store.set_admin("bob@example.com", True)
    actions = [e["action"] for e in api.bob.console.get("/api/admin/audit").json()]
    assert "drive.start" in actions and "screen.read" in actions and "task.finished" in actions


# --- the same rules as the loop ------------------------------------------------------------------


def test_blocked_apps_are_refused_and_read_black(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    refused = ok(call(api, driver_key, "launch_app", session_id=sid, app=BANK))
    assert refused["step"]["outcome"]["is_error"] and "blocked" in refused["step"]["outcome"]["text"]
    assert not did(api.alice.driver, "launch")
    api.alice.driver.package = BANK  # the owner opened the bank by hand
    dark = ok(call(api, driver_key, "observe", session_id=sid))
    assert dark["screen"]["blocked_app"] is True and dark["screen"]["elements"] == [] and dark["screen"]["screenshot"] is None
    assert "blocked" in dark["screenshot_omitted"]
    tap = ok(call(api, driver_key, "tap", session_id=sid, element=0, target="Balance"))
    assert tap["step"]["outcome"]["is_error"] and not did(api.alice.driver, "tap")
    home = ok(call(api, driver_key, "press_key", session_id=sid, key="HOME"))
    assert not home["step"]["outcome"]["is_error"] and ("key", "HOME") in api.alice.driver.actions
    api.alice.driver.package = "com.android.launcher3"
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


def test_password_and_code_fields_are_never_typed(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    label = button(1, "Sign in", cls="TextView", clickable=False, bounds=(0, 200, 1000, 300))  # a labeled screen
    api.alice.driver.elements = [field(0, password=True), label]
    ok(call(api, driver_key, "observe", session_id=sid))  # the screen changed: look before acting
    refused = ok(call(api, driver_key, "type_text", session_id=sid, text="hunter2"))
    assert refused["step"]["outcome"]["refused"] and "password" in refused["step"]["outcome"]["text"]
    assert "don't retry" in refused["next"]
    api.alice.driver.elements = [field(0, hint="Enter the verification code"), label]
    ok(call(api, driver_key, "observe", session_id=sid))
    refused = ok(call(api, driver_key, "type_text", session_id=sid, text="123456"))
    assert refused["step"]["outcome"]["refused"] and "verification codes" in refused["step"]["outcome"]["text"]
    assert not did(api.alice.driver, "type")
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


def test_a_risky_tap_waits_for_the_owner_on_the_phone(api, driver_key):
    api.alice.driver.elements = [button(0, "Buy now")]
    sid = start(api, driver_key, goal="check the cart")["session_id"]
    asking = ok(call(api, driver_key, "tap", session_id=sid, element=0, target="the product"))
    assert asking["status"] == "needs_approval" and asking["step"]["status"] == "needs_approval"
    assert "Buy now" in asking["question"] and asking["approver"] == "phone" and "can't approve" in asking["next"]
    assert not did(api.alice.driver, "tap")
    # Meanwhile: another act is refused, get_task shows the task waiting, the agent can't approve.
    busy = call(api, driver_key, "press_key", session_id=sid, key="HOME")
    assert busy.is_error and "step_in_progress" in text(busy)
    assert driver_key.v1.get(f"/v1/tasks/{sid}").json()["status"] == "needs_confirmation"
    forbidden = driver_key.v1.post(f"/v1/tasks/{sid}/answer", json={"approve": True})
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "confirmation_requires_owner"
    # The agent declines: no tap, the session goes on.
    declined = ok(call(api, driver_key, "decline_step", session_id=sid))
    assert declined["ok"] is True and declined["session"]["status"] == "active"
    assert "did not approve" in declined["session"]["step"]["outcome"]["text"] and not did(api.alice.driver, "tap")
    # Asked again: the owner approves on the phone, with the screen lock, and the tap runs.
    pending = ok(call(api, driver_key, "tap", session_id=sid, element=0, target="the product"))
    assert pending["status"] == "needs_approval"
    assert wait_for(lambda: len(api.alice.phone.approval_requests) >= 2)
    request = api.alice.phone.approval_requests[-1]
    assert request["task_id"] == sid and request["requester"] == "agent"
    api.alice.phone.send(api.alice.phone.approval(request["id"], request["task_id"], True))
    after = ok(call(api, driver_key, "get_session", session_id=sid, wait_seconds=10))
    assert after["status"] == "active" and after["step"]["status"] == "done" and not after["step"]["outcome"]["is_error"]
    assert did(api.alice.driver, "tap") == [("tap", 500, 1050)]
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


def test_ask_every_step_asks_before_each_act(api, driver_key):
    sid = start(api, driver_key, permission_mode="ask")["session_id"]
    asking = ok(call(api, driver_key, "launch_app", session_id=sid, app="youtube"))
    assert asking["status"] == "needs_approval" and "Next step" in asking["question"] and not did(api.alice.driver, "launch")
    ended = ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))  # declines the pending step first
    assert ended["status"] == "ended" and not did(api.alice.driver, "launch")


def test_a_mode_looser_than_the_key_allows_is_refused(api, driver_key):
    result = call(api, driver_key, "start_session", goal="x", permission_mode="never")
    assert result.is_error and "mode_not_allowed" in text(result)


# --- the owner sees it and can stop it -----------------------------------------------------------


def test_the_session_is_visible_and_the_owner_can_stop_it(api, driver_key):
    sid = start(api, driver_key, goal="tidy the home screen")["session_id"]
    devices = ok(call(api, api.alice, "list_devices"))["devices"]
    assert devices[0]["busy"] is True
    snap = api.manager.get(api.alice.device).coordinator.snapshot()
    assert snap["current"] == "tidy the home screen" and snap["task_id"] == sid
    assert wait_for(lambda: ("controller", "agent") in api.alice.phone.events)  # "agent is using your phone"
    task = driver_key.v1.get(f"/v1/tasks/{sid}").json()
    assert task["status"] == "running" and task["instruction"] == "tidy the home screen"
    # Stop from the dashboard / API: the session ends, the agent is told.
    assert driver_key.v1.post(f"/v1/tasks/{sid}/cancel").json()["cancelled"] is True
    ended = wait_ended(api, driver_key, sid)
    assert ended["ended"]["reason"] == "stopped"
    assert wait_for(lambda: ("controller", "") in api.alice.phone.events)
    later = call(api, driver_key, "tap", session_id=sid, element=0, target="x")
    assert later.is_error and "session_ended" in text(later)

    # Stop from the phone ("stop" said or typed on it).
    sid = start(api, driver_key)["session_id"]
    api.alice.phone.send({"type": "command", "text": "stop"})
    assert wait_ended(api, driver_key, sid)["ended"]["reason"] == "stopped"
    assert wait_status(driver_key, sid, "cancelled")


def test_another_key_or_account_cannot_see_the_session(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    other = new_key(api, api.alice, DRIVE)
    for key in (other, new_key(api, api.bob, DRIVE)):
        for tool, args in [("get_session", {}), ("observe", {}), ("tap", {"element": 0}), ("end_session", {"summary": "x"})]:
            result = call(api, key, tool, session_id=sid, **args)
            assert result.is_error and "Unknown" in text(result), (tool, text(result))
    assert ok(call(api, driver_key, "get_session", session_id=sid))["status"] == "active"
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


# --- limits ----------------------------------------------------------------------------------------


def test_rate_limit_applies_to_drive_calls(api, driver_key):
    api.cfg.api_rate_per_minute = 3
    r = [httpx.post(f"{api.base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {key_of(driver_key)}"}) for _ in range(4)]
    assert r[-1].status_code == 429 and r[-1].headers["retry-after"]


def test_the_step_cap_ends_the_session(api, driver_key):
    sid = start(api, driver_key, max_steps=2)["session_id"]
    ok(call(api, driver_key, "press_key", session_id=sid, key="BACK"))
    last = ok(call(api, driver_key, "press_key", session_id=sid, key="BACK"))
    assert last["status"] == "ended" and last["ended"]["reason"] == "step_cap" and last["step"]["status"] == "done"
    assert not last["step"]["outcome"]["is_error"] and last["steps"] == 2
    assert wait_status(driver_key, sid, "failed")["steps"] == 2
    more = call(api, driver_key, "press_key", session_id=sid, key="BACK")
    assert more.is_error and "session_ended" in text(more)


def test_an_idle_session_ends(api, driver_key):
    api.cfg.api_drive_idle_seconds = 1
    sid = start(api, driver_key)["session_id"]
    time.sleep(1.5)
    ended = ok(call(api, driver_key, "get_session", session_id=sid))
    assert ended["status"] == "ended" and ended["ended"]["reason"] == "idle_timeout"
    assert wait_status(driver_key, sid, "failed")


def test_a_session_and_a_task_cannot_run_at_once(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    busy = call(api, api.alice, "run_task", instruction="open maps", wait_seconds=0)
    assert busy.is_error and "phone_busy" in text(busy)
    assert api.alice.v1.post(f"/v1/devices/{api.alice.device}/tasks", json={"instruction": "open maps"}).status_code == 409
    second = call(api, driver_key, "start_session", goal="another")
    assert second.is_error and "phone_busy" in text(second)
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))

    task = run_task(api.alice, "wait a while")
    busy = call(api, driver_key, "start_session", goal="x")
    assert busy.is_error and "phone_busy" in text(busy)
    api.alice.v1.post(f"/v1/tasks/{task['id']}/cancel")
    wait_status(api.alice, task["id"], "cancelled")
    sid = start(api, driver_key)["session_id"]  # free again
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


def test_a_long_act_can_be_waited_for_with_get_session(api, driver_key):
    sid = start(api, driver_key)["session_id"]
    running = ok(call(api, driver_key, "wait_until", session_id=sid, until="text_appears", target="Nope", timeout_s=2,
                      wait_seconds=0))
    assert running["step"]["status"] == "running" and "still running" in running["next"]
    done = ok(call(api, driver_key, "get_session", session_id=sid, wait_seconds=10))
    assert done["step"]["status"] == "done" and done["step"]["tool"] == "wait_until"
    ok(call(api, driver_key, "end_session", session_id=sid, summary="x"))


def test_results_name_no_model_or_provider(api, driver_key):
    results = [call(api, driver_key, "start_session", goal="look")]
    sid = results[0].structured_content["session_id"]
    results.append(call(api, driver_key, "launch_app", session_id=sid, app="youtube"))
    results.append(call(api, driver_key, "get_session", session_id=sid))
    results.append(call(api, driver_key, "end_session", session_id=sid, summary="done"))
    blob = "\n".join(text(r) + json.dumps(r.structured_content) for r in results)
    assert not LEAKS.search(blob), LEAKS.search(blob)
    assert "matched by" not in blob  # how an app name was resolved is internal
