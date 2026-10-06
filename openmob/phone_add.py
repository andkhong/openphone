"""`openmob phone add`: set up and pair an Android phone over USB in one command (for developers).

Every step checks first and only changes what's missing, so running it again fixes whatever is
left (and does nothing on a phone that's already set up):

1. find the phone (clear errors for no phone, "Allow USB debugging?", Samsung Auto Blocker, old Android);
2. install or update the OpenMob app (adb installs aren't subject to Android's "restricted settings");
3. allow notifications (`pm grant`) and running in the background (the battery exemption);
4. turn on the accessibility service, keeping every other enabled service;
5. try to pre-approve screen capture (Android 14+ may still ask once per connection);
6. pair: the server mints a single-use, 2-minute enrollment code (API key with the `devices:add`
   scope) and the app opens on it. The phone still shows its own confirmation naming the server and
   the account, passed with the screen lock: the server doesn't pair the phone until then. USB
   access plus that one touch is the trust anchor;
7. wait until the phone is online, and print a summary.

adb runs through an injectable `run` (subprocess.run's signature), so tests drive a fake phone.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

PACKAGE = "dev.openmob.companion"
A11Y_SERVICE = f"{PACKAGE}/.OpenMobAccessibilityService"
A11Y_SERVICE_LONG = f"{PACKAGE}/{PACKAGE}.OpenMobAccessibilityService"
MIN_SDK = 30  # Android 11
POLL = 1.5  # seconds between status checks (well inside the API's per-key rate limit)
# USB vendor ids of common Android phone makers, to tell "nothing plugged in" from "adb can't see it".
USB_VENDORS = {0x04E8: "Samsung", 0x18D1: "Google", 0x22B8: "Motorola", 0x2A70: "OnePlus", 0x2717: "Xiaomi",
               0x12D1: "Huawei", 0x0FCE: "Sony", 0x1004: "LG", 0x05C6: "Qualcomm", 0x0BB4: "HTC"}


class PhoneAddError(RuntimeError):
    """A problem the developer has to fix; the message says how."""


@dataclass
class Report:
    started: float = field(default_factory=time.monotonic)
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (step, outcome, detail)

    def add(self, step: str, outcome: str, detail: str = "", out: Callable[[str], None] = print) -> None:
        self.rows.append((step, outcome, detail))
        mark = {"done": "✓", "already": "✓", "warn": "!", "skipped": "·"}.get(outcome, "·")
        out(f"  {mark} {step}: {detail or outcome}")


class Adb:
    """`adb [-s SERIAL] ...`, through `run` (subprocess.run or a test double)."""

    def __init__(self, adb: str = "adb", serial: str | None = None, run: Callable = subprocess.run):
        self.adb, self.serial, self.run = adb, serial, run

    def __call__(self, *args: str, timeout: float = 60, check: bool = True) -> str:
        cmd = [self.adb, *(["-s", self.serial] if self.serial else []), *args]
        try:
            res = self.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise PhoneAddError("adb isn't installed or isn't on your PATH. Install it "
                                "(macOS: brew install android-platform-tools) or pass --adb PATH.") from None
        except subprocess.TimeoutExpired:
            raise PhoneAddError(f"adb {' '.join(args[:2])} timed out. Unplug the phone, plug it back in, and retry.") from None
        out = (res.stdout or "") + (res.stderr or "")
        if check and res.returncode != 0:
            raise PhoneAddError(f"adb {' '.join(args[:3])} failed: {out.strip()[:300]}")
        return out

    def shell(self, *words: str, timeout: float = 60, check: bool = True) -> str:
        """One device-shell command; every word is quoted for the phone's shell."""
        return self(*["shell", " ".join(shlex.quote(w) for w in words)], timeout=timeout, check=check).strip()


# --- finding the phone ------------------------------------------------------------------------


