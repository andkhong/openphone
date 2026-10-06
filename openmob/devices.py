"""Every connected Android phone, each with its own agent, task queue, and log."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import permissions as perm
from .config import Config
from .phone.adb import AdbError, PhoneDriver
from .phone.approval_keys import action_digest
from .phone.apps import DEFAULT_LABEL_CACHE
from .phone.base import Device, DeviceError
from .phone.remote import PhoneLink, RemoteDriver
from .runner import Coordinator
from .voice.tts import Speaker


@dataclass
class DeviceInfo:
    id: str  # hardware serial: stable across USB and Wi-Fi
    transport: str  # adb serial currently used (USB serial or ip:port); "" for app phones
    model: str
    connection: str  # usb | wifi (adb on this computer) | app (connected to us over WebSocket)
    platform: str = "android"  # android | ios


class Session:
    def __init__(self, info: DeviceInfo, coordinator: Coordinator, driver: Device, account_id: str | None = None):
        self.info = info
        self.coordinator = coordinator
        self.driver = driver
        self.online = True
        self.account_id = account_id  # owning account in hosted mode; None for local adb phones
        self.link: PhoneLink | None = None  # app phones only
        # (task id, who) of the last API task to run here; who is "" once it finished
        self.controller: tuple[str, str] | None = None
        self.reconnecting = False  # the socket dropped mid-task; the task waits for the phone to come back
        self._grace: object | None = None  # identifies the current wait, so a stale timer does nothing

    def describe(self) -> dict:
        snap = self.coordinator.snapshot()
        return {
            "id": self.info.id,
            "model": self.info.model,
            "connection": self.info.connection,
            "platform": self.info.platform,
            "online": self.online,
            "reconnecting": self.reconnecting,
            "state": snap["state"],
            "current": snap["current"],
        }


class DeviceManager:
    # The phone dismisses an unanswered approval after about 2 minutes and sends nothing, so the
    # server treats silence as "no".
    approval_timeout = 120.0
    # A socket blip mid-task (Wi-Fi to cellular, a tunnel hiccup) pauses the task instead of killing it.
    reconnect_grace = 45.0

    def __init__(self, cfg: Config, build_agent, speaker: Speaker, adb_discovery: bool = True, history=None,
                 memory=None):
        self.cfg = cfg
        self.history = history  # chat history store shared by every phone (None: not saved)
        self.memory = memory  # pinned notes per account (None: the agent remembers nothing)
        self.adb_discovery = adb_discovery  # off for a hosted server: only app-connected phones
        self.build_agent = build_agent  # (cfg, io, driver, label_cache) -> Agent
        self.speaker = speaker
        self.sessions: dict[str, Session] = {}
        # callables(task id) → {"name", "human"} or None: who started a task (the developer API
        # registers one). "human": its risky actions must be approved by the owner on the phone.
        self.task_requesters: list = []
        self.approval_listeners: list = []  # callables(task id, approved) after a phone approval
        # callables(session, task ids, how): the owner stopped work from that phone ("stop", "denied")
        self.phone_stop_listeners: list = []
        # open approval id → {session (the acting phone), task_id, question, who, sent (phones asked)}
        self.approvals: dict[str, dict] = {}
        # Gatekeeper phones own approval (docs/design/phone-gatekeeper.md, 2.8): (session id, approval id) -> the
        # phone's last gate_event state, and the ids already acted on (a yes re-sends the call at most once).
        self.gate_states: dict[tuple[str, str], str] = {}
        self.gate_answered: set[tuple[str, str]] = set()
        self.gate_timeout = 130.0  # seconds: the phone expires a prompt after 2 min
        # Every open socket per phone (session id → links). Exactly one is live (session.link); any
        # other is closed as soon as a newer one attaches, and unpair / re-pair close them all.
        self.links: dict[str, set[PhoneLink]] = {}
        # callables(session, old link, new link): a live socket was replaced by one from another address
        self.replaced_listeners: list = []
        # (answering session, approval record, approved, message) -> bool: is this approval genuine
        # (signed with the phone's screen-lock key)? None: no check (local mode).
        self.approval_verifier = None
        # Per-phone permission settings (mode, ceiling, trusted entries, never). Hosted servers swap in
        # AccountPermissions (kept on each phone's pairing); local mode a JSON file (main.py).
        self.permissions = perm.PermissionStore()
        # (session) -> bool: may this phone approve a loosening? Hosted: only with a registered
        # approval key (its signature is the proof). None: yes (token/dev mode, no keys exist).
        self.can_loosen = None
        # (session) -> str | None: why this phone can't approve at all yet (hosted: an app phone with no
        # registered approval key), or None. Such a phone is still asked, so it can decline.
        self.approval_blocker = None
        # (session) -> bool: its approvals must be signed (hosted app phones). Sent with each request.
        self.requires_signature = None
        # approval id -> {status: pending|approved|denied|expired|replaced, device_id, account_id, patch,
        # loosens, created, decided}: permission changes waiting for (or decided on) the phone.
        self.permission_requests: dict[str, dict] = {}
        # (session) -> bool: may this phone answer approvals for the owner's OTHER phones? A newly
        # paired phone may not until the owner trusts it. None: every phone may (local mode).
        self.approval_trust = None
        self._lock = threading.Lock()
        self._approvals_lock = threading.Lock()  # never held while the coordinator logs (it calls back in)
        self._probe = PhoneDriver(adb_path=cfg.adb_path)

    def refresh(self) -> list[Session]:
        """Rescan adb. New phones get a session; known phones keep theirs."""
        if not self.adb_discovery:
            return list(self.sessions.values())
        with self._lock:
            seen: dict[str, DeviceInfo] = {}
            for transport, state in self._probe.devices():
                if state != "device":
                    continue
                driver = PhoneDriver(transport, self.cfg.adb_path)
                try:
                    props = driver.shell("getprop ro.serialno; getprop ro.product.manufacturer; getprop ro.product.model")
                except AdbError:
                    continue
                hw, maker, model = (props.splitlines() + ["", "", ""])[:3]
                info = DeviceInfo(
                    id=hw.strip() or transport,
                    transport=transport,
                    model=f"{maker.strip().title()} {model.strip()}".strip(),
                    connection="wifi" if ":" in transport else "usb",
                )
                # The same phone can be attached over USB and Wi-Fi at once; prefer USB.
                if info.id not in seen or info.connection == "usb":
                    seen[info.id] = info

            for session in self.sessions.values():
                if session.info.connection != "app":  # app phones report their own presence
                    session.online = session.info.id in seen
            for device_id, info in seen.items():
                session = self.sessions.get(device_id)
                if session:
                    session.info = info
                    session.driver.serial = info.transport  # switches USB <-> Wi-Fi in place
                else:
                    driver = PhoneDriver(info.transport, self.cfg.adb_path, self.cfg.screenshot_max_side)
                    self.sessions[device_id] = self._create(info, driver)
            return list(self.sessions.values())

    def attach_remote(self, device: dict, link: PhoneLink, account_id: str | None = None) -> Session:
        """A phone connected over WebSocket. Reconnects reuse its session (and task history)."""
        info = DeviceInfo(
            id=f"app-{device.get('id') or 'unknown'}",
            transport="",
            model=" ".join(filter(None, [str(device.get("manufacturer", "")).title(), device.get("model")])) or "Android phone",
            connection="app",
            platform="ios" if device.get("platform") == "ios" else "android",
        )
        stale = resumed = None
        with self._lock:
            replaced = [old for old in self.links.get(info.id, set()) if old is not link]
            self.links[info.id] = {link}
            session = self.sessions.get(info.id)
            if session and session.account_id != account_id:
                # Same device id, different owner: never hand over the old session or its history.
                stale = session
                del self.sessions[info.id]
                session = None
            if session:
                if session.reconnecting:
                    session.reconnecting, session._grace = False, None
                    resumed = session
                session.driver.attach(link)  # a task waiting for the phone carries on
                session.info = info
            else:
                session = self._create(info, RemoteDriver(link), account_id)
                session.coordinator.text_approvals = False  # approvals only via the screen-lock prompt
                self.sessions[info.id] = session
                # Mirror agent activity to the phone so its app can show or speak it.
                session.coordinator.listeners.append(
                    lambda kind, message, s=session: s.link and s.link.notify(kind, message)
                )
                session.coordinator.entry_listeners.append(lambda entry, s=session: self._on_entry(s, entry))
            session.link = link
            session.online = True
            session.driver.video_available = bool(device.get("video"))
            caps = device.get("capabilities")
            session.driver.capabilities = {str(c) for c in caps} if isinstance(caps, list) else set()
        if stale:
            self._stop(stale)
        for old in replaced:  # one live socket per phone: the older one can't act any more
            old.disconnect(4409, "Replaced by a newer connection from this phone.")
            if getattr(old, "peer", None) != getattr(link, "peer", None):
                session.coordinator.event("status", f"{info.model} reconnected from a new connection.", task=None)
                for listener in self.replaced_listeners:
                    try:
                        listener(session, old, link)
                    except Exception:
                        pass
        if resumed:
            resumed.coordinator.event("status", "The phone is back. Carrying on.")
        return session

    def is_live(self, session: Session, link: PhoneLink) -> bool:
        """This socket is still the phone's one live connection (else whatever it sends is ignored)."""
        return not link.closed and session.link is link and self.sessions.get(session.info.id) is session

    def close_links(self, session_id: str, code: int, reason: str) -> int:
        """Close every socket of a phone (unpaired, paired again, or its key replaced)."""
        with self._lock:
            links = self.links.pop(session_id, set())
        for link in links:
            link.disconnect(code, reason)
        return len(links)

    def detach_remote(self, session: Session, link: PhoneLink, close_code: int | None = None) -> None:
        """The phone's socket closed. Mid-task, wait `reconnect_grace` seconds for it to come back
        before failing the task; nothing is replayed. A normal close (1000: the user tapped Stop or
        disconnected on purpose) stops the work right away, as does a drop with nothing running."""
        with self._lock:
            open_links = self.links.get(session.info.id)
            if open_links is not None:
                open_links.discard(link)
            if session.link is not link:  # ignore a stale socket closing after a reconnect
                return
            session.online = False
            session.link = None
            c = session.coordinator
            busy = c.current is not None or not c.tasks.empty()
            wait = (busy and close_code != 1000 and isinstance(session.driver, RemoteDriver)
                    and self.sessions.get(session.info.id) is session)  # not for a phone just unpaired
            if wait:
                session.reconnecting = True
                session._grace = grace = object()
                session.driver.hold(self.reconnect_grace)
        if not wait:
            if close_code == 1000:  # the owner's Stop on the phone (it closes the connection normally)
                self.phone_stopped(session, self.phone_stop_targets(session), "stop")
            session.coordinator.stop()
            return
        session.coordinator.event("status", f"Lost the connection to the phone. Waiting up to "
                                            f"{self.reconnect_grace:g} s for it to come back.")
        timer = threading.Timer(self.reconnect_grace, self._grace_over, (session, grace))
        timer.daemon = True
        timer.start()

    def _grace_over(self, session: Session, grace: object) -> None:
        """The phone didn't come back in time: its waiting call fails (and so the task); queued tasks are cancelled."""
        with self._lock:
            if session._grace is not grace or not session.reconnecting:
                return  # it reconnected (or was removed) meanwhile
            session.reconnecting, session._grace = False, None
            session.driver.hold(None)
        c = session.coordinator
        if c.current_task:
            c.event("status", f"The phone didn't reconnect within {self.reconnect_grace:g} s.")
        for task_id, info in list(c.task_info.items()):
            if info["status"] == "queued":
                c.cancel_task(task_id)

    @staticmethod
    def _stop(session: Session, reason: str | None = None) -> None:
        session.reconnecting, session._grace = False, None
        if isinstance(session.driver, RemoteDriver):
            session.driver.hold(None)  # release a task waiting for this phone
        session.coordinator.stop(reason)

    # --- approvals on the phone and the "who is using it" line --------------------------

    def _requester(self, task_id: str) -> dict | None:
        for hook in self.task_requesters:
            try:
                if who := hook(task_id):
                    return who
            except Exception:  # a broken hook must never stop the agent
                pass
        return None

    def _on_entry(self, session: Session, entry: dict) -> None:
        """Every log entry of an app phone's coordinator (on the agent thread)."""
        task_id = entry.get("task")
        if not task_id:
            return
        data = entry.get("data") or {}
        if data.get("final"):
            self._forget_approvals(session, task_id)
            if session.controller and session.controller[0] == task_id and session.controller[1]:
                session.controller = (task_id, "")  # its usage entry still follows: don't announce it again
                self._post(session, {"type": "event", "kind": "controller", "message": ""})
            return
        if entry["kind"] == "heard":  # an answer arrived (dashboard, phone, API): older approvals are void
            self._forget_approvals(session, task_id)
        who = self._requester(task_id)
        started = session.controller is None or session.controller[0] != task_id
        if who and started and session.coordinator.current_task == task_id:
            session.controller = (task_id, who["name"])  # the API task just started
            self._post(session, {"type": "event", "kind": "controller", "message": who["name"]})
        approval = data.get("approval") if isinstance(data.get("approval"), dict) else {}
        if entry["kind"] == "question" and data.get("confirm") and approval.get("approver") == "phone":
            self._gate_question(session, task_id, approval)  # the phone asks its owner itself: no approval_request
            return
        if entry["kind"] == "question" and data.get("confirm") and (who is None or who["human"]):
            self._ask_owner(session, task_id, entry["message"], who, data.get("approval"))

    # Approvals go to every online phone of the owner, not just the one acting: the owner may be
    # holding another. The first answer wins (screen lock on that phone); the others are told to
    # dismiss their prompt with approval_cancel.

    def _may_answer(self, answering: Session, acting: Session) -> bool:
        """A phone may always answer for its own actions (the owner is holding it); for another
        phone's, only once the owner has trusted it for approvals."""
        if answering is acting or self.approval_trust is None:
            return True
        try:
            return bool(self.approval_trust(answering))
        except Exception:
            return False  # fail closed

    def _approval_targets(self, acting: Session) -> list[Session]:
        if getattr(acting.driver, "gatekeeper", False):
            return [acting] if acting.link else []  # D10: only the acting phone, until cross-device trust ships
        return [s for s in list(self.sessions.values())
                if s.link and s.account_id == acting.account_id and self._may_answer(s, acting)]

    @staticmethod
    def _bound_action(kind: str, question: str, details: dict) -> dict:
        """The action an approval binds (phone/approval_keys.py): the agent's own (tool, app, target,
        typed text), or, for a plan or anything without one, the request itself."""
        action = details.get("action")
        if isinstance(action, dict) and action.get("tool"):
            return dict(action)
        text = "\n".join(str(s) for s in details.get("plan") or []) or question
        return {"tool": kind or "action", "package": details.get("package") or "", "input": text}

    def _ask_owner(self, acting: Session, task_id: str, question: str, who: dict | None,
                   details: dict | None = None) -> None:
        approval_id = uuid.uuid4().hex
        details = details or {}
        kind = details.get("kind") or "action"
        action = self._bound_action(kind, question, details)
        record = {"session": acting, "task_id": task_id, "question": question, "who": who, "sent": set(),
                  "kind": kind, "details": details, "action": action, "digest": action_digest(action)}
        with self._approvals_lock:
            self.approvals[approval_id] = record
        for target in self._approval_targets(acting):
            self._send_approval(target, approval_id, record)
        self._expire_later(approval_id)

    def _expire_later(self, approval_id: str) -> None:
        timer = threading.Timer(self.approval_timeout, self._expire_approval, (approval_id,))
        timer.daemon = True
        timer.start()

    def _send_approval(self, target: Session, approval_id: str, record: dict) -> None:
        acting = record["session"]
        with self._approvals_lock:
            record["sent"].add(target.info.id)
        message = {
            "type": "approval_request", "id": approval_id, "task_id": record["task_id"],
            "question": record["question"], "requester": record["who"]["name"] if record["who"] else "You",
            "device_id": acting.info.id, "device_name": acting.info.model,  # which phone would act
            "kind": record.get("kind", "action"),  # action | step | plan | mode_change
            # What the approval binds: the phone shows it, recomputes the digest and signs that.
            "action": record.get("action") or {}, "digest": record.get("digest") or "",
        }
        if self.requires_signature is not None and self.requires_signature(target):
            message["signature_required"] = True
        if self.approval_blocker is not None and (why := self.approval_blocker(target)):
            message["can_approve"] = False  # this phone can only decline (its app shows why)
            message["cannot_approve_reason"] = why
        details = record.get("details") or {}
        if details.get("plan"):
            message["plan"] = list(details["plan"])
        if record.get("kind") == "mode_change":
            message["change"] = record.get("change") or {}
        # "Always allow this" is offered only on the phone that would act, and only where it can be signed.
        if details.get("always") and target is acting and self._may_loosen(target):
            message["always"] = details["always"]
        self._post(target, message)

    def _dismiss(self, approval_id: str, record: dict, except_id: str | None = None) -> None:
        """Tell every phone that was asked (but the one that answered) to drop the prompt."""
        for session_id in record["sent"]:
            target = self.sessions.get(session_id)
            if target is not None and session_id != except_id:
                self._post(target, {"type": "approval_cancel", "id": approval_id})

    def _may_loosen(self, session: Session) -> bool:
        return self.can_loosen is None or bool(self.can_loosen(session))

    def _forget_approvals(self, session: Session, task_id: str) -> None:
        with self._approvals_lock:
            gone = [(a, r) for a, r in self.approvals.items() if r["task_id"] == task_id and r["session"] is session
                    and r.get("kind") != "mode_change"]
            for approval_id, _ in gone:
                del self.approvals[approval_id]
        for approval_id, record in gone:
            self._dismiss(approval_id, record)

    @staticmethod
    def _post(session: Session, message: dict) -> None:
        if session.link:
            session.link.post(message)

    def resolve_approval(self, session: Session, approval_id: str, approved: bool, message: dict | None = None) -> bool:
        """The owner answered an approval_request on one of their phones (after its screen-lock
        check). Ignored (False) unless that request is still open and its task still waiting, and,
        for a yes, the phone proved the screen lock (approval_verifier). A no needs no proof."""
        message = message or {}
        with self._approvals_lock:
            record = self.approvals.get(str(approval_id or ""))
            if record is None or record["session"].account_id != session.account_id:
                return False
        # A permission change is answered only by the phone being loosened (its own key, checked in
        # _resolve_permission_change): trusting another phone never lets it loosen this one.
        if record.get("kind") == "mode_change":
            return self._resolve_permission_change(session, str(approval_id), record, approved, message)
        if not self._may_answer(session, record["session"]):
            session.coordinator.event("status", "This phone isn't trusted for approvals on your other phones yet, "
                                                "so its answer was ignored. Trust it on your dashboard.", task=None)
            return False
        # "Always allow this": only from the phone that would act, where it was offered.
        always = (approved and message.get("always") is True and bool((record.get("details") or {}).get("always"))
                  and session is record["session"] and self._may_loosen(session))
        if approved and self.approval_verifier is not None and not self.approval_verifier(
                session, approval_id, record, {**message, "always": always}):
            why = self.approval_blocker(session) if self.approval_blocker is not None else None
            session.coordinator.event("status", why or "An approval wasn't signed with this phone's screen lock "
                                                       "for this action, so it was ignored.", task=None)
            return False
        with self._approvals_lock:
            if self.approvals.pop(str(approval_id), None) is None:
                return False  # answered meanwhile (first answer wins)
        acting, task_id = record["session"], record["task_id"]
        self._dismiss(approval_id, record, except_id=session.info.id)
        info = acting.coordinator.task(task_id)
        if not info or info["status"] != "needs_confirmation":
            return False
        if always:
            offer = record["details"]["always"]
            if offer.get("kind") in perm.KINDS and offer.get("app"):
                entry = perm.always_entry(offer["app"], offer["kind"], offer.get("target"))
                self.permissions.set(acting.info.id, perm.add_entry(self.permissions.get(acting.info.id), entry))
                acting.coordinator.event("status", f"Always allowed from now on: {offer.get('text') or offer['kind']}",
                                         task=task_id)
                self.push_permissions(acting)
        answer = ("yes, always" if always else "yes") if approved else "no"
        # A yes names the action it approved: the agent runs it only if that's still the action at hand.
        if not acting.coordinator.answer(task_id, answer, digest=record.get("digest") if approved else None):
            return False
        for listener in self.approval_listeners:
            try:
                listener(task_id, approved)
            except Exception:
                pass
        if not approved:  # the owner said no on a phone: for an orchestration, that's a stop
            self.phone_stopped(acting, [task_id], "denied")
        return True

    def _expire_approval(self, approval_id: str) -> None:
        """Nobody answered on any phone in time: decline, so the agent never acts on silence."""
        with self._approvals_lock:
            record = self.approvals.pop(approval_id, None)
        if record is None:
            return
        self._dismiss(approval_id, record)
        if record.get("kind") == "mode_change":
            self._decide_request(approval_id, "expired")
            return
        acting, task_id = record["session"], record["task_id"]
        info = acting.coordinator.task(task_id)
        if info and info["status"] == "needs_confirmation":
            acting.coordinator.event("status", "No answer on the phone in time, so I won't do it.", task=task_id)
            acting.coordinator.answer(task_id, "no")

    # --- gatekeeper phones: the phone asks its owner itself -------------------------------

    def _gate_question(self, session: Session, task_id: str, approval: dict) -> None:
        """The agent waits on the phone's own prompt (gate_needs_approval). Nothing is sent to the phone: it is
        already asking. The owner's other phones get a notice without an Approve button (D10)."""
        approval_id = str(approval.get("approval_id") or "")
        key = (session.info.id, approval_id)
        for other in list(self.sessions.values()):
            if other is not session and other.link and other.account_id == session.account_id:
                self._post(other, {"type": "event", "kind": "status",
                                   "message": f"{session.info.model} needs your approval on that phone."})
        state = self.gate_states.get(key)
        if state is not None:  # the phone answered before the question went up
            self._answer_gate(session, task_id, approval_id, state)
            return

        def expire() -> None:
            self._answer_gate(session, task_id, approval_id, "expired")
        timer = threading.Timer(self.gate_timeout, expire)
        timer.daemon = True
        timer.start()

    def gate_event(self, session: Session, message: dict) -> None:
        """The phone's prompt changed state (pending | approved | denied | expired). Only the acting phone's own
        events count, and each approval id is acted on once (a replayed event does nothing)."""
        approval_id = str(message.get("approval_id") or "")
        state = str(message.get("state") or "")
        if not approval_id or state not in ("pending", "approved", "denied", "expired"):
            return
        key = (session.info.id, approval_id)
        if state == "pending":
            return
        self.gate_states[key] = state
        while len(self.gate_states) > 500:
            self.gate_states.pop(next(iter(self.gate_states)))
        c = session.coordinator
        details = c.question_details or {}
        if details.get("approver") == "phone" and details.get("approval_id") == approval_id and c.current_task:
            self._answer_gate(session, c.current_task, approval_id, state)

    def _answer_gate(self, session: Session, task_id: str, approval_id: str, state: str) -> None:
        key = (session.info.id, approval_id)
        c = session.coordinator
        details = c.question_details or {}
        if key in self.gate_answered or details.get("approval_id") != approval_id:
            return
        self.gate_answered.add(key)
        if len(self.gate_answered) > 1000:
            self.gate_answered.clear()
        if state == "approved":
            c.answer(task_id, "yes")
        else:
            if state == "expired":
                c.event("status", "No answer on the phone in time, so I won't do it.", task=task_id)
            c.answer(task_id, "no")
            if state == "denied":
                self.phone_stopped(session, [task_id], "denied")

    def device_lock(self, session: Session, message: dict) -> None:
        """The phone locked mid-task (a secure lock: only the owner can open it), or the owner unlocked it."""
        driver = session.driver
        if hasattr(driver, "on_device_lock"):
            driver.on_device_lock(message)
        if session.coordinator.current_task:
            session.coordinator.event("status", "Unlock your phone to continue." if message.get("locked")
                                      else "The phone is unlocked. Carrying on.")

    # --- the owner stopping work from the phone ----------------------------------------

    def phone_stop_targets(self, session: Session) -> list[str]:
        """The tasks a stop on this phone ends: the running one and everything queued."""
        c = session.coordinator
        with c._lock:
            return [t for t, i in c.task_info.items() if i["status"] not in ("succeeded", "failed", "cancelled")]

    def phone_stopped(self, session: Session, task_ids: list[str], how: str) -> None:
        """The owner stopped these tasks from the phone (Stop, "stop", or denying an approval)."""
        for listener in self.phone_stop_listeners:
            try:
                listener(session, list(task_ids), how)
            except Exception:
                pass

    def confirm_on_phone(self, session: Session) -> bool:
        """True while the pending confirmation may only be approved on the phone, so the dashboard
        shows Decline but not Approve. That's every confirmation on an app phone (approvals need the
        screen-lock prompt); phones without the app (adb) can still be approved from the dashboard."""
        snap = session.coordinator.snapshot()
        if snap["question_kind"] not in ("confirm", "plan") or not snap["task_id"]:
            return False
        return not getattr(session.coordinator, "text_approvals", True)

    def resume(self, session: Session) -> None:
        """After a (re)connected phone got its welcome: show who's using it and re-ask pending approvals."""
        if session.controller and session.controller[1]:
            self._post(session, {"type": "event", "kind": "controller", "message": session.controller[1]})
        with self._approvals_lock:
            pending = [(a, r) for a, r in self.approvals.items() if r["session"].account_id == session.account_id]
        for approval_id, record in pending:
            if record.get("kind") == "mode_change":
                if record["session"] is session:
                    self._send_approval(session, approval_id, record)
                continue
            info = record["session"].coordinator.task(record["task_id"])
            if info and info["status"] == "needs_confirmation" and self._may_answer(session, record["session"]):
                self._send_approval(session, approval_id, record)  # same id: phones ignore ones they answered
        self.push_permissions(session)

    # --- permission modes (openmob/permissions.py) ---------------------------------------

    def permission_settings(self, session_id: str) -> dict:
        settings = self.permissions.get(session_id)
        if settings == perm.default_settings():  # a phone without a setting: the server's default mode
            settings = {**settings, "default_mode": perm.tightest(self.cfg.default_permission_mode, perm.DEFAULT_MODE)}
        return perm.normalize(settings)

    def task_permissions(self, session_id: str, mode: str | None = None) -> perm.TaskPermissions:
        """A task's policy: `mode` (or the phone's default), always checked against the live settings."""
        requested = mode if mode in perm.MODES else self.permission_settings(session_id)["default_mode"]
        return perm.TaskPermissions(requested, lambda: self.permission_settings(session_id))

    def check_mode(self, session_id: str, mode: str | None, max_mode: str = "never") -> str:
        """The mode a new task runs in: `mode` (None: the phone's default, tightened to `max_mode`),
        or ModeNotAllowed when it's looser than the phone's ceiling or `max_mode`."""
        settings = self.permission_settings(session_id)
        ceiling = perm.effective_ceiling(settings)
        if mode is None:
            return perm.tightest(settings["default_mode"], ceiling, max_mode)
        if mode not in perm.MODES:
            raise perm.PermissionsInvalid(f"permission_mode must be one of: {', '.join(perm.MODES)}.")
        allowed = perm.tightest(ceiling, max_mode)
        if perm.looser(mode, allowed):
            who = ("this key" if perm.looser(ceiling, max_mode) else "this phone" if perm.looser(max_mode, ceiling)
                   else "this key and phone")
            raise perm.ModeNotAllowed(
                f"permission_mode {mode} is looser than {who} allows ({allowed}). Only the phone's owner can allow "
                "more: a key's max_mode in the developer console, a phone's ceiling on the phone itself.")
        return mode

    def change_permissions(self, session: Session, patch: dict, requester: str = "You",
                           max_mode: str = "never") -> dict:
        """Apply a change. A pure tightening applies now: {"status": "applied", "settings"}. Anything
        that loosens waits for a screen-lock-signed approval on that phone: {"status":
        "needs_phone_approval", "approval_id", "question"}. ModeNotAllowed: a loosening past `max_mode`
        (an API key's), or one this phone can't sign (hosted, no approval key). PhoneUnavailable: offline."""
        sid = session.info.id
        new, loosens = perm.apply_patch(self.permission_settings(sid), patch, self.cfg.app_categories)
        if not loosens:
            self.permissions.set(sid, new)
            self.push_permissions(session)
            return {"status": "applied", "settings": perm.public(new)}
        wanted = "never" if any(x.startswith(("never:", "money:")) for x in loosens) else max(
            [x.split(":")[1] for x in loosens if x.startswith("ceiling:")] + ["trusted" if any(
                x.startswith("trusted:") for x in loosens) else "ask"], key=perm.rank)
        if perm.looser(wanted, max_mode):
            raise perm.ModeNotAllowed(f"This key may ask for at most {max_mode}; {wanted} needs a key whose max_mode "
                                      "allows it.")
        if not self._may_loosen(session):
            raise perm.ModeNotAllowed("This phone's app can't sign approvals yet, so it stays at auto or tighter. "
                                      "Update the OpenMob app and pair it again.")
        if session.link is None:
            if isinstance(session.driver, RemoteDriver) or session.account_id is not None:
                raise perm.PhoneUnavailable("The phone must be online to approve this. Open the OpenMob app on it.")
            # A USB/adb phone in local token mode has no approval prompt: whoever holds the dashboard
            # token controls the computer it's plugged into, so the change applies (docs/safety.md).
            self.permissions.set(sid, new)
            return {"status": "applied", "settings": perm.public(new)}
        return self.request_permission_change(session, patch, loosens, requester)

    def request_permission_change(self, session: Session, patch: dict, loosens: list[str], requester: str) -> dict:
        """Ask the phone itself (only that phone: its own screen lock) to approve a loosening. A newer
        request for the same phone replaces an older one still pending."""
        approval_id = uuid.uuid4().hex
        question = perm.describe_loosening(loosens)
        action = {"tool": "mode_change", "input": f"{question}\n{json.dumps(patch, sort_keys=True)}"}
        record = {"session": session, "task_id": f"mode_change:{approval_id[:12]}", "question": question,
                  "action": action, "digest": action_digest(action),
                  "who": {"name": requester, "human": True}, "sent": set(), "kind": "mode_change", "details": {},
                  "patch": dict(patch), "change": {"loosens": loosens, **{k: v for k, v in patch.items()
                                                                          if k in ("default_mode", "ceiling", "never",
                                                                                   "allow_money")}}}
        with self._approvals_lock:
            older = [(a, r) for a, r in self.approvals.items()
                     if r.get("kind") == "mode_change" and r["session"] is session]
            for a, _ in older:
                del self.approvals[a]
            self.approvals[approval_id] = record
            self.permission_requests[approval_id] = {
                "id": approval_id, "status": "pending", "device_id": session.info.id, "account_id": session.account_id,
                "question": question, "loosens": loosens, "created": time.time(), "decided": None}
            for a, _ in older:
                self.permission_requests[a].update(status="replaced", decided=time.time())
            while len(self.permission_requests) > 500:
                self.permission_requests.pop(next(iter(self.permission_requests)))
        for a, r in older:
            self._dismiss(a, r)
        self._send_approval(session, approval_id, record)
        self._expire_later(approval_id)
        session.coordinator.event("status", f"Asked on the phone: {question}", task=None)
        return {"status": "needs_phone_approval", "approval_id": approval_id, "question": question}

    def _resolve_permission_change(self, session: Session, approval_id: str, record: dict, approved: bool,
                                   message: dict) -> bool:
        target = record["session"]
        if session is not target:
            return False  # only the phone being loosened can approve it, with its own screen lock
        if approved and (not self._may_loosen(session) or (self.approval_verifier is not None and not
                                                           self.approval_verifier(session, approval_id, record,
                                                                                  message))):
            session.coordinator.event("status", "A permission change wasn't signed with this phone's screen lock, "
                                                "so it was ignored.", task=None)
            return False
        with self._approvals_lock:
            if self.approvals.pop(approval_id, None) is None:
                return False
        if approved:
            try:
                new, _ = perm.apply_patch(self.permission_settings(target.info.id), record["patch"],
                                          self.cfg.app_categories)
            except perm.PermissionsInvalid:
                self._decide_request(approval_id, "denied")
                return False
            self.permissions.set(target.info.id, new)
            target.coordinator.event("status", f"Approved on the phone: {record['question']}", task=None)
            self.push_permissions(target)
        self._decide_request(approval_id, "approved" if approved else "denied")
        return True

    def _decide_request(self, approval_id: str, status: str) -> None:
        with self._approvals_lock:
            row = self.permission_requests.get(approval_id)
            if row and row["status"] == "pending":
                row.update(status=status, decided=time.time())

    def permission_request(self, account_id: str | None, session_id: str, approval_id: str) -> dict | None:
        with self._approvals_lock:
            row = self.permission_requests.get(approval_id)
            if not row or row["device_id"] != session_id or (account_id is not None and row["account_id"] != account_id):
                return None
            return {k: row[k] for k in ("id", "status", "device_id", "question", "created", "decided")}

    def push_permissions(self, session: Session) -> None:
        """Tell the phone its mode, so its notification can show the `never` banner ("Turn off"), which
        says "including purchases" while never_money is on."""
        settings = self.permission_settings(session.info.id)
        self._post(session, {"type": "permissions", "default_mode": settings["default_mode"],
                             "ceiling": perm.effective_ceiling(settings), "never_until": settings["never_until"],
                             "never_money": settings["never_money"]})

    def phone_turned_off_never(self, session: Session) -> None:
        """"Turn off" on the phone: back to auto. A tightening, so it needs no signature."""
        self.change_permissions(session, {"never": "off"})
        session.coordinator.event("status", "Turned off on the phone: agents ask again.", task=None)

    def _create(self, info: DeviceInfo, driver: Device, account_id: str | None = None) -> Session:
        coordinator = Coordinator(self.speaker, history=self.history)
        coordinator.account_id = account_id
        coordinator.device_id = info.id
        coordinator.device_model = info.model
        coordinator.question_timeout = float(getattr(self.cfg, "question_timeout_seconds", 300) or 0)
        coordinator.permissions_for = lambda mode, sid=info.id: self.task_permissions(sid, mode)
        cache = Path(DEFAULT_LABEL_CACHE).with_name(f"labels-{info.id}.json")
        coordinator.agent = self.build_agent(self.cfg, coordinator, driver, cache)
        if coordinator.agent is not None:
            coordinator.agent.platform = info.platform
            coordinator.agent.memory = self.memory
            coordinator.agent.account_id = account_id
        coordinator.start()
        threading.Thread(target=self._index_apps, args=(coordinator,), daemon=True).start()
        return Session(info, coordinator, driver, account_id)

    @staticmethod
    def _index_apps(coordinator: Coordinator) -> None:
        """Read app display names up front so the first "open X" is instant."""
        try:
            coordinator.agent.resolver.ensure_labels()
        except DeviceError:
            pass  # phone went away; names are read on demand next time

    def remove(self, session_id: str, reason: str = "device_revoked") -> None:
        """Forget a phone entirely (e.g. once unpaired): it disappears from every phone list, and its
        running and queued tasks end cancelled with `reason`."""
        with self._lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.online = False
            self._stop(session, reason)

    def get(self, device_id: str) -> Session | None:
        return self.sessions.get(device_id)

    def sessions_for(self, account_id: str | None) -> list[Session]:
        """Sessions the dashboard may show: this account's phones, or all when unscoped (local mode)."""
        if account_id is None:
            return list(self.sessions.values())
        return [s for s in self.sessions.values() if s.account_id == account_id]

    def first_online(self) -> Session | None:
        return next((s for s in self.sessions.values() if s.online), None)
