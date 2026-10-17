"""OpenMob web dashboard: live phone screens, text commands, agent activity.

Serve it on the LAN, or publicly over HTTPS with `openmob run --public`.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import permissions as perm
from .agent.models import model_listing
from .devices import DeviceManager, Session
from .phone.base import DeviceError
from .phone.protocol import PROTOCOL_VERSION, decode_message
from .phone.remote import PhoneLink
from .screencast import ScreenFeed, View, live_frame, preview_params

STATIC = Path(__file__).parent / "static"
# Public developer docs served to the /developer console, by name. Only these files, nothing else
# from docs/ (architecture notes and research stay out). Found next to the package (a source checkout)
# or in the working directory (the Docker image copies docs/ to /app/docs).
PUBLIC_DOCS = {"quickstart": "quickstart.md", "concepts": "concepts.md", "phone-setup": "phone-setup.md",
               "api": "api.md", "mcp": "mcp.md", "examples": "examples.md",
               "webhooks": "webhooks.md", "safety": "safety.md", "conventions": "conventions.md",
               "errors": "errors.md", "testing": "testing.md", "self-hosting": "self-hosting.md"}
DOCS_DIRS = [Path(os.environ["OPENMOB_DOCS_DIR"])] if os.environ.get("OPENMOB_DOCS_DIR") else []
DOCS_DIRS += [Path(__file__).parent.parent / "docs", Path.cwd() / "docs"]
PAGE = STATIC / "index.html"
OLD_APP_APPROVALS = ("This phone's OpenMob app is out of date: it signs approvals in an older format "
                     "this server no longer accepts. Install the latest app to approve; until then it "
                     "can only decline.")
LOGIN_MAX_FAILURES = 10  # failed sign-ins per address or email…
LOGIN_WINDOW = 600  # …in this many seconds, then 429
SIGNUP_MAX_PER_IP = 20  # sign-up attempts per address in LOGIN_WINDOW
PAIR_MAX_FAILURES_IP = 10  # wrong pairing codes per address in LOGIN_WINDOW, then 429
PAIR_MAX_FAILURES_ACCOUNT = 10  # wrong secrets for one account's codes in LOGIN_WINDOW, then 429
SIGNUP_REPLY = "If this email can be used, your account is ready: sign in with it and your password."
# Debug build of the companion app (built from android/ in this repo).
APK = Path(
    os.environ.get("OPENMOB_APK")
    or Path(__file__).resolve().parent.parent / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
)
HELLO_TIMEOUT = 10
LINK_RECHECK = 5.0  # seconds between re-checks of a phone socket's pairing against the account store


PermissionMode = Literal["ask", "plan", "auto", "trusted", "never"]


class CommandIn(BaseModel):
    text: str
    mode: Literal["auto", "queue"] = "auto"  # auto: corrects a running task; queue: always a new task
    permission_mode: PermissionMode | None = None  # this chat's mode; None: the phone's default


class PermissionsIn(BaseModel):
    """A change to a phone's permission settings. Tightening applies at once; loosening asks the phone."""
    default_mode: PermissionMode | None = None
    ceiling: Literal["ask", "plan", "auto", "trusted"] | None = None
    trusted: list[dict] | None = None  # the whole new list: [{app | category, kinds, expires_at}]
    never: Literal["1h", "24h", "7d", "off"] | None = None
    allow_money: bool | None = None  # "Also allow purchases" with never (off by default; the phone approves)
    task_approval: bool | None = None  # one phone approval per task covers its non-money risky steps
    everyday: bool | None = None  # don't confirm everyday actions (turning it on is approved on the phone)
    permission_prompts: dict[str, str] | None = None  # Android permission dialogs: {type: setting} (loosening asks)


class Credentials(BaseModel):
    email: str
    password: str


class SignupIn(Credentials):
    invite: str = ""  # one-time code from `openmob account invite`


class ModelIn(BaseModel):
    model: str


class ChatOpenIn(BaseModel):
    chat_id: str


class RenameIn(BaseModel):
    title: str


class FeedbackIn(BaseModel):
    value: int  # 1 = 👍, -1 = 👎, 0 = clear
    note: str = ""


class RedeemIn(BaseModel):
    code: str
    device_id: str
    model: str = ""
    approval_key: str | None = None  # the app's screen-lock-bound public key (see phone/approval_keys.py)


class CodeIn(BaseModel):
    code: str


class PhoneUnpairIn(BaseModel):
    device_id: str


class PhoneTrustIn(BaseModel):
    device_id: str  # the trusted phone signing
    target: str  # the phone to trust (raw id or "app-<id>")
    timestamp: int
    signature: str


class EnrollIn(BaseModel):
    device_id: str | None = None  # the app's id, when the CLI could read it: already paired → no new code


class FailureWindow:
    """Recent failures per key (address, email, account) within `window` seconds, for 429s."""

    def __init__(self, window: float = LOGIN_WINDOW):
        self.window = window
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def count(self, key: str) -> int:
        now = time.time()
        with self.lock:
            recent = [t for t in self.hits.get(key, []) if now - t < self.window]
            if recent:
                self.hits[key] = recent
            else:
                self.hits.pop(key, None)
            return len(recent)

    def add(self, *keys: str) -> None:
        now = time.time()
        with self.lock:
            for key in keys:
                self.hits.setdefault(key, []).append(now)


def client_ip(request) -> str:
    """The visitor's address. On Fly every request arrives from Fly's proxy, so per-IP limits would lump everyone
    together (one visitor could lock out sign-in for all): Fly's own Fly-Client-IP header is used there. It's
    trusted only when FLY_APP_NAME is set, i.e. behind Fly's proxy, which overwrites any value a client sends."""
    if os.environ.get("FLY_APP_NAME") and (fly := request.headers.get("fly-client-ip", "").strip()):
        return fly
    return request.client.host if request.client else "?"