def usb_phone_present(run: Callable = subprocess.run) -> str | None:
    """macOS only, cheap: the maker of an Android phone on USB that adb can't see, else None."""
    if sys.platform != "darwin":
        return None
    try:
        out = run(["ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True, timeout=5).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for match in re.finditer(r'"idVendor"\s*=\s*(\d+)', out):
        maker = USB_VENDORS.get(int(match.group(1)))
        if maker:
            return maker
    return None


def pick_device(adb: Adb, serial: str | None) -> str:
    out = adb("devices", timeout=15)
    rows = [line.split()[:2] for line in out.splitlines()[1:] if line.strip() and not line.startswith("*")]
    rows = [(r[0], r[1]) for r in rows if len(r) == 2]
    if serial:
        rows = [r for r in rows if r[0] == serial]
    ready = [s for s, state in rows if state == "device"]
    if serial and not rows:
        raise PhoneAddError(f"No phone with serial {serial}. `adb devices` lists what's connected.")
    if not rows:
        maker = usb_phone_present(adb.run)
        if maker == "Samsung":
            raise PhoneAddError(
                "A Samsung phone is plugged in, but adb can't see it. Samsung's Auto Blocker blocks USB "
                "debugging: on the phone open Settings › Security and privacy › Auto Blocker and turn it off "
                "(you can turn it back on afterwards), then make sure Developer options › USB debugging is on.")
        if maker:
            raise PhoneAddError(
                f"A {maker} phone is plugged in, but adb can't see it. Turn on Developer options › USB debugging "
                "on the phone, and use a data cable (some cables only charge).")
        raise PhoneAddError("No phone found. Plug it in with a USB data cable and turn on USB debugging "
                            "(Settings › About phone › tap Build number 7 times, then Developer options › USB debugging).")
    if not ready:
        state = rows[0][1]
        if state == "unauthorized":
            raise PhoneAddError("The phone hasn't allowed this computer yet. Unlock it and tap Allow on "
                                "\"Allow USB debugging?\" (tick \"Always allow from this computer\"), then run this again.")
        if state == "offline":
            raise PhoneAddError("The phone shows as offline to adb. Unplug it, plug it back in, and run this again.")
        raise PhoneAddError(f"adb can't use the phone ({state}). Check the cable and USB debugging, then retry.")
    if len(ready) > 1:
        raise PhoneAddError(f"Several phones are connected ({', '.join(ready)}). Pick one with --serial.")
    return ready[0]


def check_android(adb: Adb) -> tuple[int, str, str]:
    """(SDK level, maker, model); refuses Android older than 11."""
    sdk = int(re.sub(r"\D", "", adb.shell("getprop", "ro.build.version.sdk")) or 0)
    release = adb.shell("getprop", "ro.build.version.release")
    if sdk < MIN_SDK:
        raise PhoneAddError(f"This phone runs Android {release or sdk}. OpenMob needs Android 11 or newer.")
    return sdk, adb.shell("getprop", "ro.product.manufacturer"), adb.shell("getprop", "ro.product.model")


# --- the app ---------------------------------------------------------------------------------


def installed_apk_hash(adb: Adb) -> str | None:
    out = adb.shell("pm", "path", PACKAGE, check=False)
    paths = [line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:")]
    base = next((p for p in paths if p.endswith("base.apk")), paths[0] if paths else None)
    if not base:
        return None
    digest = adb.shell("sha256sum", base, check=False).split()
    return digest[0] if digest and re.fullmatch(r"[0-9a-f]{64}", digest[0]) else ""


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_apk(server: str, http) -> Path:
    r = http.get(f"{server}/download/openmob.apk")
    if r.status_code == 404:
        raise PhoneAddError("The server has no app to download yet. Build it (cd android && ./gradlew assembleDebug) "
                            "or pass --apk PATH.")
    r.raise_for_status()
    fd, name = tempfile.mkstemp(prefix="openmob-", suffix=".apk")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return Path(name)


def install_app(adb: Adb, apk: Path, report: Report) -> None:
    current = installed_apk_hash(adb)
    if current and current == file_sha256(apk):
        report.add("App", "already", "OpenMob is installed and up to date")
        return
    out = adb("install", "-r", str(apk), timeout=180, check=False)
    if "Success" not in out:
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out or "signatures do not match" in out:
            raise PhoneAddError("An OpenMob app signed with a different key is installed. Remove it first "
                                f"(adb uninstall {PACKAGE}; this unpairs the phone), then run this again.")
        raise PhoneAddError(f"Installing the app failed: {out.strip()[:300]}")
    report.add("App", "done", "updated" if current is not None else "installed")


def grant_notifications(adb: Adb, sdk: int, report: Report) -> None:
    if sdk < 33:
        report.add("Notifications", "already", "allowed (no permission needed before Android 13)")
        return
    perm = "android.permission.POST_NOTIFICATIONS"
    if f"{perm}: granted=true" in adb.shell("dumpsys", "package", PACKAGE, check=False):
        report.add("Notifications", "already", "allowed")
        return
    adb.shell("pm", "grant", PACKAGE, perm)
    report.add("Notifications", "done", "allowed")


def exempt_battery(adb: Adb, report: Report) -> None:
    listed = adb.shell("dumpsys", "deviceidle", "whitelist", check=False)
    if any(line.split(",")[1:2] == [PACKAGE] for line in listed.splitlines()):
        report.add("Background", "already", "exempt from battery optimization")
        return
    adb.shell("dumpsys", "deviceidle", "whitelist", f"+{PACKAGE}")
    report.add("Background", "done", "exempt from battery optimization")


def enable_accessibility(adb: Adb, report: Report) -> None:
    raw = adb.shell("settings", "get", "secure", "enabled_accessibility_services")
    services = [s for s in raw.split(":") if s and s != "null"]
    present = any(s in (A11Y_SERVICE, A11Y_SERVICE_LONG) for s in services)
    enabled = adb.shell("settings", "get", "secure", "accessibility_enabled").strip() == "1"
    if present and enabled:
        report.add("Accessibility", "already", "OpenMob is on")
        return
    if not present:  # keep every other service the owner has on (screen readers, password managers…)
        adb.shell("settings", "put", "secure", "enabled_accessibility_services", ":".join(services + [A11Y_SERVICE]))
    if not enabled:
        adb.shell("settings", "put", "secure", "accessibility_enabled", "1")
    kept = f" (kept {len(services)} other service{'s' if len(services) != 1 else ''})" if services else ""
    report.add("Accessibility", "done", f"OpenMob turned on{kept}")


def allow_screen_capture(adb: Adb, sdk: int, report: Report) -> None:
    """Best effort: Android may ignore the app op and still ask each time sharing starts."""
    adb.shell("appops", "set", PACKAGE, "PROJECT_MEDIA", "allow", check=False)
    state = adb.shell("appops", "get", PACKAGE, "PROJECT_MEDIA", check=False)
    honored = "allow" in state.lower()
    if honored and sdk < 34:
        report.add("Screen sharing", "done", "pre-approved")
    elif honored:
        report.add("Screen sharing", "warn", "pre-approved, but Android 14+ still asks once per connection, "
                                             "the first time you open the live view")
    else:
        report.add("Screen sharing", "warn", "Android didn't accept a pre-approval; the phone asks the first "
                                             "time you open the live view")


def app_identity(adb: Adb) -> dict:
    """The app's device id and server, readable on debug builds (run-as); {} otherwise."""
    out = adb.shell("run-as", PACKAGE, "cat", "shared_prefs/openmob.xml", check=False)
    found = dict(re.findall(r'<string name="(device_id|server)">([^<]*)</string>', out))
    return found


# --- pairing ---------------------------------------------------------------------------------


class Busy(Exception):
    """The server's rate limit: wait and ask again."""


def api_call(http, method: str, path: str, **kw) -> dict:
    r = http.request(method, path, **kw)
    if r.status_code == 429:
        raise Busy()
    if r.status_code in (401, 403):
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = ""
        raise PhoneAddError(f"The server refused the API key ({detail or r.status_code}). It needs the devices:add "
                            "scope: create one on /developer or with `openmob key create EMAIL --scopes devices:add`.")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise PhoneAddError(f"{method} {path}: {detail or r.status_code}")
    return r.json()


def pair_phone(adb: Adb, http, server: str, report: Report, wait_paired: float = 130, wait_online: float = 45,
               sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print) -> dict:
    """Mint an enrollment, open the app on it, and wait for the owner to confirm on the phone."""
    # The store is per server, so if it has this app's id paired to this account, it's paired here
    # (under whichever address of this server the phone uses).
    device_id = app_identity(adb).get("device_id")
    enrollment = api_call(http, "POST", "/v1/enrollments", json={"device_id": device_id})
    if enrollment.get("already_paired"):
        report.add("Pairing", "already", f"paired as {enrollment.get('email')}")
        if not enrollment.get("online"):
            adb.shell("am", "start", "-n", f"{PACKAGE}/.MainActivity", "-a", "dev.openmob.action.CONNECT", check=False)
        return wait_for_online(http, enrollment["device_id"], None, wait_online, sleep, report)

    adb.shell("input", "keyevent", "KEYCODE_WAKEUP", check=False)
    adb.shell("am", "start", "-n", f"{PACKAGE}/.MainActivity", "-a", "android.intent.action.VIEW",
              "-d", enrollment["app_link"])
    host = urlsplit(enrollment.get("server") or server).netloc
    out(f"  → On the phone: unlock it and confirm \"Connect this phone to {host} as {enrollment.get('email')}?\" "
        "with your fingerprint, face or PIN.")
    deadline = time.monotonic() + min(wait_paired, enrollment.get("ttl", wait_paired) + 10)
    while True:
        try:
            status = api_call(http, "GET", f"/v1/enrollments/{enrollment['enrollment_id']}")
        except Busy:
            status = {"status": "pending"}
        if status["status"] == "paired":
            report.add("Pairing", "done", f"confirmed on the phone, paired as {enrollment.get('email')}")
            return wait_for_online(http, status["device_id"], enrollment["enrollment_id"], wait_online, sleep, report)
        if status["status"] == "expired" or time.monotonic() > deadline:
            raise PhoneAddError("The phone wasn't confirmed within 2 minutes, so it isn't paired. Run this again "
                                "and confirm on the phone when it asks.")
        sleep(POLL)


def wait_for_online(http, device_id: str, enrollment_id: str | None, wait: float, sleep, report: Report) -> dict:
    deadline = time.monotonic() + wait
    while True:
        try:
            if enrollment_id:
                online = api_call(http, "GET", f"/v1/enrollments/{enrollment_id}")["online"]
            else:
                online = api_call(http, "POST", "/v1/enrollments",
                                  json={"device_id": device_id.removeprefix("app-")}).get("online")
        except Busy:
            online = False
        if online:
            report.add("Online", "done", f"{device_id} is connected")
            return {"device_id": device_id, "online": True}
        if time.monotonic() > deadline:
            report.add("Online", "warn", "paired, but not connected yet: open OpenMob on the phone and tap Connect")
            return {"device_id": device_id, "online": False}
        sleep(POLL)


def phone_add(server: str, key: str, serial: str | None = None, apk: str | None = None, adb_path: str = "adb",
              run: Callable = subprocess.run, http=None, sleep: Callable[[float], None] = time.sleep,
              out: Callable[[str], None] = print, wait_paired: float = 130, wait_online: float = 45) -> Report:
    """The whole flow; raises PhoneAddError with a fix-it message."""
    import httpx

    server = server.rstrip("/")
    if not key:
        raise PhoneAddError("An API key with the devices:add scope is needed: pass --key or set OPENMOB_API_KEY "
                            "(create one on /developer, or `openmob key create EMAIL --scopes devices:add`).")
    own_http = http is None
    http = http or httpx.Client(base_url=server, timeout=30, headers={"Authorization": f"Bearer {key}"})
    report = Report()
    try:
        serial = pick_device(Adb(adb_path, serial, run), serial)
        adb = Adb(adb_path, serial, run)
        sdk, maker, model = check_android(adb)
        out(f"📱 {maker.title()} {model} (Android SDK {sdk}, {serial}) → {server}")
        apk_path = Path(apk).expanduser() if apk else fetch_apk(server, http)
        if not apk_path.is_file():
            raise PhoneAddError(f"No APK at {apk_path}.")
        try:
            install_app(adb, apk_path, report)
        finally:
            if not apk:
                apk_path.unlink(missing_ok=True)
        grant_notifications(adb, sdk, report)
        exempt_battery(adb, report)
        enable_accessibility(adb, report)
        allow_screen_capture(adb, sdk, report)
        result = pair_phone(adb, http, server, report, wait_paired, wait_online, sleep=sleep, out=out)
    finally:
        if own_http:
            http.close()
    took = time.monotonic() - report.started
    if result["online"]:
        out(f"✓ {maker.title()} {model} is ready ({result['device_id']}), in {took:.0f} s.")
    else:
        out(f"! {maker.title()} {model} is paired ({result['device_id']}) but not online yet, after {took:.0f} s.")
    return report
