"""Live view as video: phones whose owner allowed screen streaming send H.264, the server forwards
it to every viewer starting at a keyframe, and everything falls back to screenshots otherwise.
The agent never uses the video."""

import asyncio
import json
import struct

import websockets
from conftest import FakeDriver
from test_remote import TOKEN, FakePhone, server, wait_for  # noqa: F401  (fixture)

from openmob import screencast
from openmob.phone import protocol


def viewer_url(url, device="app-xyz"):
    return f"{url}/api/devices/{device}/screen/ws?t={TOKEN}"


async def watch(url, count, device="app-xyz", timeout=5):
    """Collect `count` messages from one viewer socket: ("text", dict) or ("video"/"jpeg", bytes)."""
    out = []
    async with websockets.connect(viewer_url(url, device)) as ws:
        while len(out) < count:
            raw = await asyncio.wait_for(ws.recv(), timeout)
            if isinstance(raw, str):
                out.append(("text", json.loads(raw)))
            elif raw[:1] == b"V":
                out.append(("video", raw))
            else:
                out.append(("jpeg", raw))
    return out


def unpack(packet):
    key = bool(packet[1] & 1)
    (pts,) = struct.unpack(">q", packet[2:10])
    return key, pts, packet[10:]


def test_viewer_gets_video_starting_at_a_keyframe_with_sps(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True)
    assert phone.welcome.wait(5)
    messages = asyncio.run(watch(url, 6))
    assert messages[0] == ("text", {"mode": "video", "width": 592, "height": 1280})
    packets = [unpack(m[1]) for m in messages[1:]]
    key, _, data = packets[0]
    assert key and data.startswith(b"\x00\x00\x00\x01SPSPPS") and b"IDR" in data
    # Every keyframe carries the SPS/PPS, so a decoder can start from any of them. (The server may
    # ask for one fresh keyframe at start, because frames that raced ahead of the start were dropped.)
    assert all(d.startswith(b"\x00\x00\x00\x01SPSPPS") for k, _, d in packets if k)
    assert any(not k for k, _, _ in packets[1:])
    pts = [p for _, p, _ in packets]
    assert pts == sorted(pts)
    # No screenshots were taken for the live view: the agent's screenshot budget stays free.
    assert not any(m == "screenshot" for m, _ in phone.calls)
    assert ("stream_start", {"max_side": 1280, "bitrate": 2_500_000, "fps": 30}) in phone.calls
    # The last viewer left: the phone stops encoding.
    assert wait_for(lambda: ("stream_stop", {}) in phone.calls)
    phone.disconnect()


def test_a_second_viewer_joins_at_a_fresh_keyframe(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True)
    assert phone.welcome.wait(5)

    async def two_viewers():
        first = asyncio.ensure_future(watch(url, 40))
        await asyncio.sleep(0.4)
        second = await watch(url, 4)
        return await first, second

    first, second = asyncio.run(two_viewers())
    assert second[0][0] == "text" and second[0][1]["mode"] == "video"
    key, _, data = unpack(second[1][1])
    assert key and data.startswith(b"\x00\x00\x00\x01SPSPPS")  # joined mid-stream, still decodable
    assert ("stream_keyframe", {}) in phone.calls
    assert sum(1 for m, _ in phone.calls if m == "stream_start") == 1  # one stream, shared
    phone.disconnect()


def test_without_streaming_permission_the_viewer_gets_screenshots(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), binary=True)  # no "video" in its hello: the owner declined
    assert phone.welcome.wait(5)
    messages = asyncio.run(watch(url, 2))
    assert [kind for kind, _ in messages] == ["jpeg", "jpeg"]
    assert not any(m.startswith("stream_") for m, _ in phone.calls)
    phone.disconnect()


