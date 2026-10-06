"""Phone sockets and approvals under a leaked device key (security review T5–T9, and the rest).

- One live socket per phone: a newer connection closes the older one (4409), and a stale socket
  can't act. Unpair and re-pair close every socket.
- Approvals are bound to the phone's screen lock: once a phone has registered its approval key, a
  yes must carry a fresh signature by it. A stolen device key can't approve purchases.

Waits are on conditions, never fixed sleeps.
"""

import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeDriver, ScriptedClient, tool_use
from test_hosted import _up
from test_remote import FakePhone, free_port, new_signer, public_key_b64, sign_approval, wait_for

from openmob.accounts import AccountStore
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.history import History
from openmob.ledger import Ledger
from openmob.phone.apps import AppResolver
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker


@pytest.fixture
def hosted(tmp_path):
    """Accounts-mode server whose agent always wants to tap "Buy now". Alice has two phones, Bob one;
    every phone registered a screen-lock approval key when it paired."""
    def build_agent(cfg, io, driver, cache):
        script = [[tool_use("tap", "t", x=10, y=10, target="Buy now")],
                  [tool_use("done", "d", summary="ok", success=True)]] * 20
        return Agent(ScriptedClient(script), driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, io,
                     use_ocr=False)

    store = AccountStore(tmp_path / "accounts.json")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    manager = DeviceManager(Config(settle_delay=0, launch_settle_delay=0), build_agent, Speaker(enabled=False),
                            adb_discovery=False, history=History(tmp_path / "h.sqlite"))
    port = free_port()
    serve_in_background(create_app(manager, store=store, ledger=ledger), "127.0.0.1", port)
    base, ws = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"
    assert wait_for(lambda: _up(base))

    def user(email):
        c = httpx.Client(base_url=base)
        r = c.post("/api/auth/signup", json={"email": email, "password": "password123"})
        c.session_token = r.json()["session"]
        return c

    def pair(c, device_id, signer=None):
        code = c.get("/api/pairing").json()["code"]
        body = {"code": code, "device_id": device_id, "model": "Pixel"}
        if signer is not None:
            body["approval_key"] = public_key_b64(signer)
        r = httpx.post(f"{base}/api/pair/redeem", headers={"X-OpenMob-Session": c.session_token}, json=body)
        assert r.status_code == 200, r.text
        return r.json()["key"]

    def phone(device_id, key, signer=None, **kw):
        p = FakePhone(ws, FakeDriver(), device_id=device_id, key=key, signer=signer, **kw)
        return p

    alice, bob = user("alice@example.com"), user("bob@example.com")
    phones = []
    ids = SimpleNamespace(a1=str(uuid.uuid4()), a2=str(uuid.uuid4()), b1=str(uuid.uuid4()))
    signers = SimpleNamespace(a1=new_signer(), a2=new_signer(), b1=new_signer())
    keys = SimpleNamespace(a1=pair(alice, ids.a1, signers.a1), a2=pair(alice, ids.a2, signers.a2),
                           b1=pair(bob, ids.b1, signers.b1))
    pa1, pa2 = phone(ids.a1, keys.a1, signers.a1), phone(ids.a2, keys.a2, signers.a2)
    pb1 = phone(ids.b1, keys.b1, signers.b1)
    for p in (pa1, pa2, pb1):
        assert p.welcome.wait(5)
        phones.append(p)

    def connect(*args, **kw):
        p = phone(*args, **kw)
        phones.append(p)
        return p

    def status(device_id):
        infos = list(manager.get(f"app-{device_id}").coordinator.task_info.values())
        return infos[-1]["status"] if infos else None

    yield SimpleNamespace(base=base, store=store, ledger=ledger, manager=manager, alice=alice, bob=bob, ids=ids,
                          keys=keys, signers=signers, pa1=pa1, pa2=pa2, pb1=pb1, pair=pair, connect=connect,
                          status=status)
    for p in phones:
        try:
            p.disconnect()
        except Exception:
            pass


