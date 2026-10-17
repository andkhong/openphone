"""Emulator helpers for the Jev image benchmark. Every adb call names the emulator; never a real phone."""

from __future__ import annotations

import io
import subprocess
import time

from PIL import Image

SERIAL = "emulator-5554"
FORBIDDEN = {"R5GL115W30H"}  # the owner's phone
assert SERIAL not in FORBIDDEN


def adb(*args: str, timeout: float = 30, check: bool = True) -> bytes:
    out = subprocess.run(["adb", "-s", SERIAL, *args], capture_output=True, timeout=timeout)
    if check and out.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {out.stderr.decode()[:300]}")
    return out.stdout


def sh(cmd: str, **kw) -> str:
    return adb("shell", cmd, **kw).decode(errors="replace")


def screencap() -> bytes:
    return adb("exec-out", "screencap", "-p")


def dump_ui() -> str:
    return sh("uiautomator dump /sdcard/jevb_ui.xml >/dev/null 2>&1; cat /sdcard/jevb_ui.xml", check=False)


def tap(x: int, y: int) -> None:
    sh(f"input tap {x} {y}")


def key(code: str) -> None:
    sh(f"input keyevent {code}")


def open_url(url: str) -> None:
    sh(f"am start -n com.android.chrome/com.google.android.apps.chrome.IntentDispatcher -a android.intent.action.VIEW -d '{url}'")


def start(component: str) -> None:
    sh(f"am start -n {component}")


def foreground() -> str:
    out = sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -1", check=False)
    return out.strip()


def markers(png: bytes) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Find the canvas page's corner markers: magenta top-left, green bottom-right (device px)."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    px = img.load()
    tl = br = None
    for y in range(0, min(h, 400)):
        for x in range(0, min(w, 60)):
            r, g, b = px[x, y]
            if r > 240 and g < 15 and b > 240:
                tl = (x, y)
                break
        if tl:
            break
    for y in range(h - 1, max(h - 400, 0), -1):
        for x in range(w - 1, max(w - 60, 0), -1):
            r, g, b = px[x, y]
            if r < 15 and g > 240 and b < 15:
                br = (x + 1, y + 1)
                break
        if br:
            break
    if not tl or not br:
        return None
    return tl, br


def settle(seconds: float) -> None:
    time.sleep(seconds)
