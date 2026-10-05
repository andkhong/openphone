"""Routes commands from every input (Mac mic, phone page, terminal) to one agent worker."""

from __future__ import annotations

import itertools
import queue
import re
import uuid
import threading
import time
from collections import deque

from .agent import Agent
from .usage import Usage
from .voice.tts import Speaker

_CURRENT = object()  # event(): tag with whichever task is running

STOP_PHRASES = {"stop", "cancel", "abort", "stop it", "never mind", "nevermind", "stop stop"}

ICONS = {"usage": "🧮", "jev": "🔎", "status": "·", "thought": "💭", "action": "👉", "error": "⚠️ ", "question": "❓", "result": "✅", "heard": "🎙"}


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
        self.tasks: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self.answers: queue.Queue[str] = queue.Queue()
        self.cancel = threading.Event()
        self.log: deque[dict] = deque(maxlen=500)
        self.current_task: str | None = None  # id of the running task; every event is tagged with one
        self._seq = itertools.count(1)
        self.current: str | None = None
        self.question: str | None = None
        self.listeners: list = []
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
        # Answers and stops belong to the running task's turn; a new request starts its own.
        # Ids are random so turns stay unique in the history across server restarts.
        task_id = uuid.uuid4().hex[:12] if kind == "task" else running
        if kind == "task" and self.chat_id is None and self.history is not None:
            self.chat_id = self.history.create_chat(self.account_id, self.device_id, self.device_model, text)
        self.event("heard", f"[{source}] {text}", task=task_id)
        if kind == "answer":
            self.answers.put(text)
            return "answered"
        if kind == "stop":
            self.stop()
            return "stopped"
        if kind == "ignore":
            return "ignored"
        self.tasks.put((text, source, task_id))
        if running is not None:
            self.event("status", "Queued until the current task finishes.", task=task_id)
        return "queued"

    def stop(self) -> None:
        while not self.tasks.empty():
            try:
                _, _, task_id = self.tasks.get_nowait()
            except queue.Empty:
                break
            self.event("error", "Cancelled before it started.", task=task_id, data={"final": True, "success": False})
        if self.current is not None:
            self.cancel.set()
            self.event("status", "Stopping…")

    # --- UserIO ----------------------------------------------------------

    def ask(self, question: str) -> str:
        with self._lock:
            self.question = question
        self.event("question", question)
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

    def event(self, kind: str, message: str, task=_CURRENT, data: dict | None = None) -> None:
        """Log one entry. `task` defaults to the running task; `data` carries structured details
        for the dashboard (e.g. {"final": True, "steps": 4} on a task's outcome, token counts)."""
        task_id = self.current_task if task is _CURRENT else task
        entry = {"seq": next(self._seq), "time": time.time(), "kind": kind, "message": message, "task": task_id}
        if data:
            entry["data"] = data
        self.log.append(entry)
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

    # --- worker ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._work, name="openmob-agent", daemon=True).start()

    def _work(self) -> None:
        while True:
            text, source, task_id = self.tasks.get()
            self.cancel.clear()
            with self._lock:
                self.current = text
                self.current_task = task_id
            self.event("status", f"Working on: {text}")
            self.speaker.say("On it.")
            try:
                # The phone page is in the phone's browser, so get it out of the way first.
                result = self.agent.run(text, self.cancel, go_home=(source == "phone"))
                self.event("result" if result.success else "error", f"{result.summary} ({result.steps} steps)",
                           data={"final": True, "success": result.success, "steps": result.steps})
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
