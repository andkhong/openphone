"""Live screen for the dashboard, pushed to viewers while at least one viewer WebSocket is open.

Two modes:
- Video: when the phone's owner allowed screen streaming (hello "video": true), the phone encodes
  H.264 and the feed forwards each packet to every viewer. A viewer starts at a keyframe, and a
  viewer that falls behind skips ahead to the next keyframe instead of building up delay.
- Screenshots (fallback): small preview JPEGs pulled back to back (reusing the agent's own fresh
  frames when it can). Preview frames are cached apart from the agent's, so it never sees one.

With no viewers the feed stops, so an unwatched phone does no extra work. The agent always uses
screenshots; video exists only for people watching.

Viewer socket messages: binary JPEG frames (they start with FF D8); binary video packets
(b"V", a flags byte with bit 0 = keyframe, an 8-byte big-endian timestamp in µs, then Annex B
H.264 with the SPS/PPS in front of every keyframe); and JSON texts {"mode": "video", "width",
"height", "bitrate"} / {"mode": "jpeg"} when the mode changes, or {"error": "..."}.

What viewers send: "keyframe" (their decoder needs a fresh start), and {"view": {"side", "quality"}}:
the long side of their screen panel in device pixels and a preference ("sharp", "balanced" or
"saver"), also accepted as ?side=&quality= on the socket URL. The feed sizes the video (and the
preview JPEGs) for the largest panel watching, so a small panel doesn't pull a huge stream and a
large or high-DPI one isn't fed an upscaled, blurry one. Viewers that say nothing get the defaults.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool

from .phone.adb import Screenshot

PREVIEW_MAX_SIDE = 720  # screenshots for a viewer that didn't say its panel size
PREVIEW_QUALITY = 55
REUSE_MAX_AGE = 1.0  # reuse an agent frame this fresh instead of capturing another
MIN_INTERVAL = 0.05  # at most ~20 frames a second, even from a fast phone
ERROR_BACKOFF = 2.0
RECHECK_SECONDS = 2.0  # how often an open viewer's sign-in and ownership are checked again

VIDEO_MAX_SIDE = 1280  # long side of the encoded video, in pixels (viewer size unknown)
VIDEO_BITRATE = 2_500_000  # target; the phone lowers it when its upload can't keep up
VIDEO_FPS = 30
VIDEO_RETRY = 30.0  # after the phone fails to start a stream, use screenshots this long
VIEWER_BACKLOG = 45  # packets: a viewer further behind skips to the next keyframe
VIEWER_MAX_DELAY = 0.5  # seconds: a viewer whose oldest unsent packet is older skips ahead too...
VIEWER_MIN_SKIP = 4  # ...if at least this many are waiting (one big keyframe isn't "behind")

# Sizing for the viewer's panel. Screen content is mostly text and sharp edges: it needs more bits
# per pixel than camera video while it changes, and costs almost nothing while it doesn't.
SIDE_STEP = 160  # sizes round up to this, so small panel resizes don't restart the stream
MIN_SIDE, MAX_SIDE = 480, 1920
ASPECT = 0.46  # short side / long side of a typical phone (1080 x 2340), for the bitrate estimate
MIN_BITRATE, MAX_BITRATE = 600_000, 8_000_000
QUALITIES = {  # sharpest first
    # name: (x panel pixels, video cap, bits per pixel per frame, JPEG cap, JPEG quality)
    "sharp": (1.25, 1920, 0.16, 1280, 82),
    "balanced": (1.0, 1600, 0.10, 1280, 72),
    "saver": (0.7, 960, 0.06, 720, 50),
}


@dataclass(frozen=True)
class View:
    """What one viewer wants: its panel's long side in device pixels, and a quality preference."""

    side: int
    quality: str = "balanced"

    @classmethod
    def parse(cls, side, quality=None) -> View | None:
        try:
            side = int(float(side))
        except (TypeError, ValueError):
            return None
        if side <= 0:
            return None
        return cls(min(side, 8192), quality if quality in QUALITIES else "balanced")


def _choose(views) -> tuple[list[View], str]:
    views = [v for v in views if v]
    order = list(QUALITIES)
    return views, min((v.quality for v in views), key=order.index, default="balanced")


