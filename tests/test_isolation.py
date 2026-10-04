"""Account isolation: a user can only ever see or use their own registered phones.

Runs a real server (HTTP + WebSocket) with two accounts, Alice and Bob, each with one paired
phone, and tries every cross-account combination. Every mismatch must be refused.
"""

import httpx
import pytest
from conftest import FakeDriver
from test_hosted import hosted, pair, signup  # noqa: F401  (fixture + helpers)
from test_remote import FakePhone, wait_for

DEVICE_ROUTES = [
    ("GET", "/api/devices/{id}/status", None),
    ("GET", "/api/devices/{id}/screen.jpg", None),
    ("POST", "/api/devices/{id}/command", {"json": {"text": "open my email"}}),
    ("POST", "/api/devices/{id}/stop", None),
    ("POST", "/api/devices/{id}/audio", {"files": {"file": ("a.webm", b"x")}}),
    ("POST", "/api/devices/{id}/revoke", None),
]


@pytest.fixture
def two_users(hosted):  # noqa: F811
    base, manager, store = hosted
    ws = base.replace("http", "ws")
    alice, alice_session = signup(base, "alice@example.com")
    bob, bob_session = signup(base, "bob@example.com")
    alice_key = pair(base, alice, alice_session, device_id="alice-phone")
    bob_key = pair(base, bob, bob_session, device_id="bob-phone")
    alice_phone = FakePhone(ws, FakeDriver(), device_id="alice-phone", key=alice_key)
    bob_phone = FakePhone(ws, FakeDriver(), device_id="bob-phone", key=bob_key)
    assert alice_phone.welcome.wait(5) and bob_phone.welcome.wait(5)
    yield {
        "base": base, "ws": ws, "manager": manager, "store": store,
        "alice": alice, "alice_session": alice_session, "alice_key": alice_key, "alice_phone": alice_phone,
        "bob": bob, "bob_session": bob_session, "bob_key": bob_key, "bob_phone": bob_phone,
    }
    for phone in (alice_phone, bob_phone):
        try:
            phone.disconnect()
        except Exception:
            pass


def call(client, method, path, extra):
    return client.request(method, path, **(extra or {}))


@pytest.mark.parametrize("method,path,extra", DEVICE_ROUTES)
def test_user_cannot_touch_another_users_phone(two_users, method, path, extra):
    u = two_users
    for attacker, victim_device in ((u["bob"], "app-alice-phone"), (u["alice"], "app-bob-phone")):
        r = call(attacker, method, path.format(id=victim_device), extra)
        assert r.status_code == 404, (method, path, r.status_code)  # refused, and existence not revealed
    # The victim's phone received no command and was not stopped or revoked.
    assert u["manager"].get("app-alice-phone").online and u["manager"].get("app-bob-phone").online
    assert u["manager"].get("app-alice-phone").coordinator.tasks.empty()
    assert u["manager"].get("app-bob-phone").coordinator.tasks.empty()


@pytest.mark.parametrize("method,path,extra", DEVICE_ROUTES)
def test_signed_out_requests_are_refused(two_users, method, path, extra):
    anonymous = httpx.Client(base_url=two_users["base"])
    assert call(anonymous, method, path.format(id="app-alice-phone"), extra).status_code == 401


def test_device_lists_never_mix(two_users):
    u = two_users
    assert [d["id"] for d in u["alice"].get("/api/devices").json()] == ["app-alice-phone"]
    assert [d["id"] for d in u["bob"].get("/api/devices").json()] == ["app-bob-phone"]


def test_owner_still_controls_own_phone(two_users):
    u = two_users
    assert u["alice"].get("/api/devices/app-alice-phone/status").status_code == 200
    assert u["alice"].post("/api/devices/app-alice-phone/command", json={"text": "open maps"}).json() == {"result": "queued"}


def test_phone_cannot_connect_with_another_phones_key(two_users):
    u = two_users
    # Bob's key presented under Alice's device id (and vice versa) is rejected.
    for device_id, key in (("alice-phone", u["bob_key"]), ("bob-phone", u["alice_key"])):
        imposter = FakePhone(u["ws"], FakeDriver(), device_id=device_id, key=key)
        imposter.task.result(5)
        assert imposter.close_code == 4401
    # An unknown device id with a real key is rejected too.
    ghost = FakePhone(u["ws"], FakeDriver(), device_id="ghost", key=u["alice_key"])
    ghost.task.result(5)
    assert ghost.close_code == 4401


def test_cannot_claim_a_device_already_registered_to_someone_else(two_users):
    u = two_users
    # Bob makes his own valid code, then tries to redeem it for Alice's device id.
    code = u["bob"].get("/api/pairing").json()["code"]
    r = httpx.post(f"{u['base']}/api/pair/redeem", headers={"X-OpenMob-Session": u["bob_session"]},
                   json={"code": code, "device_id": "alice-phone", "model": "Galaxy"})
    assert r.status_code == 400 and "another account" in r.json()["detail"]
    # Alice's phone is unaffected: still hers, still online, its key still valid.
    assert u["store"].verify_device("alice-phone", u["alice_key"]) == u["store"].verify_login("alice@example.com", "password123")
    assert u["manager"].get("app-alice-phone").online
    assert u["bob"].get("/api/devices").json()[0]["id"] == "app-bob-phone"


def test_pairing_code_cannot_cross_accounts(two_users):
    u = two_users
    alice_code = u["alice"].get("/api/pairing").json()["code"]
    r = httpx.post(f"{u['base']}/api/pair/redeem", headers={"X-OpenMob-Session": u["bob_session"]},
                   json={"code": alice_code, "device_id": "bob-second-phone", "model": "Pixel"})
    assert r.status_code == 400
    assert [d["id"] for d in u["bob"].get("/api/devices").json()] == ["app-bob-phone"]


def test_revoked_phone_history_is_not_inherited(two_users):
    """After Alice revokes her phone, nobody else can take over its session or activity log."""
    u = two_users
    u["alice"].post("/api/devices/app-alice-phone/command", json={"text": "private request"})
    assert u["alice"].post("/api/devices/app-alice-phone/revoke").json() == {"ok": True}
    assert wait_for(lambda: not u["manager"].get("app-alice-phone").online)

    # Bob now registers that freed-up device id to his own account and connects with it.
    code = u["bob"].get("/api/pairing").json()["code"]
    r = httpx.post(f"{u['base']}/api/pair/redeem", headers={"X-OpenMob-Session": u["bob_session"]},
                   json={"code": code, "device_id": "alice-phone", "model": "Galaxy"})
    assert r.status_code == 200
    phone = FakePhone(u["ws"], FakeDriver(), device_id="alice-phone", key=r.json()["key"])
    assert phone.welcome.wait(5)
    log = u["bob"].get("/api/devices/app-alice-phone/status").json()["log"]
    assert not any("private request" in e["message"] for e in log)  # fresh session, no leaked history
    assert u["alice"].get("/api/devices/app-alice-phone/status").status_code == 404
    phone.disconnect()
