"""ADB-backed driver for an Android phone.

Coordinates passed to the action methods (tap, swipe, ...) are device pixels in
the current orientation. Screenshots are downscaled before they go to the model;
use Screenshot.to_device() to map model coordinates back.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from PIL import Image

from .base import CachedScreenshots, DeviceError

KEYCODES = {
    "HOME": 3,
    "BACK": 4,
    "VOLUME_UP": 24,
    "VOLUME_DOWN": 25,
    "POWER": 26,
    "TAB": 61,
    "ENTER": 66,
    "DELETE": 67,
    "MENU": 82,
    "SEARCH": 84,
    "ESCAPE": 111,
    "APP_SWITCH": 187,
    "WAKEUP": 224,
}

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
FOREGROUND_POLL_SECONDS = 0.15
FOCUS_RE = re.compile(r"\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/")


class AdbError(DeviceError):
    pass


@dataclass
class Screenshot:
    jpeg: bytes
    width: int  # size of the image sent to the model
    height: int
    device_width: int  # size of the real screen
    device_height: int

    def to_device(self, x: float, y: float) -> tuple[int, int]:
        dx = round(x * self.device_width / self.width)
        dy = round(y * self.device_height / self.height)
        return (
            min(max(dx, 0), self.device_width - 1),
            min(max(dy, 0), self.device_height - 1),
        )

    def to_image(self, x: float, y: float) -> tuple[int, int]:
        return (
            round(x * self.width / self.device_width),
            round(y * self.height / self.device_height),
        )


def downscale(shot: Screenshot, max_side: int, quality: int = 80) -> Screenshot:
    """The same screen with its long side at most `max_side` px. The device size stays, so
    to_device and to_image map the smaller image's coordinates onto the same screen."""
    if max_side <= 0 or max(shot.width, shot.height) <= max_side:
        return shot
    ratio = max_side / max(shot.width, shot.height)
    size = (max(round(shot.width * ratio), 1), max(round(shot.height * ratio), 1))
    img = Image.open(io.BytesIO(shot.jpeg))
    img.draft("RGB", size)  # JPEG: decode at a reduced scale when it can (much faster)
    img = img.convert("RGB").resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return Screenshot(buf.getvalue(), img.width, img.height, shot.device_width, shot.device_height)


@dataclass
class UIElement:
    index: int
    text: str
    desc: str
    resource_id: str
    cls: str
    clickable: bool
    checked: bool | None
    bounds: tuple[int, int, int, int]
    # Optional, from apps that report them (older apps and adb leave the defaults):
    editable: bool = False  # a text field
    password: bool = False  # a password field (never typed into)
    covered: bool = False  # its center is under a higher window (a dialog, the keyboard), so a tap would miss

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def label(self) -> str:
        return self.text or self.desc or self.resource_id or self.cls


def parse_ui_xml(xml_text: str, limit: int = 150) -> list[UIElement]:
    """Turn a uiautomator dump into the elements worth showing the model."""
    start = xml_text.find("<")
    if start < 0:
        return []
    try:
        root = ET.fromstring(xml_text[start:])
    except ET.ParseError:
        return []

    elements: list[UIElement] = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        clickable = any(node.get(a) == "true" for a in ("clickable", "long-clickable", "checkable"))
        if not (text or desc or clickable):
            continue
        match = BOUNDS_RE.fullmatch(node.get("bounds", ""))
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        elements.append(
            UIElement(
                index=len(elements),
                text=text,
                desc=desc,
                resource_id=(node.get("resource-id") or "").rsplit("/", 1)[-1],
                cls=(node.get("class") or "").rsplit(".", 1)[-1],
                clickable=clickable,
                checked=(node.get("checked") == "true") if node.get("checkable") == "true" else None,
                bounds=(x1, y1, x2, y2),
                editable="EditText" in (node.get("class") or ""),
                password=node.get("password") == "true",
            )
        )
        if len(elements) >= limit:
            break
    return elements


def parse_app_label(mini_apk: bytes) -> str | None:
    from pyaxmlparser import APK

    # pyaxmlparser prints and logs noise for many real-world APKs.
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            label = APK(mini_apk, raw=True).application
    except Exception:
        return None
    finally:
        logging.disable(logging.NOTSET)
    # Labels stored only in language-split APKs come back as unresolved "@7F..." ids.
    return label.strip() if label and not label.startswith("@") else None


def _label_job(job: tuple[PhoneDriver, str | None]) -> str | None:
    driver, apk_path = job
    if not apk_path:
        return None
    try:
        return parse_app_label(driver._label_files(apk_path))
    except AdbError:
        return None


