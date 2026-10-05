"""Where each agent step's time goes (measurement only; nothing here changes what the agent does).

A step is one tool call. Its time runs from the end of the previous step's action (or the start of
the task) to the end of its own action, so every millisecond of a task is counted on exactly one
step. Phases are exclusive: time inside a nested phase (a classifier call during an observation, an
approval wait during a tap) counts there and not in the enclosing one.

- observe: foreground app, screenshot, accessibility elements and OCR (classifier calls excluded)
- jev:     the screen classifier (assess_screen, assess_tap) and classifier app picking
- wait:    human or loading time: loading-screen waits, risky-tap approvals, ask_user answers
- model:   the main model call(s), including a nudge call that produced no tool call
- action:  the phone action and the tool's own work (app resolution included)
- settle:  the post-action settle sleep before the next observation, and the `wait` tool's sleep.
           It is counted on the step it delayed: the step after the action that caused it.
- total:   wall time of the step. total minus the phases is untracked overhead (bookkeeping).

When a model response holds several tool calls, the shared observe/model time is on the first.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

PHASES = ("observe", "jev", "wait", "model", "action", "settle")
COLUMNS = tuple(f"{p}_ms" for p in PHASES) + ("total_ms",)


class StepTimer:
    def __init__(self):
        self._stack: list[list] = []  # [phase, seconds spent in nested phases]
        self.task: dict[str, float] = dict.fromkeys(PHASES + ("total",), 0.0)  # seconds, whole task
        self.reset()

    def reset(self) -> None:
        """Start a new task."""
        self._stack.clear()
        self.task = dict.fromkeys(PHASES + ("total",), 0.0)
        self._start_step()

    def _start_step(self) -> None:
        self._seconds = dict.fromkeys(PHASES, 0.0)
        self._step_started = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        self._stack.append([name, 0.0])
        try:
            yield
        finally:
            _, nested = self._stack.pop()
            elapsed = time.perf_counter() - started
            self._seconds[name] += max(elapsed - nested, 0.0)
            if self._stack:
                self._stack[-1][1] += elapsed

    def timed(self, name: str, fn, *args, **kwargs):
        with self.phase(name):
            return fn(*args, **kwargs)

    def add(self, name: str, seconds: float) -> None:
        """Time measured elsewhere (a hook) inside whatever phase is open now."""
        self._seconds[name] += seconds
        if self._stack:
            self._stack[-1][1] += seconds

    def take(self) -> dict[str, int]:
        """This step's phases in ms (total included), and start the next step."""
        total = time.perf_counter() - self._step_started
        out = {f"{p}_ms": int(round(s * 1000)) for p, s in self._seconds.items()}
        out["total_ms"] = int(round(total * 1000))
        for p, s in self._seconds.items():
            self.task[p] += s
        self.task["total"] += total
        self._start_step()
        return out

    def task_totals(self) -> dict[str, int]:
        """The whole task in ms, including time after the last recorded step."""
        tail = dict(self._seconds)
        tail_total = time.perf_counter() - self._step_started
        out = {f"{p}_ms": int(round((self.task[p] + tail[p]) * 1000)) for p in PHASES}
        out["total_ms"] = int(round((self.task["total"] + tail_total) * 1000))
        return out
