import pytest
from fastapi.testclient import TestClient

from openmob.accounts import AccountStore
from openmob.classifier_lane import load_credentials
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.server import create_app
from openmob.voice.tts import Speaker


def test_credentials_are_parsed_as_data_and_shell_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-key")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    path = tmp_path / "credentials.env"
    path.write_text("export ANTHROPIC_API_KEY='file-key'\nCLOUDFLARE_API_TOKEN='$(do-not-execute)'\n")
    load_credentials(path)
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "shell-key"
    assert os.environ["CLOUDFLARE_API_TOKEN"] == "$(do-not-execute)"
    path.write_text("SECRET_VALUE='unterminated\n")
    with pytest.raises(ValueError) as error:
        load_credentials(path)
    assert "SECRET_VALUE" not in str(error.value)


def test_lanes_keep_sessions_separate_in_one_browser(tmp_path):
    clients = {}
    for lane in ("jev", "clef"):
        store = AccountStore(tmp_path / f"{lane}.json")
        store.create_account(f"{lane}@example.com", "test-password-123")
        manager = DeviceManager(Config(), lambda *a, **k: None, Speaker(False), adb_discovery=False)
        clients[lane] = TestClient(create_app(manager, store=store, session_cookie=f"openmob_{lane}_session"))
        result = clients[lane].post("/api/auth/login", json={"email": f"{lane}@example.com", "password": "test-password-123"})
        assert result.status_code == 200
        assert f"openmob_{lane}_session" in result.cookies
    # Browsers share cookies between ports on the same hostname.
    clients["jev"].cookies.update(clients["clef"].cookies)
    clients["clef"].cookies.update(clients["jev"].cookies)
    for lane, client in clients.items():
        assert client.get("/api/auth/me").json()["email"] == f"{lane}@example.com"
    result = clients["clef"].post("/api/auth/logout")
    assert "openmob_clef_session=" in result.headers["set-cookie"]
    assert clients["jev"].get("/api/auth/me").json()["email"] == "jev@example.com"
