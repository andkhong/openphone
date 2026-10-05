"""The public developer API (/v1), the developer console's endpoints, and the admin kill switches.

Security model, enforced here once for every client (curl, SDKs, the MCP adapter):
- Authorization: Bearer om_live_… → key → account → device. Anything not this account's is a
  404, so other people's phones and tasks can't even be confirmed to exist.
- Each route needs a scope; screen:read is opt-in.
- Per-key rate limit, concurrent-task cap and daily quota.
- Every action goes to the append-only audit log; every task to the usage ledger.
- API tasks may not open sensitive apps (banking, payments, passwords) unless the owner allows it.

Responses speak OpenMob only: tiers, steps, credits. Which models or classifiers run behind a tier,
internal event kinds and per-provider token counts never appear, and upstream errors are reworded.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .accounts import SCOPES, AccessDisabled, AuthError
from .ledger import day_start
from .phone.base import DeviceError
from .runner import FINAL_STATUSES

MAX_KEYS_PER_ACCOUNT = 20
MAX_INSTRUCTION = 2000
KEEP_REGISTRY = 10_000  # live task → owner lookups kept in memory (older ones come from the ledger)
SSE_POLL = 0.25
SSE_HEARTBEAT = 15.0
WEBHOOK_TIMEOUT = 5.0
WEBHOOK_RETRIES = (1.0, 4.0)  # waits before the 2nd and 3rd attempts

# Internal names that must never reach a developer: model families, providers, classifiers.
INTERNAL_NAMES = re.compile(
    r"\b(claude[\w.-]*|anthropic|sonnet|opus|haiku|fable|jev[\w-]*|typesafe|whisper|openai|gpt-[\w.-]+)\b",
    re.IGNORECASE,
)
EXCEPTION_TEXT = re.compile(r"^[A-Za-z_.]*(Error|Exception)\b")
GENERIC_ERROR = "The task hit an internal error."
PUBLIC_KINDS = {"status": "status", "thought": "thought", "action": "action", "error": "warning", "question": "question",
                "memory": "memory"}


def clean_text(message: str | None) -> str | None:
    """Reword text produced inside OpenMob so it names no internal service."""
    if message is None:
        return None
    if EXCEPTION_TEXT.match(message):
        return GENERIC_ERROR
    message = re.sub(r" \(matched by \w+\)", "", message)  # how an app name was resolved
    return INTERNAL_NAMES.sub("OpenMob", message)


def public_event(entry: dict) -> dict | None:
    """One log entry as the API shows it, or None for internal-only entries (usage, raw input)."""
    data = entry.get("data") or {}
    if data.get("final"):
        kind = "outcome"
    else:
        kind = PUBLIC_KINDS.get(entry["kind"])
        if kind is None:
            return None
    message = clean_text(entry["message"])
    if kind == "outcome":
        message = message.rsplit(" (", 1)[0] if message.endswith(" steps)") else message
    event = {"id": entry["seq"], "time": entry["time"], "type": kind, "message": message}
    if kind == "question":
        event["confirmation"] = bool(data.get("confirm"))
    if kind == "outcome":
        event["success"] = bool(data.get("success"))
    return event


def is_public_address(host: str) -> bool:
    """True only if every address the host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(infos)