def _round_side(side: float, cap: int) -> int:
    return max(MIN_SIDE, min(cap, -(-int(side) // SIDE_STEP) * SIDE_STEP))


def video_params(views) -> tuple[int, int, int]:
    """(max_side, bitrate, fps) of a stream for every one of `views` (None: a viewer that didn't say).
    The largest panel decides the size; the sharpest preference anyone asked for, the quality."""
    views, quality = _choose(views)
    if not views:
        return VIDEO_MAX_SIDE, VIDEO_BITRATE, VIDEO_FPS
    scale, cap, bpp, _, _ = QUALITIES[quality]
    side = _round_side(max(v.side for v in views) * scale, cap)
    bitrate = round(side * side * ASPECT * VIDEO_FPS * bpp / 100_000) * 100_000
    return side, max(MIN_BITRATE, min(MAX_BITRATE, bitrate)), VIDEO_FPS


def preview_params(views) -> tuple[int, int]:
    """(max_side, JPEG quality) of the screenshot frames for `views`."""
    views, quality = _choose(views)
    if not views:
        return PREVIEW_MAX_SIDE, PREVIEW_QUALITY
    scale, _, _, cap, jpeg_quality = QUALITIES[quality]
    return _round_side(max(v.side for v in views) * scale, cap), jpeg_quality


def live_frame(driver, newer_than: float = 0.0, max_side: int = PREVIEW_MAX_SIDE,
               quality: int = PREVIEW_QUALITY) -> tuple[float, Screenshot]:
    """(capture time, frame) newer than `newer_than`, for the live view. Blocks."""
    preview = getattr(driver, "preview", None)
    if preview is None:  # a driver without preview support: its normal (shared) frames
        return time.time(), driver.screenshot(max_age=REUSE_MAX_AGE)
    return preview(max_side, quality, max_age=REUSE_MAX_AGE, newer_than=newer_than)


class _Viewer:
    """One open viewer socket and what's waiting to be sent to it. Screenshot frames replace each
    other (a slow viewer just skips some); video packets queue up in order, since each one builds
    on the last, until the viewer is so far behind that it should skip ahead to a keyframe."""

    def __init__(self, view: View | None = None) -> None:
        self.items: deque[tuple[str, object, float]] = deque()  # (kind, payload, queued at)
        self.ready = asyncio.Event()
        self.need_key = True  # video: drop packets until a keyframe
        self.view = view

    def put(self, item: tuple[str, object]) -> None:
        if item[0] == "frame":
            self.items = deque(i for i in self.items if i[0] != "frame")
        self.items.append((*item, time.monotonic()))
        self.ready.set()

    def backlog(self) -> int:
        return sum(1 for i in self.items if i[0] == "video")

    def behind(self) -> bool:
        """Too far behind the live picture: better to skip ahead to the next keyframe."""
        count = self.backlog()
        if count >= VIEWER_BACKLOG:
            return True
        if count < VIEWER_MIN_SKIP:
            return False
        oldest = next(i[2] for i in self.items if i[0] == "video")
        return time.monotonic() - oldest > VIEWER_MAX_DELAY

    def skip_video(self) -> None:
        self.items = deque(i for i in self.items if i[0] != "video")
        self.need_key = True

    async def next(self) -> tuple[str, object]:
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        kind, payload, _ = self.items.popleft()
        return kind, payload


async def _until_closed(ws, on_text: Callable[[str], None]) -> None:
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text"):
            on_text(message["text"])


class ScreenFeed:
    def __init__(self, driver) -> None:
        self.driver = driver
        self._viewers: set[_Viewer] = set()
        self._latest: tuple[str, bytes | str] | None = None
        self._task: asyncio.Task | None = None
        self.mode: str | None = None  # "video" while streaming video, else screenshots
        self._mode_message: tuple[str, dict] | None = None
        self._config = b""  # the stream's SPS/PPS, put in front of every keyframe
        self._key_wanted = False
        self._video_retry_at = 0.0
        self._last = 0.0

    async def serve(self, ws, allowed: Callable[[], bool], view: View | None = None) -> None:
        """Send frames to one accepted viewer socket until it closes, or `allowed()` turns False."""
        viewer = _Viewer(view)
        if self.mode == "video" and self._mode_message:
            viewer.put(self._mode_message)
            self._key_wanted = True  # so this viewer can start soon
        elif self._latest:
            viewer.put(self._latest)  # something to show right away
        self._viewers.add(viewer)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        closed = asyncio.create_task(_until_closed(ws, lambda text: self._from_viewer(viewer, text)))
        checked = time.monotonic()
        try:
            while not closed.done():
                waiter = asyncio.create_task(viewer.next())
                await asyncio.wait({waiter, closed}, timeout=RECHECK_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                if not waiter.done():
                    waiter.cancel()  # no await before this, so no item can be lost
                if time.monotonic() - checked >= RECHECK_SECONDS:
                    if not await run_in_threadpool(allowed):  # signed out, or the phone was revoked
                        await ws.close(code=4404)
                        return
                    checked = time.monotonic()
                if closed.done() or not waiter.done() or waiter.cancelled():
                    continue
                kind, payload = waiter.result()
                if kind in ("frame", "video"):
                    await ws.send_bytes(payload)
                elif kind == "mode":
                    await ws.send_json(payload)
                else:
                    await ws.send_json({"error": payload})
        except Exception:
            pass  # the viewer went away mid-send
        finally:
            self._viewers.discard(viewer)
            closed.cancel()

    def _from_viewer(self, viewer: _Viewer, text: str) -> None:
        """"keyframe": the viewer's decoder fell behind or failed and needs one to start again from.
        {"view": {"side", "quality"}}: its panel was resized, or its quality preference changed."""
        if text == "keyframe":
            if self.mode == "video":
                self._key_wanted = True
            return
        try:
            view = json.loads(text).get("view")
        except (ValueError, AttributeError):
            return
        if isinstance(view, dict):
            viewer.view = View.parse(view.get("side"), view.get("quality")) or viewer.view

    def views(self) -> list[View | None]:
        return [v.view for v in self._viewers]

    async def _run(self) -> None:
        try:
            while self._viewers:
                if getattr(self.driver, "video_available", False) and time.monotonic() >= self._video_retry_at:
                    await self._run_video()
                else:
                    await self._screenshot_step()
        finally:
            self._latest = None
            self.mode = None

    async def _screenshot_step(self) -> None:
        started = time.monotonic()
        try:
            taken, shot = await run_in_threadpool(live_frame, self.driver, self._last, *preview_params(self.views()))
        except Exception as exc:  # DeviceError: phone offline, accessibility off, ...
            self._broadcast(("error", str(exc) or type(exc).__name__))
            await asyncio.sleep(ERROR_BACKOFF)
            return
        if taken > self._last:
            self._last = taken
            self._broadcast(("frame", shot.jpeg))
        await asyncio.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - started)))

    async def _run_video(self) -> None:
        """Stream video until the last viewer leaves, the phone reconnects, streaming ends, or the
        panels watching want another size (then the caller starts it again at that size)."""
        link = self.driver.link
        self._config = b""
        link.video_listeners.add(self._on_video)
        started = restart = False
        params = video_params(self.views())
        try:
            try:
                size = await run_in_threadpool(self.driver.stream_start, *params)
            except Exception:  # an app without video, or the phone refused: screenshots for a while
                self._video_retry_at = time.monotonic() + VIDEO_RETRY
                return
            started = True
            self.mode = "video"
            self._mode_message = ("mode", {"mode": "video", **size, "bitrate": params[1]})
            for viewer in self._viewers:
                viewer.need_key = True
                viewer.put(self._mode_message)
            # Packets that raced ahead of stream_start's reply were dropped (viewers hadn't been
            # told the mode yet), so ask for a fresh keyframe to start from.
            self._key_wanted = True
            while (self._viewers and self.driver.link is link and not link.closed
                   and getattr(self.driver, "video_available", False)):
                if video_params(self.views()) != params:
                    # A panel was resized or a preference changed. The phone restarts its encoder in
                    # place on the next stream_start; viewers get the new mode, then a keyframe.
                    restart = True
                    break
                if self._key_wanted:
                    self._key_wanted = False
                    try:
                        await run_in_threadpool(self.driver.stream_keyframe)
                    except Exception:
                        pass
                await asyncio.sleep(0.05)
        finally:
            link.video_listeners.discard(self._on_video)
            self.mode = None
            if restart:
                for viewer in self._viewers:
                    viewer.skip_video()  # the old stream's packets are no use to the new decoder
            else:
                if started and not link.closed:
                    try:
                        await run_in_threadpool(self.driver.stream_stop)
                    except Exception:
                        pass
                if started and self._viewers:
                    for viewer in self._viewers:
                        viewer.skip_video()
                        viewer.put(("mode", {"mode": "jpeg"}))

    def _on_video(self, packet: dict) -> None:
        data = packet.get("data") or b""
        if packet.get("config"):
            self._config = data
            return
        if self.mode != "video":
            return  # arrived before stream_start's reply: viewers don't know the mode yet
        key = bool(packet.get("key"))
        if key and self._config and self._config not in data[:len(self._config) + 16]:
            data = self._config + data  # decoders joining here need the SPS/PPS
        message = b"V" + bytes([1 if key else 0]) + struct.pack(">q", int(packet.get("pts") or 0)) + data
        if key and all(not v.need_key for v in self._viewers if not v.behind()):
            self._key_wanted = False  # this keyframe serves everyone waiting: don't ask for another
        for viewer in self._viewers:
            if viewer.need_key:
                if not key:
                    continue
                viewer.need_key = False
            elif viewer.behind():
                viewer.skip_video()  # too far behind: catch up at the next keyframe
                self._key_wanted = True
                continue
            viewer.put(("video", message))

    def _broadcast(self, item: tuple[str, bytes | str]) -> None:
        self._latest = item
        for viewer in self._viewers:
            viewer.put(item)
