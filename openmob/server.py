"""OpenMob web dashboard: live phone screens, voice/text commands, agent activity.

Serve it on the LAN, or publicly over HTTPS with `openmob run --public`.
"""

from __future__ import annotations

import asyncio
import io
import os
import socket
import threading
import weakref
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from .agent.models import model_listing
from .devices import DeviceManager, Session
from .phone.base import DeviceError
from .phone.protocol import PROTOCOL_VERSION, decode_message
from .phone.remote import PhoneLink
from .screencast import ScreenFeed, live_frame

STATIC = Path(__file__).parent / "static"
PAGE = STATIC / "index.html"
# Debug build of the companion app (built from android/ in this repo).
APK = Path(
    os.environ.get("OPENMOB_APK")
    or Path(__file__).resolve().parent.parent / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
)
HELLO_TIMEOUT = 10


class CommandIn(BaseModel):
    text: str
    mode: Literal["auto", "queue"] = "auto"  # auto: corrects a running task; queue: always a new task


class Credentials(BaseModel):
    email: str
    password: str


class ModelIn(BaseModel):
    model: str


class ChatOpenIn(BaseModel):
    chat_id: str


class RenameIn(BaseModel):
    title: str


class FeedbackIn(BaseModel):
    value: int  # 1 = 👍, -1 = 👎, 0 = clear
    note: str = ""


class NoteIn(BaseModel):
    text: str
    package: str | None = None  # set: a note about that app; else about the user


class NotePatch(BaseModel):
    text: str | None = None
    status: str | None = None  # "active" approves a proposal


class RedeemIn(BaseModel):
    code: str
    device_id: str
    model: str = ""


MAX_AUDIO_BYTES = 10 * 1024 * 1024  # about 10 minutes of browser-recorded speech


