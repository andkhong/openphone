"""V1Service: the developer platform's one enforcement point, free of any transport.

Security model, enforced here once for every front door (REST /v1, MCP /mcp, SDKs):
- A Principal (see principal.py) → account → device. Anything not this account's is NotFound, so
  other people's phones and tasks can't even be confirmed to exist.
- Each operation needs a scope; screen:read is opt-in.
- Per-key rate limit, concurrent-task cap and daily quota.
- Every action goes to the append-only audit log; every task to the usage ledger.
- API tasks may not open sensitive apps (banking, payments, passwords) unless the owner allows it.

Results speak OpenMob only: tiers, steps, credits. Which models or classifiers run behind a tier,
internal event kinds and per-provider token counts never appear, and upstream errors are reworded.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlparse

from ..ledger import day_start
from ..phone.base import DeviceError
from ..runner import FINAL_STATUSES
from .errors import BadRequest, Conflict, Forbidden, NotEnabled, NotFound, RateLimited, Unavailable
from .principal import Principal, resolve
from .webhooks import WebhookOutbox

MAX_INSTRUCTION = 2000
KEEP_REGISTRY = 10_000  # live task → owner lookups kept in memory (older ones come from the ledger)

# Internal names that must never reach a developer: model families, providers, classifiers.
INTERNAL_NAMES = re.compile(
    r"\b(claude[\w.-]*|anthropic|sonnet|opus|haiku|fable|jev[\w-]*|typesafe|whisper|openai|gpt-[\w.-]+"
    r"|o[134](?:-[\w.-]+)?|gemini[\w.-]*|google[ -]?(?:ai|deepmind|vertex)[\w-]*|vertex ai|googleapis[\w.]*"
    r"|deepmind)\b",
    re.IGNORECASE,
)
# Plus the exact model ids this server is configured with (any provider, local models too).
_configured_names: re.Pattern | None = None


def register_model_names(names) -> None:
    """Scrub these configured model ids from public text too (e.g. a self-hosted "llama-3.3-70b")."""
    global _configured_names
    names = sorted({n for n in names if n}, key=len, reverse=True)
    _configured_names = (re.compile("|".join(rf"(?<![\w.-]){re.escape(n)}(?![\w-])" for n in names), re.IGNORECASE)
                         if names else None)
EXCEPTION_TEXT = re.compile(r"^[A-Za-z_.]*(Error|Exception)\b")
GENERIC_ERROR = "The task hit an internal error."
PUBLIC_KINDS = {"status": "status", "thought": "thought", "action": "action", "error": "warning", "question": "question",
                "memory": "memory"}
OFFLINE = "That phone is offline. Open the OpenMob app on it."
NOT_WAITING = "This task isn't waiting for an answer."
OWNER_APPROVES = ("Risky actions are approved by the phone's owner, who has been asked on their phone. "
                  'Check the task again later, or send {"approve": false} to decline.')


def clean_text(message: str | None) -> str | None:
    """Reword text produced inside OpenMob so it names no internal service."""
    if message is None:
        return None
    if EXCEPTION_TEXT.match(message):
        return GENERIC_ERROR
    message = re.sub(r" \(matched by \w+\)", "", message)  # how an app name was resolved
    if _configured_names is not None:
        message = _configured_names.sub("OpenMob", message)
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


def webhook_url_ok(url: str, allow_private: bool = False) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname or parsed.scheme not in ("https", "http"):
        return False
    if allow_private:
        return True
    return parsed.scheme == "https" and is_public_address(parsed.hostname)


def sign_webhook(secret: str, body: bytes, timestamp: int) -> str:
    """X-OpenMob-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