def closed_with(phone, code):
    return wait_for(lambda: phone.close_code == code)


def start_cart(h, device_id):
    h.alice.post(f"/api/devices/app-{device_id}/command", json={"text": "check the cart"})
    assert wait_for(lambda: h.status(device_id) == "needs_confirmation")


# --- T5: a leaked device key ---------------------------------------------------------------------


@pytest.fixture
def h(hosted):
    return hosted


def test_t5_a_leaked_key_cant_approve_and_the_real_socket_is_told(h):
    attacker = h.connect(h.ids.a1, h.keys.a1, approve=True)  # no screen-lock key: can't sign
    assert attacker.welcome.wait(5)
    assert closed_with(h.pa1, 4409)  # one live socket per phone: the older one is closed, not orphaned
    start_cart(h, h.ids.a2)  # a risky task on Alice's OTHER phone
    assert wait_for(lambda: attacker.approval_requests)  # it is asked (it's the phone's live socket)…
    assert not wait_for(lambda: h.status(h.ids.a2) == "succeeded", timeout=1.0)  # …but its yes is ignored
    assert not any(a[0] == "tap" for a in h.pa2.driver.actions)
    # The real owner can still approve on the other phone, with its screen lock.
    request = h.pa2.approval_requests[-1]
    h.pa2.send(h.pa2.approval(request["id"], request["task_id"], True))
    assert wait_for(lambda: h.status(h.ids.a2) == "succeeded")


# --- T6: re-pairing cuts off sockets on the old key -------------------------------------------------


def test_t6_re_pairing_closes_sockets_on_the_old_key(h):
    attacker = h.connect(h.ids.a1, h.keys.a1)
    assert attacker.welcome.wait(5)
    new_signer_ = new_signer()
    new_key = h.pair(h.alice, h.ids.a1, new_signer_)
    assert closed_with(attacker, 4401)
    again = h.connect(h.ids.a1, h.keys.a1)  # the old key can't come back
    assert closed_with(again, 4401)
    fresh = h.connect(h.ids.a1, new_key, new_signer_)
    assert fresh.welcome.wait(5)


# --- T7: unpairing closes every socket ---------------------------------------------------------------


def test_t7_unpairing_closes_every_socket(h):
    attacker = h.connect(h.ids.a1, h.keys.a1)
    assert attacker.welcome.wait(5) and closed_with(h.pa1, 4409)
    assert h.alice.post(f"/api/devices/app-{h.ids.a1}/revoke").json() == {"ok": True}
    assert closed_with(attacker, 4401)
    assert not h.manager.links.get(f"app-{h.ids.a1}")
    again = h.connect(h.ids.a1, h.keys.a1)
    assert closed_with(again, 4401)
    start_cart(h, h.ids.a2)
    assert wait_for(lambda: h.pa2.approval_requests)
    assert not attacker.approval_requests  # a revoked phone is never asked


# --- T8: another account's phone can't answer ----------------------------------------------------------


def test_t8_another_accounts_phone_cant_answer_an_approval(h):
    start_cart(h, h.ids.a2)
    assert wait_for(lambda: h.pa2.approval_requests)
    request = h.pa2.approval_requests[-1]
    h.pb1.send(h.pb1.approval(request["id"], request["task_id"], True))  # validly signed, by Bob's phone
    assert not wait_for(lambda: h.status(h.ids.a2) == "succeeded", timeout=1.0)
    assert not h.pb1.approval_requests
    h.pa2.send(h.pa2.approval(request["id"], request["task_id"], False))


# --- T9: a superseded socket can't act ----------------------------------------------------------------


