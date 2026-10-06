"""Hosted mode with accounts: a pairing code only works for its owner, and each account
sees and controls only its own phones."""

import time

import httpx
import pytest
from conftest import FakeDriver, ScriptedClient, tool_use
from test_remote import FakePhone, free_port, wait_for

from openmob.accounts import AccountStore
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.phone.apps import AppResolver
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker


@pytest.fixture
def hosted(tmp_path):
    def build_agent(cfg, io, driver, label_cache):
        client = ScriptedClient([[tool_use("done", "a", summary="ok", success=True)]] * 5)
        return Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"), cfg, io, use_ocr=False)

    from openmob.history import History

    store = AccountStore(tmp_path / "accounts.json")
    manager = DeviceManager(Config(settle_delay=0, launch_settle_delay=0), build_agent, Speaker(enabled=False),
                            adb_discovery=False, history=History(tmp_path / "history.sqlite"))
    port = free_port()
    serve_in_background(create_app(manager, store=store), "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    assert wait_for(lambda: _up(base))
    return base, manager, store


def _up(base):
    try:
        return httpx.get(f"{base}/api/auth/me", timeout=0.5).status_code == 200
    except httpx.HTTPError:
        return False


def signup(base, email):
    client = httpx.Client(base_url=base)
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return client, r.json()["session"]


def pair(base, client, session, device_id="phone-1"):
    code = client.get("/api/pairing").json()["code"]
    r = httpx.post(f"{base}/api/pair/redeem", headers={"X-OpenMob-Session": session},
                   json={"code": code, "device_id": device_id, "model": "Galaxy"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def test_dashboard_requires_sign_in(hosted):
    base, _, _ = hosted
    assert httpx.get(f"{base}/api/devices").status_code == 401
    assert httpx.get(f"{base}/api/pairing").status_code == 401
    assert httpx.get(f"{base}/api/auth/me").json() == {"accounts": True, "email": None, "admin": False}
    client, _ = signup(base, "owner@example.com")
    assert client.get("/api/auth/me").json()["email"] == "owner@example.com"  # cookie session
    client.post("/api/auth/logout")
    assert client.get("/api/devices").status_code == 401


def test_pairing_code_is_useless_to_another_account(hosted):
    base, _, _ = hosted
    owner, _ = signup(base, "owner@example.com")
    _, intruder_session = signup(base, "intruder@example.com")
    code = owner.get("/api/pairing").json()["code"]
    r = httpx.post(f"{base}/api/pair/redeem", headers={"X-OpenMob-Session": intruder_session},
                   json={"code": code, "device_id": "their-phone", "model": "Pixel"})
    assert r.status_code == 400 and "different account" in r.json()["detail"]
    # Not signed in at all: refused too.
    r = httpx.post(f"{base}/api/pair/redeem", json={"code": code, "device_id": "x", "model": "x"})
    assert r.status_code == 401


def test_each_account_sees_and_controls_only_its_own_phones(hosted):
    base, manager, _ = hosted
    owner, owner_session = signup(base, "owner@example.com")
    other, _ = signup(base, "other@example.com")
    key = pair(base, owner, owner_session)

    phone = FakePhone(base.replace("http", "ws"), FakeDriver(), device_id="phone-1", key=key)
    assert phone.welcome.wait(5)
    assert [d["id"] for d in owner.get("/api/devices").json()] == ["app-phone-1"]
    assert other.get("/api/devices").json() == []
    for path in ("/api/devices/app-phone-1/status", "/api/devices/app-phone-1/screen.jpg"):
        assert other.get(path).status_code == 404  # existence isn't revealed
        assert owner.get(path).status_code == 200
    assert other.post("/api/devices/app-phone-1/command", json={"text": "open maps"}).status_code == 404
    assert owner.post("/api/devices/app-phone-1/command", json={"text": "open maps"}).json() == {"result": "queued"}
    phone.disconnect()


def test_phone_needs_a_valid_key(hosted):
    base, manager, _ = hosted
    ws = base.replace("http", "ws")
    no_key = FakePhone(ws, FakeDriver(), device_id="phone-1")
    no_key.task.result(5)
    assert no_key.close_code == 4401

    owner, session = signup(base, "owner@example.com")
    pair(base, owner, session)
    wrong = FakePhone(ws, FakeDriver(), device_id="phone-1", key="not-the-key")
    wrong.task.result(5)
    assert wrong.close_code == 4401 and not manager.sessions


def test_revoke_cuts_the_connection_and_the_key(hosted):
    base, manager, _ = hosted
    owner, session = signup(base, "owner@example.com")
    other, _ = signup(base, "other@example.com")
    key = pair(base, owner, session)
    ws = base.replace("http", "ws")
    phone = FakePhone(ws, FakeDriver(), device_id="phone-1", key=key)
    assert phone.welcome.wait(5)

    assert other.post("/api/devices/app-phone-1/revoke").status_code == 404  # not their phone
    assert owner.post("/api/devices/app-phone-1/revoke").json() == {"ok": True}
    phone.task.result(5)
    assert phone.close_code == 4401  # the live connection was cut
    assert wait_for(lambda: manager.get("app-phone-1") is None)  # gone from the lists

    again = FakePhone(ws, FakeDriver(), device_id="phone-1", key=key)
    again.task.result(5)
    assert again.close_code == 4401


def test_pages_and_app_download_are_public(hosted):
    base, _, _ = hosted
    for page in ("/", "/connect", "/pair?code=abc", "/login"):
        assert httpx.get(f"{base}{page}").status_code == 200
    # /pair checks the download with HEAD; it must not be rejected as a wrong method.
    assert httpx.head(f"{base}/download/openmob.apk").status_code in (200, 404)


def test_one_time_sign_in_link(hosted):
    base, _, store = hosted
    account = store.create_account("owner@example.com", "password123")
    code = store.create_login_link(account)
    client = httpx.Client(base_url=base)
    r = client.get(f"/auth/link?code={code}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/api/auth/me").json()["email"] == "owner@example.com"
    assert httpx.get(f"{base}/auth/link?code={code}").status_code == 400  # single use
    assert httpx.get(f"{base}/auth/link?code=guess").status_code == 400


def test_server_sees_accounts_added_by_another_process(hosted, tmp_path):
    base, _, store = hosted
    from openmob.accounts import AccountStore

    AccountStore(store.path).create_account("late@example.com", "password123")  # e.g. the CLI
    r = httpx.post(f"{base}/api/auth/login", json={"email": "late@example.com", "password": "password123"})
    assert r.status_code == 200


def test_signed_out_visitors_get_the_landing_page(hosted):
    base, _, store = hosted
    body = httpx.get(f"{base}/").text
    assert "Tell your phone what to do" in body and "Accounts are disabled" not in body
    for page in ("/login", "/login?next=/connect"):  # the sign-in form, email and password
        body = httpx.get(f"{base}{page}").text
        assert 'id="email"' in body and 'id="password"' in body
    client, _ = signup(base, "owner@example.com")
    assert "Tell your phone what to do…" in client.get("/").text  # signed in: the dashboard


class FakeTranscriber:
    def transcribe(self, audio):
        return "open youtube" if audio.read() else ""


def test_transcribe_endpoint(tmp_path):
    from openmob.accounts import AccountStore

    store = AccountStore(tmp_path / "a.json")
    manager = DeviceManager(Config(), lambda *a: None, Speaker(enabled=False), adb_discovery=False)
    from fastapi.testclient import TestClient

    with_whisper = TestClient(create_app(manager, store=store, transcriber=FakeTranscriber()))
    without = TestClient(create_app(manager, store=store))
    audio = {"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")}
    assert with_whisper.post("/api/transcribe", files=audio).status_code == 401  # signed out
    for client in (with_whisper, without):
        client.post("/api/auth/signup", json={"email": f"u{id(client)}@example.com", "password": "password123"})
    assert with_whisper.post("/api/transcribe", files=audio).json() == {"text": "open youtube"}
    assert without.post("/api/transcribe", files=audio).status_code == 501
    import openmob.server as server_mod

    big = {"file": ("voice.webm", b"x" * (server_mod.MAX_AUDIO_BYTES + 1), "audio/webm")}
    assert with_whisper.post("/api/transcribe", files=big).status_code == 413


def test_pairing_from_localhost_uses_the_computers_network_name(hosted, monkeypatch):
    # A phone can't reach "localhost" (and the computer's IP changes with sleep or another Wi-Fi):
    # the QR code points at its network name instead. Behind a tunnel the public address is kept.
    from openmob import server

    base, _, _ = hosted
    monkeypatch.setattr(server, "local_hostname", lambda: "my-mac.local")
    owner, _ = signup(base, "net@example.com")
    port = base.rsplit(":", 1)[1]
    p = owner.get("/api/pairing").json()
    assert p["server"] == f"http://my-mac.local:{port}" and p["pair_url"].startswith(f"http://my-mac.local:{port}/pair?")
    assert p["local"] is False
    tunneled = owner.get("/api/pairing", headers={"x-forwarded-host": "abc.trycloudflare.com",
                                                 "x-forwarded-proto": "https"}).json()
    assert tunneled["server"] == "https://abc.trycloudflare.com"
