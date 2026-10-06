"""The server with a gatekeeper phone (docs/design/phone-gatekeeper.md, 2.8 and 3; docs/phone-protocol.md).

The phone owns approval: the server never sends it an approval_request for an action, waits on the phone's own
prompt (gate_needs_approval, then gate_event), re-sends the identical call at most once per approval, and reports
refusals to the agent with their stable codes instead of retrying. API keys can decline but never approve
(409 approval_on_phone). A secure lock screen mid-task pauses the task instead of failing it.
"""

import threading
import uuid

from test_api_v1 import api, run_task, wait_status  # noqa: F401  (the fixture)
from test_remote import FakePhone, PhoneReply, new_signer, wait_for

from conftest import FakeDriver


class GatekeeperPhone(FakePhone):
    """A FakePhone whose app has the gatekeeper: taps on "Buy now" ask the owner on the phone."""

    def __init__(self, *args, owner=None, refuse=None, locked=None, dialog=None, task_owner=None, **kw):
        kw.setdefault("capabilities", ["wait_idle", "resolve_url", "gatekeeper", "device_lock"])
        self.owner = owner  # "approve" | "deny" | None (never answers)
        self.refuse = refuse  # a refusal code every tap gets
        self.task_owner = task_owner  # "approve" | "deny" | None: the answer to the task prompt
        self.task_requests, self.tasks_granted, self.act_params = [], set(), []
        self.dialog = dialog  # seconds an unreadable system dialog stays up (None: none; -1: forever)
        self.locked = locked  # seconds the phone stays locked (None: never locked; -1: forever)
        self.prompts = []  # approval ids the phone asked its owner about
        self.permits = set()
        super().__init__(*args, **kw)

    def handle(self, method, params):
        if method == "task_grant":
            self.task_requests.append(params)
            if params["task"] in self.tasks_granted:
                return {"granted": True}
            approval_id = "gk" + uuid.uuid4().hex
            if self.task_owner:
                state = {"approve": "approved", "deny": "denied"}[self.task_owner]
                if state == "approved":
                    self.tasks_granted.add(params["task"])
                threading.Timer(0.3, self.send, ({"type": "gate_event", "approval_id": approval_id,
                                                  "state": state},)).start()
            raise PhoneReply({"ok": False, "code": "gate_needs_approval", "approval_id": approval_id,
                              "error": "Waiting for the owner's approval of this task on the phone.",
                              "gate": {"rule": "risk:task", "kind": "task",
                                       "question": "Agent wants to: " + params["text"] + ". Allow the steps this needs?"}})
        if method == "tap":
            if self.dialog is not None:
                up, self.dialog = self.dialog, None
                message = "Kalshi is asking for location: choose on your phone, then the task continues."
                threading.Timer(0, self.send, ({"type": "owner_dialog", "open": True, "message": message},)).start()
                if up >= 0:
                    threading.Timer(up, self.send, ({"type": "owner_dialog", "open": False},)).start()
                raise PhoneReply({"ok": False, "code": "needs_owner_dialog", "error": message,
                                  "gate": {"rule": "builtin:owner_dialog"}})
            if self.locked is not None:
                lock_for, self.locked = self.locked, None
                threading.Timer(0, self.send, ({"type": "device_lock", "locked": True, "secure": True},)).start()
                if lock_for >= 0:
                    threading.Timer(lock_for, self.send, ({"type": "device_lock", "locked": False},)).start()
                raise PhoneReply({"ok": False, "code": "phone_locked", "error": "The phone is locked.",
                                  "gate": {"rule": "builtin:keyguard"}})
            if self.refuse:
                raise PhoneReply({"ok": False, "code": self.refuse, "error": "Blocked on the phone: a banking app.",
                                  "gate": {"rule": "builtin:sensitive_apps", "package": "com.chase.sig.android"}})
            key = (params["x"], params["y"])
            if key not in self.permits:
                approval_id = uuid.uuid4().hex
                self.prompts.append(approval_id)
                # A phone with the task prompt (just in time): this tap-level step asks for the whole task.
                task_prompt = bool(self.task_owner and params.get("task_text") and not self.tasks_granted)
                answer = self.task_owner if task_prompt else self.owner
                if answer:
                    state = {"approve": "approved", "deny": "denied", "expire": "expired"}[answer]
                    if state == "approved":
                        self.permits.add(key)
                        if task_prompt:
                            self.tasks_granted.add(params["task"])
                    threading.Timer(0.3, self.send, ({"type": "gate_event", "approval_id": approval_id,
                                                      "state": state},)).start()
                question = (f"Agent wants to: {params.get('task_text')}. Allow the steps this needs? Next: tap Send"
                            if task_prompt else "Allow tapping “Buy now” in Amazon?")
                raise PhoneReply({"ok": False, "code": "gate_needs_approval", "approval_id": approval_id,
                                  "error": "Waiting for the owner's approval on the phone.",
                                  "gate": {"rule": "risk:words:buy", "kind": "task" if task_prompt else "pay",
                                           "question": question}})
            self.permits.discard(key)  # one-shot
        self.act_params.append(dict(params))
        params = {k: v for k, v in params.items() if k not in ("mode", "task", "hint", "task_text", "task_source")}  # the gate's, not the gesture's
        return super().handle(method, params)


