"""REST front door: the public /v1 API, the developer console's endpoints and the admin kill switches.

Routes only parse HTTP and call V1Service; its domain errors become HTTP responses in
`service_error_response` ({"detail": …}, plus "code" when the error has one).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AfterValidator, BaseModel, Field

from ..accounts import SCOPES, AuthError
from ..ledger import day_start
from ..runner import FINAL_STATUSES
from .errors import ServiceError
from .principal import Principal, bearer_token
from .service import MAX_INSTRUCTION, V1Service, public_event

MAX_KEYS_PER_ACCOUNT = 20
SESSION_SCAN = 1000  # newest API tasks a sessions filter looks through
LOG_SCAN = 2000  # newest audit rows an action filter looks through
SSE_POLL = 0.25
SSE_HEARTBEAT = 15.0


def service_error_response(request: Request, exc: ServiceError) -> JSONResponse:
    body = {"detail": exc.message}
    if exc.code:
        body["code"] = exc.code
    body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status, headers=exc.headers or None)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must contain more than whitespace")
    return value


NonBlank = Annotated[str, AfterValidator(_not_blank)]  # whitespace-only is a 422, like an empty string
PermissionMode = Literal["ask", "plan", "auto", "trusted", "never"]


class TaskIn(BaseModel):
    instruction: NonBlank = Field(min_length=1, max_length=MAX_INSTRUCTION)
    tier: str | None = None
    max_steps: int | None = Field(default=None, ge=1)
    # When the phone asks its owner; ≤ the key's max_mode and the phone's ceiling. None: the phone's default.
    permission_mode: PermissionMode | None = None


class AnswerIn(BaseModel):
    answer: str | None = Field(default=None, max_length=500)
    approve: bool | None = None  # for needs_confirmation: true = go ahead, false = don't


class KeyIn(BaseModel):
    name: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["devices:read", "tasks:run", "tasks:read"])
    # Off by default: risky actions are approved by the owner (on the phone), not by the caller.
    confirm_approve: bool = False
    max_mode: PermissionMode = "auto"  # the loosest permission mode this key's tasks may ask for


class KeyPatch(BaseModel):
    confirm_approve: bool | None = None
    max_mode: PermissionMode | None = None
    # Drive sessions of this key may use Faster mode on phones that have it on (off by default).
    faster_drive: bool | None = None


class PermissionsPatch(BaseModel):
    default_mode: PermissionMode | None = None
    ceiling: Literal["ask", "plan", "auto", "trusted"] | None = None
    trusted: list[dict] | None = None
    never: Literal["1h", "24h", "7d", "off"] | None = None
    allow_money: bool | None = None  # with never: purchases too (a loosening: 202 needs_phone_approval)
    # Android permission dialogs (answered by rule): {type: setting}; a loosening is approved on the phone
    permission_prompts: dict[str, str] | None = None
    task_approval: bool | None = None  # one phone approval per task covers its non-money risky steps
    everyday: bool | None = None  # don't confirm everyday actions (turning it on is approved on the phone)
    faster_mode: Literal["off"] | None = None  # Faster mode: a key can only turn it off (on is the phone's own)


class SettingsIn(BaseModel):
    allow_sensitive_apps: bool


class WebhookIn(BaseModel):
    url: str = Field(max_length=500)


class SwitchIn(BaseModel):
    disabled: bool


class DeveloperApi:
    """The HTTP routes. `service` holds every rule; this class only translates."""

    def __init__(self, service: V1Service, account_of: Callable[[Request], str | None], require_admin):
        self.service = service
        self.store = service.store
        self.ledger = service.ledger
        self.cfg = service.cfg
        self.account_of = account_of
        self.webhook_allow_private = service.webhook_allow_private
        self.router = self._v1_routes()
        self.console = self._console_routes()
        self.admin = self._admin_routes(require_admin)

    def _webhook_url_ok(self, url: str) -> bool:
        from .service import webhook_url_ok

        return webhook_url_ok(url, self.webhook_allow_private)

    def need(self, scope: str):
        """Dependency: authenticate the bearer key (rate-limited) and require one scope, before
        the body is even validated, so a key without the scope learns nothing more."""
        def dependency(request: Request) -> Principal:
            principal = self.service.authenticate(bearer_token(request.headers.get("authorization")))
            self.service.require(principal, scope)
            return principal
        return dependency

    # --- /v1 --------------------------------------------------------------------------------

    def _v1_routes(self) -> APIRouter:
        r = APIRouter(prefix="/v1")
        s = self.service

        @r.get("/devices")
        def list_devices(p: Principal = Depends(self.need("devices:read"))) -> dict:
            return s.list_devices(p)

        @r.post("/devices/{device_id}/tasks", status_code=201)
        def create_task(device_id: str, body: TaskIn, response: Response, p: Principal = Depends(self.need("tasks:run")),
                        idempotency_key: str | None = Header(default=None)) -> dict:
            request = {"device_id": device_id, **body.model_dump()}
            task, replayed = s.idempotent(p, idempotency_key, request, lambda: s.create_task(
                p, device_id, body.instruction, body.tier, body.max_steps,
                permission_mode=body.permission_mode))
            if replayed:  # a retry with the same Idempotency-Key and body: the original response, as 200
                response.status_code = 200
                response.headers["Idempotent-Replayed"] = "true"
            return task

        @r.get("/devices/{device_id}/permissions")
        def get_permissions(device_id: str, p: Principal = Depends(self.need("devices:read"))) -> dict:
            return s.get_permissions(p, device_id)

        @r.patch("/devices/{device_id}/permissions")
        def patch_permissions(device_id: str, body: PermissionsPatch,
                              p: Principal = Depends(self.need("tasks:run"))) -> Response:
            result = s.change_permissions(p, device_id, body.model_dump(exclude_none=True))
            return JSONResponse(result, status_code=200 if result["status"] == "applied" else 202)

        @r.get("/devices/{device_id}/permissions/approvals/{approval_id}")
        def permission_approval(device_id: str, approval_id: str,
                                p: Principal = Depends(self.need("devices:read"))) -> dict:
            return s.permission_approval(p, device_id, approval_id)

        @r.get("/limits")
        def limits(p: Principal = Depends(self.need("tasks:read"))) -> dict:
            return s.limits(p)

        @r.get("/tasks")
        def list_tasks(limit: int = 20, cursor: str | None = None, p: Principal = Depends(self.need("tasks:read"))) -> dict:
            return s.list_tasks(p, limit, cursor)

        @r.get("/tasks/{task_id}")
        def get_task(task_id: str, p: Principal = Depends(self.need("tasks:read"))) -> dict:
            return s.get_task(p, task_id)

        @r.post("/tasks/{task_id}/answer")
        def answer(task_id: str, body: AnswerIn, p: Principal = Depends(self.need("tasks:run"))) -> dict:
            return s.answer(p, task_id, body.answer, body.approve)

        @r.post("/tasks/{task_id}/cancel")
        def cancel(task_id: str, p: Principal = Depends(self.need("tasks:run"))) -> dict:
            return s.cancel(p, task_id)

        @r.get("/tasks/{task_id}/events")
        async def events(task_id: str, request: Request, p: Principal = Depends(self.need("tasks:read"))):
            account_id = p.account_id
            await run_in_threadpool(s.check_task, p, task_id)  # 404 unless this key created it
            try:
                after = int(request.headers.get("last-event-id") or 0)
            except ValueError:
                after = 0

            async def stream():
                sent, quiet = after, 0.0
                while not await request.is_disconnected():
                    entries, finished = s.live_events(account_id, task_id)
                    for entry in entries:
                        if entry["seq"] <= sent:
                            continue
                        sent = entry["seq"]
                        if event := public_event(entry):
                            quiet = 0.0
                            yield f"id: {sent}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    if finished:
                        task = await run_in_threadpool(s.public_task, account_id, task_id)
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
        def screenshot(device_id: str, p: Principal = Depends(self.need("screen:read"))) -> Response:
            # Never stored, never cached: it goes straight from the phone to the caller.
            return Response(s.screenshot(p, device_id), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        @r.get("/history")
        def history(limit: int = 50, cursor: str | None = None, p: Principal = Depends(self.need("history:read"))) -> dict:
            return s.history(p, limit, cursor)

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
                key_id, key = self.store.create_api_key(account_id, body.name, body.scopes, body.confirm_approve,
                                                        body.max_mode)
            except AuthError as exc:
                raise HTTPException(400, str(exc)) from None
            self.ledger.audit("key.create", account_id, key_id, outcome=",".join(sorted(set(body.scopes))))
            if body.confirm_approve:
                self.ledger.audit("key.confirm_approve", account_id, key_id, outcome="on")
            if body.max_mode != "auto":
                self.ledger.audit("key.max_mode", account_id, key_id, outcome=body.max_mode)
            created = next(k for k in self.store.list_api_keys(account_id) if k["id"] == key_id)
            return {**created, "key": key}  # the only time the full key is ever shown

        @r.patch("/keys/{key_id}")
        def update_key(key_id: str, body: KeyPatch, account_id: str = Depends(owner)) -> dict:
            if not any(k["id"] == key_id for k in self.store.list_api_keys(account_id)):
                raise HTTPException(404, "Unknown key.")
            if body.confirm_approve is not None:
                self.store.set_key_confirm_approve(account_id, key_id, body.confirm_approve)
                self.ledger.audit("key.confirm_approve", account_id, key_id,
                                  outcome="on" if body.confirm_approve else "off")
            if body.max_mode is not None:
                self.store.set_key_max_mode(account_id, key_id, body.max_mode)
                self.ledger.audit("key.max_mode", account_id, key_id, outcome=body.max_mode)
            if body.faster_drive is not None:
                self.store.set_key_faster_drive(account_id, key_id, body.faster_drive)
                self.ledger.audit("key.faster_drive", account_id, key_id, outcome="on" if body.faster_drive else "off")
            return next(k for k in self.store.list_api_keys(account_id) if k["id"] == key_id)

        @r.post("/keys/{key_id}/revoke")
        def revoke_key(key_id: str, account_id: str = Depends(owner)) -> dict:
            if not self.store.revoke_api_key(account_id, key_id):
                raise HTTPException(404, "Unknown key.")
            self.service.cancel_tasks(key_id=key_id)
            self.ledger.audit("key.revoke", account_id, key_id)
            return {"ok": True}

        @r.post("/keys/{key_id}/rotate")
        def rotate_key(key_id: str, account_id: str = Depends(owner)) -> dict:
            new = self.store.rotate_api_key(account_id, key_id)
            if new is None:
                raise HTTPException(404, "Unknown key.")
            self.ledger.audit("key.rotate", account_id, key_id, outcome="new secret; same key, limits and tasks")
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
                           "tasks_per_day": self.cfg.api_daily_tasks, "max_steps": self.cfg.api_max_steps,
                           "credits_per_day": self.cfg.api_daily_credits or None,
                           "resets_at": day_start(now) + 86400,  # daily limits reset at midnight UTC
                           "account_tasks_per_day": self.cfg.api_account_daily_tasks,  # all keys together
                           "account_credits_per_day": self.cfg.api_account_daily_credits or None},
                "usage": {"today": self.ledger.usage(account_id, day_start(now)),
                          "last_30_days": self.ledger.usage(account_id, now - 30 * 86400)},
                "settings": self.store.settings(account_id),
                "recent_tasks": [self.service.with_live_status(account_id, row)
                                 for row in self.ledger.recent_tasks(account_id, 25)],
            }

        # --- sessions (this account's API/MCP tasks) and its own audit log ----------------

        def key_names(account_id: str) -> dict[str, str]:
            return {k["id"]: k["name"] for k in self.store.list_api_keys(account_id)}  # revoked keys: no name

        def phone_names(account_id: str) -> dict[str, str]:
            return {f"app-{d.id}": d.model for d in self.store.list_devices(account_id)}

        def session_status(account_id: str, row: dict) -> str:
            """The live status while the task runs; a ledger status otherwise (unfinished = lost in a restart)."""
            live = self.service._live(account_id, row["id"])
            if live and live[1]:
                return live[1]["status"]
            return row["status"] if row["status"] in FINAL_STATUSES else "failed"

        def session(account_id: str, task_id: str, key_id: str | None, keys: dict, phones: dict) -> dict:
            task = self.service.public_task(account_id, task_id)  # NotFound unless it's this account's
            return {**task, "key_id": key_id, "key_name": keys.get(key_id),
                    "device_name": phones.get(task["device_id"])}

        @r.get("/sessions")
        def sessions(limit: int = 50, status: str | None = None, key_id: str | None = None,
                     account_id: str = Depends(owner)) -> dict:
            rows = self.ledger.recent_tasks(account_id, SESSION_SCAN)
            if key_id:
                rows = [row for row in rows if row["key_id"] == key_id]
            if status:
                rows = [row for row in rows if session_status(account_id, row) == status]
            keys, phones = key_names(account_id), phone_names(account_id)
            return {"sessions": [session(account_id, row["id"], row["key_id"], keys, phones)
                                 for row in rows[:max(1, min(limit, 200))]]}

        @r.get("/sessions/{task_id}")
        def session_detail(task_id: str, account_id: str = Depends(owner)) -> dict:
            row = self.ledger.task(account_id, task_id)
            if row is None:
                raise HTTPException(404, f"Unknown task {task_id}")
            detail = session(account_id, task_id, row["key_id"], key_names(account_id), phone_names(account_id))
            return {**detail, "events": session_events(account_id, task_id)}

        def session_events(account_id: str, task_id: str) -> list[dict]:
            """The task's public events: live while it's in memory, else from the owner's chat history."""
            entries, _ = self.service.live_events(account_id, task_id)
            history = getattr(self.service.manager, "history", None)
            if not entries and history is not None:
                turn = history.turn(account_id, task_id)
                logged = history.events(account_id, turn["chat_id"]) if turn else None
                entries = [{**e, "seq": i} for i, e in enumerate(logged or [], 1) if e["task"] == task_id]
            return [event for entry in entries if (event := public_event(entry))]

        @r.get("/logs")
        def logs(limit: int = 200, action: str | None = None, account_id: str = Depends(owner)) -> dict:
            rows = self.ledger.audit_log(LOG_SCAN if action else max(1, min(limit, 1000)), account_id=account_id)
            if action:
                rows = [row for row in rows if row["action"] == action or row["action"].startswith(action + ".")]
            keys = key_names(account_id)
            return {"logs": [{"id": row["id"], "time": row["time"], "action": row["action"], "key_id": row["key_id"],
                              "key_name": keys.get(row["key_id"]), "device_id": row["device_id"],
                              "task_id": row["task_id"], "outcome": row["outcome"]}
                             for row in rows[:max(1, min(limit, 1000))]]}

        @r.post("/settings")
        def settings(body: SettingsIn, account_id: str = Depends(owner)) -> dict:
            self.store.set_allow_sensitive_apps(account_id, body.allow_sensitive_apps)
            self.ledger.audit("settings.sensitive_apps", account_id, outcome="allowed" if body.allow_sensitive_apps else "blocked")
            return self.store.settings(account_id)

        @r.post("/webhook")
        def set_webhook(body: WebhookIn, account_id: str = Depends(owner)) -> dict:
            url = body.url.strip()
            if not self.service.webhook_url_ok(url):
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
            stopped = self.service.cancel_tasks(key_id=key_id) if body.disabled else 0
            self.ledger.audit("admin.key." + ("disable" if body.disabled else "enable"), account_id, key_id)
            return {"ok": True, "cancelled_tasks": stopped}

        @r.post("/users/{account_id}/api")
        def disable_account_api(account_id: str, body: SwitchIn) -> dict:
            if not self.store.admin_set_api_disabled(account_id, body.disabled):
                raise HTTPException(404, "Unknown account.")
            stopped = self.service.cancel_tasks(account_id=account_id) if body.disabled else 0
            self.ledger.audit("admin.account_api." + ("disable" if body.disabled else "enable"), account_id)
            return {"ok": True, "cancelled_tasks": stopped}

        @r.get("/audit")
        def audit(limit: int = 200) -> list[dict]:
            return self.ledger.audit_log(max(1, min(limit, 1000)))

        return r