def test_t9_a_superseded_socket_is_closed_and_cant_drive_the_phone(h):
    attacker = h.connect(h.ids.a1, h.keys.a1)
    assert attacker.welcome.wait(5)
    legit = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)  # the real phone reconnects after it
    assert legit.welcome.wait(5)
    assert closed_with(attacker, 4409)
    with pytest.raises(Exception):
        attacker.send({"type": "command", "text": "open settings"})
    start_cart(h, h.ids.a2)
    assert wait_for(lambda: legit.approval_requests) and not attacker.approval_requests


# --- one live socket; stale sockets can't act ----------------------------------------------------------


def test_only_the_live_socket_counts(h):
    session = h.manager.get(f"app-{h.ids.a1}")
    old = session.link
    newer = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert newer.welcome.wait(5)
    assert not h.manager.is_live(session, old) and h.manager.is_live(session, session.link)
    assert h.manager.links[f"app-{h.ids.a1}"] == {session.link}


def test_a_replaced_socket_from_elsewhere_alerts_the_owner(h):
    session = h.manager.get(f"app-{h.ids.a1}")
    session.link.peer = "198.51.100.7"  # as if the first connection came from another network
    newer = h.connect(h.ids.a1, h.keys.a1, h.signers.a1)
    assert newer.welcome.wait(5)
    assert wait_for(lambda: any(e["action"] == "device.connection_replaced" for e in h.ledger.audit_log()))
    assert any("reconnected from a new connection" in e["message"] for e in session.coordinator.log)
    assert h.status(h.ids.a1) in (None, "succeeded", "failed")  # nothing was cancelled


# --- signed approvals -----------------------------------------------------------------------------------


def test_signed_approval_works(h):
    start_cart(h, h.ids.a1)
    request = h.pa1.approval_requests[-1]
    h.pa1.send(h.pa1.approval(request["id"], request["task_id"], True))
    assert wait_for(lambda: h.status(h.ids.a1) == "succeeded")


@pytest.mark.parametrize("forge", ["unsigned", "bad_signature", "other_key", "stale", "other_approval"])
def test_unsigned_or_forged_approvals_are_rejected(h, forge):
    start_cart(h, h.ids.a1)
    request = h.pa1.approval_requests[-1]
    approval_id, task_id = request["id"], request["task_id"]
    now = int(time.time())
    message = {"type": "approval", "id": approval_id, "approved": True, "timestamp": now}
    if forge == "bad_signature":
        message["signature"] = "AAAA"
    elif forge == "other_key":
        message["signature"] = sign_approval(new_signer(), approval_id, task_id, True, now)
    elif forge == "stale":  # a real signature, but replayed long after it was made
        message.update(timestamp=now - 3600, signature=sign_approval(h.signers.a1, approval_id, task_id, True, now - 3600))
    elif forge == "other_approval":  # a real signature for a different approval
        message["signature"] = sign_approval(h.signers.a1, "another-id", task_id, True, now)
    h.pa1.send(message)
    assert not wait_for(lambda: h.status(h.ids.a1) == "succeeded", timeout=1.0)
    # Unsigned "no" still works: anyone may decline.
    h.pa1.send({"type": "approval", "id": approval_id, "approved": False})
    assert wait_for(lambda: h.status(h.ids.a1) in ("succeeded", "failed"))
    assert not any(a[0] == "tap" for a in h.pa1.driver.actions)


def test_an_app_phone_without_an_approval_key_can_only_decline(h):
    old_app = str(uuid.uuid4())
    key = h.pair(h.alice, old_app)  # an app too old to send an approval key
    p = h.connect(old_app, key, approve=True)  # it answers yes, unsigned
    assert p.welcome.wait(5)
    h.alice.post(f"/api/devices/app-{old_app}/command", json={"text": "check the cart"})
    assert wait_for(lambda: p.approval_requests)
    request = p.approval_requests[-1]
    assert request["can_approve"] is False and "can only decline" in request["cannot_approve_reason"]
    assert request["signature_required"] is True
    assert wait_for(lambda: any("can't approve yet" in m for k, m in p.events if k == "status"))
    assert not wait_for(lambda: h.status(old_app) == "succeeded", timeout=1.0)
    assert not any(a[0] == "tap" for a in p.driver.actions)
    p.send({"type": "approval", "id": request["id"], "approved": False})  # declining still works
    assert wait_for(lambda: h.status(old_app) in ("succeeded", "failed"))
    assert not any(a[0] == "tap" for a in p.driver.actions)