def gatekeeper(api, **kw):
    """Replace Alice's phone with a gatekeeper app (same pairing, a newer app)."""
    api.alice.phone.disconnect()
    driver = FakeDriver(packages=["com.android.settings"])
    phone = GatekeeperPhone(api.base.replace("http", "ws"), driver, device_id="alice-phone", key=api.alice.device_key,
                            signer=new_signer(), **kw)
    assert phone.welcome.wait(5)
    session = api.manager.get(api.alice.device)
    assert wait_for(lambda: session.online and session.driver.gatekeeper)
    api.alice.phone, api.alice.driver = phone, driver
    return phone, driver


def taps(phone):
    return [p for m, p in phone.calls if m == "tap"]


def test_one_prompt_on_the_phone_and_one_resend(api):
    phone, driver = gatekeeper(api, owner=None)
    task = run_task(api.alice, "check the cart")
    pending = wait_status(api.alice, task["id"], "needs_confirmation")
    assert "Buy now" in pending["question"]
    # The key can't approve, even one allowed to approve risky actions: only the phone's owner can.
    r = api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": True})
    assert r.status_code == 409 and r.json()["code"] == "approval_on_phone"
    assert not phone.approval_requests  # no server-worded prompt: the phone is already asking
    first = taps(phone)[0]
    assert first["mode"] == "auto" and first["task"] == task["id"] and "Buy now" in first.get("hint", "")
    phone.permits.add((first["x"], first["y"]))  # the owner approves with the screen lock
    phone.send({"type": "gate_event", "approval_id": phone.prompts[0], "state": "approved"})
    wait_status(api.alice, task["id"], "succeeded")
    assert len(taps(phone)) == 2 and any(a[0] == "tap" for a in driver.actions)  # refused once, re-sent once, ran once
    assert len(phone.prompts) == 1
    # A replayed gate_event does nothing: no third tap.
    phone.send({"type": "gate_event", "approval_id": phone.prompts[0], "state": "approved"})
    assert not wait_for(lambda: len(taps(phone)) > 2, timeout=0.5)


def test_a_denial_on_the_phone_means_no_tap(api):
    phone, driver = gatekeeper(api, owner="deny")
    task = run_task(api.alice, "check the cart")
    done = wait_status(api.alice, task["id"], "succeeded", "failed", "cancelled")
    assert done["status"] in ("succeeded", "failed", "cancelled")
    assert not any(a[0] == "tap" for a in driver.actions)
    assert len(taps(phone)) == 1  # never re-sent


def _log(api):
    return [e["message"] for e in api.manager.get(api.alice.device).coordinator.log]