def test_when_streaming_ends_the_viewer_falls_back_to_screenshots(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True, binary=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url)) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            await asyncio.wait_for(ws.recv(), 5)  # a video packet
            # The owner tapped "stop sharing" in the status bar.
            await asyncio.get_running_loop().run_in_executor(
                None, phone.send, {"type": "video_state", "available": False, "reason": "stopped"})
            seen = []
            while not seen or seen[-1] != "jpeg":
                raw = await asyncio.wait_for(ws.recv(), 5)
                seen.append(json.loads(raw)["mode"] if isinstance(raw, str) else ("video" if raw[:1] == b"V" else "jpeg"))
            return first, seen

    first, seen = asyncio.run(run())
    assert first["mode"] == "video"
    assert "jpeg" in seen and seen[-1] == "jpeg"
    assert ("stream_stop", {}) in phone.calls
    phone.disconnect()


def test_an_app_that_cannot_start_a_stream_falls_back(server):  # noqa: F811
    url, manager, _ = server

    class Refusing(FakePhone):
        def _stream(self, ws, method):
            raise RuntimeError("Screen streaming permission was revoked")

    phone = Refusing(url, FakeDriver(), video=True, binary=True)
    assert phone.welcome.wait(5)
    messages = asyncio.run(watch(url, 2))
    assert [kind for kind, _ in messages] == ["jpeg", "jpeg"]  # screenshots instead, no error shown
    assert sum(1 for m, _ in phone.calls if m == "stream_start") == 1  # not retried every frame
    phone.disconnect()


def test_packets_that_race_ahead_of_the_start_reply_are_not_sent_before_the_mode():
    viewer = screencast._Viewer()
    feed = screencast.ScreenFeed(driver=None)
    feed._viewers.add(viewer)
    feed._on_video({"config": True, "data": b"SPS"})
    feed._on_video({"key": True, "pts": 0, "data": b"IDR"})  # before the mode is set
    assert not viewer.items and feed._config == b"SPS"


def test_slow_viewer_skips_ahead_to_a_keyframe():
    viewer = screencast._Viewer()
    feed = screencast.ScreenFeed(driver=None)
    feed.mode = "video"
    feed._viewers.add(viewer)
    feed._on_video({"config": True, "data": b"SPS"})
    feed._on_video({"key": True, "pts": 0, "data": b"IDR0"})
    for n in range(screencast.VIEWER_BACKLOG + 5):
        feed._on_video({"key": False, "pts": n + 1, "data": b"P"})
    # It never drained its queue, so it was cut back and now waits for a keyframe.
    assert viewer.need_key and viewer.backlog() < screencast.VIEWER_BACKLOG and feed._key_wanted
    feed._on_video({"key": False, "pts": 99, "data": b"P"})
    assert viewer.backlog() < screencast.VIEWER_BACKLOG
    feed._on_video({"key": True, "pts": 100, "data": b"IDR1"})
    last = viewer.items[-1][1]
    assert last[1] == 1 and last[10:] == b"SPSIDR1" and not viewer.need_key


def test_video_packets_round_trip():
    raw = protocol.encode_video_packet(b"\x00\x00\x00\x01abc", 123_456, key=True)
    message = protocol.decode_message(None, raw)
    assert message == {"type": "video", "key": True, "config": False, "pts": 123_456, "data": b"\x00\x00\x00\x01abc"}


def test_a_viewer_whose_decoder_fell_behind_can_ask_for_a_keyframe(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url)) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))["mode"] == "video"
            # Let the start-up keyframe(s) through, then ask: some keyframe must follow the request
            # (a fresh one, or one already on its way when we asked: either lets the decoder restart).
            for _ in range(10):
                await asyncio.wait_for(ws.recv(), 5)
            await ws.send("keyframe")
            deadline = asyncio.get_running_loop().time() + 3
            while True:
                assert asyncio.get_running_loop().time() < deadline, "no keyframe after the request"
                packet = await asyncio.wait_for(ws.recv(), 5)
                if isinstance(packet, bytes) and packet[:1] == b"V" and packet[1] & 1:
                    assert packet[10:].startswith(b"\x00\x00\x00\x01SPSPPS")
                    break

    asyncio.run(run())
    phone.disconnect()
