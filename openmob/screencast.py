"""Live screen for the dashboard: small preview frames, pulled from the phone and pushed to viewers.

A ScreenFeed runs while at least one viewer WebSocket is open: it pulls frames from the phone
back to back (reusing the agent's own fresh frames when it can) and fans each one out to every
viewer. With no viewers it stops, so an unwatched phone does no extra work.

Preview frames are smaller and lower quality than the agent's (see CachedScreenshots.preview);
they are cached apart, so the agent never sees one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from fastapi.concurrency import run_in_threadpool

from .phone.adb import Screenshot

PREVIEW_MAX_SIDE = 720
PREVIEW_QUALITY = 55
REUSE_MAX_AGE = 1.0  # reuse an agent frame this fresh instead of capturing another
MIN_INTERVAL = 0.05  # at most ~20 frames a second, even from a fast phone
ERROR_BACKOFF = 2.0
RECHECK_SECONDS = 2.0  # how often an open viewer's sign-in and ownership are checked again


def live_frame(driver, newer_than: float = 0.0) -> tuple[float, Screenshot]:
    """(capture time, frame) newer than `newer_than`, for the live view. Blocks."""
    preview = getattr(driver, "preview", None)
    if preview is None:  # a driver without preview support: its normal (shared) frames
        return time.time(), driver.screenshot(max_age=REUSE_MAX_AGE)
    return preview(PREVIEW_MAX_SIDE, PREVIEW_QUALITY, max_age=REUSE_MAX_AGE, newer_than=newer_than)


class _Viewer:
    """One open viewer socket. Holds only the newest item, so a slow viewer skips frames."""

    def __init__(self) -> None:
        self.item: tuple[str, bytes | str] | None = None
        self.ready = asyncio.Event()

    def put(self, item: tuple[str, bytes | str]) -> None:
        self.item = item
        self.ready.set()

    async def next(self) -> tuple[str, bytes | str]:
        await self.ready.wait()
        self.ready.clear()
        item, self.item = self.item, None
        return item


async def _until_closed(ws) -> None:
    while (await ws.receive())["type"] != "websocket.disconnect":
        pass  # viewers have nothing to say; anything they send is ignored


class ScreenFeed:
    def __init__(self, driver) -> None:
        self.driver = driver
        self._viewers: set[_Viewer] = set()
        self._latest: tuple[str, bytes | str] | None = None
        self._task: asyncio.Task | None = None

    async def serve(self, ws, allowed: Callable[[], bool]) -> None:
        """Send frames to one accepted viewer socket until it closes, or `allowed()` turns False."""
        viewer = _Viewer()
        if self._latest:
            viewer.put(self._latest)  # something to show right away
        self._viewers.add(viewer)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        closed = asyncio.create_task(_until_closed(ws))
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
                if kind == "frame":
                    await ws.send_bytes(payload)
                else:
                    await ws.send_json({"error": payload})
        except Exception:
            pass  # the viewer went away mid-send
        finally:
            self._viewers.discard(viewer)
            closed.cancel()

    async def _run(self) -> None:
        last = 0.0
        while self._viewers:
            started = time.monotonic()
            try:
                taken, shot = await run_in_threadpool(live_frame, self.driver, last)
            except Exception as exc:  # DeviceError: phone offline, accessibility off, ...
                self._broadcast(("error", str(exc) or type(exc).__name__))
                await asyncio.sleep(ERROR_BACKOFF)
                continue
            if taken > last:
                last = taken
                self._broadcast(("frame", shot.jpeg))
            await asyncio.sleep(max(0.0, MIN_INTERVAL - (time.monotonic() - started)))
        self._latest = None

    def _broadcast(self, item: tuple[str, bytes | str]) -> None:
        self._latest = item
        for viewer in self._viewers:
            viewer.put(item)