def format_elements(elements: list[UIElement], shot: Screenshot) -> str:
    """One line per element, with centers in screenshot (model) coordinates."""
    lines = []
    for el in elements:
        x, y = shot.to_image(*el.center)
        parts = [f"[{el.index}] {el.cls}"]
        if el.text:
            parts.append(repr(el.text[:80]))
        if el.desc and el.desc != el.text:
            parts.append(f"desc={el.desc[:80]!r}")
        if el.resource_id:
            parts.append(f"id={el.resource_id}")
        if el.checked is not None:
            parts.append("checked" if el.checked else "unchecked")
        if not el.clickable:
            parts.append("(not clickable)")
        if el.covered:
            parts.append("(covered by another window)")
        parts.append(f"@ ({x},{y})")
        lines.append(" ".join(parts))
    return "\n".join(lines)


class PhoneDriver(CachedScreenshots):
    def __init__(
        self,
        serial: str | None = None,
        adb_path: str = "adb",
        max_side: int = 1280,
        jpeg_quality: int = 80,
    ):
        self.serial = serial
        self.adb = shutil.which(adb_path) or adb_path
        self.max_side = max_side
        self.jpeg_quality = jpeg_quality
        self._init_screenshot_cache()

    def __getstate__(self):  # picklable for the app-label worker processes
        state = self.__dict__.copy()
        state["_shot_lock"] = None
        state["_last_shot"] = None
        state["_last_preview"] = None
        return state

    # --- low level -------------------------------------------------------

    def _exec(self, args: list[str], timeout: float = 30, device: bool = True) -> bytes:
        cmd = [self.adb]
        if device and self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise AdbError("adb not found. Install it with: brew install android-platform-tools") from None
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb timed out after {timeout}s: {' '.join(args)}") from None
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            raise AdbError(message or f"adb exited with {proc.returncode}: {' '.join(args)}")
        return proc.stdout

    def shell(self, command: str, timeout: float = 30) -> str:
        return self._exec(["shell", command], timeout).decode("utf-8", "replace")

    def devices(self) -> list[tuple[str, str]]:
        out = self._exec(["devices"], device=False).decode()
        rows = [line.split("\t") for line in out.splitlines()[1:] if "\t" in line]
        return [(serial, state) for serial, state in rows]

    # --- observation -----------------------------------------------------

    def _capture(self, max_side: int | None = None, quality: int | None = None) -> Screenshot:
        raw = self._exec(["exec-out", "screencap", "-p"], timeout=30)
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            raise AdbError(f"Could not decode screenshot ({len(raw)} bytes): {exc}") from None
        dw, dh = img.size
        ratio = min(1.0, (max_side or self.max_side) / max(dw, dh))
        if ratio < 1.0:
            img = img.resize((round(dw * ratio), round(dh * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality or self.jpeg_quality)
        return Screenshot(buf.getvalue(), img.width, img.height, dw, dh)

    def capture_preview(self, max_side: int, quality: int) -> Screenshot:
        return self._capture(min(max_side, self.max_side), quality)

    def ui_elements(self, timeout: float = 10) -> list[UIElement]:
        """Accessibility tree of the current screen. Empty for most games."""
        try:
            out = self.shell(
                "uiautomator dump /sdcard/openmob_ui.xml >/dev/null 2>&1 && cat /sdcard/openmob_ui.xml",
                timeout=timeout,
            )
        except AdbError:
            return []
        return parse_ui_xml(out)

    def wait_foreground(self, package: str, timeout: float, cancel=None) -> bool:
        """After a launch: poll the focused window (`dumpsys window`, ~0.1 s a call) until `package` is
        in front. True once it is; False at the timeout. adb has no "screen stopped changing" signal,
        so the agent still waits a short settle after this."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return False
            if self.current_app() == package:
                return True
            time.sleep(FOREGROUND_POLL_SECONDS)
        return False

    def current_app(self) -> str | None:
        try:
            out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        except AdbError:
            return None
        match = FOCUS_RE.search(out)
        return match.group(1) if match else None

    def wifi_ip(self) -> str | None:
        out = self.shell("ip -f inet addr show wlan0")
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else None

    def enable_tcpip(self, port: int = 5555) -> None:
        """Make adbd also listen on Wi-Fi. Lasts until the phone reboots."""
        self._exec(["tcpip", str(port)])

    def connect(self, address: str) -> str:
        out = self._exec(["connect", address], device=False, timeout=15).decode().strip()
        if "connected to" not in out:
            raise AdbError(out or f"Could not connect to {address}")
        return out

    def stay_awake(self, on: bool = True) -> None:
        self.shell(f"svc power stayon {'true' if on else 'false'}")

    def screen_size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        sizes = re.findall(r"(\d+)x(\d+)", out)
        if not sizes:
            raise AdbError(f"Unexpected `wm size` output: {out!r}")
        w, h = sizes[-1]  # "Override size" comes last when present
        return int(w), int(h)

    # --- apps ------------------------------------------------------------

    def launchable_packages(self) -> list[str]:
        try:
            out = self.shell(
                "cmd package query-activities --brief -a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER"
            )
            pkgs = sorted({line.split("/")[0].strip() for line in out.splitlines() if "/" in line})
            if pkgs:
                return pkgs
        except AdbError:
            pass
        out = self.shell("pm list packages")
        return sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))

    def launch_app(self, package: str) -> None:
        out = self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1")
        if "No activities found" in out or "monkey aborted" in out:
            raise AdbError(f"{package} has no launchable activity")

    def apk_paths(self) -> dict[str, str]:
        """Package -> path of its base APK, for every installed package (one adb call)."""
        out = self.shell("pm list packages -f")
        paths = {}
        for line in out.splitlines():
            # package:/data/app/~~x==/com.foo-y==/base.apk=com.foo
            path, sep, package = line.removeprefix("package:").strip().rpartition("=")
            if sep and path.endswith(".apk"):
                paths[package] = path
        return paths

    def read_zip_entry(self, apk_path: str, entry: str) -> bytes:
        """Read one file out of an APK on the phone without copying the whole APK."""
        return self._exec(["exec-out", f"unzip -p {shlex.quote(apk_path)} {shlex.quote(entry)}"], timeout=60)

    def _label_files(self, apk_path: str) -> bytes:
        """A tiny zip holding just the manifest and resource table of an APK on the phone."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as mini_apk:
            for entry in ("AndroidManifest.xml", "resources.arsc"):
                mini_apk.writestr(entry, self.read_zip_entry(apk_path, entry))
        return buf.getvalue()

    def app_labels(self, packages: list[str], workers: int = 8) -> dict[str, str | None]:
        """Display names for many apps. None where the name can't be read.

        Parsing resource tables is CPU-heavy pure Python, so each worker process both
        fetches and parses its own apps.
        """
        paths = self.apk_paths()
        jobs = [(self, paths.get(package)) for package in packages]
        with ProcessPoolExecutor(workers) as pool:
            return dict(zip(packages, pool.map(_label_job, jobs)))

    def open_url(self, url: str) -> None:
        out = self.shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")
        if "Error" in out:
            raise AdbError(out.strip())

    # --- actions ---------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.swipe(x, y, x, y, duration_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def type_text(self, text: str) -> None:
        if not text.isascii():
            raise AdbError(
                "adb `input text` only supports ASCII. Install ADBKeyboard on the phone for other characters."
            )
        # `input text` uses %s as its escape for a space.
        self.shell(f"input text {shlex.quote(text.replace(' ', '%s'))}")

    def key(self, name: str) -> None:
        code = KEYCODES.get(name.upper())
        if code is None:
            raise AdbError(f"Unknown key {name!r}. Known keys: {', '.join(KEYCODES)}")
        self.shell(f"input keyevent {code}")

    def wake_unlock(self) -> None:
        """Turn the screen on and dismiss a non-secure lock screen."""
        self.key("WAKEUP")
        try:
            self.shell("wm dismiss-keyguard")
        except AdbError:
            pass


def _cli(argv: list[str]) -> None:
    """Manual driver checks: python -m openmob.phone.adb <command> [args]."""
    usage = "usage: python -m openmob.phone.adb {screenshot OUT.jpg | tap X Y | launch PKG | tree | current | apps}"
    if not argv:
        sys.exit(usage)
    driver = PhoneDriver()
    cmd, args = argv[0], argv[1:]
    if cmd == "screenshot":
        shot = driver.screenshot()
        out = args[0] if args else "screenshot.jpg"
        with open(out, "wb") as f:
            f.write(shot.jpeg)
        print(f"saved {out}: {shot.width}x{shot.height} (device {shot.device_width}x{shot.device_height})")
    elif cmd == "tap":
        driver.tap(int(args[0]), int(args[1]))
    elif cmd == "launch":
        driver.launch_app(args[0])
    elif cmd == "tree":
        start = time.time()
        elements = driver.ui_elements()
        shot = driver.screenshot()
        print(format_elements(elements, shot) or "(no elements)")
        print(f"-- {len(elements)} elements in {time.time() - start:.1f}s")
    elif cmd == "current":
        print(driver.current_app())
    elif cmd == "apps":
        print("\n".join(driver.launchable_packages()))
    else:
        sys.exit(usage)


if __name__ == "__main__":
    _cli(sys.argv[1:])
