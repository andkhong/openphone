"""Server fixes from the security review of 2026-10-09 (lows and mediums)."""

import re

from conftest import FakeDriver

from openmob.agent import gate
from openmob.config import Config
from openmob.phone.adb import UIElement as U
from openmob.phone.apps import AppResolver


def test_two_apps_with_the_same_name_resolve_to_neither(tmp_path):
    # A planted app labeled "Chase" must not win over the real one because it's listed first.
    labels = {"com.aaa.wallet": "Chase", "com.chase.sig.android": "Chase", "com.spotify.music": "Spotify"}
    resolver = AppResolver(FakeDriver(packages=list(labels), labels=labels), label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    assert resolver.resolve("chase") is None  # no picker here: a tie is never settled by list order
    assert resolver.resolve("spotify").package == "com.spotify.music"


def test_citi_and_keeper_are_sensitive_on_the_server():
    patterns = Config().sensitive_apps if hasattr(Config(), "sensitive_apps") else []
    pats = [re.compile(p) for p in patterns]
    for package in ("com.citi.citimobile", "com.callpod.android_apps.keeper"):
        assert any(p.search(package) for p in pats), package


def test_allow_all_the_time_asks_on_a_location_page():
    page = [U(0, "Location permission", "", "", "android.widget.TextView", False, None, (0, 100, 1080, 180)),
            U(1, "Allow all the time", "", "", "android.widget.RadioButton", True, False, (0, 400, 1080, 480))]
    tier = gate.settings_tier(Config(), "com.android.settings", "Allow all the time", page[1], page)
    assert tier == "ask"


def test_fly_client_ip_is_used_only_behind_fly(monkeypatch):
    from types import SimpleNamespace
    from openmob.server import client_ip

    request = SimpleNamespace(client=SimpleNamespace(host="172.16.0.2"), headers={"fly-client-ip": "203.0.113.9"})
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    assert client_ip(request) == "172.16.0.2"  # off Fly the header is anyone's to forge
    monkeypatch.setenv("FLY_APP_NAME", "openmob")
    assert client_ip(request) == "203.0.113.9"
