"""Routes commands from every input (Mac mic, phone page, terminal) to one agent worker."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque

from .agent import Agent
from .voice.tts import Speaker

STOP_PHRASES = {"stop", "cancel", "abort", "stop it", "never mind", "nevermind", "stop stop"}

ICONS = {"jev": "🔎", "status": "·", "thought": "💭", "action": "👉", "error": "⚠️ ", "question": "❓", "result": "✅", "heard": "🎙"}


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

    def __init__(self, speaker: Speaker):
        self.agent: Agent | None = None
        self.speaker = speaker
        self.tasks: queue.Queue[tuple[str, str]] = queue.Queue()
        self.answers: queue.Queue[str] = queue.Queue()
        self.cancel = threading.Event()
        self.log: deque[dict] = deque(maxlen=200)
        self.current: str | None = None
        self.question: str | None = None
        self.listeners: list = []  # callables(kind, message), e.g. forward to the phone app
        self._lock = threading.Lock()

    # --- inputs ----------------------------------------------------------

    def submit(self, text: str, source: str) -> str:
        """Handle one utterance. Returns what happened: queued, answered, stopped, or ignored."""
        text = text.strip()
        if not text:
            return "ignored"
        self.event("heard", f"[{source}] {text}")
        with self._lock:
            if self.question is not None:
                if is_stop(text):
                    self.stop()
                    return "stopped"
                self.answers.put(text)
                return "answered"
            if is_stop(text):
                if self.current is None and self.tasks.empty():
                    return "ignored"
                self.stop()
                return "stopped"
            busy = self.current is not None
        self.tasks.put((text, source))
        if busy:
            self.event("status", "Queued until the current task finishes.")
        return "queued"

    def stop(self) -> None:
        while not self.tasks.empty():
            try:
                self.tasks.get_nowait()
            except queue.Empty:
                break
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

    def event(self, kind: str, message: str) -> None:
        self.log.append({"time": time.time(), "kind": kind, "message": message})
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
            text, source = self.tasks.get()
            self.cancel.clear()
            with self._lock:
                self.current = text
            self.event("status", f"Working on: {text}")
            self.speaker.say("On it.")
            try:
                # The phone page is in the phone's browser, so get it out of the way first.
                result = self.agent.run(text, self.cancel, go_home=(source == "phone"))
                self.event("result" if result.success else "error", f"{result.summary} ({result.steps} steps)")
                self.speaker.say(result.summary)
            except Exception as exc:  # keep the worker alive for the next command
                self.event("error", f"{type(exc).__name__}: {exc}")
                self.speaker.say("Sorry, something went wrong.")
            finally:
                with self._lock:
                    self.current = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": "asking" if self.question else ("running" if self.current else "idle"),
                "current": self.current,
                "question": self.question,
                "queued": self.tasks.qsize(),
                "log": list(self.log)[-60:],
            }
