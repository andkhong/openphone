"""Connecting a phone: the QR code is the credential (no session on the phone), unpair by device key,
every paired phone listed and removable, a sign-up that doesn't reveal accounts, and enrollments
for `openmob phone add`.

Waits are on conditions, never fixed sleeps.
"""

import time

import httpx
import pytest
from conftest import FakeDriver, ScriptedClient, tool_use
from fastapi.testclient import TestClient
from test_hosted import _up
from test_remote import FakePhone, free_port, new_signer, public_key_b64, wait_for

from openmob import accounts
from openmob.accounts import AccountStore, AuthError, PairingLimit
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.history import History
from openmob.ledger import Ledger
from openmob.phone.apps import AppResolver
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker


def _manager(tmp_path):
    def build_agent(cfg, io, driver, cache):
        return Agent(ScriptedClient([[tool_use("done", "d", summary="ok", success=True)]] * 5), driver,
                     AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, io, use_ocr=False)

    return DeviceManager(Config(settle_delay=0, launch_settle_delay=0), build_agent, Speaker(enabled=False),
                         adb_discovery=False, history=History(tmp_path / "h.sqlite"))


@pytest.fixture
def srv(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    manager = _manager(tmp_path)
    port = free_port()
    serve_in_background(create_app(manager, store=store, ledger=ledger), "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    assert wait_for(lambda: _up(base))

    def user(email):
        c = httpx.Client(base_url=base)
        c.post("/api/auth/signup", json={"email": email, "password": "password123"})
        assert c.post("/api/auth/login", json={"email": email, "password": "password123"}).status_code == 200
        return c

    def claim(code, device_id, model="SM-S948U1", **extra):
        return httpx.post(f"{base}/api/pair/claim", json={"code": code, "device_id": device_id, "model": model, **extra})

    class S:
        pass

    s = S()
    s.base, s.ws, s.store, s.ledger, s.manager, s.user, s.claim = base, base.replace("http", "ws"), store, ledger, manager, user, claim
    return s


# --- the code is the credential ---------------------------------------------------------------


def test_claim_needs_no_session_and_the_phone_never_gets_one(srv):
    alice = srv.user("alice@example.com")
    p = alice.post("/api/pairing").json()
    assert p["ttl"] <= 120 and p["email"] == "alice@example.com" and p["pair_url"].endswith(p["code"])
    # The phone first learns who it would pair with (shown in its screen-lock prompt); the code isn't used.
    check = httpx.post(f"{srv.base}/api/pair/check", json={"code": p["code"]})
    assert check.status_code == 200 and check.json()["email"] == "alice@example.com"
    r = srv.claim(p["code"], "phone-1", approval_key=public_key_b64(new_signer()))  # no cookie, no header
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"key", "server", "email", "device_id"} and body["email"] == "alice@example.com"
    assert "set-cookie" not in r.headers and "session" not in r.text.lower()
    assert srv.store.verify_device("phone-1", body["key"]) == srv.store.account_id_for("alice@example.com")
    assert len(srv.store._data["sessions"]) == 1  # only the dashboard's own sign-in


def test_code_is_single_use_expires_in_two_minutes_and_is_stored_hashed(srv, monkeypatch):
    alice = srv.user("alice@example.com")
    code = alice.post("/api/pairing").json()["code"]
    assert len(code) >= 8 + 22  # 8-character id + 128-bit secret
    raw = srv.store.path.read_text()
    assert code not in raw and code[8:] not in raw  # only a hash of the secret is stored
    assert srv.claim(code, "phone-1").status_code == 200
    again = srv.claim(code, "phone-2")
    assert again.status_code == 400 and "invalid or has expired" in again.json()["detail"]

    code = alice.post("/api/pairing").json()["code"]
    real = time.time
    monkeypatch.setattr(accounts.time, "time", lambda: real() + accounts.PAIRING_TTL + 1)
    assert srv.claim(code, "phone-3").status_code == 400
    monkeypatch.setattr(accounts.time, "time", real)
    assert accounts.PAIRING_TTL == 120


def test_code_is_bound_to_its_account(srv):
    alice, bob = srv.user("alice@example.com"), srv.user("bob@example.com")
    code = alice.post("/api/pairing").json()["code"]
    # Bob's signed-in session can't redeem Alice's code on the older route…
    r = httpx.post(f"{srv.base}/api/pair/redeem", headers={"X-OpenMob-Session": bob.cookies["openmob_session"]},
                   json={"code": code, "device_id": "bob-phone", "model": "Pixel"})
    assert r.status_code == 400
    # …and the code, claimed, pairs the phone to Alice, never to whoever claims it.
    key = srv.claim(code, "phone-1").json()["key"]
    assert srv.store.verify_device("phone-1", key) == srv.store.account_id_for("alice@example.com")
    assert [d["id"] for d in bob.get("/api/devices").json()] == []


def test_a_new_code_voids_the_previous_one(srv):
    alice = srv.user("alice@example.com")
    first = alice.get("/api/pairing").json()["code"]
    alice.get("/api/pairing")
    assert srv.claim(first, "phone-1").status_code == 400


def test_wrong_guesses_void_the_code_and_are_rate_limited_per_address(srv):
    alice = srv.user("alice@example.com")
    code = alice.post("/api/pairing").json()["code"]
    wrong = code[:8] + "x" * len(code[8:])
    for _ in range(accounts.PAIRING_MAX_FAILURES):
        assert srv.claim(wrong, "thief").status_code == 400
    # Voided: even the right code no longer works.
    assert srv.claim(code, "phone-1").status_code == 400
    statuses = [httpx.post(f"{srv.base}/api/pair/check", json={"code": "nope" * 8}).status_code for _ in range(10)]
    assert statuses[-1] == 429  # this address made too many wrong attempts
    assert srv.claim(alice.post("/api/pairing").json()["code"], "phone-1").status_code == 429


def test_guesses_are_rate_limited_per_account_across_addresses(tmp_path):
    store = AccountStore(tmp_path / "a.json")
    app = create_app(_manager(tmp_path), store=store, ledger=Ledger(tmp_path / "l.sqlite"))
    owner = TestClient(app, client=("10.0.0.1", 1))
    owner.post("/api/auth/signup", json={"email": "o@example.com", "password": "password123"})
    owner.post("/api/auth/login", json={"email": "o@example.com", "password": "password123"})
    code = owner.post("/api/pairing").json()["code"]
    for i in range(12):  # many addresses, a few wrong guesses each, all at Alice's code id
        guesser = TestClient(app, client=(f"10.1.0.{i}", 1))
        code = owner.post("/api/pairing").json()["code"] if i % 4 == 0 else code
        guesser.post("/api/pair/claim", json={"code": code[:8] + "y" * 22, "device_id": "x", "model": "x"})
    fresh = owner.post("/api/pairing").json()["code"]
    r = TestClient(app, client=("10.2.0.1", 1)).post("/api/pair/claim", json={"code": fresh, "device_id": "p", "model": "p"})
    assert r.status_code == 429 and "this account" in r.json()["detail"]


def test_minting_codes_is_limited_per_account(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "a.json")
    acct = store.create_account("o@example.com", "password123")
    monkeypatch.setattr(accounts, "PAIRING_MINTS_PER_HOUR", 3)
    for _ in range(3):
        store.create_pairing_code(acct)
    with pytest.raises(PairingLimit):
        store.create_pairing_code(acct)


def test_pairing_is_audited(srv):
    alice = srv.user("alice@example.com")
    srv.claim(alice.post("/api/pairing").json()["code"], "phone-1")
    actions = [r["action"] for r in srv.ledger.audit_log(account_id=srv.store.account_id_for("alice@example.com"))]
    assert "device.paired" in actions and "pairing.code_created" in actions


# --- unpair with the device key -----------------------------------------------------------------


def test_phone_unpairs_itself_with_its_device_key(srv):
    alice = srv.user("alice@example.com")
    key = srv.claim(alice.post("/api/pairing").json()["code"], "phone-1").json()["key"]
    phone = FakePhone(srv.ws, FakeDriver(), device_id="phone-1", key=key)
    assert phone.welcome.wait(5)
    bad = httpx.post(f"{srv.base}/api/phone/unpair", json={"device_id": "phone-1"},
                     headers={"X-OpenMob-Device-Key": "wrong"})
    assert bad.status_code == 401
    ok = httpx.post(f"{srv.base}/api/phone/unpair", json={"device_id": "phone-1"}, headers={"X-OpenMob-Device-Key": key})
    assert ok.status_code == 200
    phone.task.result(5)
    assert phone.close_code == 4401
    assert alice.get("/api/devices").json() == []
    with pytest.raises(AuthError):
        srv.store.verify_device("phone-1", key)


# --- every paired phone is listed and removable --------------------------------------------------


def test_offline_and_never_connected_phones_are_listed_and_removable(srv):
    alice = srv.user("alice@example.com")
    leaked = srv.claim(alice.post("/api/pairing").json()["code"], "leaked", model="Pixel 7").json()["key"]
    online_key = srv.claim(alice.post("/api/pairing").json()["code"], "mine", model="SM-S948U1").json()["key"]
    phone = FakePhone(srv.ws, FakeDriver(), device_id="mine", key=online_key)
    assert phone.welcome.wait(5)
    rows = {d["id"]: d for d in alice.get("/api/devices").json()}
    assert set(rows) == {"app-leaked", "app-mine"}
    assert rows["app-leaked"]["online"] is False and rows["app-leaked"]["session"] is False
    assert rows["app-leaked"]["model"] == "Pixel 7" and rows["app-leaked"]["paired_at"] > 0
    assert rows["app-mine"]["online"] is True and rows["app-mine"]["last_seen"] > 0
    # The never-connected pairing is removed from the dashboard; the key stops working.
    assert alice.post("/api/devices/app-leaked/revoke").json() == {"ok": True}
    with pytest.raises(AuthError):
        srv.store.verify_device("leaked", leaked)
    # Removing a connected phone closes its socket.
    assert alice.post("/api/devices/app-mine/revoke").json() == {"ok": True}
    phone.task.result(5)
    assert phone.close_code == 4401 and alice.get("/api/devices").json() == []
    actions = [r["action"] for r in srv.ledger.audit_log(account_id=srv.store.account_id_for("alice@example.com"))]
    assert actions.count("device.removed") == 2


def test_v1_devices_lists_offline_phones_with_details(srv):
    alice = srv.user("alice@example.com")
    srv.claim(alice.post("/api/pairing").json()["code"], "phone-1", model="SM-S948U1")
    acct = srv.store.account_id_for("alice@example.com")
    _, key = srv.store.create_api_key(acct, "t", ["devices:read"])
    [d] = httpx.get(f"{srv.base}/v1/devices", headers={"Authorization": f"Bearer {key}"}).json()["devices"]
    assert d["id"] == "app-phone-1" and d["online"] is False and d["model"] == "SM-S948U1"
    assert d["platform"] == "android" and d["last_seen"] and d["paired_at"]


# --- sign-up --------------------------------------------------------------------------------------


def test_signup_does_not_reveal_existing_emails(srv):
    srv.user("alice@example.com")
    new = httpx.post(f"{srv.base}/api/auth/signup", json={"email": "new@example.com", "password": "password123"})
    taken = httpx.post(f"{srv.base}/api/auth/signup", json={"email": "alice@example.com", "password": "different99"})
    assert new.status_code == taken.status_code == 200
    assert new.json() == taken.json() and "session" not in new.json()
    assert "set-cookie" not in new.headers and "set-cookie" not in taken.headers
    # Alice's account is untouched: her password still works, the other doesn't.
    assert httpx.post(f"{srv.base}/api/auth/login", json={"email": "alice@example.com", "password": "different99"}).status_code == 401
    bad = httpx.post(f"{srv.base}/api/auth/signup", json={"email": "x@example.com", "password": "short"})
    assert bad.status_code == 400  # only the input itself is judged


# --- enrollments (openmob phone add) --------------------------------------------------------------


def test_enrollment_needs_devices_add_and_pairs_only_after_the_phone_confirms(srv):
    alice = srv.user("alice@example.com")
    acct = srv.store.account_id_for("alice@example.com")
    _, read_only = srv.store.create_api_key(acct, "r", ["devices:read"])
    assert httpx.post(f"{srv.base}/v1/enrollments", headers={"Authorization": f"Bearer {read_only}"}).status_code == 403
    _, key = srv.store.create_api_key(acct, "cli", ["devices:add"])
    api = httpx.Client(base_url=srv.base, headers={"Authorization": f"Bearer {key}"})
    e = api.post("/v1/enrollments", json={}).json()
    assert e["already_paired"] is False and e["app_link"].startswith("openmob://connect?server=")
    status = api.get(f"/v1/enrollments/{e['enrollment_id']}").json()
    assert status == {"status": "pending", "device_id": None, "online": False, "model": None}
    assert alice.get("/api/devices").json() == []  # nothing is paired until the phone redeems
    phone_key = srv.claim(e["code"], "usb-phone").json()["key"]
    assert api.get(f"/v1/enrollments/{e['enrollment_id']}").json()["status"] == "paired"
    phone = FakePhone(srv.ws, FakeDriver(), device_id="usb-phone", key=phone_key)
    assert phone.welcome.wait(5)
    assert wait_for(lambda: api.get(f"/v1/enrollments/{e['enrollment_id']}").json()["online"])
    again = api.post("/v1/enrollments", json={"device_id": "usb-phone"}).json()
    assert again["already_paired"] is True and again["online"] is True and "code" not in again
    # Another account's key can't read this enrollment.
    srv.user("bob@example.com")
    _, bob_key = srv.store.create_api_key(srv.store.account_id_for("bob@example.com"), "b", ["devices:add"])
    assert httpx.get(f"{srv.base}/v1/enrollments/{e['enrollment_id']}",
                     headers={"Authorization": f"Bearer {bob_key}"}).status_code == 404


def test_phone_pages_are_served(srv):
    alice = srv.user("alice@example.com")
    assert "Your phones" in alice.get("/phones").text and "/api/devices" in alice.get("/phones").text
    assert 'id="newPhone"' in alice.get("/").text and 'href="/phones"' in alice.get("/").text


def test_devices_add_is_never_a_default_scope():
    assert "devices:add" in accounts.SCOPES and "devices:add" not in accounts.DEFAULT_SCOPES


def test_opening_the_live_view_offers_screen_sharing_once(srv):
    alice = srv.user("alice@example.com")
    key = srv.claim(alice.post("/api/pairing").json()["code"], "phone-1").json()["key"]
    phone = FakePhone(srv.ws, FakeDriver(), device_id="phone-1", key=key)  # no video allowed
    assert phone.welcome.wait(5)
    import websockets.sync.client as wsc

    cookie = f"openmob_session={alice.cookies['openmob_session']}"
    for _ in range(2):
        with wsc.connect(f"{srv.ws}/api/devices/app-phone-1/screen/ws", additional_headers={"Cookie": cookie}) as w:
            w.recv(timeout=5)
    assert wait_for(lambda: any(k == "live_view" for k, _ in phone.events))
    assert sum(k == "live_view" for k, _ in phone.events) == 1
