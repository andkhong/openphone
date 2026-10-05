"""Routes commands from every input (Mac mic, phone page, terminal) to one agent worker."""

from __future__ import annotations

import itertools
import queue
import re
import uuid
import threading
import time
from collections import OrderedDict, deque

from .agent import Agent
from .usage import Usage
from .voice.tts import Speaker

_CURRENT = object()  # event(): tag with whichever task is running

STOP_PHRASES = {"stop", "cancel", "abort", "stop it", "never mind", "nevermind", "stop stop"}

ICONS = {"memory": "📌", "usage": "🧮", "jev": "🔎", "status": "·", "thought": "💭", "action": "👉", "error": "⚠️ ", "question": "❓", "result": "✅", "heard": "🎙"}


FINAL_STATUSES = {"succeeded", "failed", "cancelled"}
KEEP_TASKS = 200  # per phone: finished tasks stay readable here until they age out (then from History)
KEEP_TASK_EVENTS = 500


def format_event(kind: str, message: str) -> str:
    return f"{ICONS.get(kind, '-')} {message}"


def is_stop(text: str) -> bool:
    return " ".join(re.sub(r"[^a-z ]", " ", text.lower()).split()) in STOP_PHRASES


class ConsoleIO:
    """UserIO for one-off `openmob do` runs: questions are answered in the terminal."""

    def __init__(self, speaker: Speaker):
        self.speaker = speaker

    def ask(self, question: str) -> str:
        self.speaker.say(question)
        try:
            return input(format_event("question", question) + "\n> ")
        except EOFError:  # no terminal to answer from
            print()
            return ""

    def event(self, kind: str, message: str) -> None:
        print(format_event(kind, message), flush=True)


