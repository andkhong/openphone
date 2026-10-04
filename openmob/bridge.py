"""`openmob bridge`: the reference phone-side client of the phone protocol.

Connects OUT to a hosted OpenMob server over a WebSocket and serves its calls with any
`Device` (a local adb PhoneDriver in production). The OpenMob Android app mirrors this.
See openmob/phone/protocol.py for the message format.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import sys
import threading
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import websockets

from .phone.protocol import METHODS, PROTOCOL_VERSION, encode_element, encode_screenshot

if TYPE_CHECKING:
    from .phone.adb import PhoneDriver
    from .phone.base import Device

log = logging.getLogger("openmob.bridge")

AUTH_CLOSE_CODE = 4401
DEFAULTS = {"long_press": {"duration_ms": 800}, "swipe": {"duration_ms": 300}}
MAX_MESSAGE = 32 * 1024 * 1024


class BridgeAuthError(RuntimeError):
    """The server rejected our token; retrying would not help."""


def ws_url(server_url: str, token: str) -> str:
    """http(s)://host[:port][/prefix] or ws(s)://... -> ws(s)://host[:port][/prefix]/phone/ws?t=<token>."""
    url = server_url.strip()
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme.lower())
    if scheme is None:
        raise ValueError(f"unsupported server URL scheme: {server_url!r}")
    path = parts.path.rstrip("/") + "/phone/ws"
    return urlunsplit((scheme, parts.netloc, path, f"t={quote(token, safe='')}", ""))


def print_event(kind: str, message: str) -> None:
    from .runner import format_event

    print(format_event(kind, message), flush=True)


def _close_code(exc: BaseException) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return rcvd.code if rcvd is not None else None


def _rejected_status(exc: BaseException) -> int | None:
    """HTTP status of a refused handshake (InvalidStatus in websockets>=14, InvalidStatusCode before)."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "status_code", None)



def _tls_context(url: str) -> ssl.SSLContext | None:
    """Verify wss:// with certifi's CA bundle; python.org builds on macOS ship without system CAs."""
    if not url.startswith("wss://"):
        return None
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())

