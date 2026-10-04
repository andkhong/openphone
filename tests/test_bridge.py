import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.asyncio.server import serve

from openmob.bridge import BridgeAuthError, PhoneBridge, device_info_from_adb, ws_url
from openmob.phone.adb import UIElement
from openmob.phone.protocol import PROTOCOL_VERSION, decode_element, decode_screenshot

from conftest import FakeDriver

TOKEN = "s3cret/+="
DEVICE = {"id": "abc123", "model": "Pixel 8", "manufacturer": "Google", "android": "15"}


class BrokenDriver(FakeDriver):
    def open_url(self, url):
        raise RuntimeError("no browser")


def token_ok(ws) -> bool:
    query = parse_qs(urlsplit(ws.request.path).query)
    return urlsplit(ws.request.path).path == "/phone/ws" and query.get("t") == [TOKEN]


async def call(ws, call_id, method, **params):
    await ws.send(json.dumps({"type": "call", "id": call_id, "method": method, "params": params}))


async def collect_results(ws, n):
    results, commands = {}, []
    while len(results) < n:
        msg = json.loads(await ws.recv())
        if msg["type"] == "result":
            results[msg["id"]] = msg
        elif msg["type"] == "command":
            commands.append(msg["text"])
    return results, commands


@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://example.com", "ws://example.com/phone/ws?t="),
        ("https://example.com/", "wss://example.com/phone/ws?t="),
        ("https://example.com:8443//", "wss://example.com:8443/phone/ws?t="),
        ("ws://10.0.0.2:8000", "ws://10.0.0.2:8000/phone/ws?t="),
        ("wss://h.example/openmob/", "wss://h.example/openmob/phone/ws?t="),
        ("localhost:8000", "ws://localhost:8000/phone/ws?t="),
    ],
)
def test_ws_url(given, expected):
    assert ws_url(given, "a b/c") == expected + "a%20b%2Fc"


def test_ws_url_rejects_other_schemes():
    with pytest.raises(ValueError):
        ws_url("ftp://example.com", "t")


def test_device_info_from_adb():
    driver = SimpleNamespace(serial=None, shell=lambda cmd: "R5CT\nPixel 8\nGoogle\n15\n")
    assert device_info_from_adb(driver) == DEVICE | {"id": "R5CT"}


def test_bridge_serves_calls_events_and_commands():
    driver = BrokenDriver(elements=[UIElement(0, "OK", "", "ok_btn", "Button", True, None, (10, 20, 110, 70))])
    events = []
    seen = {}

    async def handler(ws):
        assert token_ok(ws)
        seen["hello"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "welcome", "session": "abc123"}))
        await call(ws, "1", "screenshot")
        await call(ws, "2", "ui_elements")
        await call(ws, "3", "tap", x=5, y=6)
        await call(ws, "4", "long_press", x=7, y=8)
        await call(ws, "5", "swipe", x1=1, y1=2, x2=3, y2=4)
        await call(ws, "6", "app_labels", packages=["com.android.settings"])
        await call(ws, "7", "format_disk")
        await call(ws, "8", "open_url", url="https://x.test")
        await call(ws, "9", "tap", x=1)  # missing param
        seen["results"], commands = await collect_results(ws, 9)
        await ws.send(json.dumps({"type": "event", "kind": "question", "message": "Which one?"}))
        if not commands:
            commands.append(json.loads(await ws.recv())["text"])
        seen["commands"] = commands

    async def main():
        async with serve(handler, "127.0.0.1", 0, max_size=None) as server:
            port = server.sockets[0].getsockname()[1]
            bridge = PhoneBridge(driver, f"http://127.0.0.1:{port}/", TOKEN, DEVICE, on_event=lambda k, m: events.append((k, m)))
            task = asyncio.create_task(bridge.run())
            await asyncio.wait_for(bridge.connected.wait(), 3)
            assert await bridge.send_command("open youtube\n")
            for _ in range(300):
                if "commands" in seen and events:
                    break
                await asyncio.sleep(0.01)
            await bridge.stop()
            await asyncio.wait_for(task, 3)

    asyncio.run(asyncio.wait_for(main(), 5))

    assert seen["hello"] == {"type": "hello", "protocol": PROTOCOL_VERSION, "device": DEVICE}
    r = seen["results"]
    shot = decode_screenshot(r["1"]["result"])
    assert (shot.width, shot.height, shot.device_width, shot.device_height) == (540, 1200, 1080, 2400)
    assert shot.jpeg[:2] == b"\xff\xd8"
    assert [decode_element(i, e) for i, e in enumerate(r["2"]["result"])] == driver.elements
    assert r["3"] == {"type": "result", "id": "3", "ok": True, "result": None}
    assert r["6"]["result"] == {"com.android.settings": None}
    assert r["7"]["ok"] is False and r["7"]["error"].startswith("ValueError: unknown method")
    assert r["8"] == {"type": "result", "id": "8", "ok": False, "error": "RuntimeError: no browser"}
    assert r["9"]["ok"] is False and "TypeError" in r["9"]["error"]
    assert ("tap", 5, 6) in driver.actions
    assert ("long_press", 7, 8) in driver.actions
    assert ("swipe", 1, 2, 3, 4, 300) in driver.actions
    assert events == [("question", "Which one?")]
    assert seen["commands"] == ["open youtube"]


def test_bad_token_stops_without_retrying():
    attempts = []

    async def handler(ws):
        attempts.append(1)
        if not token_ok(ws):
            await ws.close(4401, "bad token")

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            bridge = PhoneBridge(FakeDriver(), f"ws://127.0.0.1:{port}", "wrong", DEVICE, initial_backoff=0.01)
            with pytest.raises(BridgeAuthError):
                await asyncio.wait_for(bridge.run(), 3)

    asyncio.run(main())
    assert len(attempts) == 1


def test_reconnects_after_drop():
    connections = []

    async def handler(ws):
        hello = json.loads(await ws.recv())
        connections.append(hello["device"]["id"])
        await ws.send(json.dumps({"type": "welcome", "session": "abc123"}))
        if len(connections) == 1:
            await ws.close(1011, "server restarting")
        else:
            await call(ws, "1", "key", name="HOME")
            await ws.recv()

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            driver = FakeDriver()
            bridge = PhoneBridge(driver, f"http://127.0.0.1:{port}", TOKEN, DEVICE, initial_backoff=0.01)
            task = asyncio.create_task(bridge.run())
            for _ in range(300):
                if driver.actions:
                    break
                await asyncio.sleep(0.01)
            await bridge.stop()
            await asyncio.wait_for(task, 3)
            return driver

    driver = asyncio.run(asyncio.wait_for(main(), 5))
    assert connections == ["abc123", "abc123"]
    assert driver.actions == [("key", "HOME")]


def test_reconnects_when_refused():
    """No server listening: the bridge keeps retrying with growing delays."""
    sleeps = []

    async def main():
        bridge = PhoneBridge(FakeDriver(), "http://127.0.0.1:9", TOKEN, DEVICE, initial_backoff=1, max_backoff=4)

        async def fake_wait(delay):
            sleeps.append(delay)
            if len(sleeps) >= 5:
                await bridge.stop()

        bridge._wait = fake_wait
        await asyncio.wait_for(bridge.run(), 3)

    asyncio.run(main())
    assert sleeps == [1, 2, 4, 4, 4]
