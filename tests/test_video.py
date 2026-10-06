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
    assert messages[0] == ("text", {"mode": "video", "width": 592, "height": 1280, "bitrate": 2_500_000})
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


# --- sized for the viewer's panel ---------------------------------------------------------------


def test_stream_size_follows_the_panel_in_device_pixels():
    View = screencast.View
    # Nobody said: the old defaults.
    assert screencast.video_params([None]) == (1280, 2_500_000, 30)
    assert screencast.preview_params([None]) == (720, 55)
    # The dashboard's phone panel on a 2x display: about 600 x 1230 device pixels.
    side, bitrate, fps = screencast.video_params([View(1232)])
    assert side == 1280 and 2_000_000 <= bitrate <= 2_600_000 and fps == 30
    assert screencast.preview_params([View(1232)]) == (1280, 72)  # not a 720 px JPEG blown up 1.7x
    # The same panel on a 1x display needs half the pixels (and bits).
    small, small_bitrate, _ = screencast.video_params([View(616)])
    assert small == 640 and small_bitrate < bitrate / 2
    # Sharp leaves headroom and spends more bits per pixel; capped at what phones encode.
    sharp, sharp_bitrate, _ = screencast.video_params([View(1232, "sharp")])
    assert sharp == 1600 and sharp_bitrate > 2 * bitrate
    assert screencast.video_params([View(4000, "sharp")])[:2] == (1920, screencast.MAX_BITRATE)
    # Data saver: smaller and lighter, but never below a readable floor.
    saver, saver_bitrate, _ = screencast.video_params([View(1232, "saver")])
    assert saver == 960 and saver_bitrate < bitrate / 2
    assert screencast.video_params([View(200, "saver")])[:2] == (480, screencast.MIN_BITRATE)
    assert screencast.preview_params([View(1232, "saver")]) == (720, 50)
    # Several viewers: the largest panel and the sharpest preference win.
    assert screencast.video_params([View(616, "sharp"), View(1232, "saver"), None])[0] == 1600
    assert screencast.video_params([View(616), View(1232), None])[0] == 1280
    # Small resizes stay in the same step, so they don't restart the stream.
    assert screencast.video_params([View(1200)]) == screencast.video_params([View(1270)])


def test_view_parsing_is_forgiving():
    View = screencast.View
    assert View.parse("1232.4", "sharp") == View(1232, "sharp")
    assert View.parse(900, "ultra") == View(900, "balanced")
    assert View.parse(None, "sharp") is None and View.parse("x") is None and View.parse(0) is None
    assert View.parse(10**9).side == 8192


def test_viewer_panel_size_sets_the_stream(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), video=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url) + "&side=1232&quality=balanced") as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            for _ in range(3):
                await asyncio.wait_for(ws.recv(), 5)
            # The panel got bigger (window resized, or "Sharp" picked): restart at the new size.
            await ws.send(json.dumps({"view": {"side": 1232, "quality": "sharp"}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), 5)
                if isinstance(raw, str):
                    second = json.loads(raw)
                    break
            packet = await asyncio.wait_for(ws.recv(), 5)  # the new stream starts at a keyframe
            return first, second, packet

    first, second, packet = asyncio.run(run())
    starts = [(i, p) for i, (m, p) in enumerate(phone.calls) if m == "stream_start"]
    assert starts[0][1]["max_side"] == 1280 and starts[0][1]["bitrate"] == first["bitrate"]
    assert starts[1][1]["max_side"] == 1600 and starts[1][1]["bitrate"] == second["bitrate"] > first["bitrate"]
    assert second["mode"] == "video"
    assert packet[:1] == b"V" and packet[1] & 1 and packet[10:].startswith(b"\x00\x00\x00\x01SPSPPS")
    # Restarted in place: the phone's encoder wasn't stopped in between, and no screenshots taken.
    assert ("stream_stop", {}) not in phone.calls[: starts[1][0]]
    assert not any(m == "screenshot" for m, _ in phone.calls)
    phone.disconnect()


def test_screenshot_fallback_is_sized_for_the_panel(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), binary=True)
    assert phone.welcome.wait(5)

    async def run():
        async with websockets.connect(viewer_url(url) + "&side=1232") as ws:
            await asyncio.wait_for(ws.recv(), 5)

    asyncio.run(run())
    shots = [p for m, p in phone.calls if m == "screenshot"]
    assert shots and shots[0]["max_side"] == 1280 and shots[0]["quality"] == 72
    phone.disconnect()


def test_a_viewer_more_than_half_a_second_behind_skips_ahead():
    viewer = screencast._Viewer()
    feed = screencast.ScreenFeed(driver=None)
    feed.mode = "video"
    feed._viewers.add(viewer)

    def age(seconds):  # as if nothing could be sent for that long
        viewer.items = type(viewer.items)((k, p, t - seconds) for k, p, t in viewer.items)

    feed._on_video({"key": True, "pts": 0, "data": b"IDR0"})
    for n in range(screencast.VIEWER_MIN_SKIP):
        feed._on_video({"key": False, "pts": n + 1, "data": b"P"})
    assert not viewer.behind()  # a few packets, just queued: fine
    age(1.0)
    assert viewer.behind()
    feed._on_video({"key": False, "pts": 9, "data": b"P"})
    assert viewer.need_key and viewer.backlog() == 0 and feed._key_wanted
    # One big keyframe on its way is not "behind", however long it takes to send.
    viewer.put(("video", b"IDR"))
    age(1.0)
    assert not viewer.behind()