def create_app(manager: DeviceManager, token: str | None = None, store=None,
               ledger=None, webhook_allow_private: bool = False) -> FastAPI:
    """Two modes. Without `store`: one shared token guards everything (local/demo).
    With an AccountStore: users sign in, phones carry their own key, and everyone sees only
    their own phones — a pairing code works only for the account that made it. Accounts mode
    also serves the developer API (/v1) and its console (/developer)."""
    app = FastAPI(title="OpenMob", docs_url=None, redoc_url=None)

    def session_token(request: Request) -> str | None:
        # Cookie (browsers) or header (phone app, bridge). Never the URL: links get copied and logged.
        return request.cookies.get("openmob_session") or request.headers.get("x-openmob-session")

    def account(request: Request) -> str | None:
        """Logged-in account, or None in token mode."""
        return store.account_for_session(session_token(request)) if store else None

    def require_auth(request: Request) -> None:
        """Dashboard/API guard for both modes."""
        if store is not None:
            if account(request) is None:
                raise HTTPException(status_code=401, detail="Sign in to OpenMob first.")
            return
        if token and token not in (request.headers.get("x-openmob-token"), request.query_params.get("t")):
            raise HTTPException(status_code=401, detail="Bad or missing token")

    def owned_session(device_id: str, request: Request) -> Session:
        found = manager.get(device_id)
        acct = account(request)
        # In hosted mode, hide phones that aren't yours (404, not 403, so existence stays private).
        if not found or (store is not None and found.account_id != acct):
            raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
        return found

    def public_base(request: Request) -> str:
        # Behind the HTTPS tunnel the request arrives as http://localhost; trust the forwarded headers.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}"

    # --- pages -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        # Signed-out visitors in account mode get the landing page; everyone else the dashboard.
        if store is not None and account(request) is None:
            return (STATIC / "landing.html").read_text()
        return PAGE.read_text()  # re-read so page tweaks show up without a restart

    @app.get("/static/ds.css")
    def design_system() -> Response:
        """The design system every page links (public: the phone's /pair page uses it too)."""
        css = (STATIC / "ds.css").read_text()
        return Response(css, media_type="text/css", headers={"Cache-Control": "public, max-age=300"})

    @app.get("/docs/{name}")
    def public_doc(name: str) -> Response:
        """A public developer doc as Markdown (rendered, sanitized, by /developer). Allowlisted names only."""
        filename = PUBLIC_DOCS.get(name.removesuffix(".md"))
        path = next((d / filename for d in DOCS_DIRS if filename and (d / filename).is_file()), None)
        if path is None:
            raise HTTPException(status_code=404, detail="Unknown doc.")
        return Response(path.read_text(), media_type="text/markdown; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=300"})

    @app.get("/connect", response_class=HTMLResponse)
    def connect_page() -> str:
        return (STATIC / "connect.html").read_text()

    @app.get("/phones", response_class=HTMLResponse)
    def phones_page() -> str:
        return (STATIC / "phones.html").read_text()  # all data comes from /api/devices

    @app.get("/pair", response_class=HTMLResponse)
    def pair_page() -> str:
        return (STATIC / "pair.html").read_text()

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        # A dedicated sign-in page can be dropped in as static/login.html; until then the landing
        # page explains how to sign in with a one-time link.
        path = STATIC / "login.html"
        return (path if path.exists() else STATIC / "landing.html").read_text()

    @app.api_route("/download/openmob.apk", methods=["GET", "HEAD"])  # HEAD: /pair checks it exists
    def download_apk() -> FileResponse:
        if not APK.exists():
            raise HTTPException(status_code=404, detail="The app hasn't been built yet: cd android && ./gradlew assembleDebug")
        return FileResponse(APK, media_type="application/vnd.android.package-archive", filename="OpenMob.apk")

    # --- accounts (hosted mode only) -------------------------------------

    def _set_session(response: Response, account_id: str) -> str:
        tok = store.create_session(account_id)
        response.set_cookie("openmob_session", tok, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
        return tok

    signups = FailureWindow()

    @app.post("/api/auth/signup")
    def signup(body: SignupIn, request: Request) -> dict:
        """Create an account with a one-time invite code. The reply is the same whether or not the
        email already had one (there's no email sending yet to tell the owner instead), and it never
        signs in: the client signs in next with /api/auth/login, whose failures are rate-limited.
        A 400 judges only the request itself (the email, the password, the invite), never other
        accounts; a valid invite is used up either way."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AccountExists, AuthError

        ip = f"ip:{client_ip(request)}"
        if signups.count(ip) >= SIGNUP_MAX_PER_IP:
            raise HTTPException(status_code=429, detail="Too many sign-ups from this address. Try again in a few minutes.",
                                headers={"Retry-After": str(LOGIN_WINDOW)})
        signups.add(ip)
        try:
            store.create_account_with_invite(body.email, body.password, body.invite)
        except AccountExists:
            pass  # same reply as a new account
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "message": SIGNUP_REPLY}

    # Failed sign-ins per client address and per email, in the last LOGIN_WINDOW seconds: the
    # password form mustn't be a free password-guessing oracle.
    login_failures = FailureWindow()

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response) -> dict:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        keys = (f"ip:{client_ip(request)}", f"email:{body.email.strip().lower()}")
        if any(login_failures.count(k) >= LOGIN_MAX_FAILURES for k in keys):
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in a few minutes.",
                                headers={"Retry-After": str(LOGIN_WINDOW)})
        try:
            account_id = store.verify_login(body.email, body.password)
        except AuthError as exc:
            login_failures.add(*keys)
            raise HTTPException(status_code=401, detail=str(exc)) from None
        return {"email": store.account_email(account_id), "session": _set_session(response, account_id)}

    @app.get("/auth/link")
    def login_link(code: str = "") -> Response:
        """One-time sign-in link printed by `openmob account link EMAIL` on the server."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        try:
            account_id = store.redeem_login_link(code)
        except AuthError as exc:
            return HTMLResponse(f"<p>{exc}</p>", status_code=400)
        response = RedirectResponse("/", status_code=303)  # drops the code from the address bar
        _set_session(response, account_id)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        if store is not None:
            store.end_session(session_token(request))
        response.delete_cookie("openmob_session")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request) -> dict:
        if store is None:
            return {"accounts": False}
        acct = account(request)
        return {
            "accounts": True,
            "email": store.account_email(acct) if acct else None,
            "admin": bool(acct and store.is_admin(acct)),
        }

    def audit(action: str, account_id: str | None, **fields) -> None:
        """Into the account's audit log (/developer › Logs), when the ledger is on (accounts mode)."""
        service = getattr(app.state, "v1_service", None)
        if service is not None and account_id:
            service.ledger.audit(action, account_id, **fields)

    # Wrong pairing codes per client address and per account the code's id belongs to: a code has
    # 128 bits of secret, so this is defense in depth, and it voids itself after a few wrong tries.
    pair_failures = FailureWindow()

    def pairing_guard(request: Request) -> str:
        ip = f"ip:{client_ip(request)}"
        if pair_failures.count(ip) >= PAIR_MAX_FAILURES_IP:
            raise HTTPException(status_code=429, detail="Too many pairing attempts. Try again in a few minutes.",
                                headers={"Retry-After": str(LOGIN_WINDOW)})
        return ip

    def pairing_failed(ip: str, exc) -> None:
        from .accounts import PairingRefused

        pair_failures.add(ip)
        if isinstance(exc, PairingRefused) and exc.account_id:
            pair_failures.add(f"acct:{exc.account_id}")

    def account_locked(account_id: str) -> bool:
        return pair_failures.count(f"acct:{account_id}") >= PAIR_MAX_FAILURES_ACCOUNT

    def finish_pairing(body: RedeemIn, request: Request, acct: str | None) -> dict:
        """Redeem a code for this phone's own device key. `acct`: the signed-in account (older apps)
        or None (the code is the credential). The phone gets a device key, never a session."""
        from .accounts import AuthError
        from .phone.approval_keys import valid_public_key

        ip = pairing_guard(request)
        if body.approval_key and not valid_public_key(body.approval_key):
            raise HTTPException(status_code=400, detail="approval_key must be a base64 EC P-256 public key.")
        try:
            owner = store.check_pairing_code(body.code)["account_id"]
            if account_locked(owner):
                raise HTTPException(status_code=429, detail="Too many pairing attempts for this account. "
                                    "Try again in a few minutes.", headers={"Retry-After": str(LOGIN_WINDOW)})
            key = store.redeem_pairing_code(body.code, acct, body.device_id, body.model, body.approval_key)
        except AuthError as exc:
            pairing_failed(ip, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from None
        raw_id = body.device_id.strip()
        # A new key for this phone: any socket still using the old one is cut off now.
        manager.close_links(f"app-{raw_id}", 4401, "This phone was paired again. Reconnect with its new key.")
        audit("device.paired", owner, device_id=raw_id, outcome=" ".join(body.model.split())[:80] or None)
        print(f"📱 New phone paired: {body.model or raw_id}", flush=True)
        return {"key": key, "server": public_base(request), "email": store.account_email(owner),
                "device_id": raw_id, "trusted_for_approvals": store.trusted_for_approvals(raw_id)}

    @app.post("/api/pair/check")
    def pair_check(body: CodeIn, request: Request) -> dict:
        """Before the phone asks its owner to confirm: which account this code would pair it to. The
        code isn't used up. Wrong codes count toward the same limits as redeeming."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        ip = pairing_guard(request)
        try:
            info = store.check_pairing_code(body.code)
        except AuthError as exc:
            pairing_failed(ip, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"email": info["email"], "expires": info["expires"], "server": public_base(request)}

    @app.post("/api/pair/claim")
    def pair_claim(body: RedeemIn, request: Request) -> dict:
        """The phone, after its owner confirmed with the screen lock: the code is the credential."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        return finish_pairing(body, request, None)

    @app.post("/api/pair/redeem")
    def redeem(body: RedeemIn, request: Request) -> dict:
        """Older apps and `openmob bridge --email`: the same, from a signed-in session of the code's account."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        acct = account(request)
        if acct is None:
            raise HTTPException(status_code=401, detail="Sign in on the phone first.")
        return finish_pairing(body, request, acct)

    @app.post("/api/phone/unpair")
    def phone_unpair(body: PhoneUnpairIn, request: Request) -> dict:
        """The phone unpairs itself with its own device key (X-OpenMob-Device-Key), no session needed."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        raw_id = body.device_id.strip()
        try:
            owner = store.unpair_by_key(raw_id, request.headers.get("x-openmob-device-key") or "")
        except AuthError:
            raise HTTPException(status_code=401, detail="This phone isn't paired, or its key is wrong.") from None
        manager.close_links(f"app-{raw_id}", 4401, "This phone was unpaired.")
        manager.remove(f"app-{raw_id}")
        audit("device.unpaired", owner, device_id=raw_id, outcome="from the phone")
        return {"ok": True}

    def trusted(account_id: str, raw_id: str, how: str) -> dict:
        audit("device.trusted", account_id, device_id=raw_id, outcome=how)
        s = manager.get(f"app-{raw_id}")
        if s is not None and s.link is not None:
            manager.resume(s)  # it can answer the approvals still waiting on the other phones now
        return {"ok": True, "trusted_for_approvals": True}

    @app.post("/api/devices/{device_id}/trust", dependencies=[Depends(require_auth)])
    def trust(device_id: str, request: Request) -> dict:
        """The owner, signed in on the dashboard, trusts a phone for approvals on their other phones.
        Only a dashboard session can do this (an API key can't reach /api), or a trusted phone's
        screen-lock signature (/api/phone/trust)."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        raw_id = device_id.removeprefix("app-")
        if not store.trust_device(account(request), raw_id):
            raise HTTPException(status_code=404, detail="Not your phone.")
        return trusted(account(request), raw_id, "from the dashboard")

    @app.post("/api/phone/trust")
    def phone_trust(body: PhoneTrustIn, request: Request) -> dict:
        """A trusted phone vouches for another of the owner's phones, signing
        "openmob-trust-1|trust|<target>|<time>" (approval_keys.trust_message: never valid as an approval)
        with its screen-lock approval key (X-OpenMob-Device-Key authenticates the phone itself)."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError
        from .phone.approval_keys import verify_trust

        signer = body.device_id.strip()
        target = body.target.removeprefix("app-").strip()
        try:
            owner = store.verify_device(signer, request.headers.get("x-openmob-device-key") or "")
        except AuthError:
            raise HTTPException(status_code=401, detail="This phone isn't paired, or its key is wrong.") from None
        auth = store.device_auth(signer)
        if not store.trusted_for_approvals(signer) or not auth or not auth["approval_key"]:
            raise HTTPException(status_code=403, detail="Only a phone trusted for approvals can trust another.")
        if not verify_trust(auth["approval_key"], target, body.timestamp, body.signature):
            raise HTTPException(status_code=403, detail="The trust wasn't signed with this phone's screen lock.")
        if not store.trust_device(owner, target):
            raise HTTPException(status_code=404, detail="Not your phone.")
        return trusted(owner, target, f"by phone {signer}")

    @app.post("/api/devices/{device_id}/revoke", dependencies=[Depends(require_auth)])
    def revoke(device_id: str, request: Request) -> dict:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        # The dashboard knows phones by session id ("app-<device id>"); the store by device id.
        raw_id = device_id.removeprefix("app-")
        if not store.revoke_device(account(request), raw_id):
            raise HTTPException(status_code=404, detail="Not your phone.")
        manager.close_links(f"app-{raw_id}", 4401, "This phone was unpaired from its account.")  # every socket
        manager.remove(f"app-{raw_id}")  # and drop it from the phone lists
        audit("device.removed", account(request), device_id=raw_id, outcome="from the dashboard")
        return {"ok": True}

    # --- pairing / device lists ------------------------------------------

    def phone_base(request: Request) -> str:
        """The server address a phone should use. A phone can't reach "localhost": pair it with this
        computer's network name instead, which also keeps working when the computer's IP changes.
        (Not behind a tunnel: that's public.)"""
        base = public_base(request)
        tunneled = "x-forwarded-host" in request.headers
        if base.startswith(("http://localhost", "http://127.0.0.1")) and not tunneled and local_hostname():
            base = f"http://{local_hostname()}:{request.url.port or 80}"
        return base

    def mint_pairing(account_id: str, base: str, purpose: str) -> dict:
        from urllib.parse import quote

        from .accounts import PairingLimit

        try:
            code, expires = store.create_pairing_code(account_id, purpose)
        except PairingLimit as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "600"}) from None
        audit("pairing.code_created", account_id, outcome=purpose)
        return {
            "pair_url": f"{base}/pair?code={quote(code)}",
            "app_link": f"openmob://connect?server={quote(base, safe='')}&code={quote(code)}",
            "code": code,
            "enrollment_id": code[:8],
            "expires": expires,
            "ttl": round(expires - time.time()),
            "email": store.account_email(account_id),
        }

    @app.api_route("/api/pairing", methods=["GET", "POST"], dependencies=[Depends(require_auth)])
    def pairing(request: Request) -> dict:
        """Data for the QR code on /connect, made when the owner opens "Connect a phone". Accounts
        mode issues a single-use, account-bound code that works for 2 minutes."""
        from urllib.parse import quote

        base = phone_base(request)
        common = {
            "server": base,
            "apk_url": f"{base}/download/openmob.apk" if APK.exists() else None,
            "local": base.startswith(("http://localhost", "http://127.0.0.1")),
        }
        if store is None:
            tok = token or ""
            return {
                **common,
                "pair_url": f"{base}/pair?t={quote(tok)}",
                "app_link": f"openmob://connect?server={quote(base, safe='')}&t={quote(tok)}",
                "accounts": False,
            }
        return {**common, **mint_pairing(account(request), base, "dashboard"), "accounts": True}

    def paired_phones(account_id: str) -> list[dict]:
        """Every phone paired to the account, from the store, with its live state when connected:
        offline and never-connected phones are listed too, so a leaked pairing can't hide."""
        rows = []
        for d in store.list_devices(account_id):
            s = manager.get(f"app-{d.id}")
            live = s is not None and s.account_id == account_id
            row = s.describe() if live else {
                "id": f"app-{d.id}", "model": d.model, "connection": "app", "platform": "android",
                "online": False, "reconnecting": False, "state": "offline", "current": None,
            }
            rows.append({**row, "paired": True, "session": live, "name": d.model, "last_seen": d.last_seen,
                         "paired_at": d.paired_at, "trusted_for_approvals": d.trusted})
        return rows

    @app.get("/api/devices", dependencies=[Depends(require_auth)])
    def devices(request: Request, rescan: bool = False) -> list[dict]:
        if store is None:
            if rescan or not manager.sessions:
                manager.refresh()
            return [s.describe() for s in manager.sessions_for(None)]
        acct = account(request)
        rows = paired_phones(acct)
        listed = {r["id"] for r in rows}
        # Phones with a live session but no stored pairing (none in practice) still show.
        rows += [s.describe() for s in manager.sessions_for(acct) if s.info.id not in listed]
        return rows

    @app.get("/api/devices/{device_id}/screen.jpg")
    def screen(device_id: str, request: Request, after: float = 0.0, side: int = 0, quality: str = "") -> Response:
        """One live-view frame (polling fallback for screen/ws). `after`: the X-Frame-Time already shown;
        `side`, `quality`: the viewer's panel size in device pixels and its preference (see screencast)."""
        require_auth(request)
        try:
            taken, shot = live_frame(owned_session(device_id, request).driver, after,
                                     *preview_params([View.parse(side, quality)]))
        except DeviceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        headers = {"Cache-Control": "no-store", "X-Frame-Time": repr(taken)}
        return Response(shot.jpeg, media_type="image/jpeg", headers=headers)

    screen_feeds: weakref.WeakKeyDictionary[Session, ScreenFeed] = weakref.WeakKeyDictionary()

    @app.websocket("/api/devices/{device_id}/screen/ws")
    async def screen_socket(ws: WebSocket, device_id: str) -> None:
        """Live view, pushed: H.264 packets or JPEG preview frames (and JSON texts) while the viewer is
        open; ?side=&quality= size them for the viewer's panel (see screencast). Same auth as
        screen.jpg: cookie or header session, or ?t= in token mode."""
        await ws.accept()  # accept first: a close before accepting reaches clients as a bare HTTP 403
        try:
            require_auth(ws)
            session = owned_session(device_id, ws)
        except HTTPException as exc:
            await ws.close(code=4000 + exc.status_code, reason=str(exc.detail)[:100])  # 4401 or 4404, nothing sent
            return

        def allowed() -> bool:
            try:
                require_auth(ws)
                return owned_session(device_id, ws) is session
            except HTTPException:
                return False

        # Screen sharing isn't asked for at setup: the first time someone opens the live view of a phone
        # without it, the phone offers it once (a notification), and the view uses screenshots meanwhile.
        link = getattr(session, "link", None)
        if link is not None and not getattr(session.driver, "video_available", True) \
                and not getattr(link, "live_view_offered", False):
            link.live_view_offered = True
            link.notify("live_view", "Your dashboard opened the live view.")
        feed = screen_feeds.get(session) or screen_feeds.setdefault(session, ScreenFeed(session.driver))
        await feed.serve(ws, allowed, View.parse(ws.query_params.get("side"), ws.query_params.get("quality")))

    def mode_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, perm.ModeNotAllowed):
            return JSONResponse({"detail": str(exc), "code": "mode_not_allowed"}, status_code=403)
        if isinstance(exc, perm.PhoneUnavailable):
            return JSONResponse({"detail": str(exc), "code": "phone_offline"}, status_code=409)
        return JSONResponse({"detail": str(exc), "code": "invalid_permissions"}, status_code=400)

    @app.post("/api/devices/{device_id}/command")
    def command(device_id: str, body: CommandIn, request: Request) -> Response:
        require_auth(request)
        session = owned_session(device_id, request)
        mode = None
        if body.permission_mode is not None:
            try:  # a chat's mode can't be looser than the phone allows ("Approve on your phone" first)
                mode = manager.check_mode(session.info.id, body.permission_mode)
            except (perm.ModeNotAllowed, perm.PermissionsInvalid) as exc:
                return mode_error(exc)
        return {"result": session.coordinator.submit(body.text, "dashboard", mode=body.mode, permission_mode=mode)}

    def permissions_view(session) -> dict:
        settings = manager.permission_settings(session.info.id)
        return {**perm.public(settings), "labels": perm.LABELS, "descriptions": perm.DESCRIPTIONS,
                "categories": sorted(manager.cfg.app_categories), "never_durations": list(perm.NEVER_DURATIONS),
                "can_loosen": manager._may_loosen(session), "online": bool(session.online)}

    @app.get("/api/devices/{device_id}/permissions")
    def get_permissions(device_id: str, request: Request) -> dict:
        require_auth(request)
        return permissions_view(owned_session(device_id, request))

    @app.patch("/api/devices/{device_id}/permissions")
    def patch_permissions(device_id: str, body: PermissionsIn, request: Request) -> Response:
        """Tightening applies now (200). Loosening (a looser default or ceiling, trusted entries, never)
        asks the phone for its screen lock: 202 {status: needs_phone_approval, approval_id}; poll
        .../permissions/approvals/{approval_id}."""
        require_auth(request)
        session = owned_session(device_id, request)
        patch = body.model_dump(exclude_none=True)
        try:
            result = manager.change_permissions(session, patch, requester="Your dashboard")
        except (perm.ModeNotAllowed, perm.PhoneUnavailable, perm.PermissionsInvalid) as exc:
            return mode_error(exc)
        if result["status"] == "applied":
            return JSONResponse({**result, "settings": permissions_view(session)})
        return JSONResponse({**result, "code": "needs_phone_approval",
                             "detail": "Approve this on the phone, with its screen lock."}, status_code=202)

    @app.get("/api/devices/{device_id}/permissions/approvals/{approval_id}")
    def permission_approval(device_id: str, approval_id: str, request: Request) -> dict:
        require_auth(request)
        session = owned_session(device_id, request)
        row = manager.permission_request(account(request), session.info.id, approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown approval.")
        return {**row, "settings": permissions_view(session)}

    @app.post("/api/devices/{device_id}/stop")
    def stop(device_id: str, request: Request) -> dict:
        require_auth(request)
        owned_session(device_id, request).coordinator.stop()
        return {"result": "stopped"}

    @app.get("/api/models", dependencies=[Depends(require_auth)])
    def models() -> dict:
        return model_listing(manager.cfg)

    @app.post("/api/devices/{device_id}/model")
    def set_model(device_id: str, body: ModelIn, request: Request) -> dict:
        """Choose the model for this phone's next tasks (allowlisted models whose provider is set up)."""
        require_auth(request)
        session = owned_session(device_id, request)
        allowed = {m["id"] for m in manager.cfg.models}
        if body.model not in allowed:
            raise HTTPException(status_code=400, detail=f"Model not allowed. Choose one of: {', '.join(sorted(allowed))}")
        if not session.coordinator.agent.model_available(body.model):
            raise HTTPException(status_code=400, detail="That model isn't available on this server (no API key for its provider).")
        session.coordinator.agent.model = body.model
        running = session.coordinator.snapshot()["state"] != "idle"
        return {"model": body.model, "applies": "next task" if running else "now"}

    # --- chats (history) ----------------------------------------------------

    history = manager.history

    def need_history():
        if history is None:
            raise HTTPException(status_code=501, detail="Chat history isn't enabled on this server.")
        return history

    @app.post("/api/devices/{device_id}/log/clear")  # older name for "New chat"
    @app.post("/api/devices/{device_id}/chat/new")
    def new_chat(device_id: str, request: Request) -> dict:
        require_auth(request)
        try:
            owned_session(device_id, request).coordinator.clear_log()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"ok": True}

    @app.post("/api/devices/{device_id}/chat/open")
    def open_chat(device_id: str, body: ChatOpenIn, request: Request) -> dict:
        require_auth(request)
        session = owned_session(device_id, request)
        chat = need_history().chat(account(request), body.chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Unknown chat.")
        if chat["device_id"] != session.info.id:
            raise HTTPException(status_code=400, detail="That chat was with a different phone.")
        try:
            session.coordinator.open_chat(body.chat_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"ok": True, "chat_id": body.chat_id}

    @app.get("/api/chats", dependencies=[Depends(require_auth)])
    def list_chats(request: Request) -> list[dict]:
        if history is None:
            return []
        return need_history().list_chats(account(request))

    @app.get("/api/chats/{chat_id}", dependencies=[Depends(require_auth)])
    def get_chat(chat_id: str, request: Request) -> dict:
        h = need_history()
        chat = h.chat(account(request), chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Unknown chat.")
        return {**chat, "turns": h.turns(account(request), chat_id)}

    @app.patch("/api/chats/{chat_id}", dependencies=[Depends(require_auth)])
    def rename_chat(chat_id: str, body: RenameIn, request: Request) -> dict:
        if not need_history().rename_chat(account(request), chat_id, body.title):
            raise HTTPException(status_code=404, detail="Unknown chat, or empty title.")
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}", dependencies=[Depends(require_auth)])
    def delete_chat(chat_id: str, request: Request) -> dict:
        acct = account(request)
        if not need_history().delete_chat(acct, chat_id):
            raise HTTPException(status_code=404, detail="Unknown chat.")
        # If a phone is showing this chat, it continues as a fresh one.
        for s in manager.sessions_for(acct):
            if s.coordinator.chat_id == chat_id and s.coordinator.current is None:
                s.coordinator.clear_log()
        return {"ok": True}

    @app.post("/api/turns/{turn_id}/feedback", dependencies=[Depends(require_auth)])
    def feedback(turn_id: str, body: FeedbackIn, request: Request) -> dict:
        try:
            ok = need_history().set_feedback(account(request), turn_id, body.value, body.note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown turn.")
        return {"ok": True}

    @app.get("/api/devices/{device_id}/status")
    def status(device_id: str, request: Request) -> dict:
        require_auth(request)
        s = owned_session(device_id, request)
        snap = s.coordinator.snapshot()
        feedback = history.feedback_map(account(request), snap["chat_id"]) if history and snap.get("chat_id") else {}
        # confirm_on_phone: only the phone may approve the pending risky action (the dashboard hides its buttons)
        on_phone = getattr(manager, "confirm_on_phone", None)
        settings = manager.permission_settings(s.info.id) if hasattr(manager, "permission_settings") else None
        # A system dialog the phone can't read is waiting for the owner (owner_dialog): the live view says so.
        owner_dialog = getattr(s.driver, "owner_dialog", None)
        return {**snap, "device": s.describe(), "feedback": feedback, "confirm_on_phone": bool(on_phone and on_phone(s)),
                "owner_dialog": owner_dialog if isinstance(owner_dialog, str) else None,
                "permissions": perm.public(settings) if settings else None}

    # --- admin (accounts mode; admin is granted only via `openmob account admin EMAIL`) ---

    def require_admin(request: Request) -> None:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        acct = account(request)
        if acct is None:
            raise HTTPException(status_code=401, detail="Sign in to OpenMob first.")
        if not store.is_admin(acct):
            raise HTTPException(status_code=403, detail="You're not an admin.")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> str:
        return (STATIC / "admin.html").read_text()  # all data comes from the guarded API below

    @app.get("/api/admin/users", dependencies=[Depends(require_admin)])
    def admin_users() -> list[dict]:
        return store.admin_list_accounts()

    @app.get("/api/admin/devices", dependencies=[Depends(require_admin)])
    def admin_devices() -> list[dict]:
        rows = []
        for d in store.admin_list_devices():
            if d["revoked"]:
                continue  # revoked phones are kept in the account store, but not shown
            s = manager.get(f"app-{d['id']}")
            snap = s.coordinator.snapshot() if s else {}
            rows.append({
                **d,
                "session_id": f"app-{d['id']}",
                "online": bool(s and s.online and not d["revoked"]),
                "state": snap.get("state"),
                "current": snap.get("current"),
            })
        rows.sort(key=lambda r: (r["revoked"], not r["online"], -(r["last_seen"] or 0)))
        return rows

    @app.post("/api/admin/devices/{device_id}/revoke", dependencies=[Depends(require_admin)])
    def admin_revoke(device_id: str) -> dict:
        raw_id = device_id.removeprefix("app-")
        if not store.admin_revoke_device(raw_id):
            raise HTTPException(status_code=404, detail=f"Unknown device {device_id}")
        manager.close_links(f"app-{raw_id}", 4401, "This phone was unpaired by an administrator.")  # every socket
        manager.remove(f"app-{raw_id}")  # and drop it from the phone lists
        return {"ok": True}

    # --- developer API (accounts mode only: keys belong to accounts) -------------

    if store is not None:
        from .ledger import Ledger
        from .platform.errors import ServiceError
        from .platform.rest import DeveloperApi, service_error_response
        from .platform.service import V1Service

        service = V1Service(manager, store, ledger or Ledger(store.path.with_name("ledger.sqlite")),
                            webhook_allow_private=webhook_allow_private)
        developer_api = DeveloperApi(service, account, require_admin)
        app.add_exception_handler(ServiceError, service_error_response)
        app.state.v1_service = service
        app.state.developer_api = developer_api

        from .platform import mcp
        from .platform.orchestrator import Orchestrator, routes as orchestration_routes

        service.orchestrator = Orchestrator(service)  # multi-phone runs, /v1/orchestrations
        app.include_router(orchestration_routes(service.orchestrator, developer_api.need))
        app.state.mcp = mcp.mount(app, service)  # the remote MCP server at /mcp, same rules as /v1
        app.include_router(developer_api.router)
        app.include_router(developer_api.console)
        app.include_router(developer_api.admin)

        @app.post("/v1/enrollments", status_code=201)
        def enroll(request: Request, body: EnrollIn | None = None,
                   p=Depends(developer_api.need("devices:add"))) -> dict:
            """`openmob phone add`: a pairing code for a phone on the developer's USB cable. The phone
            still has to confirm on its own screen (host and account, with the screen lock) before
            it's paired. Already paired to this account (the app's id is known): no new code."""
            device_id = ((body.device_id if body else None) or "").strip()
            if device_id and store.is_paired(p.account_id, device_id):
                s = manager.get(f"app-{device_id}")
                return {"already_paired": True, "device_id": f"app-{device_id}",
                        "online": bool(s and s.account_id == p.account_id and s.online),
                        "email": store.account_email(p.account_id)}
            out = mint_pairing(p.account_id, phone_base(request), "cli")
            return {"already_paired": False, "server": phone_base(request), **out}

        @app.get("/v1/enrollments/{enrollment_id}")
        def enrollment(enrollment_id: str, p=Depends(developer_api.need("devices:add"))) -> dict:
            """pending → paired (the owner confirmed on the phone) → online (its app connected)."""
            status = store.pairing_status(p.account_id, enrollment_id)
            if status is None:
                raise HTTPException(status_code=404, detail="Unknown enrollment.")
            device_id = status["device_id"]
            s = manager.get(f"app-{device_id}") if device_id else None
            online = bool(s and s.account_id == p.account_id and s.online)
            return {"status": status["status"], "device_id": f"app-{device_id}" if device_id else None,
                    "online": online, "model": s.info.model if s else None,
                    # devices:add can't change this: trusting needs the dashboard or a trusted phone.
                    "trusted_for_approvals": bool(device_id and store.trusted_for_approvals(device_id))}

        @app.get("/developer", response_class=HTMLResponse)
        def developer_page() -> str:
            return (STATIC / "developer.html").read_text()  # all data comes from /api/developer

    v1_ledger = getattr(getattr(app.state, "v1_service", None), "ledger", None)
    if (history is not None or v1_ledger is not None) and manager.cfg.history_retention_days > 0:
        def purge_forever() -> None:
            while True:
                try:
                    purge_task_text(manager.cfg.history_retention_days, history, v1_ledger)
                except Exception as exc:  # retention must never take the server down
                    print(f"⚠️  Retention failed: {exc}", flush=True)
                threading.Event().wait(6 * 3600)

        threading.Thread(target=purge_forever, name="openmob-retention", daemon=True).start()

    # --- phone connection ------------------------------------------------

    def pairing_still_valid(raw_id: str, account_id: str | None, key_hash: str | None) -> bool:
        """The phone is still paired to this account, with the key this socket used."""
        auth = store.device_auth(raw_id)
        return bool(auth and not auth["revoked"] and auth["account_id"] == account_id and auth["key_hash"] == key_hash)

    def register_approval_key(raw_id: str, key_hash: str, public_key, device: dict) -> None:
        """Phones paired before approval keys existed register theirs on first connect (once)."""
        from .phone.approval_keys import valid_public_key

        if isinstance(public_key, str) and valid_public_key(public_key):
            if store.register_approval_key(raw_id, key_hash, public_key):
                print(f"🔐 Approval key registered for {device.get('model') or raw_id}.", flush=True)

    def verify_phone_approval(session, approval_id: str, record: dict, message: dict) -> bool:
        """A yes counts only if the answering phone signed it with its screen-lock key, over the
        digest of this very action (phone/approval_keys.py). An app phone without a registered key
        can't approve at all: it registers one on its next connect, and until then can only decline.
        (Token/dev mode has no keys, so it trusts the phone as before.)"""
        if store is None:
            return True
        from .phone.approval_keys import verify_approval

        auth = store.device_auth(session.info.id.removeprefix("app-"))
        if not auth or auth["revoked"] or auth["account_id"] != session.account_id or not auth["approval_key"]:
            return False
        return verify_approval(auth["approval_key"], str(approval_id), record["task_id"], True,
                               message.get("timestamp"), message.get("signature"),
                               extra="always" if message.get("always") is True else None,
                               digest=record.get("digest"))

    def signs_action_digests(session) -> bool:
        """Whether this phone's app signs approvals over the action digest (the only format accepted).
        Apps from before it don't say so; the release that added it is the one that also added
        `resolve_url`, so either capability marks a current app."""
        caps = getattr(session.driver, "capabilities", None) or set()
        return bool({"approval_digest", "resolve_url"} & set(caps))

    def approval_blocker(session) -> str | None:
        """Why this phone can't approve yet: hosted app phones need a registered approval key and an app
        that signs the current format. Such a phone is still asked, so it can decline."""
        if not session.info.id.startswith("app-"):
            return None
        auth = store.device_auth(session.info.id.removeprefix("app-"))
        if not (auth and not auth["revoked"] and auth["approval_key"]):
            return ("This phone can't approve yet: its OpenMob app hasn't registered a screen-lock key. Update the "
                    "app and reconnect (it registers on connect). Until then it can only decline.")
        if not signs_action_digests(session):
            return OLD_APP_APPROVALS
        return None

    def phone_can_loosen(session) -> bool:
        if not session.info.id.startswith("app-"):
            return False
        auth = store.device_auth(session.info.id.removeprefix("app-"))
        return bool(auth and not auth["revoked"] and auth["approval_key"]) and signs_action_digests(session)

    def may_approve_for_others(session) -> bool:
        return store.trusted_for_approvals(session.info.id.removeprefix("app-"))

    if hasattr(manager, "replaced_listeners"):  # (test doubles may not have these)
        manager.approval_verifier = verify_phone_approval
        if store is not None:  # hosted: settings live on each phone's pairing; loosening needs its key
            manager.permissions = perm.AccountPermissions(store)
            manager.can_loosen = phone_can_loosen
            manager.approval_blocker = approval_blocker  # approving at all needs the key
            manager.requires_signature = lambda session: session.info.id.startswith("app-")
            # Approvals for the owner's OTHER phones go only to (and are taken only from) trusted ones.
            # A trusted phone still signs each yes over the action digest with its own key; a permission
            # change is only ever answered by the phone being loosened (DeviceManager.resolve_approval).
            manager.approval_trust = may_approve_for_others

    def phone_reconnected_elsewhere(session, old, new) -> None:
        service = getattr(app.state, "v1_service", None)
        if service is not None:
            service.ledger.audit("device.connection_replaced", session.account_id,
                                 device_id=session.info.id.removeprefix("app-"),
                                 outcome=f"{getattr(old, 'peer', None)} → {getattr(new, 'peer', None)}")

    if hasattr(manager, "replaced_listeners"):
        manager.replaced_listeners.append(phone_reconnected_elsewhere)

    @app.websocket("/phone/ws")
    async def phone_socket(ws: WebSocket) -> None:
        """Phones (OpenMob app or `openmob bridge`) connect here. See openmob/phone/protocol.py."""
        await ws.accept()  # accept first: a close before accepting reaches clients as a bare HTTP 403
        if store is None and token and token not in (ws.query_params.get("t"), ws.headers.get("x-openmob-token")):
            await ws.close(code=4401, reason="Bad or missing token")
            return
        try:
            hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4400, reason="Expected hello")
            return
        if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL_VERSION:
            await ws.close(code=4400, reason=f"Expected hello with protocol {PROTOCOL_VERSION}")
            return

        account_id = None
        device = hello.get("device") or {}
        raw_id = str(device.get("id") or "")
        key_hash = None
        if store is not None:
            from .accounts import AuthError

            try:
                account_id = await run_in_threadpool(store.verify_device, raw_id, hello.get("key") or "")
            except AuthError as exc:
                await ws.close(code=4401, reason=str(exc))
                return
            key_hash = store.key_fingerprint(hello.get("key") or "")
            await run_in_threadpool(register_approval_key, raw_id, key_hash, hello.get("approval_key"), device)

        link = PhoneLink(ws, asyncio.get_running_loop())
        link.peer = ws.client.host if ws.client else None  # to tell the owner about a connection from elsewhere
        link.key_hash = key_hash  # the device key this socket proved; a re-pair makes it stale
        # Creating a session builds an agent (API clients etc.), so keep it off the event loop.
        session = await run_in_threadpool(manager.attach_remote, hello.get("device") or {}, link, account_id)
        await link.send({"type": "welcome", "session": session.info.id})
        session.coordinator.event("status", f"{session.info.model} connected over the OpenMob app protocol.")
        await run_in_threadpool(manager.resume, session)  # pending approvals, "who is using it"
        close_code = None
        checked = time.monotonic()
        try:
            while True:
                raw = await ws.receive()
                if raw["type"] == "websocket.disconnect":
                    close_code = raw.get("code")  # 1000: the user disconnected on purpose
                    break
                # A replaced, unpaired or re-paired socket can't act: drop what it sends and cut it off.
                if not manager.is_live(session, link):
                    link.disconnect(4409, "Replaced by a newer connection from this phone.")
                    break
                if store is not None and time.monotonic() - checked > LINK_RECHECK:
                    checked = time.monotonic()
                    if not await run_in_threadpool(pairing_still_valid, raw_id, account_id, key_hash):
                        manager.close_links(session.info.id, 4401, "This phone's pairing changed. Pair it again.")
                        break
                message = decode_message(raw.get("text"), raw.get("bytes"))  # binary: a screenshot result
                kind = message.get("type")
                if kind == "result":
                    link.resolve(message)
                elif kind == "video":
                    link.on_video(message)
                elif kind == "video_state":
                    session.driver.video_available = bool(message.get("available"))
                elif kind == "approval":  # the owner approved or denied a risky action, with screen lock
                    await run_in_threadpool(manager.resolve_approval, session, message.get("id"),
                                            message.get("approved") is True, message)
                elif kind == "gate_event":  # the phone's own approval prompt: pending, approved, denied, expired
                    await run_in_threadpool(manager.gate_event, session, message)
                elif kind == "owner_dialog":  # a system dialog the phone can't read waits for the owner, or closed
                    await run_in_threadpool(manager.owner_dialog, session, message)
                elif kind == "device_lock":  # a secure lock came up mid-task, or the owner unlocked
                    await run_in_threadpool(manager.device_lock, session, message)
                elif kind == "task_stop":  # the owner tapped Stop on the phone's agent indicator: stop, stay connected
                    await run_in_threadpool(manager.phone_task_stop, session)
                elif kind == "permissions_off":  # "Turn off" in the never banner: an unsigned tightening
                    await run_in_threadpool(manager.phone_turned_off_never, session)
                elif kind == "command" and str(message.get("text", "")).strip():
                    targets = manager.phone_stop_targets(session)
                    result = await run_in_threadpool(session.coordinator.submit, message["text"], "phone")
                    if result == "stopped":  # "stop" said or typed on the phone: the owner's call
                        await run_in_threadpool(manager.phone_stopped, session, targets, "stop")
        except (WebSocketDisconnect, ValueError, RuntimeError):  # RuntimeError: we closed it ourselves
            pass
        finally:
            # Decide about the reconnect wait first, then fail the calls still in flight.
            manager.detach_remote(session, link, close_code)
            link.close()
            session.coordinator.event("status", f"{session.info.model} disconnected.")

    return app


def purge_task_text(days: float, history=None, ledger=None) -> dict:
    """The one retention job: every stored copy of task text and screen summaries older than `days`
    goes (chats with their turns, events and steps; orchestration plans and results; delivered or
    dead webhook bodies; idempotency replays). Billing totals and the hashed audit log stay. 0: keep all."""
    out = {}
    if history is not None:
        out["chats"] = history.purge(days)
    if ledger is not None:
        out.update(ledger.purge_text(days))
    return out


def serve_in_background(app: FastAPI, host: str, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, name="openmob-http", daemon=True).start()
    return server


def lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packets are sent; this just picks the LAN interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


_local_name: list[str | None] = []


def local_hostname() -> str | None:
    """This computer's Bonjour name (e.g. "ans-MacBook-Air.local"), which phones and browsers on the
    same network can reach. Unlike lan_ip() it survives the IP changing (sleep, another Wi-Fi)."""
    if not _local_name:
        name = None
        if sys.platform == "darwin":
            try:
                out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2)
                name = out.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                name = None
        if not name:
            host = socket.gethostname()
            name = host.removesuffix(".local") if host.endswith(".local") else None
        _local_name.append(f"{name}.local" if name else None)
    return _local_name[0]
