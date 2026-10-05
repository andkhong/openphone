"""Every connected Android phone, each with its own agent, task queue, and log."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .phone.adb import AdbError, PhoneDriver
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
        if stale:
            self._stop(stale)
        if resumed:
            resumed.coordinator.event("status", "The phone is back. Carrying on.")
        return session

    def detach_remote(self, session: Session, link: PhoneLink, close_code: int | None = None) -> None:
        """The phone's socket closed. Mid-task, wait `reconnect_grace` seconds for it to come back
        before failing the task; nothing is replayed. A normal close (1000: the user tapped Stop or
        disconnected on purpose) stops the work right away, as does a drop with nothing running."""
        with self._lock:
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
        if entry["kind"] == "question" and data.get("confirm") and (who is None or who["human"]):
            self._ask_owner(session, task_id, entry["message"], who)

    # Approvals go to every online phone of the owner, not just the one acting: the owner may be
    # holding another. The first answer wins (screen lock on that phone); the others are told to
    # dismiss their prompt with approval_cancel.

    def _approval_targets(self, acting: Session) -> list[Session]:
        return [s for s in list(self.sessions.values()) if s.link and s.account_id == acting.account_id]

    def _ask_owner(self, acting: Session, task_id: str, question: str, who: dict | None) -> None:
        approval_id = uuid.uuid4().hex
        record = {"session": acting, "task_id": task_id, "question": question, "who": who, "sent": set()}
        with self._approvals_lock:
            self.approvals[approval_id] = record
        for target in self._approval_targets(acting):
            self._send_approval(target, approval_id, record)
        timer = threading.Timer(self.approval_timeout, self._expire_approval, (approval_id,))
        timer.daemon = True
        timer.start()

    def _send_approval(self, target: Session, approval_id: str, record: dict) -> None:
        acting = record["session"]
        with self._approvals_lock:
            record["sent"].add(target.info.id)
        self._post(target, {
            "type": "approval_request", "id": approval_id, "task_id": record["task_id"],
            "question": record["question"], "requester": record["who"]["name"] if record["who"] else "You",
            "device_id": acting.info.id, "device_name": acting.info.model,  # which phone would act
        })

    def _dismiss(self, approval_id: str, record: dict, except_id: str | None = None) -> None:
        """Tell every phone that was asked (but the one that answered) to drop the prompt."""
        for session_id in record["sent"]:
            target = self.sessions.get(session_id)
            if target is not None and session_id != except_id:
                self._post(target, {"type": "approval_cancel", "id": approval_id})

    def _forget_approvals(self, session: Session, task_id: str) -> None:
        with self._approvals_lock:
            gone = [(a, r) for a, r in self.approvals.items() if r["task_id"] == task_id and r["session"] is session]
            for approval_id, _ in gone:
                del self.approvals[approval_id]
        for approval_id, record in gone:
            self._dismiss(approval_id, record)

    @staticmethod
    def _post(session: Session, message: dict) -> None:
        if session.link:
            session.link.post(message)

    def resolve_approval(self, session: Session, approval_id: str, approved: bool) -> bool:
        """The owner answered an approval_request on one of their phones (after its screen-lock
        check). Ignored (False) unless that request is still open and its task still waiting."""
        with self._approvals_lock:
            record = self.approvals.get(str(approval_id or ""))
            if record is None or record["session"].account_id != session.account_id:
                return False
            del self.approvals[approval_id]
        acting, task_id = record["session"], record["task_id"]
        self._dismiss(approval_id, record, except_id=session.info.id)
        info = acting.coordinator.task(task_id)
        if not info or info["status"] != "needs_confirmation":
            return False
        if not acting.coordinator.answer(task_id, "yes" if approved else "no"):
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
        acting, task_id = record["session"], record["task_id"]
        info = acting.coordinator.task(task_id)
        if info and info["status"] == "needs_confirmation":
            acting.coordinator.event("status", "No answer on the phone in time, so I won't do it.", task=task_id)
            acting.coordinator.answer(task_id, "no")

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
        if snap["question_kind"] != "confirm" or not snap["task_id"]:
            return False
        return not getattr(session.coordinator, "text_approvals", True)

    def resume(self, session: Session) -> None:
        """After a (re)connected phone got its welcome: show who's using it and re-ask pending approvals."""
        if session.controller and session.controller[1]:
            self._post(session, {"type": "event", "kind": "controller", "message": session.controller[1]})
        with self._approvals_lock:
            pending = [(a, r) for a, r in self.approvals.items() if r["session"].account_id == session.account_id]
        for approval_id, record in pending:
            info = record["session"].coordinator.task(record["task_id"])
            if info and info["status"] == "needs_confirmation":
                self._send_approval(session, approval_id, record)  # same id: phones ignore ones they answered

    def _create(self, info: DeviceInfo, driver: Device, account_id: str | None = None) -> Session:
        coordinator = Coordinator(self.speaker, history=self.history)
        coordinator.account_id = account_id
        coordinator.device_id = info.id
        coordinator.device_model = info.model
        coordinator.question_timeout = float(getattr(self.cfg, "question_timeout_seconds", 300) or 0)
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