def sign_webhook(secret: str, body: bytes, timestamp: int) -> str:
    """X-OpenMob-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


class TaskIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION)
    tier: str | None = None
    max_steps: int | None = Field(default=None, ge=1)
    memory: Literal["on", "read_only", "off"] = "on"  # use and/or add to the owner's pinned notes


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    app: str | None = None  # a package name: a note about that app


class AnswerIn(BaseModel):
    answer: str | None = Field(default=None, max_length=500)
    approve: bool | None = None  # for needs_confirmation: true = go ahead, false = don't


class KeyIn(BaseModel):
    name: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["devices:read", "tasks:run", "tasks:read"])


class SettingsIn(BaseModel):
    allow_sensitive_apps: bool


class WebhookIn(BaseModel):
    url: str = Field(max_length=500)


class SwitchIn(BaseModel):
    disabled: bool


class DeveloperApi:
    def __init__(self, manager, store, ledger, account_of: Callable[[Request], str | None],
                 require_admin, webhook_allow_private: bool = False):
        self.manager = manager
        self.cfg = manager.cfg
        self.store = store
        self.ledger = ledger
        self.account_of = account_of
        self.webhook_allow_private = webhook_allow_private  # tests deliver to 127.0.0.1
        # task id → {account_id, key_id, session_id, device_id, tier, credits_per_step}
        self.registry: OrderedDict[str, dict] = OrderedDict()
        self.active: dict[str, set[str]] = {}  # key id → its unfinished task ids
        self.hits: dict[str, deque] = {}  # key id → request times in the last minute
        self._lock = threading.Lock()
        self.router = self._v1_routes()
        self.console = self._console_routes()
        self.admin = self._admin_routes(require_admin)

    # --- auth -----------------------------------------------------------------------

    def _principal(self, request: Request) -> dict:
        header = request.headers.get("authorization", "")
        scheme, _, key = header.partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise HTTPException(401, "Send your API key as: Authorization: Bearer om_live_…",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            principal = self.store.verify_api_key(key.strip())
        except AccessDisabled as exc:
            raise HTTPException(403, str(exc)) from None
        except AuthError:
            raise HTTPException(401, "Invalid API key.", headers={"WWW-Authenticate": "Bearer"}) from None
        self._rate_limit(principal["id"])
        return principal

    def _rate_limit(self, key_id: str) -> None:
        now = time.time()
        with self._lock:
            hits = self.hits.setdefault(key_id, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.cfg.api_rate_per_minute:
                retry = max(1, int(hits[0] + 60 - now) + 1)
                raise HTTPException(429, "Rate limit reached. Slow down.", headers={"Retry-After": str(retry)})
            hits.append(now)

    def need(self, scope: str):
        def dependency(request: Request) -> dict:
            principal = self._principal(request)
            if scope not in principal["scopes"]:
                raise HTTPException(403, f"This API key lacks the {scope} scope.")
            return principal
        return dependency

    # --- lookups (everything resolves through the key's account) --------------------

    def _owned_device(self, account_id: str, device_id: str):
        """(stored device, live session or None) if this account owns the phone, else 404."""
        raw = device_id.removeprefix("app-")
        device = next((d for d in self.store.list_devices(account_id) if d.id == raw), None)
        if device is None:
            raise HTTPException(404, f"Unknown device {device_id}")
        session = self.manager.get(f"app-{raw}")
        if session is not None and session.account_id != account_id:
            session = None
        return device, session

    def _live(self, account_id: str, task_id: str) -> tuple[dict, dict | None] | None:
        """(registry entry, coordinator record) for a task this account created over the API."""
        with self._lock:
            meta = self.registry.get(task_id)
        if not meta or meta["account_id"] != account_id:
            return None
        session = self.manager.get(meta["session_id"])
        info = session.coordinator.task(task_id) if session and session.account_id == account_id else None
        return meta, info

    def _public_task(self, account_id: str, task_id: str) -> dict:
        live = self._live(account_id, task_id)
        if live and live[1]:
            meta, info = live
            credits = info["steps"] * meta["credits_per_step"] if info["status"] in FINAL_STATUSES else None
            return {
                "id": task_id, "device_id": meta["session_id"], "status": info["status"], "tier": meta["tier"],
                "instruction": info["request"], "created": info["created"], "started": info["started"],
                "finished": info["finished"], "steps": info["steps"], "question": clean_text(info["question"]),
                "outcome": ({"success": info["success"], "summary": clean_text(info["summary"])}
                            if info["status"] in FINAL_STATUSES else None),
                "credits": credits,
            }
        row = self.ledger.task(account_id, task_id)
        if row is None:
            raise HTTPException(404, f"Unknown task {task_id}")
        # Finished long ago or before a restart: the ledger has the numbers, the history the words.
        turn = self.manager.history.turn(account_id, task_id) if self.manager.history else None
        status = row["status"] if row["status"] in FINAL_STATUSES else "failed"  # lost in a restart
        return {
            "id": task_id, "device_id": f"app-{row['device_id']}", "status": status, "tier": row["tier"],
            "instruction": turn["request"] if turn else None, "created": row["created"],
            "started": turn["started"] if turn else None, "finished": row["finished"],
            "steps": row["steps"], "question": None,
            "outcome": {"success": status == "succeeded", "summary": clean_text(turn["summary"]) if turn else None},
            "credits": row["credits"],
        }

    def _with_live_status(self, account_id: str, row: dict) -> dict:
        """A ledger row only changes when its task finishes; fill in where a live task is now."""
        live = self._live(account_id, row["id"])
        if live and live[1] and live[1]["status"] not in FINAL_STATUSES:
            return {**row, "status": live[1]["status"], "steps": live[1]["steps"]}
        return row

    def _blocker(self, account_id: str):
        if self.store.settings(account_id)["allow_sensitive_apps"]:
            return None
        patterns = [re.compile(p, re.IGNORECASE) for p in self.cfg.sensitive_apps]
        return lambda package: any(p.search(package) for p in patterns)

    # --- task lifecycle hooks ----------------------------------------------------------

    def _watch(self, coordinator) -> None:
        if getattr(coordinator, "_api_watched", False):
            return
        coordinator._api_watched = True
        coordinator.entry_listeners.append(lambda entry, c=coordinator: self._on_entry(c, entry))

    def _on_entry(self, coordinator, entry: dict) -> None:
        task_id = entry.get("task")
        with self._lock:
            meta = self.registry.get(task_id) if task_id else None
        if meta is None:
            return
        data = entry.get("data") or {}
        if data.get("final"):
            info = coordinator.task(task_id) or {"status": "failed", "steps": data.get("steps", 0)}
            credits = info["steps"] * meta["credits_per_step"]
            with self._lock:
                self.active.get(meta["key_id"], set()).discard(task_id)
            self.ledger.finish_task(task_id, info["status"], info["steps"], credits)
            self.ledger.audit("task.finished", meta["account_id"], meta["key_id"], meta["device_id"], task_id,
                              outcome=info["status"])
            self._send_webhook(meta["account_id"], "task.finished", task_id)
        elif entry["kind"] == "usage":
            self.ledger.set_internal_usage(task_id, data)  # for billing reconciliation; never shown
        elif entry["kind"] == "question":
            self._send_webhook(meta["account_id"], "task.needs_confirmation" if data.get("confirm") else "task.needs_input",
                               task_id)

    def _send_webhook(self, account_id: str, event: str, task_id: str) -> None:
        hook = self.store.webhook(account_id)
        if not hook:
            return
        try:
            task = self._public_task(account_id, task_id)
        except HTTPException:
            return
        threading.Thread(target=self._deliver, args=(account_id, hook, event, task), daemon=True).start()

    def _deliver(self, account_id: str, hook: tuple[str, str], event: str, task: dict) -> None:
        url, secret = hook
        body = json.dumps({"type": event, "created": time.time(), "task": task}).encode()
        outcome = "failed"
        for attempt, wait in enumerate((0.0, *WEBHOOK_RETRIES)):
            time.sleep(wait)
            if not self._webhook_url_ok(url):  # checked at send time too (DNS can change)
                outcome = "blocked"
                break
            ts = int(time.time())
            try:
                r = httpx.post(url, content=body, timeout=WEBHOOK_TIMEOUT, follow_redirects=False, headers={
                    "Content-Type": "application/json", "User-Agent": "OpenMob-Webhooks/1",
                    "X-OpenMob-Event": event, "X-OpenMob-Signature": sign_webhook(secret, body, ts),
                })
                if 200 <= r.status_code < 300:
                    outcome = "delivered"
                    break
            except httpx.HTTPError:
                pass
        self.ledger.audit("webhook." + outcome, account_id, task_id=task["id"], outcome=event)

    def _webhook_url_ok(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.hostname or parsed.scheme not in ("https", "http"):
            return False
        if self.webhook_allow_private:
            return True
        return parsed.scheme == "https" and is_public_address(parsed.hostname)

    def cancel_tasks(self, key_id: str | None = None, account_id: str | None = None) -> int:
        """Kill switch: cancel every unfinished API task of a key or an account."""
        with self._lock:
            targets = [(t, m) for t, m in self.registry.items()
                       if (key_id and m["key_id"] == key_id) or (account_id and m["account_id"] == account_id)]
        count = 0
        for task_id, meta in targets:
            session = self.manager.get(meta["session_id"])
            if session and session.coordinator.cancel_task(task_id):
                count += 1
        return count

    # --- /v1 --------------------------------------------------------------------------------

    def _v1_routes(self) -> APIRouter:
        r = APIRouter(prefix="/v1")

        @r.get("/devices")
        def list_devices(p: dict = Depends(self.need("devices:read"))) -> dict:
            out = []
            for d in self.store.list_devices(p["account_id"]):
                session = self.manager.get(f"app-{d.id}")
                live = session is not None and session.account_id == p["account_id"]
                snap = session.coordinator.snapshot() if live else {}
                out.append({
                    "id": f"app-{d.id}", "name": d.model,
                    "platform": session.info.platform if live else "android",
                    "online": bool(live and session.online),
                    "busy": bool(live and snap.get("state") != "idle"),
                    "last_seen": d.last_seen,
                })
            return {"devices": out}

        @r.post("/devices/{device_id}/tasks", status_code=201)
        def create_task(device_id: str, body: TaskIn, p: dict = Depends(self.need("tasks:run"))) -> dict:
            account_id, key_id = p["account_id"], p["id"]
            _, session = self._owned_device(account_id, device_id)
            if session is None or not session.online:
                raise HTTPException(409, "That phone is offline. Open the OpenMob app on it.")
            tier = body.tier or self.cfg.api_default_tier
            if tier not in self.cfg.api_tiers:
                raise HTTPException(400, f"Unknown tier. Choose one of: {', '.join(self.cfg.api_tiers)}")
            max_steps = min(body.max_steps or self.cfg.api_max_steps, self.cfg.api_max_steps)
            with self._lock:
                if len(self.active.get(key_id, ())) >= self.cfg.api_max_concurrent_tasks:
                    raise HTTPException(429, f"This key already has {self.cfg.api_max_concurrent_tasks} unfinished "
                                             "tasks. Wait for one to finish or cancel it.")
            if self.ledger.tasks_today(key_id) >= self.cfg.api_daily_tasks:
                reset = int(day_start() + 86400 - time.time())
                raise HTTPException(429, "Daily task quota reached for this key.", headers={"Retry-After": str(reset)})

            coordinator = session.coordinator
            self._watch(coordinator)
            task_id = coordinator.new_task_id()
            tier_cfg = self.cfg.api_tiers[tier]
            meta = {"account_id": account_id, "key_id": key_id, "session_id": session.info.id,
                    "device_id": session.info.id.removeprefix("app-"), "tier": tier,
                    "credits_per_step": int(tier_cfg.get("credits_per_step", 1))}
            with self._lock:
                self.registry[task_id] = meta
                while len(self.registry) > KEEP_REGISTRY:
                    self.registry.popitem(last=False)
                self.active.setdefault(key_id, set()).add(task_id)
            self.ledger.start_task(task_id, account_id, key_id, meta["device_id"], tier, body.instruction)
            self.ledger.audit("task.create", account_id, key_id, meta["device_id"], task_id, instruction=body.instruction)
            # isolated: an API caller neither sees nor extends the phone chat's recent requests.
            options = {"model": tier_cfg["model"], "max_steps": max_steps, "memory": body.memory, "isolated": True}
            if blocked := self._blocker(account_id):
                options["blocked"] = blocked
            coordinator.enqueue(body.instruction, "api", options, task_id=task_id)
            return self._public_task(account_id, task_id)

        @r.get("/tasks")
        def list_tasks(limit: int = 20, p: dict = Depends(self.need("tasks:read"))) -> dict:
            rows = self.ledger.recent_tasks(p["account_id"], max(1, min(limit, 100)))
            return {"tasks": [self._public_task(p["account_id"], row["id"]) for row in rows]}

        @r.get("/tasks/{task_id}")
        def get_task(task_id: str, p: dict = Depends(self.need("tasks:read"))) -> dict:
            return self._public_task(p["account_id"], task_id)

        @r.post("/tasks/{task_id}/answer")
        def answer(task_id: str, body: AnswerIn, p: dict = Depends(self.need("tasks:run"))) -> dict:
            live = self._live(p["account_id"], task_id)
            if not live:
                self._public_task(p["account_id"], task_id)  # 404 if it isn't theirs
                raise HTTPException(409, "This task isn't waiting for an answer.")
            meta, info = live
            if not info or info["status"] not in ("needs_input", "needs_confirmation"):
                raise HTTPException(409, "This task isn't waiting for an answer.")
            if info["status"] == "needs_confirmation":
                if body.approve is None:
                    raise HTTPException(400, 'This task needs a confirmation: send {"approve": true} or {"approve": false}.')
                text = "yes" if body.approve else "no"
            else:
                text = (body.answer or "").strip()
                if not text:
                    raise HTTPException(400, 'Send {"answer": "..."}.')
            session = self.manager.get(meta["session_id"])
            if not session or not session.coordinator.answer(task_id, text):
                raise HTTPException(409, "This task isn't waiting for an answer.")
            self.ledger.audit("task.answer", p["account_id"], p["id"], meta["device_id"], task_id,
                              outcome=info["status"])
            return {"ok": True}

        @r.post("/tasks/{task_id}/cancel")
        def cancel(task_id: str, p: dict = Depends(self.need("tasks:run"))) -> dict:
            task = self._public_task(p["account_id"], task_id)  # 404 if it isn't theirs
            live = self._live(p["account_id"], task_id)
            session = self.manager.get(live[0]["session_id"]) if live else None
            cancelled = bool(session and session.coordinator.cancel_task(task_id))
            self.ledger.audit("task.cancel", p["account_id"], p["id"], task["device_id"].removeprefix("app-"),
                              task_id, outcome="cancelled" if cancelled else "already finished")
            return {"ok": True, "cancelled": cancelled}

        @r.get("/tasks/{task_id}/events")
        async def events(task_id: str, request: Request, p: dict = Depends(self.need("tasks:read"))):
            account_id = p["account_id"]
            await run_in_threadpool(self._public_task, account_id, task_id)  # 404 if it isn't theirs
            try:
                after = int(request.headers.get("last-event-id") or 0)
            except ValueError:
                after = 0

            async def stream():
                sent, quiet = after, 0.0
                while not await request.is_disconnected():
                    live = self._live(account_id, task_id)
                    info = live[1] if live else None
                    for entry in (info or {}).get("events", []):
                        if entry["seq"] <= sent:
                            continue
                        sent = entry["seq"]
                        if event := public_event(entry):
                            quiet = 0.0
                            yield f"id: {sent}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    if info is None or info["status"] in FINAL_STATUSES:
                        task = await run_in_threadpool(self._public_task, account_id, task_id)
                        yield f"event: done\ndata: {json.dumps(task)}\n\n"
                        return
                    await asyncio.sleep(SSE_POLL)
                    quiet += SSE_POLL
                    if quiet >= SSE_HEARTBEAT:
                        quiet = 0.0
                        yield ": keep-alive\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @r.get("/devices/{device_id}/screenshot")
        def screenshot(device_id: str, p: dict = Depends(self.need("screen:read"))) -> Response:
            device, session = self._owned_device(p["account_id"], device_id)
            if session is None or not session.online:
                raise HTTPException(409, "That phone is offline. Open the OpenMob app on it.")
            try:
                jpeg = session.driver.screenshot(max_age=1.0).jpeg
            except DeviceError:
                raise HTTPException(503, "The phone couldn't capture its screen right now.") from None
            self.ledger.audit("screen.read", p["account_id"], p["id"], device.id)
            # Never stored, never cached: it goes straight from the phone to the caller.
            return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        def store():
            if self.manager.memory is None:
                raise HTTPException(501, "Memory isn't enabled on this server.")
            return self.manager.memory

        @r.get("/memory")
        def list_memory(p: dict = Depends(self.need("memory:read"))) -> dict:
            notes = store().list(p["account_id"])
            return {"notes": [{"id": n["id"], "text": n["text"], "app": n["package"], "status": n["status"],
                               "created": n["created"], "uses": n["uses"]} for n in notes]}

        @r.post("/memory", status_code=201)
        def add_memory(body: NoteIn, p: dict = Depends(self.need("memory:write"))) -> dict:
            from .memory import NoteError

            try:  # owner keys only today, so the note is active (third parties will only propose)
                note = store().add(p["account_id"], body.text, package=body.app, source="api")
            except NoteError as exc:
                raise HTTPException(400, str(exc)) from None
            self.ledger.audit("memory.add", p["account_id"], p["id"], instruction=body.text)
            return {"id": note["id"], "text": note["text"], "app": note["package"], "status": note["status"]}

        @r.delete("/memory/{note_id}")
        def delete_memory(note_id: str, p: dict = Depends(self.need("memory:write"))) -> dict:
            if not store().delete(p["account_id"], note_id):
                raise HTTPException(404, f"Unknown note {note_id}")
            self.ledger.audit("memory.delete", p["account_id"], p["id"])
            return {"ok": True}

        @r.get("/history")
        def history(limit: int = 50, p: dict = Depends(self.need("history:read"))) -> dict:
            if self.manager.history is None:
                return {"turns": []}
            turns = self.manager.history.recent_turns(p["account_id"], max(1, min(limit, 200)))
            return {"turns": [{
                "task_id": t["id"], "device_id": t["device_id"], "instruction": t["request"],
                "started": t["started"], "finished": t["finished"], "steps": t["steps"],
                "success": None if t["success"] is None else bool(t["success"]), "summary": clean_text(t["summary"]),
            } for t in turns]}

        return r

    # --- developer console (signed-in owner, cookie session) ----------------------------

    def _console_routes(self) -> APIRouter:
        r = APIRouter(prefix="/api/developer")

        def owner(request: Request) -> str:
            account_id = self.account_of(request)
            if account_id is None:
                raise HTTPException(401, "Sign in to OpenMob first.")
            return account_id

        @r.get("/keys")
        def keys(account_id: str = Depends(owner)) -> list[dict]:
            return self.store.list_api_keys(account_id)

        @r.post("/keys")
        def create_key(body: KeyIn, account_id: str = Depends(owner)) -> dict:
            if self.store.api_key_count(account_id) >= MAX_KEYS_PER_ACCOUNT:
                raise HTTPException(400, f"You can have at most {MAX_KEYS_PER_ACCOUNT} keys. Revoke one first.")
            try:
                key_id, key = self.store.create_api_key(account_id, body.name, body.scopes)
            except AuthError as exc:
                raise HTTPException(400, str(exc)) from None
            self.ledger.audit("key.create", account_id, key_id, outcome=",".join(sorted(set(body.scopes))))
            created = next(k for k in self.store.list_api_keys(account_id) if k["id"] == key_id)
            return {**created, "key": key}  # the only time the full key is ever shown

        @r.post("/keys/{key_id}/revoke")
        def revoke_key(key_id: str, account_id: str = Depends(owner)) -> dict:
            if not self.store.revoke_api_key(account_id, key_id):
                raise HTTPException(404, "Unknown key.")
            self.cancel_tasks(key_id=key_id)
            self.ledger.audit("key.revoke", account_id, key_id)
            return {"ok": True}

        @r.post("/keys/{key_id}/rotate")
        def rotate_key(key_id: str, account_id: str = Depends(owner)) -> dict:
            new = self.store.rotate_api_key(account_id, key_id)
            if new is None:
                raise HTTPException(404, "Unknown key.")
            self.ledger.audit("key.rotate", account_id, new[0], outcome=f"replaces {key_id}")
            created = next(k for k in self.store.list_api_keys(account_id) if k["id"] == new[0])
            return {**created, "key": new[1]}

        @r.get("/overview")
        def overview(account_id: str = Depends(owner)) -> dict:
            now = time.time()
            return {
                "scopes": list(SCOPES),
                "tiers": [{"name": n, "credits_per_step": int(t.get("credits_per_step", 1)),
                           "default": n == self.cfg.api_default_tier} for n, t in self.cfg.api_tiers.items()],
                "limits": {"requests_per_minute": self.cfg.api_rate_per_minute,
                           "concurrent_tasks": self.cfg.api_max_concurrent_tasks,
                           "tasks_per_day": self.cfg.api_daily_tasks, "max_steps": self.cfg.api_max_steps},
                "usage": {"today": self.ledger.usage(account_id, day_start(now)),
                          "last_30_days": self.ledger.usage(account_id, now - 30 * 86400)},
                "settings": self.store.settings(account_id),
                "recent_tasks": [self._with_live_status(account_id, row) for row in self.ledger.recent_tasks(account_id, 25)],
            }

        @r.post("/settings")
        def settings(body: SettingsIn, account_id: str = Depends(owner)) -> dict:
            self.store.set_allow_sensitive_apps(account_id, body.allow_sensitive_apps)
            self.ledger.audit("settings.sensitive_apps", account_id, outcome="allowed" if body.allow_sensitive_apps else "blocked")
            return self.store.settings(account_id)

        @r.post("/webhook")
        def set_webhook(body: WebhookIn, account_id: str = Depends(owner)) -> dict:
            url = body.url.strip()
            if not self._webhook_url_ok(url):
                raise HTTPException(400, "Use a public https:// URL for webhooks.")
            secret = self.store.set_webhook(account_id, url)
            self.ledger.audit("webhook.set", account_id)
            return {"url": url, "secret": secret}  # shown once, used to verify signatures

        @r.delete("/webhook")
        def clear_webhook(account_id: str = Depends(owner)) -> dict:
            self.store.set_webhook(account_id, None)
            self.ledger.audit("webhook.clear", account_id)
            return {"ok": True}

        return r

    # --- admin: kill switches and the audit log -------------------------------------------

    def _admin_routes(self, require_admin) -> APIRouter:
        r = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

        @r.get("/api-keys")
        def api_keys() -> list[dict]:
            return self.store.admin_list_api_keys()

        @r.post("/api-keys/{key_id}/disable")
        def disable_key(key_id: str, body: SwitchIn) -> dict:
            account_id = self.store.admin_set_key_disabled(key_id, body.disabled)
            if account_id is None:
                raise HTTPException(404, "Unknown key.")
            stopped = self.cancel_tasks(key_id=key_id) if body.disabled else 0
            self.ledger.audit("admin.key." + ("disable" if body.disabled else "enable"), account_id, key_id)
            return {"ok": True, "cancelled_tasks": stopped}

        @r.post("/users/{account_id}/api")
        def disable_account_api(account_id: str, body: SwitchIn) -> dict:
            if not self.store.admin_set_api_disabled(account_id, body.disabled):
                raise HTTPException(404, "Unknown account.")
            stopped = self.cancel_tasks(account_id=account_id) if body.disabled else 0
            self.ledger.audit("admin.account_api." + ("disable" if body.disabled else "enable"), account_id)
            return {"ok": True, "cancelled_tasks": stopped}

        @r.get("/audit")
        def audit(limit: int = 200) -> list[dict]:
            return self.ledger.audit_log(max(1, min(limit, 1000)))

        return r
