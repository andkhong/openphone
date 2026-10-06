"""Server side of the phone protocol: a phone that connected to us over WebSocket.

PhoneLink owns one WebSocket and turns synchronous calls from agent threads into
request/response messages on the server's event loop. RemoteDriver implements the
Device interface on top of a PhoneLink, so the agent can't tell it from adb.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid

from .adb import Screenshot, UIElement
from .base import CachedScreenshots, DeviceError, GateRefused, PhoneLocked, PhoneTimeout
from .protocol import METHODS, decode_element, decode_screenshot

# Calls that only read, so one cut off by a dropped connection can be asked again after the
# phone reconnects. Anything that acts (tap, type, launch…) is never repeated: it may have happened.
REPEATABLE = {"screenshot", "ui_elements", "current_app", "launchable_packages", "app_labels", "wake_unlock", "wait_idle",
              "resolve_url"}
# Calls the phone's gatekeeper judges as actions (they carry the task's mode, task id and the server's hint).
GATED = {"tap", "long_press", "swipe", "type_text", "key", "launch_app", "open_url"}


class PhoneLink:
    def __init__(self, websocket, loop: asyncio.AbstractEventLoop):
        self.websocket = websocket
        self.loop = loop
        self.closed = False
        self.peer: str | None = None  # the client address it connected from
        self.key_hash: str | None = None  # the device key it proved at connect (None: token mode)
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.video_listeners: set = set()  # callables(packet dict), run on the event loop

    # --- called from agent / worker threads -------------------------------

    def call(self, method: str, params: dict | None = None):
        """Run one protocol call on the phone and return its result. Blocks the calling thread."""
        if self.closed:
            raise DeviceError("Phone is disconnected")
        timeout = METHODS[method][2]
        future = asyncio.run_coroutine_threadsafe(self._call(method, params or {}, timeout), self.loop)
        try:
            return future.result(timeout + 5)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise PhoneTimeout(f"Phone did not answer {method} within {timeout:g}s") from None

    def notify(self, kind: str, message: str) -> None:
        """Fire-and-forget activity event for the phone to show or speak."""
        if not self.closed:
            asyncio.run_coroutine_threadsafe(self._notify(kind, message), self.loop)

    async def _notify(self, kind: str, message: str) -> None:
        await self._post({"type": "event", "kind": kind, "message": message})

    def post(self, message: dict) -> None:
        """Fire-and-forget message to the phone (an approval_request, a controller event)."""
        if not self.closed:
            asyncio.run_coroutine_threadsafe(self._post(message), self.loop)

    async def _post(self, message: dict) -> None:
        try:
            await self.send(message)
        except Exception:
            pass  # best-effort; the phone may have just disconnected (pending approvals are re-sent on reconnect)

    # --- event loop side --------------------------------------------------

    async def send(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def _call(self, method: str, params: dict, timeout: float):
        call_id = uuid.uuid4().hex
        future = self.loop.create_future()
        self._pending[call_id] = future
        try:
            try:
                await self.send({"type": "call", "id": call_id, "method": method, "params": params})
            except Exception as exc:  # the socket closed under us (server-specific exception types)
                raise DeviceError("Phone disconnected") from exc
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise PhoneTimeout(f"Phone did not answer {method} within {timeout:g}s") from None
        finally:
            self._pending.pop(call_id, None)
        if not reply.get("ok"):
            code = reply.get("code")
            if code == "phone_locked":
                raise PhoneLocked.from_reply(reply)
            if isinstance(code, str) and code:  # the gatekeeper refused it, with a stable code
                raise GateRefused.from_reply(reply)
            raise DeviceError(reply.get("error") or f"{method} failed on the phone")
        return reply.get("result")

    def disconnect(self, code: int = 4401, reason: str = "") -> None:
        """Close the socket from any thread, e.g. when the owner revokes this phone."""
        self.close()
        asyncio.run_coroutine_threadsafe(self._close_socket(code, reason), self.loop)

    async def _close_socket(self, code: int, reason: str) -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass  # already closed

    def on_video(self, packet: dict) -> None:
        """A live-view video packet from the phone (event loop)."""
        for listener in list(self.video_listeners):
            try:
                listener(packet)
            except Exception:  # one broken viewer must not stop the stream for the others
                pass

    def resolve(self, message: dict) -> None:
        future = self._pending.get(message.get("id", ""))
        if future and not future.done():
            future.set_result(message)

    def close(self) -> None:
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeviceError("Phone disconnected"))


class RemoteDriver(CachedScreenshots):
    def __init__(self, link: PhoneLink):
        self.link = link
        self.video_available = False  # the owner allowed screen streaming (hello "video", video_state)
        self._state_unsupported = False  # an older app without device_state
        self.capabilities: set[str] = set()  # optional methods its hello offered (device.capabilities)
        self._init_screenshot_cache()
        self._reconnect = threading.Condition()
        self._wait_until: float | None = None  # monotonic deadline while the phone may come back
        # Gatekeeper phones (hello capability "gatekeeper", docs/phone-protocol.md): the phone owns approval.
        self.gate_mode: str | None = None  # the task's permission mode, sent with each act call (it can only tighten)
        self.gate_task: str = ""  # the task id, sent with act calls (a purchase flow's permit is per task)
        self._gate_hint: str = ""  # the server's own caution for the next act call (Agent._guard)
        # (GateRefused) -> bool: wait for the owner's answer on the phone (the agent sets it). True: re-send once.
        self.gate_approver = None
        # (PhoneLocked) -> bool: pause until the owner unlocks (the agent sets it). True: unlocked in time.
        self.lock_waiter = None
        self.lock_state: dict | None = None  # {"locked", "secure"} as the phone last pushed it (device_lock)
        self._lock_changed = threading.Condition()

    @property
    def gatekeeper(self) -> bool:
        """The phone enforces the gatekeeper: refusals come back with codes, and it asks its owner itself."""
        return "gatekeeper" in self.capabilities

    def gate_hint(self, text: str) -> None:
        """The server's checks found the next act risky: the phone asks (one prompt covering both detectors)."""
        self._gate_hint = " ".join((text or "").split())[:200]

    def on_device_lock(self, message: dict) -> None:
        """The phone pushed its lock state (device_lock): a secure lock mid-task, or the owner unlocked."""
        with self._lock_changed:
            self.lock_state = {"locked": bool(message.get("locked")), "secure": bool(message.get("secure", True))}
            self._lock_changed.notify_all()

    @property
    def locked(self) -> bool:
        state = self.lock_state
        return bool(state and state["locked"] and state["secure"])

    def wait_unlocked(self, timeout: float) -> bool:
        """Block until the phone says it's unlocked (or, for a phone that doesn't push, device_state says so)."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock_changed:
                if self.lock_state is not None and not self.locked:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock_changed.wait(min(remaining, 2.0))
            if self.lock_state is None or "device_lock" not in self.capabilities:
                try:
                    state = self.device_state()
                except DeviceError:
                    state = None
                if state is not None and not (state["locked"] and state["secure"]):
                    self.on_device_lock({"locked": False})
                    return True

    def attach(self, link: PhoneLink) -> None:
        """Swap in a new connection after the phone reconnects; calls waiting for it go ahead."""
        with self._reconnect:
            self.link = link
            self._wait_until = None
            self._last_shot = None
            self._last_preview = None
            self._state_unsupported = False  # the app may have been updated
            self.capabilities = set()  # the new hello says again (DeviceManager.attach_remote)
            self._reconnect.notify_all()

    def hold(self, seconds: float | None) -> None:
        """The connection dropped mid-task: let calls wait up to `seconds` for a reconnect.
        None gives up: waiting calls fail with DeviceError now."""
        with self._reconnect:
            self._wait_until = None if seconds is None else time.monotonic() + seconds
            self._reconnect.notify_all()

    def _usable_link(self, wait: bool) -> PhoneLink:
        with self._reconnect:
            while self.link.closed:
                remaining = (self._wait_until or 0) - time.monotonic()
                if not wait or remaining <= 0:
                    raise DeviceError("Phone is disconnected")
                self._reconnect.wait(remaining)
            return self.link

    def _call(self, method: str, _wait: bool = True, **params):
        """One call on the phone. On a gatekeeper phone an act call carries the task's mode (and the server's
        hint); `gate_needs_approval` waits for the owner's answer on the phone and then re-sends the identical
        call once; `phone_locked` pauses until the owner unlocks (the call is not repeated: the screen moved)."""
        if self.gatekeeper and method in GATED:
            if self.gate_mode:
                params["mode"] = self.gate_mode
            if self.gate_task:
                params["task"] = self.gate_task
            if self._gate_hint:
                params["hint"], self._gate_hint = self._gate_hint, ""
        try:
            return self._call_once(method, _wait, **params)
        except PhoneLocked as exc:
            if self.lock_waiter is None:
                raise
            self.on_device_lock({"locked": True, "secure": True})
            if self.lock_waiter(exc):
                raise DeviceError("The phone locked before this ran, so it didn't happen. It's unlocked now: look at "
                                  "the screen again before acting.") from None
            raise
        except GateRefused as exc:
            if exc.code != "gate_needs_approval" or self.gate_approver is None:
                raise
            if not self.gate_approver(exc):
                raise GateRefused("gate_denied", "The owner didn't approve this on the phone. Don't do it; pick "
                                  "another approach or call done.", exc.rule, exc.package, exc.approval_id) from None
            return self._call_once(method, _wait, **params)  # at most once per approval

    def _call_once(self, method: str, _wait: bool = True, **params):
        """One call on the phone. While it's reconnecting, wait for it (`_wait`, the agent's calls)."""
        while True:
            link = self._usable_link(_wait)
            try:
                return link.call(method, params)
            except DeviceError:
                if not (_wait and link.closed):
                    raise
                if method not in REPEATABLE:
                    raise DeviceError(f"The phone disconnected during {method}, so it wasn't repeated. "
                                      "Look at the screen before trying again.") from None
                # A read cut off by the drop: ask again on the next connection (or fail when time's up).

    def _capture(self) -> Screenshot:
        return decode_screenshot(self._call("screenshot"))

    def capture_preview(self, max_side: int, quality: int) -> Screenshot:
        # Older apps ignore these params and send a full JSON frame; that decodes the same way.
        return decode_screenshot(self._call("screenshot", _wait=False, max_side=max_side, quality=quality, binary=True))

    # Live-view video (the dashboard's screen panel only; see screencast.ScreenFeed).

    def stream_start(self, max_side: int, bitrate: int, fps: int) -> dict:
        size = self._call("stream_start", _wait=False, max_side=int(max_side), bitrate=int(bitrate), fps=int(fps)) or {}
        return {"width": int(size.get("width", 0)), "height": int(size.get("height", 0))}

    def stream_stop(self) -> None:
        self._call("stream_stop", _wait=False)

    def stream_keyframe(self) -> None:
        self._call("stream_keyframe", _wait=False)

    def ui_elements(self) -> list[UIElement]:
        return [decode_element(i, data) for i, data in enumerate(self._call("ui_elements") or [])]

    def current_app(self) -> str | None:
        return self._call("current_app")

    def launchable_packages(self) -> list[str]:
        return sorted(self._call("launchable_packages") or [])

    def app_labels(self, packages: list[str]) -> dict[str, str | None]:
        labels = self._call("app_labels", packages=packages) or {}
        return {p: labels.get(p) for p in packages}

    def launch_app(self, package: str) -> None:
        self._call("launch_app", package=package)

    def tap(self, x: int, y: int) -> None:
        self._call("tap", x=int(x), y=int(y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self._call("long_press", x=int(x), y=int(y), duration_ms=int(duration_ms))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._call("swipe", x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2), duration_ms=int(duration_ms))

    def type_text(self, text: str) -> None:
        self._call("type_text", text=text)

    def key(self, name: str) -> None:
        self._call("key", name=name.upper())

    def open_url(self, url: str) -> None:
        self._call("open_url", url=url)

    def resolve_url(self, url: str) -> dict | None:
        """Which app would open `url` ({"package", "candidates"}), or None when this phone's app is too
        old to say (the agent then checks the app in front right after opening)."""
        if "resolve_url" not in self.capabilities:
            return None
        result = self._call("resolve_url", url=url) or {}
        package = result.get("package")
        return {"package": str(package) if package else None,
                "candidates": [str(c) for c in result.get("candidates") or []]}

    def wake_unlock(self) -> None:
        self._call("wake_unlock")

    # Asking the phone for its accessibility tree is quick (no uiautomator dump), so the agent can
    # re-check a tap target just before tapping it.
    fast_ui_tree = True

    def wait_idle(self, quiet_ms: int, max_ms: int) -> dict | None:
        """Wait until the screen stops changing: {"waited_ms", "idle"}, or None when this phone's app
        can't (an older app), so the caller sleeps a fixed time instead."""
        if "wait_idle" not in self.capabilities:
            return None
        try:
            result = self._call("wait_idle", quiet_ms=int(quiet_ms), max_ms=int(max_ms)) or {}
        except PhoneTimeout:
            raise
        except DeviceError:
            if self.link.closed:
                raise
            self.capabilities.discard("wait_idle")  # it said so, but can't after all: stop asking
            return None
        return {"waited_ms": int(result.get("waited_ms", 0)), "idle": bool(result.get("idle"))}

    def device_state(self) -> dict | None:
        """{"interactive", "locked"} right now, or None if this app version can't say. Never waits
        for a reconnect; PhoneTimeout means connected but not answering."""
        if self._state_unsupported:
            return None
        try:
            state = self._call("device_state", _wait=False) or {}
        except PhoneTimeout:
            raise
        except DeviceError:
            if self.link.closed:
                raise
            self._state_unsupported = True  # "Unknown method device_state": skip the check from now on
            return None
        locked = bool(state.get("locked", False))
        # "secure" (PIN, pattern, password, biometric) came later: without it, assume a lock is secure.
        return {"interactive": bool(state.get("interactive", True)), "locked": locked,
                "secure": bool(state.get("secure", locked))}
