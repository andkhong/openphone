"""What the agent needs from a phone, independent of how we reach it.

Two implementations:
- PhoneDriver (adb.py): a phone attached to this computer over USB or Wi-Fi adb.
- RemoteDriver (remote.py): a phone connected to the server over WebSocket, via the
  OpenMob Android app or the `openmob bridge` reference client.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .adb import Screenshot, UIElement


class DeviceError(RuntimeError):
    """A phone command failed, timed out, or the phone went away."""


class PhoneTimeout(DeviceError):
    """The phone is connected but didn't answer a command in time (e.g. asleep or stuck)."""


class GateRefused(DeviceError):
    """The phone's gatekeeper refused a call (docs/phone-protocol.md, "Gatekeeper"). `code` is stable
    (gate_blocked_app, gate_needs_approval, phone_locked…); report it, don't retry the same call."""

    def __init__(self, code: str, message: str = "", rule: str = "", package: str = "", approval_id: str = "",
                 question: str = "", kind: str = ""):
        super().__init__(message or code)
        self.code, self.rule, self.package = code, rule, package
        self.approval_id, self.question, self.kind = approval_id, question, kind

    @classmethod
    def from_reply(cls, reply: dict) -> "GateRefused":
        gate = reply.get("gate") if isinstance(reply.get("gate"), dict) else {}
        return cls(str(reply.get("code")), str(reply.get("error") or reply.get("code")), str(gate.get("rule") or ""),
                   str(gate.get("package") or ""), str(reply.get("approval_id") or ""), str(gate.get("question") or ""),
                   str(gate.get("kind") or ""))


class PhoneLocked(GateRefused):
    """The phone has a secure lock up: only its owner can open it. The task pauses until they do."""


class OwnerDialog(GateRefused):
    """A system dialog the phone can't read is up (Android 14+ hides permission dialogs from accessibility): only
    its owner can answer it. The task pauses until it closes (needs_owner_dialog)."""


class Device(Protocol):
    def screenshot(self, max_age: float = 0.0, attempts: int = 2) -> Screenshot: ...
    def capture_preview(self, max_side: int, quality: int) -> Screenshot: ...
    def preview(self, max_side: int, quality: int, max_age: float = 0.0, newer_than: float = 0.0) -> tuple[float, Screenshot]: ...
    def ui_elements(self) -> list[UIElement]: ...
    def current_app(self) -> str | None: ...
    def launchable_packages(self) -> list[str]: ...
    def app_labels(self, packages: list[str]) -> dict[str, str | None]: ...
    def launch_app(self, package: str) -> None: ...
    def tap(self, x: int, y: int) -> None: ...
    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...
    def type_text(self, text: str, replace: bool = False) -> None: ...
    def key(self, name: str) -> None: ...
    def open_url(self, url: str) -> None: ...
    def wake_unlock(self) -> None: ...


class CachedScreenshots:
    """screenshot() with sharing and retry. Subclasses implement _capture().

    Callers that accept a slightly old frame pass max_age, so they reuse the agent's captures
    instead of adding load on a slow link. The dashboard's live view uses preview(), which
    keeps its smaller frames apart: screenshot() never returns a preview frame.
    """

    def _init_screenshot_cache(self) -> None:
        self._shot_lock = threading.Lock()
        self._last_shot: tuple[float, Screenshot] | None = None
        self._last_preview: tuple[float, Screenshot] | None = None

    def _capture(self) -> Screenshot:
        raise NotImplementedError

    def capture_preview(self, max_side: int, quality: int) -> Screenshot:
        """One uncached, smaller frame for the live view. Drivers that can't scale return a full one."""
        return self._capture()

    def preview(self, max_side: int, quality: int, max_age: float = 0.0, newer_than: float = 0.0) -> tuple[float, Screenshot]:
        """(capture time, frame) for the live view: the newest cached frame (the agent's or an earlier
        preview) that is newer than `newer_than` and at most max_age old, else a fresh small capture."""
        with self._shot_lock:
            now = time.time()
            fresh = [c for c in (self._last_shot, self._last_preview) if c and c[0] > newer_than and now - c[0] <= max_age]
            if fresh:
                return max(fresh, key=lambda c: c[0])
            shot = self.capture_preview(max_side, quality)
            self._last_preview = (time.time(), shot)
            return self._last_preview

    def latest_capture(self) -> Screenshot | None:
        """The newest frame already captured (the agent's or the live view's), or None; never captures.
        Its device_width/height are the screen's size (a preview frame is smaller, the device isn't)."""
        frames = [c for c in (self._last_shot, self._last_preview) if c]
        return max(frames, key=lambda c: c[0])[1] if frames else None

    def screenshot(self, max_age: float = 0.0, attempts: int = 2) -> Screenshot:
        with self._shot_lock:
            if self._last_shot and time.time() - self._last_shot[0] <= max_age:
                return self._last_shot[1]
            for attempt in range(attempts):
                try:
                    shot = self._capture()
                    break
                except DeviceError:
                    if attempt == attempts - 1:
                        raise
            self._last_shot = (time.time(), shot)
            return shot
