"""The bridge in account mode, against a real server with accounts."""

import asyncio
import os
import stat

import httpx
import pytest
from conftest import FakeDriver
from test_hosted import hosted, signup  # noqa: F401

from openmob import bridge as bridge_mod
from openmob.bridge import BridgeAuthError, PhoneBridge, load_key, pair_with_account, save_key

INFO = {"id": "serial-123", "model": "SM-S948U1", "manufacturer": "samsung", "android": "16"}


async def connect_and_check(base, key, check):
    phone = PhoneBridge(FakeDriver(), base, "", INFO, on_event=lambda *a: None, key=key,
                        initial_backoff=0.05, max_backoff=0.1)
    task = asyncio.create_task(phone.run())
    try:
        await asyncio.wait_for(phone.connected.wait(), 5)
        await asyncio.to_thread(check)
    finally:
        await phone.stop()
        await asyncio.wait_for(task, 5)


def test_bridge_pairs_with_account_and_connects(hosted):  # noqa: F811
    base, manager, _ = hosted
    owner, _ = signup(base, "owner@example.com")
    other, _ = signup(base, "other@example.com")
    key = pair_with_account(base, "owner@example.com", "password123", INFO["id"], "Samsung SM-S948U1")

    def check():
        assert [d["id"] for d in owner.get("/api/devices").json()] == ["app-serial-123"]
        assert other.get("/api/devices").json() == []

    asyncio.run(connect_and_check(base, key, check))


def test_wrong_password_or_token_server_is_refused(hosted):  # noqa: F811
    base, _, _ = hosted
    signup(base, "owner@example.com")
    with pytest.raises(BridgeAuthError, match="Wrong email or password"):
        pair_with_account(base, "owner@example.com", "nope-nope", INFO["id"], "Samsung")


def test_revoked_key_stops_the_bridge(hosted):  # noqa: F811
    base, _, store = hosted
    owner, _ = signup(base, "owner@example.com")
    key = pair_with_account(base, "owner@example.com", "password123", INFO["id"], "Samsung")
    assert owner.post("/api/devices/app-serial-123/revoke").json() == {"ok": True}
    phone = PhoneBridge(FakeDriver(), base, "", INFO, on_event=lambda *a: None, key=key)
    with pytest.raises(BridgeAuthError, match="key"):
        asyncio.run(asyncio.wait_for(phone.run(), 5))


def test_keys_are_saved_per_server_and_phone_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_mod, "KEY_FILE", tmp_path / "keys.json")
    save_key("https://a.example.com/", "serial-1", "k1")
    save_key("https://b.example.com", "serial-1", "k2")
    assert load_key("https://a.example.com", "serial-1") == "k1"
    assert load_key("https://b.example.com", "serial-1") == "k2"
    assert load_key("https://a.example.com", "serial-2") is None
    assert stat.S_IMODE(os.stat(tmp_path / "keys.json").st_mode) == 0o600
    save_key("https://a.example.com", "serial-1", None)
    assert load_key("https://a.example.com", "serial-1") is None