def test_a_decline_and_no_answer_are_told_apart(api):
    """Only the owner's explicit no is "declined"; a prompt that ended without a decision (timed out, or the
    system interrupted it and nobody came back) is "no answer", and the agent isn't told the owner said no."""
    phone, driver = gatekeeper(api, owner="deny")
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "succeeded", "failed", "cancelled")
    assert any("gate_denied" in m and "declined" in m for m in _log(api))
    before = len(_log(api))
    phone, driver = gatekeeper(api, owner="expire")
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "succeeded", "failed", "cancelled")
    log = _log(api)[before:]
    assert any("gate_no_answer" in m and "no decision" in m for m in log)
    assert not any("gate_denied" in m or "declined" in m for m in log)
    assert not any(a[0] == "tap" for a in driver.actions) and len(taps(phone)) == 1  # never re-sent


def test_no_answer_from_the_driver_is_its_own_code():
    import pytest

    from openmob.phone.base import GateRefused
    from openmob.phone.remote import RemoteDriver

    driver = RemoteDriver(_Link([GateRefused("gate_needs_approval", "waiting", approval_id="a1")]))
    driver.capabilities = {"gatekeeper"}
    driver.gate_approver = lambda refusal: "no_answer"
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_no_answer" and len(driver.link.calls) == 1


TASK_CAPS = ["wait_idle", "resolve_url", "gatekeeper", "device_lock", "task_grant"]


def test_task_approval_is_just_in_time(api):
    """No request at task start: the phone asks for the task at the first step that needs a tap, worded from the
    request; the act calls carry it."""
    phone, driver = gatekeeper(api, task_owner="approve", capabilities=TASK_CAPS)
    task = run_task(api.alice, "check the cart")
    done = wait_status(api.alice, task["id"], "succeeded", timeout=12)
    assert phone.task_requests == []  # nothing asked up front
    first = next(p for p in phone.act_params if "x" in p)  # the tap
    assert first["task_text"] == "check the cart" and first["task_source"] == "test" and first["task"] == task["id"]
    log = _log(api)
    assert any("Waiting for your approval of this task on the phone: Agent wants to: check the cart" in m for m in log)
    assert done["prompts_count"] == 1


def test_a_declined_task_prompt_stops_the_task(api):
    phone, driver = gatekeeper(api, task_owner="deny", capabilities=TASK_CAPS)
    task = run_task(api.alice, "check the cart")
    done = wait_status(api.alice, task["id"], "failed", "cancelled", timeout=10)
    assert done["outcome"]["reason"] == "task_declined"
    assert not any(a[0] == "tap" for a in driver.actions)


def test_the_api_can_decline_and_nothing_is_sent(api):
    phone, driver = gatekeeper(api, owner=None)
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "needs_confirmation")
    assert api.alice.v1.post(f"/v1/tasks/{task['id']}/answer", json={"approve": False}).json() == {"ok": True}
    wait_status(api.alice, task["id"], "succeeded", "failed", "cancelled")
    assert len(taps(phone)) == 1 and not any(a[0] == "tap" for a in driver.actions)


def test_a_refusal_is_reported_with_its_code_and_not_retried(api):
    phone, driver = gatekeeper(api, refuse="gate_blocked_app")
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "succeeded", "failed")
    assert len(taps(phone)) == 1
    session = api.manager.get(api.alice.device)
    messages = [e["message"] for e in session.coordinator.log]
    assert any("gate_blocked_app" in m for m in messages)


def test_a_secure_lock_mid_task_pauses_then_resumes(api):
    api.cfg.locked_wait_seconds = 5
    phone, driver = gatekeeper(api, owner="approve", locked=0.5)
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "succeeded", timeout=12)
    session = api.manager.get(api.alice.device)
    messages = [e["message"] for e in session.coordinator.log]
    assert any("Unlock your phone to continue" in m for m in messages)
    assert any("unlocked" in m for m in messages)  # paused, not failed: the agent looked again and carried on


def test_a_phone_that_stays_locked_fails_as_phone_locked(api):
    api.cfg.locked_wait_seconds = 0.5
    phone, driver = gatekeeper(api, owner="approve", locked=-1)
    task = run_task(api.alice, "check the cart")
    done = wait_status(api.alice, task["id"], "failed", timeout=10)
    assert done["outcome"]["reason"] == "phone_locked"
    assert not any(a[0] == "tap" for a in driver.actions)


