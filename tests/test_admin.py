"""Admin dashboard: admins see every account and phone and can revoke any of them; nobody else
can reach the admin API, and admin can only be granted from the server's command line."""

import httpx
import pytest
from conftest import FakeDriver
from test_hosted import hosted, pair, signup  # noqa: F401  (fixture + helpers)
from test_remote import FakePhone, wait_for

ADMIN_ROUTES = [
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/devices"),
    ("POST", "/api/admin/devices/app-alice-phone/revoke"),
    ("POST", "/api/admin/devices/alice-phone/revoke"),
]
SECRETS = ("pw_hash", "key_hash", "salt", "password", "key")


@pytest.fixture
def setup(hosted):  # noqa: F811
    """Alice (admin, no phone), Bob with one connected phone and one paired-but-offline phone."""
    base, manager, store = hosted
    ws = base.replace("http", "ws")
    admin, _ = signup(base, "alice@example.com")
    assert store.set_admin("alice@example.com", True)
    bob, bob_session = signup(base, "bob@example.com")
    live_key = pair(base, bob, bob_session, device_id="bob-live")
    offline_key = pair(base, bob, bob_session, device_id="bob-offline")
    phone = FakePhone(ws, FakeDriver(), device_id="bob-live", key=live_key)
    assert phone.welcome.wait(5)
    yield {"base": base, "ws": ws, "manager": manager, "store": store, "admin": admin, "bob": bob,
           "phone": phone, "live_key": live_key, "offline_key": offline_key}
    try:
        phone.disconnect()
    except Exception:
        pass


def test_admin_sees_all_users_and_devices(setup):
    admin = setup["admin"]
    users = {u["email"]: u for u in admin.get("/api/admin/users").json()}
    assert set(users) == {"alice@example.com", "bob@example.com"}
    assert users["alice@example.com"]["is_admin"] and not users["bob@example.com"]["is_admin"]
    assert users["bob@example.com"]["device_count"] == 2 and users["alice@example.com"]["device_count"] == 0

    devices = {d["id"]: d for d in admin.get("/api/admin/devices").json()}
    assert set(devices) == {"bob-live", "bob-offline"}
    assert devices["bob-live"]["online"] is True and devices["bob-offline"]["online"] is False
    assert devices["bob-live"]["state"] == "idle" and devices["bob-offline"]["state"] is None
    assert all(d["email"] == "bob@example.com" and not d["revoked"] for d in devices.values())
    assert admin.get("/api/admin/devices").json()[0]["id"] == "bob-live"  # connected first


def test_admin_page_is_served(setup):
    r = httpx.get(f"{setup['base']}/admin")
    assert r.status_code == 200 and "OpenMob Admin" in r.text


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_non_admin_is_forbidden(setup, method, path):
    assert setup["bob"].request(method, path).status_code == 403
    assert setup["manager"].get("app-bob-live").online  # nothing was revoked


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_signed_out_is_unauthorized(setup, method, path):
    assert httpx.request(method, f"{setup['base']}{path}").status_code == 401


def test_admin_revoke_cuts_a_live_phone_and_its_key(setup):
    u = setup
    assert u["admin"].post("/api/admin/devices/app-bob-live/revoke").json() == {"ok": True}
    u["phone"].task.result(5)
    assert u["phone"].close_code == 4401
    assert wait_for(lambda: u["manager"].get("app-bob-live") is None)  # gone from the lists
    again = FakePhone(u["ws"], FakeDriver(), device_id="bob-live", key=u["live_key"])
    again.task.result(5)
    assert again.close_code == 4401

    # Raw device id works too; revoked phones stay listed, last, for history.
    assert u["admin"].post("/api/admin/devices/bob-offline/revoke").json() == {"ok": True}
    devices = u["admin"].get("/api/admin/devices").json()
    assert all(d["revoked"] and not d["online"] for d in devices)
    assert not any(d["online"] for d in u["bob"].get("/api/devices").json())
    assert [a["device_count"] for a in u["admin"].get("/api/admin/users").json()] == [0, 0]


def test_unknown_device_is_404(setup):
    assert setup["admin"].post("/api/admin/devices/nope/revoke").status_code == 404
    assert setup["admin"].post("/api/admin/devices/app-nope/revoke").status_code == 404


def test_admin_cannot_be_granted_over_http(setup):
    u = setup
    bob_id = u["store"].account_id_for("bob@example.com")
    for client in (u["bob"], u["admin"]):
        for method, path in (
            ("POST", f"/api/admin/users/{bob_id}/admin"),
            ("POST", "/api/admin/users/bob@example.com/admin"),
            ("PUT", f"/api/admin/users/{bob_id}"),
            ("PATCH", f"/api/admin/users/{bob_id}"),
            ("POST", "/api/admin/users"),
            ("POST", "/api/admin/grant"),
        ):
            assert client.request(method, path, json={"is_admin": True}).status_code in (404, 405)
    assert not u["store"].is_admin(bob_id)
    assert u["bob"].get("/api/admin/users").status_code == 403


def test_listings_never_leak_secrets(setup):
    admin = setup["admin"]
    for path in ("/api/admin/users", "/api/admin/devices"):
        body = admin.get(path)
        for row in body.json():
            assert not set(row) & set(SECRETS), (path, row)
        assert setup["live_key"] not in body.text and setup["offline_key"] not in body.text


def test_session_is_never_taken_from_the_url_and_me_reports_admin(hosted):  # noqa: F811
    base, _, store = hosted
    client, session = signup(base, "boss@example.com")
    assert httpx.get(f"{base}/api/devices?s={session}").status_code == 401  # URL tokens are ignored
    assert client.get("/api/auth/me").json()["admin"] is False
    store.set_admin("boss@example.com", True)
    assert client.get("/api/auth/me").json()["admin"] is True


def test_revoked_phones_disappear_from_owner_and_admin_lists(hosted):  # noqa: F811
    base, manager, store = hosted
    owner, session = signup(base, "owner@example.com")
    boss, _ = signup(base, "boss@example.com")
    store.set_admin("boss@example.com", True)
    key = pair(base, owner, session, "gone-1")
    phone = FakePhone(base.replace("http", "ws"), FakeDriver(), device_id="gone-1", key=key)
    assert phone.welcome.wait(5)
    assert [d["id"] for d in owner.get("/api/devices").json()] == ["app-gone-1"]
    assert owner.post("/api/devices/app-gone-1/revoke").json() == {"ok": True}
    assert owner.get("/api/devices").json() == []  # removed right away, not shown as "offline"
    assert all(d["id"] != "gone-1" for d in boss.get("/api/admin/devices").json())
