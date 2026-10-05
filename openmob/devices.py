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
        self.approvals: dict[str, str] = {}  # approval id sent to the phone → the task waiting on it
        # (task id, who) of the last API task to run here; who is "" once it finished
        self.controller: tuple[str, str] | None = None

    def describe(self) -> dict:
        snap = self.coordinator.snapshot()
        return {
            "id": self.info.id,
            "model": self.info.model,
            "connection": self.info.connection,
            "platform": self.info.platform,
            "online": self.online,
            "state": snap["state"],
            "current": snap["current"],
        }


class DeviceManager:
    # The phone dismisses an unanswered approval after about 2 minutes and sends nothing, so the
    # server treats silence as "no".
    approval_timeout = 120.0

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
        self._lock = threading.Lock()
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
        with self._lock:
            session = self.sessions.get(info.id)
            if session and session.account_id != account_id:
                # Same device id, different owner: never hand over the old session or its history.
                session.coordinator.stop()
                del self.sessions[info.id]
                session = None
            if session:
                session.driver.attach(link)
                session.info = info
            else:
                session = self._create(info, RemoteDriver(link), account_id)
                self.sessions[info.id] = session
                # Mirror agent activity to the phone so its app can show or speak it.
                session.coordinator.listeners.append(
                    lambda kind, message, s=session: s.link and s.link.notify(kind, message)
                )
                session.coordinator.entry_listeners.append(lambda entry, s=session: self._on_entry(s, entry))
            session.link = link
            session.online = True
            session.driver.video_available = bool(device.get("video"))
            return session

    def detach_remote(self, session: Session, link: PhoneLink) -> None:
        with self._lock:
            if session.link is link:  # ignore a stale socket closing after a reconnect
                session.online = False
                session.link = None
                session.coordinator.stop()

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
            approval_id = uuid.uuid4().hex
            with self._lock:
                session.approvals[approval_id] = task_id
            self._post(session, self._approval_request(approval_id, task_id, entry["message"], who))
            timer = threading.Timer(self.approval_timeout, self._expire_approval, (session, approval_id))
            timer.daemon = True
            timer.start()

    @staticmethod
    def _approval_request(approval_id: str, task_id: str, question: str, who: dict | None) -> dict:
        return {"type": "approval_request", "id": approval_id, "task_id": task_id, "question": question,
                "requester": who["name"] if who else "You"}

    def _forget_approvals(self, session: Session, task_id: str) -> None:
        with self._lock:
            for approval_id in [a for a, t in session.approvals.items() if t == task_id]:
                del session.approvals[approval_id]

    @staticmethod
    def _post(session: Session, message: dict) -> None:
        if session.link:
            session.link.post(message)

    def resolve_approval(self, session: Session, approval_id: str, approved: bool) -> bool:
        """The owner answered an approval_request on the phone (after the screen-lock check).
        Ignored (False) unless that request's task is still waiting for this confirmation."""
        with self._lock:
            task_id = session.approvals.pop(str(approval_id or ""), None)
        if task_id is None:
            return False
        info = session.coordinator.task(task_id)
        if not info or info["status"] != "needs_confirmation":
            return False
        if not session.coordinator.answer(task_id, "yes" if approved else "no"):
            return False
        for listener in self.approval_listeners:
            try:
                listener(task_id, approved)
            except Exception:
                pass
        return True

    def _expire_approval(self, session: Session, approval_id: str) -> None:
        """Nobody answered on the phone in time: decline, so the agent never acts on silence."""
        with self._lock:
            task_id = session.approvals.pop(approval_id, None)
        info = session.coordinator.task(task_id) if task_id else None
        if info and info["status"] == "needs_confirmation":
            session.coordinator.event("status", "No answer on the phone in time, so I won't do it.", task=task_id)
            session.coordinator.answer(task_id, "no")

    def resume(self, session: Session) -> None:
        """After a (re)connected phone got its welcome: show who's using it and re-ask pending approvals."""
        if session.controller and session.controller[1]:
            self._post(session, {"type": "event", "kind": "controller", "message": session.controller[1]})
        with self._lock:
            pending = list(session.approvals.items())
        for approval_id, task_id in pending:
            info = session.coordinator.task(task_id)
            if info and info["status"] == "needs_confirmation":
                self._post(session, self._approval_request(approval_id, task_id, info["question"],
                                                           self._requester(task_id)))

    def _create(self, info: DeviceInfo, driver: Device, account_id: str | None = None) -> Session:
        coordinator = Coordinator(self.speaker, history=self.history)
        coordinator.account_id = account_id
        coordinator.device_id = info.id
        coordinator.device_model = info.model
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

    def remove(self, session_id: str) -> None:
        """Forget a phone entirely (e.g. once revoked): it disappears from every phone list."""
        with self._lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.online = False
            session.coordinator.stop()

    def get(self, device_id: str) -> Session | None:
        return self.sessions.get(device_id)

    def sessions_for(self, account_id: str | None) -> list[Session]:
        """Sessions the dashboard may show: this account's phones, or all when unscoped (local mode)."""
        if account_id is None:
            return list(self.sessions.values())
        return [s for s in self.sessions.values() if s.account_id == account_id]

    def first_online(self) -> Session | None:
        return next((s for s in self.sessions.values() if s.online), None)
