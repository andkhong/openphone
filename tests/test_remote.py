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

    def __init__(self, url, driver, device_id="xyz", token=TOKEN, key=None, platform=None, binary=False,
                 video=False, approve=None):
        self.url = f"{url}/phone/ws?t={token}"
        self.approve = approve  # True/False: answer approval requests like the owner would; None: leave them
        self.approval_requests = []  # every approval_request received
        self.key = key
        self.platform = platform
        self.binary = binary  # answer binary screenshot requests like the current app (else: like an old one)
        self.driver = driver
        self.device_id = device_id
        self.events = []
        self.calls = []  # (method, params) of every call received
        self.video = video  # the owner allowed screen streaming: answer stream_* and send packets
        self.streaming = None  # the task sending fake H.264 packets while a stream runs
        self.want_key = False
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
                hello = {"type": "hello", "protocol": 1, "device": {
                    "id": self.device_id, "model": "Pixel 9", "manufacturer": "google"}}
                if self.key:
                    hello["key"] = self.key
                if self.platform:
                    hello["device"]["platform"] = self.platform
                if self.video:
                    hello["device"]["video"] = True
                await ws.send(json.dumps(hello))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "welcome":
                        self.welcome.set()
                    elif msg["type"] == "event":
                        self.events.append((msg["kind"], msg["message"]))
                    elif msg["type"] == "approval_request":
                        self.approval_requests.append(msg)
                        if self.approve is not None:
                            await ws.send(json.dumps({"type": "approval", "id": msg["id"], "approved": self.approve}))
                    elif msg["type"] == "call":
                        self.calls.append((msg["method"], msg["params"]))
                        if self.video and msg["method"].startswith("stream_"):
                            try:
                                reply = {"ok": True, "result": self._stream(ws, msg["method"])}
                            except Exception as exc:
                                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                            await ws.send(json.dumps({"type": "result", "id": msg["id"], **reply}))
                            continue
                        if self.binary and msg["method"] == "screenshot" and msg["params"].get("binary"):
                            await ws.send(protocol.encode_binary_result(msg["id"], self.driver.screenshot()))
                            continue
                        try:
                            reply = {"ok": True, "result": self.handle(msg["method"], msg["params"])}
                        except Exception as exc:
                            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        await ws.send(json.dumps({"type": "result", "id": msg["id"], **reply}))
        except websockets.ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None

    def _stream(self, ws, method):
        """Fake encoder: SPS/PPS, a keyframe, then a delta every 20 ms (a keyframe when asked)."""
        if method == "stream_start":
            async def packets():
                try:
                    await ws.send(protocol.encode_video_packet(b"\x00\x00\x00\x01SPSPPS", 0, config=True))
                    n = 0
                    while True:
                        key = n == 0 or self.want_key
                        self.want_key = False
                        data = b"\x00\x00\x00\x01" + (b"IDR" if key else b"P") + bytes([n % 256])
                        await ws.send(protocol.encode_video_packet(data, n * 20_000, key=key))
                        n += 1
                        await asyncio.sleep(0.02)
                except websockets.ConnectionClosed:
                    pass  # the test disconnected this phone
            self.streaming = asyncio.ensure_future(packets())
            return {"width": 592, "height": 1280}
        if method == "stream_keyframe":
            self.want_key = True
        if method == "stream_stop" and self.streaming:
            self.streaming.cancel()
            self.streaming = None
        return None

    def send(self, message):
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(message)), self.loop).result(5)

    def disconnect(self):
        """Close normally (1000), like the user tapping Stop."""
        asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop).result(5)

    def drop(self):
        """Lose the connection (a network blip), not a deliberate close."""
        asyncio.run_coroutine_threadsafe(self.ws.close(code=1001), self.loop).result(5)


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


def test_iphone_hello_sets_platform(server):
    url, manager, _ = server
    iphone = FakePhone(url, FakeDriver(), device_id="ios-1", platform="ios")
    android = FakePhone(url, FakeDriver(), device_id="and-1")
    assert iphone.welcome.wait(5) and android.welcome.wait(5)
    ios_session, android_session = manager.get("app-ios-1"), manager.get("app-and-1")
    assert ios_session.describe()["platform"] == "ios" and ios_session.coordinator.agent.platform == "ios"
    assert android_session.describe()["platform"] == "android" and android_session.coordinator.agent.platform == "android"
    iphone.disconnect()
    android.disconnect()


# --- live screen: preview frames, binary results, the viewer socket ----------------------------

def watch(url, frames=2, headers=None, timeout=5.0):
    """Open a viewer socket and read `frames` messages. Returns (messages, close code or None)."""
    async def run():
        received, code = [], None
        try:
            async with websockets.connect(url, additional_headers=headers or {}) as ws:
                while len(received) < frames:
                    received.append(await asyncio.wait_for(ws.recv(), timeout))
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
        return received, code

    return asyncio.run(run())