def test_an_old_app_signing_without_the_action_digest_is_refused(h):
    old_app = str(uuid.uuid4())
    signer = new_signer()
    key = h.pair(h.alice, old_app, signer)
    p = h.connect(old_app, key, signer, legacy_signing=True)  # signs `id|task|true|ts`, as before
    assert p.welcome.wait(5)
    start_cart(h, old_app)
    request = p.approval_requests[-1]
    p.send(p.approval(request["id"], request["task_id"], True))
    assert not wait_for(lambda: h.status(old_app) == "succeeded", timeout=1.0)
    assert not any(a[0] == "tap" for a in p.driver.actions)


def test_a_signed_approval_for_one_action_does_not_authorize_another(h):
    from openmob.phone.approval_keys import action_digest

    start_cart(h, h.ids.a1)
    request = h.pa1.approval_requests[-1]
    # The request names the action (the tool, the app, the target) and its digest; the phone signs that.
    assert request["action"]["tool"] == "tap" and request["digest"] == action_digest(request["action"])
    other = action_digest({**request["action"], "text": "Buy 10 now"})
    h.pa1.send(h.pa1.approval(request["id"], request["task_id"], True, digest=other))  # signed, other action
    assert not wait_for(lambda: h.status(h.ids.a1) == "succeeded", timeout=1.0)
    assert not any(a[0] == "tap" for a in h.pa1.driver.actions)
    h.pa1.send(h.pa1.approval(request["id"], request["task_id"], True))  # this action: approved
    assert wait_for(lambda: h.status(h.ids.a1) == "succeeded")
    assert any(a[0] == "tap" for a in h.pa1.driver.actions)


def test_an_approval_does_not_cover_a_screen_that_changed_while_asking(h):
    from openmob.phone.adb import UIElement

    driver = h.pa1.driver
    driver.elements = [UIElement(0, "Buy now", "", "buy", "Button", True, None, (0, 0, 200, 100))]
    start_cart(h, h.ids.a1)
    request = h.pa1.approval_requests[-1]
    assert request["action"]["text"] == "Buy now"
    # Before the owner answers, the page swaps the button under the same spot.
    driver.elements = [UIElement(0, "Subscribe yearly", "", "buy", "Button", True, None, (0, 0, 200, 100))]
    h.pa1.send(h.pa1.approval(request["id"], request["task_id"], True))
    assert wait_for(lambda: h.status(h.ids.a1) in ("succeeded", "failed"))
    assert not any(a[0] == "tap" for a in driver.actions)


def test_approval_key_registers_on_first_connect_once(h):
    old_app = str(uuid.uuid4())
    key = h.pair(h.alice, old_app)
    signer = new_signer()
    p = h.connect(old_app, key, signer)  # the updated app sends its key in the hello
    assert p.welcome.wait(5)
    assert wait_for(lambda: h.store.device_auth(old_app)["approval_key"] == public_key_b64(signer))
    thief = h.connect(old_app, key, new_signer())  # a later hello can't swap in another key
    assert thief.welcome.wait(5)
    assert h.store.device_auth(old_app)["approval_key"] == public_key_b64(signer)


def test_pairing_rejects_a_malformed_approval_key(h):
    code = h.alice.get("/api/pairing").json()["code"]
    r = httpx.post(f"{h.base}/api/pair/redeem", headers={"X-OpenMob-Session": h.alice.session_token},
                   json={"code": code, "device_id": "x", "model": "Pixel", "approval_key": "not-a-key"})
    assert r.status_code == 400
