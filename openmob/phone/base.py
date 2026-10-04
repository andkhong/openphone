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


class Device(Protocol):
    def screenshot(self, max_age: float = 0.0, attempts: int = 2) -> Screenshot: ...
    def ui_elements(self) -> list[UIElement]: ...
    def current_app(self) -> str | None: ...
    def launchable_packages(self) -> list[str]: ...
    def app_labels(self, packages: list[str]) -> dict[str, str | None]: ...
    def launch_app(self, package: str) -> None: ...
    def tap(self, x: int, y: int) -> None: ...
    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, name: str) -> None: ...
    def open_url(self, url: str) -> None: ...
    def wake_unlock(self) -> None: ...


class CachedScreenshots:
    """screenshot() with sharing and retry. Subclasses implement _capture().

    Callers that accept a slightly old frame (the dashboard's live view) pass max_age,
    so they reuse the agent's captures instead of adding load on a slow link.
    """

    def _init_screenshot_cache(self) -> None:
        self._shot_lock = threading.Lock()
        self._last_shot: tuple[float, Screenshot] | None = None

    def _capture(self) -> Screenshot:
        raise NotImplementedError

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
