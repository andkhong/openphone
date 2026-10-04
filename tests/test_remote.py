"""End to end: real server + WebSocket "phone" + agent, with the phone backed by FakeDriver."""

import asyncio
import json
import socket
import threading
import time

import pytest
import websockets
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.devices import DeviceManager
from openmob.phone import protocol
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError
from openmob.server import create_app, serve_in_background
from openmob.voice.tts import Speaker

TOKEN = "secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakePhone:
    """Speaks the phone side of the protocol in a background thread, backed by FakeDriver."""

    def __init__(self, url, driver, device_id="xyz", token=TOKEN):
        self.url = f"{url}/phone/ws?t={token}"
        self.driver = driver
        self.device_id = device_id
        self.events = []
        self.close_code = None
        self.welcome = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.ws = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.task = asyncio.run_coroutine_threadsafe(self._run(), self.loop)

    def handle(self, method, params):
        if method == "launch_app" and params.get("package") == "bad.pkg":
            raise RuntimeError("boom")
        if method == "screenshot":
            return protocol.encode_screenshot(self.driver.screenshot())
        if method == "ui_elements":
            return [protocol.encode_element(e) for e in self.driver.ui_elements()]
        return getattr(self.driver, method)(**params)

    async def _run(self):
        try:
            async with websockets.connect(self.url) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "hello", "protocol": 1, "device": {
                    "id": self.device_id, "model": "Pixel 9", "manufacturer": "google"}}))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "welcome":
                        self.welcome.set()
                    elif msg["type"] == "event":
                        self.events.append((msg["kind"], msg["message"]))
                    elif msg["type"] == "call":
                        try:
                            reply = {"ok": True, "result": self.handle(msg["method"], msg["params"])}
                        except Exception as exc:
                            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        await ws.send(json.dumps({"type": "result", "id": msg["id"], **reply}))
        except websockets.ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None

    def send(self, message):
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(message)), self.loop).result(5)

    def disconnect(self):
        asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop).result(5)


@pytest.fixture
def server(tmp_path):
    clients = []

    def build_agent(cfg, io, driver, label_cache):
        client = ScriptedClient([
            [tool_use("launch_app", "a", app="Settings")],
            [tool_use("done", "b", summary="Settings is open.", success=True)],
        ])
        clients.append(client)
        resolver = AppResolver(driver, label_cache=tmp_path / "labels.json")
        return Agent(client, driver, resolver, cfg, io, use_ocr=False)

    cfg = Config(settle_delay=0, launch_settle_delay=0)
    manager = DeviceManager(cfg, build_agent, Speaker(enabled=False))
    port = free_port()
    serve_in_background(create_app(manager, token=TOKEN), "127.0.0.1", port)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return f"ws://127.0.0.1:{port}", manager, clients


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_bad_token_is_rejected_with_4401(server):
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), token="wrong")
    phone.task.result(5)
    assert phone.close_code == 4401 and not manager.sessions


def test_remote_driver_calls_reach_the_phone(server):
    url, manager, _ = server
    driver = FakeDriver(packages=["com.android.settings"], labels={"com.android.settings": "Settings"})
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    session = manager.get("app-xyz")
    assert session.online and session.info.connection == "app" and session.info.model == "Google Pixel 9"

    remote = session.driver
    shot = remote.screenshot()
    assert (shot.width, shot.device_width) == (540, 1080) and shot.jpeg[:2] == b"\xff\xd8"
    remote.tap(10, 20)
    remote.key("back")
    assert ("tap", 10, 20) in driver.actions and ("key", "BACK") in driver.actions
    assert remote.launchable_packages() == ["com.android.settings"]
    assert remote.app_labels(["com.android.settings"]) == {"com.android.settings": "Settings"}

    with pytest.raises(DeviceError, match="RuntimeError: boom"):
        remote.launch_app("bad.pkg")
    phone.disconnect()


def test_phone_command_runs_agent_and_streams_events(server):
    url, manager, _ = server
    driver = FakeDriver(packages=["com.android.settings"], labels={"com.android.settings": "Settings"})
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "open settings"})
    assert wait_for(lambda: any(k == "result" for k, _ in phone.events))
    assert ("launch", "com.android.settings") in driver.actions
    assert ("key", "HOME") in driver.actions  # commands from the phone start from the home screen
    assert any(k == "result" and "Settings is open" in m for k, m in phone.events)
    phone.disconnect()


def test_disconnect_marks_offline_and_reconnect_reuses_session(server):
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver())
    assert phone.welcome.wait(5)
    session = manager.get("app-xyz")
    phone.disconnect()
    assert wait_for(lambda: not session.online)
    with pytest.raises(DeviceError):
        session.driver.tap(1, 1)

    again = FakePhone(url, FakeDriver())
    assert again.welcome.wait(5)
    assert manager.get("app-xyz") is session and session.online
    session.driver.tap(3, 4)
    assert ("tap", 3, 4) in again.driver.actions
    again.disconnect()