class _Link:
    closed = False

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_gate_params_go_only_to_gatekeeper_phones():
    from openmob.phone.remote import RemoteDriver

    old = RemoteDriver(_Link([None]))
    old.gate_mode, old.gate_task = "ask", "t1"
    old.gate_hint("risky")
    old.tap(1, 2)
    assert old.link.calls == [("tap", {"x": 1, "y": 2})]

    new = RemoteDriver(_Link([None, None]))
    new.capabilities = {"gatekeeper"}
    new.gate_mode, new.gate_task = "never", "t1"
    new.gate_hint("risky")
    new.tap(1, 2)
    new.tap(1, 2)
    assert new.link.calls[0] == ("tap", {"x": 1, "y": 2, "mode": "never", "task": "t1", "hint": "risky"})
    assert "hint" not in new.link.calls[1][1]  # a hint is for one call


def test_a_denied_or_changed_gate_is_reported_not_retried():
    import pytest

    from openmob.phone.base import GateRefused
    from openmob.phone.remote import RemoteDriver

    driver = RemoteDriver(_Link([GateRefused("gate_needs_approval", "waiting", approval_id="a1")]))
    driver.capabilities = {"gatekeeper"}
    driver.gate_approver = lambda refusal: False
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_denied" and len(driver.link.calls) == 1
    driver = RemoteDriver(_Link([GateRefused("gate_screen_changed", "moved")]))
    driver.capabilities = {"gatekeeper"}
    driver.gate_approver = lambda refusal: True
    with pytest.raises(GateRefused):
        driver.tap(1, 2)
    assert len(driver.link.calls) == 1


def test_an_unreadable_system_dialog_pauses_then_resumes(api):
    api.cfg.owner_dialog_wait_seconds = 5
    phone, driver = gatekeeper(api, owner="approve", dialog=0.5)
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "succeeded", timeout=12)
    messages = [e["message"] for e in api.manager.get(api.alice.device).coordinator.log]
    assert any("Kalshi is asking for location: choose on your phone" in m for m in messages)
    assert any("answered on the phone" in m for m in messages)


def test_the_dashboard_shows_the_open_system_dialog(api):
    api.cfg.owner_dialog_wait_seconds = 3
    phone, driver = gatekeeper(api, owner="approve", dialog=-1)
    run_task(api.alice, "check the cart")
    status = lambda: api.alice.console.get(f"/api/devices/{api.alice.device}/status").json()  # noqa: E731
    assert wait_for(lambda: (status().get("owner_dialog") or "").startswith("Kalshi is asking for location"))
    page = api.alice.console.get("/").text
    assert "function ownerDialogText" in page and "answer on your phone" in page


def test_an_unanswered_system_dialog_ends_the_task_as_needs_owner_dialog(api):
    api.cfg.owner_dialog_wait_seconds = 0.5
    phone, driver = gatekeeper(api, owner="approve", dialog=-1)
    task = run_task(api.alice, "check the cart")
    done = wait_status(api.alice, task["id"], "failed", timeout=10)
    assert done["outcome"]["reason"] == "needs_owner_dialog"
    assert not any(a[0] == "tap" for a in driver.actions)  # never tapped blindly


def test_any_pushed_lock_pauses_even_a_swipe_lock():
    """N5: after the owner presses power the phone reports the (swipe) lock and doesn't unlock itself."""
    from openmob.phone.remote import RemoteDriver

    driver = RemoteDriver(_Link([]))
    driver.on_device_lock({"locked": True, "secure": False})
    assert driver.locked
    driver.on_device_lock({"locked": False})
    assert not driver.locked and driver.wait_unlocked(0.1)


def test_old_apps_keep_server_side_approvals(api):
    """No gatekeeper capability: today's behaviour (approval_request to the phone, signed approval)."""
    task = run_task(api.alice, "check the cart")
    wait_status(api.alice, task["id"], "needs_confirmation")
    assert wait_for(lambda: api.alice.phone.approval_requests)
    tap_params = [p for m, p in api.alice.phone.calls if m == "tap"]
    assert tap_params == []  # nothing sent before the approval, and no gate params ever