def screenshot_params(phone):
    return [params for method, params in phone.calls if method == "screenshot"]


def test_binary_result_round_trips():
    shot = FakeDriver().screenshot()
    message = protocol.decode_message(None, protocol.encode_binary_result("c1", shot))
    assert message["type"] == "result" and message["id"] == "c1" and message["ok"]
    assert protocol.decode_screenshot(message["result"]) == shot
    assert protocol.decode_screenshot(protocol.encode_screenshot(shot)) == shot  # JSON path unchanged


def test_viewer_socket_pushes_preview_frames(server):
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), binary=True)
    assert phone.welcome.wait(5)
    frames, code = watch(f"{url}/api/devices/app-xyz/screen/ws?t={TOKEN}", frames=3)
    assert code is None and len(frames) == 3
    assert all(isinstance(f, bytes) and f[:2] == b"\xff\xd8" for f in frames)
    # The live view asked the phone for small binary frames...
    assert screenshot_params(phone)[0] == {"max_side": 720, "quality": 55, "binary": True}
    # ...and the feed stops once nobody is watching.
    time.sleep(0.5)
    asked = len(phone.calls)
    time.sleep(0.5)
    assert len(phone.calls) == asked

    # The agent's own screenshot is a fresh full-quality call: preview frames are never reused for it.
    manager.get("app-xyz").driver.screenshot(max_age=60)
    assert screenshot_params(phone)[-1] == {}
    phone.disconnect()


def test_viewer_socket_works_with_an_app_that_ignores_preview_params(server):
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver())  # answers every screenshot with a full JSON frame
    assert phone.welcome.wait(5)
    frames, code = watch(f"{url}/api/devices/app-xyz/screen/ws?t={TOKEN}", frames=2)
    assert code is None and all(f[:2] == b"\xff\xd8" for f in frames)
    phone.disconnect()


def test_viewer_socket_reports_errors_and_refuses_bad_tokens(server):
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), binary=True)
    assert phone.welcome.wait(5)
    assert watch(f"{url}/api/devices/app-xyz/screen/ws?t=wrong") == ([], 4401)
    assert watch(f"{url}/api/devices/app-xyz/screen/ws") == ([], 4401)
    assert watch(f"{url}/api/devices/app-nope/screen/ws?t={TOKEN}") == ([], 4404)
    phone.disconnect()
    assert wait_for(lambda: not manager.get("app-xyz").online)
    (message,), _ = watch(f"{url}/api/devices/app-xyz/screen/ws?t={TOKEN}", frames=1)
    assert "disconnected" in json.loads(message)["error"]


def test_polling_route_sends_newer_preview_frames(server):
    import httpx

    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), binary=True)
    assert phone.welcome.wait(5)
    base = url.replace("ws://", "http://")
    first = httpx.get(f"{base}/api/devices/app-xyz/screen.jpg?t={TOKEN}")
    assert first.status_code == 200 and first.content[:2] == b"\xff\xd8"
    taken = first.headers["x-frame-time"]
    again = httpx.get(f"{base}/api/devices/app-xyz/screen.jpg?t={TOKEN}&after={taken}")
    assert float(again.headers["x-frame-time"]) > float(taken)  # a new frame, not the one already shown
    assert screenshot_params(phone) == [{"max_side": 720, "quality": 55, "binary": True}] * 2
    phone.disconnect()


# --- approvals on the phone ------------------------------------------------------------------

@pytest.fixture
def risky(tmp_path):
    """A token-mode server whose agent always wants to tap "Buy now", then finishes."""
    def build_agent(cfg, io, driver, label_cache):
        script = [[tool_use("tap", "t", x=10, y=10, target="Buy now")],
                  [tool_use("done", "d", summary="Checked out.", success=True)]] * 5
        return Agent(ScriptedClient(script), driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                     cfg, io, use_ocr=False)

    manager = DeviceManager(Config(settle_delay=0, launch_settle_delay=0), build_agent, Speaker(enabled=False))
    port = free_port()
    serve_in_background(create_app(manager, token=TOKEN), "127.0.0.1", port)
    assert wait_for(lambda: _listening(port))
    return f"ws://127.0.0.1:{port}", manager


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.2).close()
        return True
    except OSError:
        return False


def _last_status(manager):
    infos = list(manager.get("app-xyz").coordinator.task_info.values())
    return infos[-1]["status"] if infos else None


