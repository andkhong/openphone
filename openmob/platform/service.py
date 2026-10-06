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

import base64
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

from .. import permissions as perm
from ..ledger import day_start
from ..phone.base import DeviceError, PhoneTimeout
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
LOCKED = "That phone is locked with a PIN, pattern or password. Unlock it to continue."
UNRESPONSIVE = "That phone is connected but isn't answering. Open the OpenMob app on it."
# Tasks that ended through OpenMob's own fault are charged 0 (the would-be amount is kept as credits_waived).
SERVER_FAULTS = {"internal_error", "server_restarted"}
NOT_WAITING = "This task isn't waiting for an answer."
REPORT = " Tell the user; don't retry or work around it."
OWNER_APPROVES = ("Risky actions are approved by the phone's owner, who has been asked on their phone. "
                  'Check the task again later, or send {"approve": false} to decline.')


def encode_cursor(row: dict) -> str:
    """An opaque page cursor: where the previous page ended (created time and id)."""
    return base64.urlsafe_b64encode(f"{row['created']!r}|{row['id']}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[float, str] | None:
    if not cursor:
        return None
    try:
        created, _, row_id = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().partition("|")
        return float(created), row_id
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("Invalid cursor. Use next_cursor from the previous page.", code="invalid_cursor") from None


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
    if isinstance(data.get("timings"), dict):
        event["timings"] = public_timings(data["timings"], data.get("jev"))
    return event


# A step's time per phase as the API shows it: phase names only, no service names.
PUBLIC_TIMINGS = {"observe_ms": "observe_ms", "jev_ms": "classifier_ms", "wait_ms": "wait_ms", "model_ms": "model_ms",
                  "action_ms": "action_ms", "settle_ms": "settle_ms", "total_ms": "total_ms"}


def public_timings(timings: dict, classifier: bool | None = None) -> dict:
    out = {public: int(timings[name]) for name, public in PUBLIC_TIMINGS.items()
           if isinstance(timings.get(name), (int, float))}
    if classifier is not None:
        out["classifier"] = bool(classifier)
    return out


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


def sign_webhook(secrets: str | list[str], body: bytes, timestamp: int) -> str:
    """X-OpenMob-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>"> — one v1= per secret
    (newest first: after the owner replaces the secret, the previous one also signs for 24 h)."""
    secrets = [secrets] if isinstance(secrets, str) else secrets
    macs = [hmac.new(s.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest() for s in secrets]
    return ",".join([f"t={timestamp}"] + [f"v1={m}" for m in macs])


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
        self.waive: set[str] = set()  # tasks to charge 0 when they end (stopped by our own failure)
        # Tasks a restart cut off: failed, charged nothing.
        ledger.fail_interrupted_tasks(lambda tier: int(self.cfg.api_tiers.get(tier, {}).get("credits_per_step", 1)))
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

    def check_task(self, p: Principal, task_id: str) -> None:
        """NotFound unless this key created the task. Other keys' tasks (even on the same account)
        look exactly like other accounts' tasks; the owner's dashboard and console still see all."""
        with self._lock:
            meta = self.registry.get(task_id)
        if meta is not None:
            owner = meta["key_id"] if meta["account_id"] == p.account_id else None
        else:
            row = self.ledger.task(p.account_id, task_id)
            owner = row["key_id"] if row else None
        if owner != p.id:
            raise NotFound(f"Unknown task {task_id}")

    def public_task(self, account_id: str, task_id: str) -> dict:
        live = self._live(account_id, task_id)
        if live and live[1]:
            meta, info = live
            credits = info["steps"] * meta["credits_per_step"] if info["status"] in FINAL_STATUSES else None
            return {
                "id": task_id, "device_id": meta["session_id"], "status": info["status"], "tier": meta["tier"],
                "instruction": info["request"], "created": info["created"], "started": info["started"],
                "finished": info["finished"], "steps": info["steps"], "question": clean_text(info["question"]),
                "permission_mode": meta.get("permission_mode"),
                "prompts_count": info.get("prompts_count"),
                "outcome": ({"success": info["success"], "summary": clean_text(info["summary"]),
                             "reason": info.get("reason")}
                            if info["status"] in FINAL_STATUSES else None),
                "credits": self.ledger_credits(account_id, task_id, credits),
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
            "steps": row["steps"], "question": None, "permission_mode": None,
            "outcome": {"success": status == "succeeded", "summary": clean_text(turn["summary"]) if turn else None,
                        "reason": row.get("reason") or (None if row["status"] in FINAL_STATUSES else "server_restarted")},
            "credits": row["credits"],
        }

    def ledger_credits(self, account_id: str, task_id: str, live: int | None) -> int | None:
        """A finished task's charge as booked (0 when waived); None while it runs."""
        if live is None:
            return None
        row = self.ledger.task(account_id, task_id)
        return row["credits"] if row and row["finished"] else live

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
            reason = info.get("reason") or data.get("reason")
            with self._lock:
                self.active.get(meta["key_id"], set()).discard(task_id)
                waived = reason in SERVER_FAULTS or task_id in self.waive
                self.waive.discard(task_id)
            self.ledger.finish_task(task_id, info["status"], info["steps"], 0 if waived else credits, reason,
                                    credits if waived else 0)
            self.ledger.audit("task.finished", meta["account_id"], meta["key_id"], meta["device_id"], task_id,
                              outcome=info["status"])
            self._send_webhook(meta["account_id"], "task.finished", task_id)
        elif entry["kind"] == "usage":
            self.ledger.set_internal_usage(task_id, data)  # for billing reconciliation; never shown
        elif entry["kind"] in ("action", "error"):
            info = coordinator.task(task_id)
            if info:
                self.ledger.set_steps(task_id, info["steps"])
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

    def account_credits_today(self, account_id: str) -> int:
        """The whole account's credits today, every key, running tasks included."""
        with self._lock:
            running = [t for keys in self.active.values() for t in keys
                       if t in self.registry and self.registry[t]["account_id"] == account_id]
        return self.ledger.credits_today_account(account_id) + sum(self.task_credits(account_id, t) for t in running)

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
            # Every paired phone, online or not, so a pairing you don't recognise can't hide.
            out.append({
                "id": f"app-{d.id}", "name": d.model, "model": d.model,
                "platform": session.info.platform if live else "android",
                "online": bool(live and session.online),
                "busy": bool(live and snap.get("state") != "idle"),
                "last_seen": d.last_seen,
                "paired_at": d.paired_at,
                "trusted_for_approvals": d.trusted,
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
            raise Conflict("None of your phones is online. Open the OpenMob app on one.", code="no_device_online")
        raise BadRequest(f"Several phones are online; pass device_id, one of: {', '.join(online)}",
                         code="device_required")

    @staticmethod
    def phone_state(session) -> str | None:
        """None if the phone is usable; "phone_locked" (a secure lock: PIN, pattern, password or
        biometric) or "phone_unresponsive" (connected but not answering). A screen that's just off, or
        a swipe-only lock screen, is woken and dismissed here (wake_unlock); only if that doesn't work
        is it phone_locked. No credential is ever entered. Phones whose app can't tell are assumed usable."""
        check = getattr(session.driver, "device_state", None)
        if check is None:
            return None
        try:
            state = check()
            if state is None:
                return None
            if state["locked"] and state["secure"]:
                return "phone_locked"  # only the owner can unlock it
            if state["locked"] or not state["interactive"]:
                session.driver.wake_unlock()  # wake the screen, swipe away a non-secure lock screen
                state = check() or state
                if state["locked"]:
                    return "phone_locked"  # the swipe didn't take
        except PhoneTimeout:
            return "phone_unresponsive"
        except DeviceError:
            return None  # disconnected meanwhile: the task waits or fails on its own
        return None

    def permission_mode(self, p: Principal, device_id: str, mode: str | None) -> str:
        """The mode a task of this key runs in on this phone: `mode` if it's no looser than both the key's
        max_mode and the phone's ceiling (else 403 mode_not_allowed), or by default the phone's default
        mode tightened to fit."""
        _, session = self._owned_device(p.account_id, device_id)
        session_id = "app-" + device_id.removeprefix("app-")
        try:
            return self.manager.check_mode(session.info.id if session else session_id, mode, p.max_mode)
        except perm.ModeNotAllowed as exc:
            raise Forbidden(str(exc) + REPORT, code="mode_not_allowed") from None
        except perm.PermissionsInvalid as exc:
            raise BadRequest(str(exc)) from None

    def create_task(self, p: Principal, device_id: str, instruction: str, tier: str | None = None,
                    max_steps: int | None = None, memory: str = "on", check_phone: bool = True,
                    clean_start: bool = False, permission_mode: str | None = None) -> dict:
        """`check_phone`: refuse a locked or unresponsive phone up front (orchestrations check it
        themselves). `clean_start`: press Home before the task (orchestrated steps). `permission_mode`:
        when the phone asks its owner (see permission_mode())."""
        self.require(p, "tasks:run")
        account_id, key_id = p.account_id, p.id
        _, session = self._owned_device(account_id, device_id)
        if session is None or not session.online:
            raise Conflict(OFFLINE, code="phone_offline")
        mode = self.permission_mode(p, device_id, permission_mode)
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
        resets_at = day_start() + 86400  # daily limits reset at midnight UTC
        reset = {"retry_after": max(1, int(resets_at - time.time())), "extra": {"resets_at": resets_at}}
        if self.ledger.tasks_today(key_id) >= self.cfg.api_daily_tasks:
            raise RateLimited("Daily task quota reached for this key.", code="daily_quota", **reset)
        if self.cfg.api_daily_credits and self.credits_today(key_id) >= self.cfg.api_daily_credits:
            raise RateLimited("Daily credit limit reached for this key.", code="credits_exhausted", **reset)
        # Account-wide too, so creating more keys doesn't multiply the allowance.
        if self.ledger.tasks_today_account(account_id) >= self.cfg.api_account_daily_tasks:
            raise RateLimited("Daily task quota reached for this account.", code="account_daily_quota", **reset)
        if (self.cfg.api_account_daily_credits
                and self.account_credits_today(account_id) >= self.cfg.api_account_daily_credits):
            raise RateLimited("Daily credit limit reached for this account.", code="account_credits_exhausted",
                              **reset)
        if check_phone and (problem := self.phone_state(session)):
            raise Conflict(LOCKED if problem == "phone_locked" else UNRESPONSIVE, code=problem)

        coordinator = session.coordinator
        self._watch(coordinator)
        task_id = coordinator.new_task_id()
        tier_cfg = self.cfg.api_tiers[tier]
        meta = {"account_id": account_id, "key_id": key_id, "session_id": session.info.id,
                "device_id": session.info.id.removeprefix("app-"), "tier": tier,
                "credits_per_step": int(tier_cfg.get("credits_per_step", 1)),
                "requester": p.name, "confirm_approve": p.confirm_approve, "permission_mode": mode}
        with self._lock:
            self.registry[task_id] = meta
            while len(self.registry) > KEEP_REGISTRY:
                self.registry.popitem(last=False)
            self.active.setdefault(key_id, set()).add(task_id)
        self.ledger.start_task(task_id, account_id, key_id, meta["device_id"], tier, instruction)
        self.ledger.audit("task.create", account_id, key_id, meta["device_id"], task_id, instruction=instruction,
                          outcome=f"mode={mode}")
        # isolated: an API caller neither sees nor extends the phone chat's recent requests.
        options = {"model": tier_cfg["model"], "max_steps": max_steps, "memory": memory, "isolated": True,
                   "permission_mode": mode}
        if clean_start:
            options["go_home"] = True
        if blocked := self._blocker(account_id):
            options["blocked"] = blocked
        coordinator.enqueue(instruction, "api", options, task_id=task_id)
        return self.public_task(account_id, task_id)

    def idempotent(self, p: Principal, idem_key: str | None, request: dict, create) -> tuple[dict, bool]:
        """Run `create()` once per Idempotency-Key (per API key, 24 h). Returns (response, replayed):
        a retry with the same request gets the first response back; a different request under the
        same key is a 409 idempotency_conflict."""
        if not idem_key:
            return create(), False
        if len(idem_key) > 255:
            raise BadRequest("Idempotency-Key is at most 255 characters.")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        earlier = self.ledger.idempotency_reserve(p.id, idem_key, digest)
        if earlier is not None:
            if earlier["request_hash"] != digest:
                raise Conflict("This Idempotency-Key was already used for a different request.",
                               code="idempotency_conflict")
            if earlier["response"] is None:
                raise Conflict("A request with this Idempotency-Key is still being handled. Retry shortly.",
                               code="idempotency_in_progress")
            return earlier["response"], True
        try:
            response = create()
        except BaseException:
            self.ledger.idempotency_release(p.id, idem_key)
            raise
        self.ledger.idempotency_store(p.id, idem_key, response)
        return response, False

    def limits(self, p: Principal) -> dict:
        """This key's limits and today's use. Daily limits reset at midnight UTC (resets_at)."""
        c = self.cfg
        with self._lock:
            running = len(self.active.get(p.id, ()))
        return {
            "requests_per_minute": c.api_rate_per_minute,
            "concurrent_tasks": {"limit": c.api_max_concurrent_tasks, "used": running},
            "tasks_per_day": {"limit": c.api_daily_tasks, "used": self.ledger.tasks_today(p.id)},
            "credits_per_day": {"limit": c.api_daily_credits or None, "used": self.credits_today(p.id)},
            "account": {  # shared by all of the account's keys
                "tasks_per_day": {"limit": c.api_account_daily_tasks,
                                  "used": self.ledger.tasks_today_account(p.account_id)},
                "credits_per_day": {"limit": c.api_account_daily_credits or None,
                                    "used": self.account_credits_today(p.account_id)},
            },
            "max_steps": c.api_max_steps,
            "running_orchestrations": {"per_key": c.api_max_running_orchestrations,
                                       "per_account": c.api_max_running_orchestrations_per_account},
            "resets_at": day_start() + 86400,
        }

    def _page(self, p: Principal, limit: int, cursor: str | None, maximum: int) -> list[dict]:
        """This key's API task rows, newest first, after `cursor` (from an earlier page)."""
        return self.ledger.recent_tasks(p.account_id, max(1, min(limit, maximum)), key_id=p.id,
                                        before=decode_cursor(cursor))

    def list_tasks(self, p: Principal, limit: int = 20, cursor: str | None = None) -> dict:
        """This key's tasks, newest first. `next_cursor` fetches the next page (None: no more)."""
        self.require(p, "tasks:read")
        limit = max(1, min(limit, 100))
        rows = self._page(p, limit, cursor, 100)
        return {"tasks": [self.public_task(p.account_id, row["id"]) for row in rows],
                "next_cursor": encode_cursor(rows[-1]) if len(rows) == limit else None}

    def get_task(self, p: Principal, task_id: str) -> dict:
        self.require(p, "tasks:read")
        self.check_task(p, task_id)
        return self.public_task(p.account_id, task_id)

    def answer(self, p: Principal, task_id: str, answer: str | None = None, approve: bool | None = None) -> dict:
        self.require(p, "tasks:run")
        self.check_task(p, task_id)
        live = self._live(p.account_id, task_id)
        if not live:
            raise Conflict(NOT_WAITING, code="not_waiting")
        meta, info = live
        if not info or info["status"] not in ("needs_input", "needs_confirmation"):
            raise Conflict(NOT_WAITING, code="not_waiting")
        if info["status"] == "needs_confirmation":
            if approve is None:
                raise BadRequest('This task needs a confirmation: send {"approve": true} or {"approve": false}.')
            session = self.manager.get(meta["session_id"])
            details = (session.coordinator.question_details if session else None) or {}
            if approve and details.get("approver") == "phone":
                # A gatekeeper phone owns this approval: only its owner, on that phone, can say yes. No key can.
                self.ledger.audit("task.approve_refused", p.account_id, p.id, meta["device_id"], task_id,
                                  outcome="approval_on_phone")
                raise Conflict("This approval can only be given on the phone, with its screen lock. You can "
                               'still decline with {"approve": false}.', code="approval_on_phone")
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
            raise Conflict(NOT_WAITING, code="not_waiting")
        self.ledger.audit("task.answer", p.account_id, p.id, meta["device_id"], task_id, outcome=info["status"])
        return {"ok": True}

    def cancel(self, p: Principal, task_id: str) -> dict:
        self.require(p, "tasks:run")
        self.check_task(p, task_id)  # NotFound unless this key created it
        task = self.public_task(p.account_id, task_id)
        live = self._live(p.account_id, task_id)
        session = self.manager.get(live[0]["session_id"]) if live else None
        cancelled = bool(session and session.coordinator.cancel_task(task_id))
        self.ledger.audit("task.cancel", p.account_id, p.id, task["device_id"].removeprefix("app-"),
                          task_id, outcome="cancelled" if cancelled else "already finished")
        return {"ok": True, "cancelled": cancelled}

    # --- a phone's permission settings ----------------------------------------------------

    def _session_for(self, p: Principal, device_id: str):
        device, session = self._owned_device(p.account_id, device_id)
        return device, session

    def get_permissions(self, p: Principal, device_id: str) -> dict:
        self.require(p, "devices:read")
        device, session = self._session_for(p, device_id)
        settings = self.manager.permission_settings(f"app-{device.id}")
        return {"device_id": f"app-{device.id}", **perm.public(settings), "key_max_mode": p.max_mode}

    def change_permissions(self, p: Principal, device_id: str, patch: dict) -> dict:
        """Tightening applies now. Loosening never applies from the API: it asks the phone's owner to
        approve on the phone (screen lock) and returns needs_phone_approval with an approval_id to poll."""
        self.require(p, "tasks:run")
        device, session = self._session_for(p, device_id)
        if session is None:
            raise Conflict(OFFLINE, code="phone_offline")
        try:
            result = self.manager.change_permissions(session, patch, requester=p.name, max_mode=p.max_mode)
        except perm.ModeNotAllowed as exc:
            self.ledger.audit("permissions.refused", p.account_id, p.id, device.id, outcome="mode_not_allowed")
            raise Forbidden(str(exc) + REPORT, code="mode_not_allowed") from None
        except perm.PhoneUnavailable as exc:
            raise Conflict(str(exc), code="phone_offline") from None
        except perm.PermissionsInvalid as exc:
            raise BadRequest(str(exc), code="invalid_permissions") from None
        self.ledger.audit("permissions." + ("change" if result["status"] == "applied" else "ask_phone"),
                          p.account_id, p.id, device.id, outcome=",".join(sorted(patch)))
        if result["status"] == "applied":
            return {"status": "applied", "settings": self.get_permissions(p, device_id)}
        return {"status": "needs_phone_approval", "code": "needs_phone_approval", "approval_id": result["approval_id"],
                "question": result["question"],
                "detail": "The owner must approve this on the phone, with its screen lock. Poll the approval; "
                          "an API key can't approve it." + REPORT}

    def permission_approval(self, p: Principal, device_id: str, approval_id: str) -> dict:
        self.require(p, "devices:read")
        device, _ = self._session_for(p, device_id)
        row = self.manager.permission_request(p.account_id, f"app-{device.id}", approval_id)
        if row is None:
            raise NotFound(f"Unknown approval {approval_id}")
        return row

    def screenshot(self, p: Principal, device_id: str) -> bytes:
        """The phone's screen as JPEG. Never stored, never cached: straight from the phone to the caller."""
        self.require(p, "screen:read")
        device, session = self._owned_device(p.account_id, device_id)
        if session is None or not session.online:
            raise Conflict(OFFLINE, code="phone_offline")
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

    def history(self, p: Principal, limit: int = 50, cursor: str | None = None) -> dict:
        """This key's own API tasks as history turns (instruction and outcome text from the chat
        history). The owner's dashboard and phone requests aren't included: a key reads only what it
        did. Text past the retention period is gone (instruction and summary are then null)."""
        self.require(p, "history:read")
        limit = max(1, min(limit, 200))
        rows = self._page(p, limit, cursor, 200)
        history = self.manager.history
        turns = []
        for row in rows:
            t = history.turn(p.account_id, row["id"]) if history else None
            turns.append({
                "task_id": row["id"], "device_id": f"app-{row['device_id']}",
                "instruction": t["request"] if t else None,
                "started": t["started"] if t else row["created"], "finished": row["finished"],
                "steps": row["steps"], "success": None if row["finished"] is None else row["status"] == "succeeded",
                "summary": clean_text(t["summary"]) if t else None,
            })
        return {"turns": turns, "next_cursor": encode_cursor(rows[-1]) if len(rows) == limit else None}