class V1Service:
    def __init__(self, manager, store, ledger, webhook_allow_private: bool = False):
        self.manager = manager
        self.cfg = manager.cfg
        self.store = store
        self.ledger = ledger
        self.webhook_allow_private = webhook_allow_private  # tests deliver to 127.0.0.1
        # task id → {account_id, key_id, session_id, device_id, tier, credits_per_step, requester, confirm_approve}
        self.registry: OrderedDict[str, dict] = OrderedDict()
        self.active: dict[str, set[str]] = {}  # key id → its unfinished task ids
        self.hits: dict[str, deque] = {}  # key id → request times in the last minute
        self._lock = threading.Lock()
        self.webhooks = WebhookOutbox(ledger, store, self.webhook_url_ok, sign_webhook)
        self.webhooks.start()
        register_model_names([m["id"] for m in self.cfg.models] + [t["model"] for t in self.cfg.api_tiers.values()])
        manager.task_requesters.append(self.requester)  # the phone shows who started a task
        manager.approval_listeners.append(self._on_phone_approval)

    # --- auth ---------------------------------------------------------------------------

    def authenticate(self, token: str | None) -> Principal:
        """Resolve a bearer token and count the request against the key's rate limit."""
        principal = resolve(self.store, token)
        self._rate_limit(principal.id)
        return principal

    def _rate_limit(self, key_id: str) -> None:
        now = time.time()
        with self._lock:
            hits = self.hits.setdefault(key_id, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.cfg.api_rate_per_minute:
                raise RateLimited("Rate limit reached. Slow down.", retry_after=max(1, int(hits[0] + 60 - now) + 1))
            hits.append(now)

    @staticmethod
    def require(principal: Principal, scope: str) -> None:
        if not principal.has(scope):
            raise Forbidden(f"This API key lacks the {scope} scope.", code="insufficient_scope")

    # --- lookups (everything resolves through the principal's account) ------------------

    def _owned_device(self, account_id: str, device_id: str):
        """(stored device, live session or None) if this account owns the phone, else NotFound."""
        raw = device_id.removeprefix("app-")
        device = next((d for d in self.store.list_devices(account_id) if d.id == raw), None)
        if device is None:
            raise NotFound(f"Unknown device {device_id}")
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

    def public_task(self, account_id: str, task_id: str) -> dict:
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
            raise NotFound(f"Unknown task {task_id}")
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

    def with_live_status(self, account_id: str, row: dict) -> dict:
        """A ledger row only changes when its task finishes; fill in where a live task is now."""
        live = self._live(account_id, row["id"])
        if live and live[1] and live[1]["status"] not in FINAL_STATUSES:
            return {**row, "status": live[1]["status"], "steps": live[1]["steps"]}
        return row

    def live_events(self, account_id: str, task_id: str) -> tuple[list[dict], bool]:
        """(log entries so far, finished?) of a task this account owns; ([], True) once it's gone."""
        live = self._live(account_id, task_id)
        info = live[1] if live else None
        if info is None:
            return [], True
        return info.get("events", []), info["status"] in FINAL_STATUSES

    def _blocker(self, account_id: str):
        if self.store.settings(account_id)["allow_sensitive_apps"]:
            return None
        patterns = [re.compile(p, re.IGNORECASE) for p in self.cfg.sensitive_apps]
        return lambda package: any(p.search(package) for p in patterns)

    # --- task lifecycle hooks -------------------------------------------------------------

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
            task = self.public_task(account_id, task_id)
        except NotFound:
            return
        # The task as it was at this event, frozen into the outbox; the worker sends and retries it.
        self.webhooks.enqueue(account_id, event, task_id, json.dumps({"type": event, "created": time.time(), "task": task}))

    def requester(self, task_id: str) -> dict | None:
        """Who started an API task, for the phone: {"name", "human"} (human: only the owner may
        approve its risky actions). None for tasks the API didn't start."""
        with self._lock:
            meta = self.registry.get(task_id)
        if meta is None:
            return None
        return {"name": meta["requester"], "human": not meta["confirm_approve"]}

    def _on_phone_approval(self, task_id: str, approved: bool) -> None:
        with self._lock:
            meta = self.registry.get(task_id)
        if meta:
            self.ledger.audit("task.phone_approval", meta["account_id"], meta["key_id"], meta["device_id"], task_id,
                              outcome="approved" if approved else "denied")

    def task_credits(self, account_id: str, task_id: str) -> int:
        """Credits a task has used so far: steps × its tier's rate while running, the ledger's figure after."""
        live = self._live(account_id, task_id)
        if live and live[1]:
            return live[1]["steps"] * live[0]["credits_per_step"]
        row = self.ledger.task(account_id, task_id)
        return row["credits"] if row else 0

    def credits_today(self, key_id: str) -> int:
        """This key's credits today: finished tasks from the ledger, plus what running ones have used."""
        with self._lock:
            running = [(t, self.registry[t]["account_id"]) for t in self.active.get(key_id, ()) if t in self.registry]
        return self.ledger.credits_today(key_id) + sum(self.task_credits(a, t) for t, a in running)

    def stop_task(self, account_id: str, task_id: str) -> bool:
        """Cancel an API task of this account on the platform's own authority (kill switches, orchestrations)."""
        live = self._live(account_id, task_id)
        session = self.manager.get(live[0]["session_id"]) if live else None
        return bool(session and session.coordinator.cancel_task(task_id))

    def webhook_url_ok(self, url: str) -> bool:
        return webhook_url_ok(url, self.webhook_allow_private)

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

    # --- operations ---------------------------------------------------------------------------

    def list_devices(self, p: Principal) -> dict:
        self.require(p, "devices:read")
        out = []
        for d in self.store.list_devices(p.account_id):
            session = self.manager.get(f"app-{d.id}")
            live = session is not None and session.account_id == p.account_id
            snap = session.coordinator.snapshot() if live else {}
            out.append({
                "id": f"app-{d.id}", "name": d.model,
                "platform": session.info.platform if live else "android",
                "online": bool(live and session.online),
                "busy": bool(live and snap.get("state") != "idle"),
                "last_seen": d.last_seen,
            })
        return {"devices": out}

    def only_device(self, p: Principal, scope: str = "tasks:run") -> str:
        """The caller's phone when exactly one is online, so callers can leave device_id out."""
        self.require(p, scope)
        online = []
        for d in self.store.list_devices(p.account_id):
            session = self.manager.get(f"app-{d.id}")
            if session is not None and session.account_id == p.account_id and session.online:
                online.append(f"app-{d.id}")
        if len(online) == 1:
            return online[0]
        if not online:
            raise Conflict("None of your phones is online. Open the OpenMob app on one.")
        raise BadRequest(f"Several phones are online; pass device_id, one of: {', '.join(online)}")

    def create_task(self, p: Principal, device_id: str, instruction: str, tier: str | None = None,
                    max_steps: int | None = None, memory: str = "on") -> dict:
        self.require(p, "tasks:run")
        account_id, key_id = p.account_id, p.id
        _, session = self._owned_device(account_id, device_id)
        if session is None or not session.online:
            raise Conflict(OFFLINE)
        if not instruction or len(instruction) > MAX_INSTRUCTION:  # REST validates this before it gets here
            raise BadRequest(f"The instruction must be 1 to {MAX_INSTRUCTION} characters.")
        if memory not in ("on", "read_only", "off"):
            raise BadRequest("memory must be one of: on, read_only, off.")
        tier = tier or self.cfg.api_default_tier
        if tier not in self.cfg.api_tiers:
            raise BadRequest(f"Unknown tier. Choose one of: {', '.join(self.cfg.api_tiers)}")
        max_steps = min(max_steps or self.cfg.api_max_steps, self.cfg.api_max_steps)
        with self._lock:
            if len(self.active.get(key_id, ())) >= self.cfg.api_max_concurrent_tasks:
                raise RateLimited(f"This key already has {self.cfg.api_max_concurrent_tasks} unfinished "
                                  "tasks. Wait for one to finish or cancel it.", code="too_many_tasks")
        reset = int(day_start() + 86400 - time.time())
        if self.ledger.tasks_today(key_id) >= self.cfg.api_daily_tasks:
            raise RateLimited("Daily task quota reached for this key.", retry_after=reset, code="daily_quota")
        if self.cfg.api_daily_credits and self.credits_today(key_id) >= self.cfg.api_daily_credits:
            raise RateLimited("Daily credit limit reached for this key.", retry_after=reset, code="credits_exhausted")

        coordinator = session.coordinator
        self._watch(coordinator)
        task_id = coordinator.new_task_id()
        tier_cfg = self.cfg.api_tiers[tier]
        meta = {"account_id": account_id, "key_id": key_id, "session_id": session.info.id,
                "device_id": session.info.id.removeprefix("app-"), "tier": tier,
                "credits_per_step": int(tier_cfg.get("credits_per_step", 1)),
                "requester": p.name, "confirm_approve": p.confirm_approve}
        with self._lock:
            self.registry[task_id] = meta
            while len(self.registry) > KEEP_REGISTRY:
                self.registry.popitem(last=False)
            self.active.setdefault(key_id, set()).add(task_id)
        self.ledger.start_task(task_id, account_id, key_id, meta["device_id"], tier, instruction)
        self.ledger.audit("task.create", account_id, key_id, meta["device_id"], task_id, instruction=instruction)
        # isolated: an API caller neither sees nor extends the phone chat's recent requests.
        options = {"model": tier_cfg["model"], "max_steps": max_steps, "memory": memory, "isolated": True}
        if blocked := self._blocker(account_id):
            options["blocked"] = blocked
        coordinator.enqueue(instruction, "api", options, task_id=task_id)
        return self.public_task(account_id, task_id)

    def list_tasks(self, p: Principal, limit: int = 20) -> dict:
        self.require(p, "tasks:read")
        rows = self.ledger.recent_tasks(p.account_id, max(1, min(limit, 100)))
        return {"tasks": [self.public_task(p.account_id, row["id"]) for row in rows]}

    def get_task(self, p: Principal, task_id: str) -> dict:
        self.require(p, "tasks:read")
        return self.public_task(p.account_id, task_id)

    def answer(self, p: Principal, task_id: str, answer: str | None = None, approve: bool | None = None) -> dict:
        self.require(p, "tasks:run")
        live = self._live(p.account_id, task_id)
        if not live:
            self.public_task(p.account_id, task_id)  # NotFound if it isn't theirs
            raise Conflict(NOT_WAITING)
        meta, info = live
        if not info or info["status"] not in ("needs_input", "needs_confirmation"):
            raise Conflict(NOT_WAITING)
        if info["status"] == "needs_confirmation":
            if approve is None:
                raise BadRequest('This task needs a confirmation: send {"approve": true} or {"approve": false}.')
            if approve and not p.confirm_approve:
                # The calling model may have read the very screen text that asks for this purchase.
                self.ledger.audit("task.approve_refused", p.account_id, p.id, meta["device_id"], task_id)
                raise Forbidden(OWNER_APPROVES, code="confirmation_requires_owner")
            text = "yes" if approve else "no"
        else:
            text = (answer or "").strip()
            if not text:
                raise BadRequest('Send {"answer": "..."}.')
        session = self.manager.get(meta["session_id"])
        if not session or not session.coordinator.answer(task_id, text):
            raise Conflict(NOT_WAITING)
        self.ledger.audit("task.answer", p.account_id, p.id, meta["device_id"], task_id, outcome=info["status"])
        return {"ok": True}

    def cancel(self, p: Principal, task_id: str) -> dict:
        self.require(p, "tasks:run")
        task = self.public_task(p.account_id, task_id)  # NotFound if it isn't theirs
        live = self._live(p.account_id, task_id)
        session = self.manager.get(live[0]["session_id"]) if live else None
        cancelled = bool(session and session.coordinator.cancel_task(task_id))
        self.ledger.audit("task.cancel", p.account_id, p.id, task["device_id"].removeprefix("app-"),
                          task_id, outcome="cancelled" if cancelled else "already finished")
        return {"ok": True, "cancelled": cancelled}

    def screenshot(self, p: Principal, device_id: str) -> bytes:
        """The phone's screen as JPEG. Never stored, never cached: straight from the phone to the caller."""
        self.require(p, "screen:read")
        device, session = self._owned_device(p.account_id, device_id)
        if session is None or not session.online:
            raise Conflict(OFFLINE)
        try:
            jpeg = session.driver.screenshot(max_age=1.0).jpeg
        except DeviceError:
            raise Unavailable("The phone couldn't capture its screen right now.") from None
        self.ledger.audit("screen.read", p.account_id, p.id, device.id)
        return jpeg

    def _memory(self):
        if self.manager.memory is None:
            raise NotEnabled("Memory isn't enabled on this server.")
        return self.manager.memory

    def list_memory(self, p: Principal) -> dict:
        self.require(p, "memory:read")
        notes = self._memory().list(p.account_id)
        return {"notes": [{"id": n["id"], "text": n["text"], "app": n["package"], "status": n["status"],
                           "created": n["created"], "uses": n["uses"]} for n in notes]}

    def add_memory(self, p: Principal, text: str, app: str | None = None) -> dict:
        from ..memory import NoteError

        self.require(p, "memory:write")
        try:  # owner keys only today, so the note is active (third parties will only propose)
            note = self._memory().add(p.account_id, text, package=app, source="api")
        except NoteError as exc:
            raise BadRequest(str(exc)) from None
        self.ledger.audit("memory.add", p.account_id, p.id, instruction=text)
        return {"id": note["id"], "text": note["text"], "app": note["package"], "status": note["status"]}

    def delete_memory(self, p: Principal, note_id: str) -> dict:
        self.require(p, "memory:write")
        if not self._memory().delete(p.account_id, note_id):
            raise NotFound(f"Unknown note {note_id}")
        self.ledger.audit("memory.delete", p.account_id, p.id)
        return {"ok": True}

    def history(self, p: Principal, limit: int = 50) -> dict:
        self.require(p, "history:read")
        if self.manager.history is None:
            return {"turns": []}
        turns = self.manager.history.recent_turns(p.account_id, max(1, min(limit, 200)))
        return {"turns": [{
            "task_id": t["id"], "device_id": t["device_id"], "instruction": t["request"],
            "started": t["started"], "finished": t["finished"], "steps": t["steps"],
            "success": None if t["success"] is None else bool(t["success"]), "summary": clean_text(t["summary"]),
        } for t in turns]}