def test_phone_approves_a_risky_tap_with_screen_lock(risky):
    url, manager = risky
    driver = FakeDriver()
    phone = FakePhone(url, driver, approve=True)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: _last_status(manager) == "succeeded")
    (request,) = phone.approval_requests
    assert request["requester"] == "You" and "Buy now" in request["question"] and request["task_id"]
    assert any(a[0] == "tap" for a in driver.actions)
    phone.disconnect()


def test_phone_denial_means_no_tap(risky):
    url, manager = risky
    driver = FakeDriver()
    phone = FakePhone(url, driver, approve=False)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: _last_status(manager) in ("succeeded", "failed"))
    assert phone.approval_requests and not any(a[0] == "tap" for a in driver.actions)
    phone.disconnect()


def test_unknown_or_stale_approvals_are_ignored(risky):
    url, manager = risky
    driver = FakeDriver()
    phone = FakePhone(url, driver)  # doesn't answer by itself
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    phone.send({"type": "approval", "id": "not-a-real-id", "approved": True})
    time.sleep(0.3)
    assert _last_status(manager) == "needs_confirmation" and not any(a[0] == "tap" for a in driver.actions)
    request = phone.approval_requests[0]
    phone.send({"type": "approval", "id": request["id"], "approved": True})
    assert wait_for(lambda: _last_status(manager) == "succeeded")
    phone.send({"type": "approval", "id": request["id"], "approved": True})  # answered already: ignored
    phone.disconnect()


def test_unanswered_approval_times_out_as_no(risky):
    url, manager = risky
    manager.approval_timeout = 0.3
    driver = FakeDriver()
    phone = FakePhone(url, driver)
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: _last_status(manager) in ("succeeded", "failed"))
    assert phone.approval_requests and not any(a[0] == "tap" for a in driver.actions)
    phone.disconnect()


# --- reconnect grace -------------------------------------------------------------------------

def test_a_dropped_connection_pauses_the_task_until_the_phone_is_back(risky):
    url, manager = risky
    phone = FakePhone(url, FakeDriver())
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    session = manager.get("app-xyz")
    phone.drop()
    assert wait_for(lambda: session.reconnecting and not session.online)
    time.sleep(0.2)
    assert _last_status(manager) == "needs_confirmation"  # still alive, not cancelled
    # The phone comes back, gets the same approval again, and approves it.
    driver = FakeDriver()
    again = FakePhone(url, driver, approve=True)
    assert again.welcome.wait(5)
    assert wait_for(lambda: _last_status(manager) == "succeeded")
    assert again.approval_requests[0]["id"] == phone.approval_requests[0]["id"]
    assert not session.reconnecting and any(a[0] == "tap" for a in driver.actions)
    again.disconnect()


def test_the_task_fails_when_the_phone_stays_away(risky):
    url, manager = risky
    manager.reconnect_grace, manager.approval_timeout = 0.3, 1.0
    phone = FakePhone(url, FakeDriver())
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    phone.drop()
    assert wait_for(lambda: _last_status(manager) == "failed", timeout=5)
    assert not manager.get("app-xyz").reconnecting


def test_a_normal_close_stops_the_task_right_away(risky):
    url, manager = risky
    phone = FakePhone(url, FakeDriver())
    assert phone.welcome.wait(5)
    phone.send({"type": "command", "text": "check the cart"})
    assert wait_for(lambda: phone.approval_requests)
    phone.disconnect()  # the user tapped Stop: close code 1000
    assert wait_for(lambda: _last_status(manager) == "cancelled")
    assert not manager.get("app-xyz").reconnecting


class _Link:
    """A PhoneLink stand-in: records calls; `drop` makes the next call die with the connection."""

    def __init__(self, drop=False):
        self.closed, self.drop, self.calls = False, drop, []

    def call(self, method, params):
        self.calls.append(method)
        if self.drop:
            self.closed = True
            raise DeviceError("Phone disconnected")
        return "com.example"


def test_reads_wait_for_the_reconnect_but_taps_are_never_repeated():
    from openmob.phone.remote import RemoteDriver

    first = _Link(drop=True)
    driver = RemoteDriver(first)
    driver.hold(5)
    with pytest.raises(DeviceError, match="wasn't repeated"):
        driver.tap(1, 2)
    assert first.calls == ["tap"]  # sent once, never again

    second = _Link()
    threading.Timer(0.2, driver.attach, (second,)).start()
    started = time.time()
    assert driver.current_app() == "com.example"  # waited for the new connection
    assert time.time() - started >= 0.15 and second.calls == ["current_app"]

    third = _Link(drop=True)
    driver.attach(third)
    driver.hold(None)  # no grace (nothing running, or time's up): fail right away
    with pytest.raises(DeviceError, match="disconnected"):
        driver.current_app()
