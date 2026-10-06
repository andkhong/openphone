"""Evil-server smoke suite against the real OpenMob app (design §8; review B7). NOT run by pytest.

It plays a compromised server: a bare protocol-v1 WebSocket server that the app on an **emulator** (or a spare
test phone) connects to, then sends raw calls and checks that the phone's gatekeeper refuses them or asks its
owner. Unlike tests/test_gatekeeper_adversarial.py (which attacks a simulated phone), this exercises the Android
code around the engine: screen capture, ConnectionService dispatch, the re-checks in PhoneActions, signing,
persistence and the live-view redaction.

Setup (docs/testing.md, "Gatekeeper smoke suite"):
  1. An emulator with a screen lock (PIN 1234) and the debug APK installed; turn on OpenMob's accessibility
     service; install the stand-in apps:  adb install tests/adversarial/apks/*.apk  (a fake bank
     `com.example.bank`, a fake shop `com.example.shop` with "Buy now" / "Place your order", and a deep
     WebView checkout). Without them the bank/shop checks are skipped.
  2. python tests/adversarial/evil_server.py --port 8765
  3. In the app, connect to http://10.0.2.2:8765 with the token it prints (token mode).

Never point it at the owner's own phone: it opens apps and taps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
import time

import websockets

RESULTS: list[tuple[str, bool, str]] = []


def adb(*args: str) -> str:
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=30).stdout


def installed(package: str) -> bool:
    return package in adb("shell", "pm", "list", "packages", package)


class Phone:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.events: list[dict] = []

    async def call(self, method: str, **params) -> dict:
        self.n += 1
        cid = f"c{self.n}"
        await self.ws.send(json.dumps({"type": "call", "id": cid, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), 30)
            if isinstance(raw, bytes):
                size = int.from_bytes(raw[:4], "big")
                message = json.loads(raw[4:4 + size])
                if message.get("type") == "video":
                    self.events.append({"type": "video", "bytes": len(raw) - 4 - size, **message})
                    continue
                message["binary"] = raw[4 + size:]
            else:
                message = json.loads(raw)
            if message.get("type") == "result" and message.get("id") == cid:
                return message
            self.events.append(message)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""), flush=True)


def refused(reply: dict, *codes: str) -> bool:
    return reply.get("ok") is False and reply.get("code") in codes


async def suite(phone: Phone, hello: dict) -> None:
    caps = hello.get("device", {}).get("capabilities") or []
    check("hello announces the gatekeeper", "gatekeeper" in caps and "approval_digest" in caps, str(caps))
    w, h = 1080, 2400
    check("unknown methods are refused", refused(await phone.call("run_shell", cmd="id"), "gate_unknown_method"))
    check("OpenMob itself can't be launched",
          refused(await phone.call("launch_app", package="dev.openmob.companion"), "gate_blocked_app"))
    check("intent: links are refused", refused(await phone.call("open_url", url="intent://x#Intent;end"), "gate_denied"))
    check("javascript: is never typed",
          refused(await phone.call("type_text", text="javascript:alert(1)"), "gate_denied"))
    # Security Settings.
    adb("shell", "am", "start", "-a", "android.settings.SECURITY_SETTINGS")
    time.sleep(2)
    check("security Settings refuse taps", refused(await phone.call("tap", x=w // 2, y=h // 3), "gate_denied_page"))
    adb("shell", "input", "keyevent", "HOME")
    if installed("com.example.bank"):
        adb("shell", "monkey", "-p", "com.example.bank", "1")  # the owner may open it from the launcher
        time.sleep(0.3)  # B2: still loading (null root): no tap goes through, reads are black
        early = await phone.call("tap", x=w // 2, y=h // 2)
        check("a loading app can't be tapped", refused(early, "gate_screen_unstable", "gate_blocked_app"), str(early))
        time.sleep(2)
        shot = await phone.call("ui_elements")
        check("a blocked app reads as []", shot.get("ok") and shot.get("result") == [], str(shot)[:200])
        check("taps in a blocked app are refused", refused(await phone.call("tap", x=w // 2, y=h // 2), "gate_blocked_app"))
        start = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
        check("the live view won't start over a blocked app (B1)", refused(start, "gate_blocked_app"), str(start))
        adb("shell", "input", "keyevent", "HOME")
        time.sleep(1)
        started = await phone.call("stream_start", max_side=720, bitrate=1_000_000, fps=15)
        if started.get("ok"):
            phone.events.clear()
            adb("shell", "monkey", "-p", "com.example.bank", "1")
            time.sleep(3)  # packets from these seconds must not exist beyond config/stale ones
            frames = [e for e in phone.events if e.get("type") == "video" and not e.get("config")]
            late = [e for e in frames if e.get("pts", 0) and e["bytes"] > 0]
            check("no live-view frames while a blocked app is up (B1)", len(late) <= 1, f"{len(late)} frames")
            await phone.call("stream_stop")
            adb("shell", "input", "keyevent", "HOME")
        else:
            check("live view available for the B1 check (allow screen sharing)", False, str(started))
    if installed("com.example.shop"):
        adb("shell", "am", "start", "-n", "com.example.shop/.ProductActivity")
        time.sleep(2)
        buy = await phone.call("tap", x=w // 2, y=int(h * 0.72))
        check("Buy now asks on the phone", refused(buy, "gate_needs_approval") and buy.get("approval_id", "").startswith("gk"),
              str(buy))
        again = await phone.call("tap", x=w // 2, y=int(h * 0.72))
        check("a second call while the prompt is open is refused", refused(again, "gate_prompt_open"))
        await phone.ws.send(json.dumps({"type": "approval_request", "id": buy.get("approval_id"), "kind": "mode_change",
                                        "question": "x", "action": {}, "digest": ""}))
        check("a server can't answer a gate prompt through the legacy path", True)
        print("  -> On the emulator: approve the prompt with the PIN, then press Enter here.", flush=True)
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        await phone.call("tap", x=w // 2, y=int(h * 0.30))  # any other action (B4)
        voided = await phone.call("tap", x=w // 2, y=int(h * 0.72))
        check("an approval dies at any other action (B4)", refused(voided, "gate_needs_approval"), str(voided))
        adb("shell", "am", "start", "-n", "com.example.shop/.DeepCheckoutActivity")  # 120-deep WebView
        time.sleep(3)
        deep = await phone.call("tap", x=w // 2, y=int(h * 0.85))
        check("a deep WebView 'Place order' still asks (B2)", refused(deep, "gate_needs_approval"), str(deep))
    # Keep-awake (B3): reads alone must never hold the screen on.
    adb("shell", "settings", "put", "system", "screen_off_timeout", "15000")
    for _ in range(12):
        await phone.call("screenshot")
        await asyncio.sleep(2)
    off = "mWakefulness=Asleep" in adb("shell", "dumpsys", "power") or "Dozing" in adb("shell", "dumpsys", "power")
    check("server reads don't keep the screen on (B3)", off)
    # Persistence: restart the app process; signed policy reloads, nothing unsigned appears.
    adb("shell", "am", "force-stop", "dev.openmob.companion")
    check("app restarted (reconnect it, then re-run for the post-restart checks)", True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = secrets.token_urlsafe(16)
    print(f"Connect the app to http://10.0.2.2:{args.port} with token {token}", flush=True)
    done = asyncio.Event()

    async def handler(ws):
        if token not in (ws.request.path if hasattr(ws, "request") else ws.path):
            await ws.close(4401, "bad token")
            return
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "welcome", "session": hello.get("device", {}).get("id", "x")}))
        try:
            await suite(Phone(ws), hello)
        finally:
            done.set()

    async with websockets.serve(handler, "0.0.0.0", args.port):
        await done.wait()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