class PhoneBridge:
    """Serves protocol calls from a server by dispatching them to `driver`."""

    def __init__(
        self,
        driver: Device,
        server_url: str,
        token: str,
        device_info: dict,
        on_event: Callable[[str, str], None] = print_event,
        key: str | None = None,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        self.driver = driver
        self.url = ws_url(server_url, token)
        self.device_info = device_info
        self.key = key  # this phone's own key in account mode (see pair_with_account)
        self.on_event = on_event
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self._stopped = asyncio.Event()
        self.connected = asyncio.Event()

    # --- public ----------------------------------------------------------

    async def run(self) -> None:
        """Connect and serve forever, reconnecting with backoff. Raises BridgeAuthError on a bad token."""
        backoff = self.initial_backoff
        while not self._stopping:
            try:
                if await self.run_once():
                    backoff = self.initial_backoff  # we got in; start over after this drop
            except BridgeAuthError:
                raise
            except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as exc:
                log.warning("Connection failed: %s", exc or type(exc).__name__)
            if self._stopping:
                break
            log.info("Reconnecting in %.0fs…", backoff)
            await self._wait(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        log.info("Bridge stopped.")

    async def run_once(self) -> bool:
        """One connection, served until it closes. Returns True if the server welcomed us."""
        log.info("Connecting to %s", self.url.split("?", 1)[0])
        welcomed = False
        try:
            async with websockets.connect(
                self.url, max_size=MAX_MESSAGE, open_timeout=15, ssl=_tls_context(self.url)
            ) as ws:
                self._ws = ws
                hello = {"type": "hello", "protocol": PROTOCOL_VERSION, "device": self.device_info}
                if self.key:
                    hello["key"] = self.key
                await self._send(hello)
                welcomed = await self._serve(ws)
            if not self._stopping:
                log.warning("Server closed the connection")
        except websockets.exceptions.ConnectionClosed as exc:
            if _close_code(exc) == AUTH_CLOSE_CODE:
                what = "this phone's key (it may have been revoked)" if self.key else "the token"
                raise BridgeAuthError(f"server rejected {what} (close code 4401)") from exc
            log.warning("Disconnected: %s", exc)
        except websockets.exceptions.InvalidHandshake as exc:
            if _rejected_status(exc) in (401, 403):
                raise BridgeAuthError(f"server rejected the token (HTTP {_rejected_status(exc)})") from exc
            raise
        finally:
            self._ws = None
            self.connected.clear()
        return welcomed

    async def send_command(self, text: str) -> bool:
        """Forward a user request to the server. Returns False if not connected."""
        text = text.strip()
        if not text:
            return False
        if self._ws is None:
            log.warning("Not connected; command dropped: %s", text)
            return False
        try:
            await self._send({"type": "command", "text": text})
        except websockets.exceptions.ConnectionClosed:
            log.warning("Connection lost; command dropped: %s", text)
            return False
        return True

    async def stop(self) -> None:
        self._stopping = True
        self._stopped.set()
        if self._ws is not None:
            await self._ws.close()

    # --- internals -------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        """Sleep between reconnects, waking early on stop()."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), seconds)

    async def _serve(self, ws) -> bool:
        welcomed = False
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    log.warning("Ignoring non-JSON message")
                    continue
                kind = msg.get("type")
                if kind == "welcome":
                    welcomed = True
                    self.connected.set()
                    log.info("Connected (session %s)", msg.get("session"))
                elif kind == "call":
                    task = asyncio.create_task(self._handle_call(msg))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif kind == "event":
                    try:
                        self.on_event(str(msg.get("kind", "")), str(msg.get("message", "")))
                    except Exception:
                        log.exception("on_event hook failed")
                else:
                    log.debug("Ignoring message type %r", kind)
        finally:
            for task in tasks:
                task.cancel()
        return welcomed

    async def _handle_call(self, msg: dict) -> None:
        call_id = msg.get("id")
        method = msg.get("method")
        try:
            result = await asyncio.to_thread(self._dispatch, method, msg.get("params") or {})
            reply = {"type": "result", "id": call_id, "ok": True, "result": result}
        except Exception as exc:
            reply = {"type": "result", "id": call_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log.warning("%s failed: %s", method, reply["error"])
        try:
            await self._send(reply)
        except websockets.exceptions.ConnectionClosed:
            log.warning("Connection lost before the %s result was sent", method)

    def _dispatch(self, method: Any, params: dict) -> Any:
        """Run one call on the driver (in a worker thread) and encode its result."""
        if method not in METHODS:
            raise ValueError(f"unknown method {method!r}")
        if not isinstance(params, dict):
            raise TypeError("params must be an object")
        kwargs = {**DEFAULTS.get(method, {}), **{k: params[k] for k in METHODS[method][0] if k in params}}
        if method == "screenshot":
            return encode_screenshot(self.driver.screenshot())
        if method == "ui_elements":
            return [encode_element(e) for e in self.driver.ui_elements()]
        if method == "app_labels":
            return dict(self.driver.app_labels(list(kwargs["packages"])))
        return getattr(self.driver, method)(**kwargs)

    async def _send(self, msg: dict) -> None:
        ws = self._ws
        if ws is None:
            raise websockets.exceptions.ConnectionClosedError(None, None)
        async with self._send_lock:
            await ws.send(json.dumps(msg))


def device_info_from_adb(driver: PhoneDriver) -> dict:
    keys = ["ro.serialno", "ro.product.model", "ro.product.manufacturer", "ro.build.version.release"]
    out = driver.shell("; ".join(f"getprop {k}" for k in keys))
    values = [line.strip() for line in out.splitlines()] + [""] * len(keys)
    serial, model, manufacturer, android = values[: len(keys)]
    return {
        "id": serial or driver.serial or "unknown",
        "model": model,
        "manufacturer": manufacturer,
        "android": android,
    }


def _forward_stdin(bridge: PhoneBridge, loop: asyncio.AbstractEventLoop) -> None:
    """Thread: each line typed at the terminal becomes a command."""
    for line in sys.stdin:
        if line.strip():
            asyncio.run_coroutine_threadsafe(bridge.send_command(line), loop)


async def _main(bridge: PhoneBridge, read_stdin: bool) -> None:
    if read_stdin:
        loop = asyncio.get_running_loop()
        threading.Thread(target=_forward_stdin, args=(bridge, loop), name="openmob-stdin", daemon=True).start()
    await bridge.run()


def run_bridge(
    driver: Device,
    server_url: str,
    token: str,
    device_info: dict,
    read_stdin: bool = True,
    key: str | None = None,
    on_rejected: Callable[[], None] | None = None,
) -> None:
    """Blocking CLI entrypoint: serve until Ctrl+C; exit with a message on a bad token or key."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    bridge = PhoneBridge(driver, server_url, token, device_info, key=key)
    name = " ".join(filter(None, [device_info.get("manufacturer"), device_info.get("model")])) or device_info.get("id")
    print(f"📱 Bridging {name} to {bridge.url.split('?', 1)[0]}")
    if read_stdin:
        print("⌨️  Type commands here to send them to the agent. Ctrl+C to quit.")
    try:
        asyncio.run(_main(bridge, read_stdin))
    except BridgeAuthError as exc:
        if key:
            if on_rejected:
                on_rejected()
            sys.exit(f"✗ {exc}. Pair again: openmob bridge {server_url} --email YOUR_EMAIL")
        sys.exit(f"✗ {exc}. Check --token / OPENMOB_TOKEN.")
    except KeyboardInterrupt:
        print("\nStopped.")


# --- account mode ------------------------------------------------------------------------------

KEY_FILE = Path.home() / ".config" / "openmob" / "bridge-keys.json"


def _http_base(server_url: str) -> str:
    """ws(s):// or a bare host → http(s)://host[/prefix], for the REST calls."""
    url = ws_url(server_url, "")
    base = url.split("/phone/ws", 1)[0]
    return "https://" + base.removeprefix("wss://") if base.startswith("wss://") else "http://" + base.removeprefix("ws://")


def pair_with_account(server_url: str, email: str, password: str, device_id: str, model: str) -> str:
    """Sign in as the account owner, make a pairing code, and redeem it for this phone.

    The bridge's operator controls the phone over adb and knows the account password, so it
    can do both halves of pairing itself. Returns the phone's own key.
    """
    import httpx

    verify = _tls_context(server_url.replace("https://", "wss://")) or True
    with httpx.Client(base_url=_http_base(server_url), timeout=20, verify=verify) as client:
        def check(response: httpx.Response) -> dict:
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = response.text[:200]
                raise BridgeAuthError(str(detail or f"HTTP {response.status_code}"))
            return response.json()

        session = check(client.post("/api/auth/login", json={"email": email, "password": password}))["session"]
        headers = {"X-OpenMob-Session": session}
        pairing = check(client.get("/api/pairing", headers=headers))
        if not pairing.get("accounts"):
            raise BridgeAuthError("this server doesn't use accounts; use --token instead")
        redeemed = check(client.post(
            "/api/pair/redeem", headers=headers,
            json={"code": pairing["code"], "device_id": device_id, "model": model},
        ))
        return redeemed["key"]


def _key_id(server_url: str, device_id: str) -> str:
    return f"{_http_base(server_url)}|{device_id}"


def load_key(server_url: str, device_id: str) -> str | None:
    try:
        return json.loads(KEY_FILE.read_text()).get(_key_id(server_url, device_id))
    except (OSError, ValueError):
        return None


def save_key(server_url: str, device_id: str, key: str | None) -> None:
    """Store (or with key=None, forget) a phone's key. The file is owner-readable only."""
    try:
        keys = json.loads(KEY_FILE.read_text())
    except (OSError, ValueError):
        keys = {}
    if key:
        keys[_key_id(server_url, device_id)] = key
    else:
        keys.pop(_key_id(server_url, device_id), None)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps(keys, indent=2))
    KEY_FILE.chmod(0o600)