class Coordinator:
    """Queues tasks, runs them one at a time, and routes answers to the agent's questions."""

    def __init__(self, speaker: Speaker, history=None):
        self.agent: Agent | None = None
        self.history = history  # History store, or None (tests, no persistence)
        self.account_id: str | None = None  # owner of this phone (hosted mode)
        self.device_id = ""
        self.device_model = ""
        self.chat_id: str | None = None  # created on the first request after "New chat"
        self.speaker = speaker
        self.tasks: queue.Queue[tuple[str, str, str, dict]] = queue.Queue()
        self.answers: queue.Queue[str] = queue.Queue()
        self.cancel = threading.Event()
        self.log: deque[dict] = deque(maxlen=500)
        self.current_task: str | None = None  # id of the running task; every event is tagged with one
        self._seq = itertools.count(1)
        self.current: str | None = None
        self.question: str | None = None
        self.question_kind: str | None = None  # "question" (ask_user) or "confirm" (risky action)
        self.listeners: list = []
        self.entry_listeners: list = []  # callables(entry): every log entry, with its task id
        # Per-task records (status, steps, outcome, events) for the developer API, newest last.
        self.task_info: OrderedDict[str, dict] = OrderedDict()
        self._skip: set[str] = set()  # queued tasks cancelled before they started
        self.session_usage = Usage()  # all tasks on this phone since the server started  # callables(kind, message), e.g. forward to the phone app
        self._lock = threading.Lock()

    # --- inputs ----------------------------------------------------------

    def submit(self, text: str, source: str) -> str:
        """Handle one utterance. Returns what happened: queued, answered, stopped, or ignored."""
        text = text.strip()
        if not text:
            return "ignored"
        with self._lock:
            running = self.current_task
            if self.question is not None:
                kind = "stop" if is_stop(text) else "answer"
            elif is_stop(text):
                kind = "stop" if (self.current is not None or not self.tasks.empty()) else "ignore"
            else:
                kind = "task"
        if kind == "task":
            self.enqueue(text, source)
            return "queued"
        # Answers and stops belong to the running task's turn.
        self.event("heard", f"[{source}] {text}", task=running)
        if kind == "answer":
            self.answers.put(text)
            return "answered"
        if kind == "stop":
            self.stop()
            return "stopped"
        return "ignored"

    @staticmethod
    def new_task_id() -> str:
        # Random, so turns stay unique in the history across server restarts.
        return uuid.uuid4().hex[:12]

    def enqueue(self, text: str, source: str, options: dict | None = None, task_id: str | None = None) -> str:
        """Queue a task as-is (never read as an answer or "stop") and return its id.

        `options` go to Agent.run for this task only: model, max_steps, blocked. Pass `task_id`
        (from new_task_id) to know the id before the task can start."""
        task_id = task_id or self.new_task_id()
        with self._lock:
            running = self.current_task
            self.task_info[task_id] = {
                "id": task_id, "status": "queued", "request": text, "source": source,
                "created": time.time(), "started": None, "finished": None, "steps": 0,
                "success": None, "summary": None, "question": None, "usage": None, "events": [],
            }
            while len(self.task_info) > KEEP_TASKS:
                self.task_info.popitem(last=False)
        if self.chat_id is None and self.history is not None:
            self.chat_id = self.history.create_chat(self.account_id, self.device_id, self.device_model, text)
        self.event("heard", f"[{source}] {text}", task=task_id)
        self.tasks.put((text, source, task_id, options or {}))
        if running is not None:
            self.event("status", "Queued until the current task finishes.", task=task_id)
        return task_id

    def task(self, task_id: str) -> dict | None:
        """A copy of one task's record (None once it has aged out)."""
        with self._lock:
            info = self.task_info.get(task_id)
            return {**info, "events": list(info["events"])} if info else None

    def cancel_task(self, task_id: str) -> bool:
        """Cancel one task, queued or running. False if it's unknown or already finished."""
        with self._lock:
            info = self.task_info.get(task_id)
            if not info or info["status"] in FINAL_STATUSES:
                return False
            running = self.current_task == task_id
            if not running:
                self._skip.add(task_id)
        if running:
            self.cancel.set()
            self.event("status", "Stopping…", task=task_id)
        else:
            self.event("error", "Cancelled before it started.", task=task_id,
                       data={"final": True, "success": False, "cancelled": True})
        return True

    def answer(self, task_id: str, text: str) -> bool:
        """Answer the running task's pending question. False if that task isn't asking."""
        with self._lock:
            if self.current_task != task_id or self.question is None:
                return False
        self.event("heard", f"[api] {text}", task=task_id)
        self.answers.put(text)
        return True

    def stop(self) -> None:
        while not self.tasks.empty():
            try:
                _, _, task_id, _ = self.tasks.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                if task_id in self._skip:  # already cancelled on its own
                    self._skip.discard(task_id)
                    continue
            self.event("error", "Cancelled before it started.", task=task_id,
                       data={"final": True, "success": False, "cancelled": True})
        if self.current is not None:
            self.cancel.set()
            self.event("status", "Stopping…")

    # --- UserIO ----------------------------------------------------------

    def ask(self, question: str, kind: str = "question") -> str:
        with self._lock:
            self.question = question
            self.question_kind = kind
        self.event("question", question, data={"confirm": True} if kind == "confirm" else None)
        self.speaker.say(question)
        try:
            while not self.cancel.is_set():
                try:
                    return self.answers.get(timeout=0.5)
                except queue.Empty:
                    continue
            return "no"
        finally:
            with self._lock:
                self.question = None
                self.question_kind = None
            self._set_status(self.current_task, "running")

    def confirm(self, question: str) -> str:
        """The agent wants approval for a risky action (buy, send, delete…)."""
        return self.ask(question, kind="confirm")

    def event(self, kind: str, message: str, task=_CURRENT, data: dict | None = None) -> None:
        """Log one entry. `task` defaults to the running task; `data` carries structured details
        for the dashboard (e.g. {"final": True, "steps": 4} on a task's outcome, token counts)."""
        task_id = self.current_task if task is _CURRENT else task
        entry = {"seq": next(self._seq), "time": time.time(), "kind": kind, "message": message, "task": task_id}
        if data:
            entry["data"] = data
        self.log.append(entry)
        self._track(entry)
        if self.history is not None and self.chat_id is not None:
            try:
                self.history.record(self.chat_id, entry)
            except Exception as exc:  # history must never break the agent
                print(format_event("error", f"History not saved: {exc}"), flush=True)
        print(format_event(kind, message), flush=True)
        for listener in self.listeners:
            try:
                listener(kind, message)
            except Exception:  # a broken listener must never stop the agent
                pass
        for listener in self.entry_listeners:
            try:
                listener(entry)
            except Exception:
                pass

    def _track(self, entry: dict) -> None:
        """Keep the task's record (for the developer API) in step with its log."""
        data = entry.get("data") or {}
        with self._lock:
            info = self.task_info.get(entry["task"]) if entry["task"] else None
            if info is None:
                return
            info["events"].append(entry)
            del info["events"][:-KEEP_TASK_EVENTS]
            kind = entry["kind"]
            if data.get("final"):
                info["status"] = "cancelled" if data.get("cancelled") else ("succeeded" if data.get("success") else "failed")
                info["success"] = bool(data.get("success"))
                info["summary"] = entry["message"].rsplit(" (", 1)[0] if entry["message"].endswith(" steps)") else entry["message"]
                info["steps"] = data.get("steps", info["steps"])
                info["finished"] = entry["time"]
                info["question"] = None
            elif kind == "usage":
                info["usage"] = data
            elif info["status"] in FINAL_STATUSES:
                return
            elif kind == "question":
                info["status"] = "needs_confirmation" if data.get("confirm") else "needs_input"
                info["question"] = entry["message"]
            elif kind in ("action", "error"):
                info["steps"] += 1

    def _set_status(self, task_id: str | None, status: str) -> None:
        with self._lock:
            info = self.task_info.get(task_id) if task_id else None
            if info and info["status"] not in FINAL_STATUSES:
                info["status"] = status
                info["question"] = None
                if status == "running" and info["started"] is None:
                    info["started"] = time.time()

    # --- worker ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._work, name="openmob-agent", daemon=True).start()

    def _work(self) -> None:
        while True:
            text, source, task_id, options = self.tasks.get()
            with self._lock:
                if task_id in self._skip:  # cancelled while it waited
                    self._skip.discard(task_id)
                    continue
            self.cancel.clear()
            with self._lock:
                self.current = text
                self.current_task = task_id
            self._set_status(task_id, "running")
            self.event("status", f"Working on: {text}")
            self.speaker.say("On it.")
            try:
                # The phone page is in the phone's browser, so get it out of the way first.
                result = self.agent.run(text, self.cancel, go_home=(source == "phone"), **options)
                data = {"final": True, "success": result.success, "steps": result.steps}
                if self.cancel.is_set() and not result.success:
                    data["cancelled"] = True
                self.event("result" if result.success else "error", f"{result.summary} ({result.steps} steps)", data=data)
                self.speaker.say(result.summary)
            except Exception as exc:  # keep the worker alive for the next command
                self.event("error", f"{type(exc).__name__}: {exc}", data={"final": True, "success": False})
                self.speaker.say("Sorry, something went wrong.")
            finally:
                # Count tokens even when a task fails or is stopped part-way.
                usage = getattr(self.agent, "usage", None)
                if usage is not None:
                    self.session_usage.add(usage)
                    self.event("usage", f"Tokens this task: {usage.summary()}", data=usage.as_dict())
                with self._lock:
                    self.current = None
                    self.current_task = None

    def clear_log(self) -> None:
        """New chat: empty the conversation and the agent's memory of earlier requests.
        The next request starts a new saved chat; earlier chats stay in the history."""
        with self._lock:
            if self.current is not None:
                raise RuntimeError("Stop the current task before starting a new chat.")
            self.log.clear()
            self.chat_id = None
        recent = getattr(self.agent, "recent", None)
        if recent is not None:
            recent.clear()

    def open_chat(self, chat_id: str) -> bool:
        """Show a saved chat and continue it: its log comes back, and the agent remembers its requests."""
        if self.history is None:
            return False
        events = self.history.events(self.account_id, chat_id)
        if events is None:
            return False
        with self._lock:
            if self.current is not None:
                raise RuntimeError("Stop the current task before opening another chat.")
            self.log.clear()
            for entry in events:
                self.log.append({"seq": next(self._seq), **entry})
            self.chat_id = chat_id
        recent = getattr(self.agent, "recent", None)
        if recent is not None:
            recent.clear()
            recent.extend(self.history.recent_requests(self.account_id, chat_id))
        return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": "asking" if self.question else ("running" if self.current else "idle"),
                "question_kind": self.question_kind,
                "current": self.current,
                "question": self.question,
                "queued": self.tasks.qsize(),
                "model": getattr(self.agent, "model", None),
                "task_model": getattr(self.agent, "task_model", None) if self.current else None,
                "usage": {
                    "task": self.agent.usage.as_dict() if self.current and hasattr(self.agent, "usage") else None,
                    "session": self.session_usage.as_dict(),
                },
                "task_id": self.current_task,
                "chat_id": self.chat_id,
                "log": list(self.log)[-400:],
            }
