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
"height"} / {"mode": "jpeg"} when the mode changes, or {"error": "..."}.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from collections.abc import Callable

from fastapi.concurrency import run_in_threadpool

from .phone.adb import Screenshot

PREVIEW_MAX_SIDE = 720
PREVIEW_QUALITY = 55
REUSE_MAX_AGE = 1.0  # reuse an agent frame this fresh instead of capturing another
MIN_INTERVAL = 0.05  # at most ~20 frames a second, even from a fast phone
ERROR_BACKOFF = 2.0
RECHECK_SECONDS = 2.0  # how often an open viewer's sign-in and ownership are checked again

VIDEO_MAX_SIDE = 1280  # long side of the encoded video, in pixels
VIDEO_BITRATE = 2_500_000  # target; the phone lowers it when its upload can't keep up
VIDEO_FPS = 30
VIDEO_RETRY = 30.0  # after the phone fails to start a stream, use screenshots this long
VIEWER_BACKLOG = 45  # packets (~1.5 s): a viewer further behind skips to the next keyframe


def live_frame(driver, newer_than: float = 0.0) -> tuple[float, Screenshot]:
    """(capture time, frame) newer than `newer_than`, for the live view. Blocks."""
    preview = getattr(driver, "preview", None)
    if preview is None:  # a driver without preview support: its normal (shared) frames
        return time.time(), driver.screenshot(max_age=REUSE_MAX_AGE)
    return preview(PREVIEW_MAX_SIDE, PREVIEW_QUALITY, max_age=REUSE_MAX_AGE, newer_than=newer_than)


class _Viewer:
    """One open viewer socket and what's waiting to be sent to it. Screenshot frames replace each
    other (a slow viewer just skips some); video packets queue up in order, since each one builds
    on the last, until the backlog says this viewer should skip ahead to a keyframe."""

    def __init__(self) -> None:
        self.items: deque[tuple[str, object]] = deque()
        self.ready = asyncio.Event()
        self.need_key = True  # video: drop packets until a keyframe

    def put(self, item: tuple[str, object]) -> None:
        if item[0] == "frame":
            self.items = deque(i for i in self.items if i[0] != "frame")
        self.items.append(item)
        self.ready.set()

    def backlog(self) -> int:
        return sum(1 for i in self.items if i[0] == "video")

    def skip_video(self) -> None:
        self.items = deque(i for i in self.items if i[0] != "video")
        self.need_key = True

    async def next(self) -> tuple[str, object]:
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        return self.items.popleft()


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

    async def serve(self, ws, allowed: Callable[[], bool]) -> None:
        """Send frames to one accepted viewer socket until it closes, or `allowed()` turns False."""
        viewer = _Viewer()
        if self.mode == "video" and self._mode_message:
            viewer.put(self._mode_message)
            self._key_wanted = True  # so this viewer can start soon
        elif self._latest:
            viewer.put(self._latest)  # something to show right away
        self._viewers.add(viewer)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        closed = asyncio.create_task(_until_closed(ws, self._from_viewer))
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

    def _from_viewer(self, text: str) -> None:
        """A viewer's decoder fell behind or failed: it asks for a keyframe to start again from."""
        if text == "keyframe" and self.mode == "video":
            self._key_wanted = True

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
            taken, shot = await run_in_threadpool(live_frame, self.driver, self._last)
        except Exception as exc:  # DeviceError: phone offline, accessibility off, ...
            self._broadcast(("error", str(exc) or type(exc).__name__))
            await asyncio.sleep(ERROR_BACKOFF)
            return
        if taken > self._last:
            self._last = taken
            self._broadcast(("frame", shot.jpeg))
        await asyncio.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - started)))

    async def _run_video(self) -> None:
        """Stream video until the last viewer leaves, the phone reconnects, or streaming ends."""
        link = self.driver.link
        self._config = b""
        link.video_listeners.add(self._on_video)
        started = False
        try:
            try:
                size = await run_in_threadpool(self.driver.stream_start, VIDEO_MAX_SIDE, VIDEO_BITRATE, VIDEO_FPS)
            except Exception:  # an app without video, or the phone refused: screenshots for a while
                self._video_retry_at = time.monotonic() + VIDEO_RETRY
                return
            started = True
            self.mode = "video"
            self._mode_message = ("mode", {"mode": "video", **size})
            for viewer in self._viewers:
                viewer.need_key = True
                viewer.put(self._mode_message)
            # Packets that raced ahead of stream_start's reply were dropped (viewers hadn't been
            # told the mode yet), so ask for a fresh keyframe to start from.
            self._key_wanted = True
            while (self._viewers and self.driver.link is link and not link.closed
                   and getattr(self.driver, "video_available", False)):
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
        if key and self._config and not data.startswith(self._config):
            data = self._config + data  # decoders joining here need the SPS/PPS
        message = b"V" + bytes([1 if key else 0]) + struct.pack(">q", int(packet.get("pts") or 0)) + data
        for viewer in self._viewers:
            if viewer.need_key:
                if not key:
                    continue
                viewer.need_key = False
            elif viewer.backlog() >= VIEWER_BACKLOG:
                viewer.skip_video()  # too far behind: catch up at the next keyframe
                self._key_wanted = True
                continue
            viewer.put(("video", message))

    def _broadcast(self, item: tuple[str, bytes | str]) -> None:
        self._latest = item
        for viewer in self._viewers:
            viewer.put(item)