def create_app(manager: DeviceManager, token: str | None = None, store=None, transcriber=None,
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

    @app.get("/connect", response_class=HTMLResponse)
    def connect_page() -> str:
        return (STATIC / "connect.html").read_text()

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

    @app.post("/api/auth/signup")
    def signup(body: Credentials, response: Response) -> dict:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        try:
            account_id = store.create_account(body.email, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"email": store.account_email(account_id), "session": _set_session(response, account_id)}

    @app.post("/api/auth/login")
    def login(body: Credentials, response: Response) -> dict:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        try:
            account_id = store.verify_login(body.email, body.password)
        except AuthError as exc:
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

    @app.post("/api/pair/redeem")
    def redeem(body: RedeemIn, request: Request) -> dict:
        """The phone app calls this after the owner has signed in and passed the screen-lock check."""
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        from .accounts import AuthError

        acct = account(request)
        if acct is None:
            raise HTTPException(status_code=401, detail="Sign in on the phone first.")
        try:
            key = store.redeem_pairing_code(body.code, acct, body.device_id, body.model)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"key": key, "server": public_base(request)}

    @app.post("/api/devices/{device_id}/revoke", dependencies=[Depends(require_auth)])
    def revoke(device_id: str, request: Request) -> dict:
        if store is None:
            raise HTTPException(status_code=404, detail="Accounts are not enabled.")
        # The dashboard knows phones by session id ("app-<device id>"); the store by device id.
        raw_id = device_id.removeprefix("app-")
        if not store.revoke_device(account(request), raw_id):
            raise HTTPException(status_code=404, detail="Not your phone.")
        s = manager.get(f"app-{raw_id}")
        if s and s.link:
            s.link.disconnect(4401, "This phone was unpaired from its account.")  # cut it off now
        manager.remove(f"app-{raw_id}")  # and drop it from the phone lists
        return {"ok": True}

    # --- pairing / device lists ------------------------------------------

    @app.get("/api/pairing", dependencies=[Depends(require_auth)])
    def pairing(request: Request) -> dict:
        """Data for the QR code on /connect. Hosted mode issues a one-time, account-bound code."""
        from urllib.parse import quote

        base = public_base(request)
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
        code, expires = store.create_pairing_code(account(request))
        return {
            **common,
            "pair_url": f"{base}/pair?code={quote(code)}",
            "app_link": f"openmob://connect?server={quote(base, safe='')}&code={quote(code)}",
            "code": code,
            "expires": expires,
            "accounts": True,
        }

    @app.get("/api/devices", dependencies=[Depends(require_auth)])
    def devices(request: Request, rescan: bool = False) -> list[dict]:
        if store is None and (rescan or not manager.sessions):
            manager.refresh()
        return [s.describe() for s in manager.sessions_for(account(request))]

    @app.get("/api/devices/{device_id}/screen.jpg")
    def screen(device_id: str, request: Request, after: float = 0.0) -> Response:
        """One live-view frame (polling fallback for screen/ws). `after`: the X-Frame-Time already shown."""
        require_auth(request)
        try:
            taken, shot = live_frame(owned_session(device_id, request).driver, newer_than=after)
        except DeviceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        headers = {"Cache-Control": "no-store", "X-Frame-Time": repr(taken)}
        return Response(shot.jpeg, media_type="image/jpeg", headers=headers)

    screen_feeds: weakref.WeakKeyDictionary[Session, ScreenFeed] = weakref.WeakKeyDictionary()

    @app.websocket("/api/devices/{device_id}/screen/ws")
    async def screen_socket(ws: WebSocket, device_id: str) -> None:
        """Live view, pushed: binary JPEG preview frames (and {"error"} texts) while the viewer is open.
        Same auth as screen.jpg: cookie or header session, or ?t= in token mode."""
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

        feed = screen_feeds.get(session) or screen_feeds.setdefault(session, ScreenFeed(session.driver))
        await feed.serve(ws, allowed)

    @app.post("/api/devices/{device_id}/command")
    def command(device_id: str, body: CommandIn, request: Request) -> dict:
        require_auth(request)
        return {"result": owned_session(device_id, request).coordinator.submit(body.text, "dashboard", mode=body.mode)}

    @app.post("/api/devices/{device_id}/stop")
    def stop(device_id: str, request: Request) -> dict:
        require_auth(request)
        owned_session(device_id, request).coordinator.stop()
        return {"result": "stopped"}

    @app.post("/api/transcribe", dependencies=[Depends(require_auth)])
    async def transcribe(file: UploadFile) -> dict:
        """The composer's mic: speech in, text out (text goes into the message box, not sent)."""
        if transcriber is None:
            raise HTTPException(status_code=501, detail="Speech-to-text isn't installed on this server: pip install -e '.[voice]'")
        data = await file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Recording is too long. Keep it under a few minutes.")
        try:
            text = await run_in_threadpool(transcriber.transcribe, io.BytesIO(data))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't read that recording: {type(exc).__name__}") from None
        return {"text": text}

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

    # --- memory: pinned notes (what the agent remembers about its user) ---------------

    memory = getattr(manager, "memory", None)  # optional: no store, no memory

    def need_memory():
        if memory is None:
            raise HTTPException(status_code=501, detail="Memory isn't enabled on this server.")
        return memory

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page() -> str:
        return (STATIC / "memory.html").read_text()  # all data comes from /api/memory

    @app.get("/api/memory", dependencies=[Depends(require_auth)])
    def list_notes(request: Request) -> dict:
        return {"notes": need_memory().list(account(request))}

    @app.post("/api/memory", dependencies=[Depends(require_auth)])
    def add_note(body: NoteIn, request: Request) -> dict:
        from .memory import NoteError

        try:  # typed by the user themselves: active right away
            return need_memory().add(account(request), body.text, package=body.package, source="user")
        except NoteError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.patch("/api/memory/{note_id}", dependencies=[Depends(require_auth)])
    def edit_note(note_id: str, body: NotePatch, request: Request) -> dict:
        from .memory import NoteError

        try:
            note = need_memory().update(account(request), note_id, text=body.text, status=body.status)
        except NoteError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if note is None:
            raise HTTPException(status_code=404, detail="Unknown note.")
        return note

    @app.delete("/api/memory/{note_id}", dependencies=[Depends(require_auth)])
    def delete_note(note_id: str, request: Request) -> dict:
        """Delete a note, or dismiss a proposal."""
        if not need_memory().delete(account(request), note_id):
            raise HTTPException(status_code=404, detail="Unknown note.")
        return {"ok": True}

    @app.delete("/api/memory", dependencies=[Depends(require_auth)])
    def forget_everything(request: Request, confirm: bool = False) -> dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="Add ?confirm=true to delete every note.")
        return {"deleted": need_memory().delete_all(account(request))}

    @app.get("/api/devices/{device_id}/status")
    def status(device_id: str, request: Request) -> dict:
        require_auth(request)
        s = owned_session(device_id, request)
        snap = s.coordinator.snapshot()
        feedback = history.feedback_map(account(request), snap["chat_id"]) if history and snap.get("chat_id") else {}
        return {**snap, "device": s.describe(), "feedback": feedback}

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
        s = manager.get(f"app-{raw_id}")
        if s and s.link:
            s.link.disconnect(4401, "This phone was unpaired by an administrator.")  # cut it off now
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
        app.include_router(developer_api.router)
        app.include_router(developer_api.console)
        app.include_router(developer_api.admin)

        @app.get("/developer", response_class=HTMLResponse)
        def developer_page() -> str:
            return (STATIC / "developer.html").read_text()  # all data comes from /api/developer

    if history is not None and manager.cfg.history_retention_days > 0:
        def purge_forever() -> None:
            while True:
                try:
                    history.purge(manager.cfg.history_retention_days)
                except Exception as exc:  # retention must never take the server down
                    print(f"⚠️  History retention failed: {exc}", flush=True)
                threading.Event().wait(6 * 3600)

        threading.Thread(target=purge_forever, name="openmob-retention", daemon=True).start()

    # --- phone connection ------------------------------------------------

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
        if store is not None:
            from .accounts import AuthError

            device = hello.get("device") or {}
            try:
                account_id = await run_in_threadpool(
                    store.verify_device, device.get("id") or "", hello.get("key") or ""
                )
            except AuthError as exc:
                await ws.close(code=4401, reason=str(exc))
                return

        link = PhoneLink(ws, asyncio.get_running_loop())
        # Creating a session builds an agent (API clients etc.), so keep it off the event loop.
        session = await run_in_threadpool(manager.attach_remote, hello.get("device") or {}, link, account_id)
        await link.send({"type": "welcome", "session": session.info.id})
        session.coordinator.event("status", f"{session.info.model} connected over the OpenMob app protocol.")
        await run_in_threadpool(manager.resume, session)  # pending approvals, "who is using it"
        close_code = None
        try:
            while True:
                raw = await ws.receive()
                if raw["type"] == "websocket.disconnect":
                    close_code = raw.get("code")  # 1000: the user disconnected on purpose
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
                                            message.get("approved") is True)
                elif kind == "command" and str(message.get("text", "")).strip():
                    await run_in_threadpool(session.coordinator.submit, message["text"], "phone")
        except (WebSocketDisconnect, ValueError):
            pass
        finally:
            # Decide about the reconnect wait first, then fail the calls still in flight.
            manager.detach_remote(session, link, close_code)
            link.close()
            session.coordinator.event("status", f"{session.info.model} disconnected.")

    return app


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
