"""Replaying UI paths that worked before (config.replay_paths, off by default; docs/design/fewer-model-calls.md).

After a task succeeds, its path is stored: for each step, a fingerprint of the screen it was taken on,
the app, and the action by identity (a tapped element's text, description, resource id and class; a
scroll direction; Back; the package an app launch opened). A later task with the same request replays a
stored step without the main model whenever that step's screen matches the one in front and its target
is there exactly once; the call then goes through loop._execute like any other, so every check and
approval applies. Anything else goes to the fast path or the vision model as usual.

Only a successful, navigation-only task that typed nothing, asked nothing and tapped nothing risky is
stored. Never stored: typed text, coordinates, done, links, Enter, waits, questions, OCR words,
screenshots, outcomes, or the request text (its hash keys the path).

This module holds the pure parts and the store; the loop's side is Agent._replay_step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from ..phone.adb import UIElement
from . import fast

VERSION = 1
SOURCE = "replay"  # the steps table's source for replayed steps
# Tools a path may hold: each names its target by identity, never a coordinate, and none types or commits.
REPLAYABLE = ("tap_element", "scroll", "press_key", "launch_app")
DIRECTIONS = ("up", "down", "left", "right")
BACK = "BACK"
MAX_STEPS = 20  # a longer path isn't stored
MAX_TRIES = 2  # attempts per stored step in one task: once more after a tap refused before it ran
# A tap _recheck_target refused before it ran (its element moved): worth one more try at the next look.
NOT_RUN = "no longer where it was"
# Screen states (jev.SCREEN_STATES) a stored path never runs on when the classifier is confident.
BLOCKING_STATES = {"popup", "login", "lock_screen", "loading"}
DIGITS = re.compile(r"\d+")


def goal_key(task: str) -> str:
    """The request as a key: lowercased, without leading filler, punctuation and extra spaces, then
    hashed, so the store holds no request text."""
    text = fast.LEADING_FILLER.sub("", " ".join(task.split()).lower())
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def eligible(task: str, confirm_keywords: list[str]) -> bool:
    """A request whose path may be stored or replayed: it only opens or shows things (fast.navigation_only:
    every clause starts with a navigation verb, no side-effect word, no correction)."""
    return fast.navigation_only(task, confirm_keywords)


def _token(label: str) -> str:
    return hashlib.sha256(DIGITS.sub("#", label.strip().lower()).encode()).hexdigest()[:12]


def fingerprint(elements: list[UIElement]) -> frozenset[str]:
    """What a screen shows, unreadably: one short hash per labeled accessibility element's label, digits
    masked (counts, clocks). Positions, OCR words and unlabeled views are left out."""
    return frozenset(_token(el.text or el.desc) for el in elements if fast.labeled(el))


def overlap(stored: frozenset[str], now: frozenset[str]) -> float:
    """How much of a stored screen is on this one (1.0: every one of its labels)."""
    return len(stored & now) / len(stored) if stored else 0.0


def identity(el: UIElement) -> dict:
    return {"text": el.text, "desc": el.desc, "resource_id": el.resource_id, "cls": el.cls}


def recordable(name: str, args: dict, package: str | None, elements: list[UIElement],
               launched: str | None) -> dict | None:
    """A step a path may hold, or None: a tap by element (one with a text, description or id to find it
    by again), a scroll, Back, or an app launch (by the package it opened). Nothing else: coordinate taps,
    swipes, typing, Enter, links, waits, questions, done."""
    if name == "tap_element":
        try:
            index = int(args["index"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= index < len(elements):
            return None
        el = elements[index]
        if el.cls == fast.OCR_CLASS or not (el.text or el.desc or el.resource_id):
            return None
        target = identity(el)
    elif name == "scroll":
        if args.get("direction") not in DIRECTIONS:
            return None
        target = {"direction": args["direction"]}
    elif name == "press_key":
        if str(args.get("key", "")).upper() != BACK:
            return None
        target = {"key": BACK}
    elif name == "launch_app":
        if not launched:
            return None
        target = {"app": launched}
    else:
        return None
    return {"tool": name, "target": target, "package": package or "", "screen": sorted(fingerprint(elements))}


def resolve(step: dict, package: str | None, elements: list[UIElement]) -> tuple[str, dict] | None:
    """The stored step as a tool call on this screen, or None. The app must be the stored one; a tap's
    element must be on screen exactly once, by identity, and not covered. Its index now is used (a stored
    index would be stale)."""
    if (package or "") != step.get("package", ""):
        return None
    tool, target = step.get("tool"), step.get("target") or {}
    if tool == "tap_element":
        same = [el for el in elements if el.cls != fast.OCR_CLASS and identity(el) == target]
        if len(same) != 1 or same[0].covered:
            return None
        return tool, {"index": same[0].index, "target": same[0].label}
    if tool == "scroll" and target.get("direction") in DIRECTIONS:
        return tool, {"direction": target["direction"]}
    if tool == "press_key" and target.get("key") == BACK:
        return tool, {"key": BACK}
    if tool == "launch_app" and target.get("app"):
        return tool, {"app": str(target["app"])}
    return None


def match(path: list[dict], cursor: int, package: str | None, elements: list[UIElement],
          min_overlap: float) -> tuple[int, str, dict] | None:
    """The stored step to replay on this screen: of the steps from `cursor` on, the one whose screen
    overlaps this one the most (at least `min_overlap`; the earliest on a tie) and whose target resolves
    here. (its index in the path, tool, args), or None."""
    now = fingerprint(elements)
    best = None
    for at in range(cursor, len(path)):
        step = path[at]
        score = overlap(frozenset(step.get("screen") or ()), now)
        if score < min_overlap or (best is not None and score <= best[0]):
            continue
        call = resolve(step, package, elements)
        if call is not None:
            best = (score, at, call)
    return (best[1], *best[2]) if best else None


def store_path(label_cache: Path | str | None = None) -> Path:
    """The store next to the phone's app-label cache: labels-<phone>.json -> paths-<phone>.json, else
    paths-local.json."""
    from ..phone.apps import DEFAULT_LABEL_CACHE

    cache = Path(label_cache) if label_cache else Path(DEFAULT_LABEL_CACHE)
    phone = cache.stem[len("labels-"):] if cache.stem.startswith("labels-") else "local"
    return cache.with_name(f"paths-{phone}.json")


class PathStore:
    """Stored paths by request key: one JSON file (None: memory only), the newest path per request, at
    most `max_goals` requests (the oldest go first), paths older than `retention_days` dropped when read
    or written (0: kept)."""

    def __init__(self, path: Path | str | None = None, max_goals: int = 100, retention_days: float = 30):
        self.path = Path(path) if path else None
        self.max_goals = max(1, int(max_goals))
        self.retention_days = float(retention_days or 0)
        self._lock = threading.Lock()
        self._goals: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return  # an unreadable file is an empty store; the next path stored rewrites it
        goals = data.get("goals") if isinstance(data, dict) and data.get("version") == VERSION else None
        if isinstance(goals, dict):
            self._goals = {k: v for k, v in goals.items() if isinstance(v, dict) and isinstance(v.get("steps"), list)}

    def _fresh(self, entry: dict) -> bool:
        return not self.retention_days or time.time() - float(entry.get("saved") or 0) <= self.retention_days * 86400

    def get(self, goal: str) -> list[dict] | None:
        with self._lock:
            entry = self._goals.get(goal)
            if entry is None:
                return None
            if not self._fresh(entry):
                del self._goals[goal]
                return None
            return [dict(step) for step in entry["steps"]]

    def put(self, goal: str, steps: list[dict]) -> None:
        if not steps or len(steps) > MAX_STEPS:
            return
        with self._lock:
            self._goals.pop(goal, None)  # re-inserted last: the newest
            self._goals[goal] = {"saved": time.time(), "steps": [dict(step) for step in steps]}
            self._goals = {k: v for k, v in self._goals.items() if self._fresh(v)}
            while len(self._goals) > self.max_goals:
                del self._goals[next(iter(self._goals))]
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": VERSION, "goals": self._goals}))
            os.replace(tmp, self.path)
        except OSError:
            pass  # memory still holds it; the next path stored tries again

    def __len__(self) -> int:
        return len(self._goals)
