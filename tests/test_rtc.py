"""Live view over WebRTC: the viewer socket carries only the offer/answer (so the same sign-in and
ownership checks gate it), video then flows directly, and closing the socket cuts the phone's peer
connection. Without WebRTC on either side, viewers get the WebSocket stream as before."""

import asyncio
import base64
import hashlib
import hmac
import json
import time

import websockets
from conftest import FakeDriver
from test_remote import TOKEN, FakePhone, server, wait_for  # noqa: F401  (fixture)

from openmob.config import Config
from openmob.screencast import TURN_TTL, ice_servers_for


def viewer_url(url, rtc=True, token=TOKEN, device="app-xyz"):
    return f"{url}/api/devices/{device}/screen/ws?t={token}" + ("&rtc=1" if rtc else "")


async def recv_json(ws):
    while True:
        raw = await asyncio.wait_for(ws.recv(), 5)
        if isinstance(raw, str):
            return json.loads(raw)


def test_offer_and_answer_are_relayed_and_closing_cuts_the_peer(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True, rtc=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url)) as ws:
            hello = await recv_json(ws)
            await ws.send(json.dumps({"rtc": "offer", "sdp": "v=0 browser-offer"}))
            answer = await recv_json(ws)
            return hello, answer

    hello, answer = asyncio.run(run())
    assert hello["mode"] == "rtc" and hello["ice_servers"][0]["urls"]
    assert answer["rtc"] == "answer" and answer["sdp"].startswith("answer-for-")
    offer = next(p for m, p in phone.calls if m == "rtc_offer")
    assert offer["sdp"] == "v=0 browser-offer" and offer["ice_servers"] == hello["ice_servers"]
    # Nothing was streamed over the socket: no screenshots, no WebSocket video.
    assert not any(m == "screenshot" or m.startswith("stream_") for m, _ in phone.calls)
    # The socket (the viewer's permission) closed: the phone drops that peer connection.
    assert wait_for(lambda: ("rtc_close", {"session": offer["session"]}) in phone.calls)
    phone.disconnect()


def test_fallback_moves_the_viewer_to_the_websocket_stream(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True, rtc=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url)) as ws:
            await recv_json(ws)
            await ws.send(json.dumps({"rtc": "offer", "sdp": "offer"}))
            await recv_json(ws)
            await ws.send(json.dumps({"rtc": "fallback"}))  # e.g. ICE couldn't connect
            mode = await recv_json(ws)
            packet = await asyncio.wait_for(ws.recv(), 5)
            return mode, packet

    mode, packet = asyncio.run(run())
    assert mode["mode"] == "video" and packet[:1] == b"V"
    assert any(m == "rtc_close" for m, _ in phone.calls)
    phone.disconnect()


def test_browsers_or_apps_without_webrtc_get_the_websocket_stream(server):  # noqa: F811
    url, manager, _ = server
    old_app = FakePhone(url, FakeDriver(), video=True)  # video, but no "rtc" in its hello
    assert old_app.welcome.wait(5)

    async def first_message(rtc):
        async with websockets.connect(viewer_url(url, rtc=rtc)) as ws:
            return await recv_json(ws)

    assert asyncio.run(first_message(rtc=True))["mode"] == "video"
    assert not any(m.startswith("rtc_") for m, _ in old_app.calls)
    old_app.disconnect()

    phone = FakePhone(url, FakeDriver(), device_id="abc", video=True, rtc=True)
    assert phone.welcome.wait(5)

    async def no_rtc_browser():
        async with websockets.connect(viewer_url(url, rtc=False, device="app-abc")) as ws:
            first = await recv_json(ws)
            await ws.send(json.dumps({"rtc": "offer", "sdp": "sneaky"}))  # ignored: not in WebRTC mode
            await asyncio.sleep(0.3)
            return first

    assert asyncio.run(no_rtc_browser())["mode"] == "video"
    assert not any(m == "rtc_offer" for m, _ in phone.calls)
    phone.disconnect()


def test_unauthorized_sockets_never_reach_the_phone(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True, rtc=True)
    assert phone.welcome.wait(5)

    async def attempt():
        async with websockets.connect(viewer_url(url, token="wrong")) as ws:
            await ws.send(json.dumps({"rtc": "offer", "sdp": "x"}))
            await asyncio.wait_for(ws.recv(), 5)

    try:
        asyncio.run(attempt())
    except websockets.ConnectionClosed as exc:
        assert exc.rcvd.code == 4401
    assert not any(m.startswith("rtc_") for m, _ in phone.calls)
    phone.disconnect()


def test_turn_credentials_are_short_lived_and_verifiable():
    cfg = Config()
    assert ice_servers_for(cfg, "app-xyz") == cfg.rtc_ice_servers  # no TURN configured: STUN only
    cfg.rtc_turn_urls = ["turn:turn.example.com:3478"]
    cfg.turn_secret = "s3cret"
    turn = ice_servers_for(cfg, "app-xyz")[-1]
    expiry, user = turn["username"].split(":", 1)
    assert user == "app-xyz" and 0 < int(expiry) - time.time() <= TURN_TTL
    expected = base64.b64encode(hmac.new(b"s3cret", turn["username"].encode(), hashlib.sha1).digest()).decode()
    assert turn["credential"] == expected and "s3cret" not in json.dumps(turn)
