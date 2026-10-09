"""wait_until: wait for something observable on the phone instead of sleeping a fixed time.

The model (or the loop itself, after a launch or a Play Store install) names a condition; the wait
returns as soon as it holds, or at the timeout with the screen as it is then. No model call is made
while waiting: only cheap phone reads.

How it waits, cheapest first:
- the phone evaluates the condition itself (protocol method `wait_for`, apps whose hello lists it),
  on its own accessibility events, and answers on the first match;
- otherwise the server polls the element list (`ui_elements`), paced by `wait_idle` on app phones
  (a poll right after the screen goes quiet) or a short sleep elsewhere;
- a screen without an element list (games, canvas apps) is compared as tiny grayscale frames, and
  text conditions read it with OCR when the agent has OCR.

A wait never acts on the phone. On a gatekeeper phone the reads it makes are the gate-filtered ones
(a blocked app's elements and frames come back empty or black), so a wait can't see past the gate.
"""

from __future__ import annotations

import io
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..phone.adb import Screenshot, UIElement
from ..phone.base import DeviceError

CONDITIONS = ("screen_changes", "text_appears", "text_disappears", "element_enabled", "app_in_front", "loading_done")
NEEDS_TARGET = {"text_appears", "text_disappears", "element_enabled", "app_in_front"}
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0
MIN_TIMEOUT_S = 0.5

POLL_QUIET_MS = 250  # between polls on a phone with wait_idle: poll once the screen has been quiet this long
POLL_MAX_MS = 1500  # ...or after this long on a screen that never goes quiet
POLL_SECONDS = 0.7  # between polls on phones without wait_idle (adb dumps take ~1 s on their own)
LOADING_QUIET_MS = 500  # loading_done: the screen must also have been quiet this long
PHONE_CHUNK_MS = 10_000  # one wait_for call at most, so a stop is noticed between calls
OCR_POLL_SECONDS = 1.0
# Text, element and app conditions end early ("not met: the screen is still") once the screen has been completely
# still this long with nothing loading on it: a label that isn't there on a still screen isn't coming by waiting
# (a guessed "3." in a chess game). A spinner, progress bar or "Installing…" keeps the full timeout.
STILL_EXIT_S = 2.5
STILL_EXIT_CONDITIONS = {"text_appears", "text_disappears", "element_enabled", "app_in_front"}

FRAME_SIZE = (18, 32)  # a frame for comparing treeless screens: 18 x 32 grayscale cells
FRAME_CHANGED_CELL = 24  # a cell moved this much (of 255)...
FRAME_CHANGED_SHARE = 0.02  # ...in this share of the cells: the screen changed
FRAME_STILL_CELL = 12  # no cell moved more than this between two frames: the screen is still

LOADING_CLASS = re.compile(r"progress|spinner|loading|shimmer", re.I)
LOADING_ID = re.compile(r"progress|spinner|loading|shimmer|skeleton", re.I)
# No bare percentage: stock Android Settings shows "100%" (battery) and "83%" (brightness) all the time, and
# reading them as progress held every Settings launch for the whole launch wait (measured on an emulator,
# 2026-10-08). A real progress bar is caught by its class or id.
LOADING_TEXT = re.compile(r"^\s*(loading|please wait|connecting|starting|signing in|downloading|installing|"
                          r"pending|preparing|updating)\b|\bloading\b\W*$", re.I)
DIGITS = re.compile(r"\d+")


@dataclass
class Condition:
    until: str
    target: str = ""
    pattern: re.Pattern | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    def describe(self) -> str:
        return f"{self.until}:{self.target}" if self.target else self.until

    def wire(self) -> dict:
        """The condition as the phone's wait_for takes it."""
        out = {"until": self.until}
        if self.until == "app_in_front":
            out["package"] = self.target
        elif self.pattern is not None:
            out["pattern"] = self.pattern.pattern
        if self.until == "loading_done":
            out["quiet_ms"] = LOADING_QUIET_MS
        return out


@dataclass
class WaitResult:
    met: bool
    waited_ms: int
    seen: str  # what matched, or what the screen shows at the timeout
    via: str  # phone (wait_for) | poll (element list) | frames (treeless screen) | none (stopped before reading)
    package: str | None = None
    stopped: str | None = None  # why it ended without a verdict: cancelled | blocked | disconnected | still

    def text(self, cond: Condition) -> str:
        secs = self.waited_ms / 1000
        if self.stopped == "cancelled":
            return "Stopped while waiting."
        if self.stopped == "blocked":
            return f"Stopped waiting after {secs:.1f}s: {self.seen}"
        if self.stopped == "still":
            return (f"Not met after {secs:.1f}s: the screen has been still with nothing loading, so {cond.describe()} "
                    f"isn't coming by waiting. Now: {self.seen}. Look at the screen and decide what to do next "
                    "(in a game, wait_until screen_changes instead of guessing at text).")
        if self.met:
            return f"{cond.describe()} met after {secs:.1f}s: {self.seen}"
        return (f"Timed out after {secs:.1f}s waiting for {cond.describe()}. Now: {self.seen}. Look at the "
                "screen and decide what to do next.")


def parse(args: dict) -> Condition:
    """The model's arguments as a condition; ValueError (shown to the model) when they don't make one."""
    until = str(args.get("until", "")).strip()
    if until not in CONDITIONS:
        raise ValueError(f"until must be one of {', '.join(CONDITIONS)}")
    target = str(args.get("target") or "").strip()
    if until in NEEDS_TARGET and not target:
        raise ValueError(f"{until} needs a target ({'a package' if until == 'app_in_front' else 'text or a regex'})")
    try:
        timeout = float(args.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    timeout = min(max(timeout, MIN_TIMEOUT_S), MAX_TIMEOUT_S)
    pattern = compile_target(target) if target and until != "app_in_front" else None
    return Condition(until, target[:200], pattern, timeout)


def compile_target(target: str) -> re.Pattern:
    """A regex, matched without case. Text that isn't a valid regex is matched literally."""
    try:
        return re.compile(target, re.I)
    except re.error:
        return re.compile(re.escape(target), re.I)


def matches(pattern: re.Pattern, value: str, exact: bool = False) -> bool:
    """The pattern is the whole text (a label "Open"), or, unless exact, stands as words inside it
    ("Welcome" in "Welcome back"; not "Open" in "Opening"). The phone's wait_for matches the same way."""
    value = value.strip()
    if not value:
        return False
    if pattern.fullmatch(value):
        return True
    if exact:
        return False
    return re.search(rf"(?<!\w)(?:{pattern.pattern})(?!\w)", value, re.I) is not None


def _labels(el: UIElement) -> tuple[str, str]:
    return el.text, el.desc


def find_text(pattern: re.Pattern, elements: list[UIElement]) -> UIElement | None:
    return next((el for el in elements if any(matches(pattern, s) for s in _labels(el))), None)


def find_enabled(pattern: re.Pattern, elements: list[UIElement]) -> UIElement | None:
    return next((el for el in elements if getattr(el, "enabled", True) and not el.covered
                 and any(matches(pattern, s, exact=True) for s in _labels(el))), None)


def loading_element(elements: list[UIElement]) -> UIElement | None:
    """A spinner, progress bar or "Loading…" label on screen."""
    for el in elements:
        if LOADING_CLASS.search(el.cls or "") or LOADING_ID.search(el.resource_id or ""):
            return el
        if any(LOADING_TEXT.search(s) for s in _labels(el) if s):
            return el
    return None


def labeled(elements: list[UIElement]) -> bool:
    return any(el.text or el.desc for el in elements)


def signature(package: str | None, elements: list[UIElement]) -> tuple:
    """What the screen shows, for "did it change": the labels and ids present. Positions, numbers (a
    clock, a progress percentage) and OCR text are left out, so ticking and settling aren't changes."""
    return package, frozenset((DIGITS.sub("#", el.text), DIGITS.sub("#", el.desc), el.resource_id, el.cls, el.checked)
                              for el in elements if el.cls != "OCRText")


def frame(shot: Screenshot) -> list[int]:
    """The screen as FRAME_SIZE grayscale cells (portrait order; a landscape frame is turned)."""
    from PIL import Image

    img = Image.open(io.BytesIO(shot.jpeg))
    img.draft("L", (64, 64))
    img = img.convert("L")
    if img.width > img.height:
        img = img.rotate(90, expand=True)
    return list(img.resize(FRAME_SIZE).tobytes())


def frame_changed(a: list[int], b: list[int]) -> bool:
    moved = sum(1 for x, y in zip(a, b) if abs(x - y) > FRAME_CHANGED_CELL)
    return moved >= max(1, int(len(a) * FRAME_CHANGED_SHARE))


def frame_still(a: list[int], b: list[int]) -> bool:
    return all(abs(x - y) <= FRAME_STILL_CELL for x, y in zip(a, b))


def moved(a: "_Look", b: "_Look") -> bool:
    """Whether the screen moved between two looks: another app, other labels (signature), or, on a screen read as
    frames, any visible motion."""
    if a.package != b.package:
        return True
    if labeled(a.elements) or labeled(b.elements):
        if signature(a.package, a.elements) != signature(b.package, b.elements):
            return True
        # OCR text (a treeless screen read for a text condition) counts too: a game board's moves.
        ocr = lambda els: sorted(el.text for el in els if el.cls == "OCRText")  # noqa: E731
        if ocr(a.elements) != ocr(b.elements):
            return True
    if a.frame is not None and b.frame is not None:
        return not frame_still(a.frame, b.frame)
    return False


def summary(package: str | None, elements: list[UIElement], limit: int = 6) -> str:
    """A short, text-only line about the screen (for the model, and the step record)."""
    words = [el.text or el.desc for el in elements if (el.text or el.desc)][:limit]
    shown = ", ".join(repr(w[:40]) for w in words) if words else "no readable elements"
    return f"{package or 'unknown app'} showing {shown}"


class _Look:
    """One read of the phone."""

    def __init__(self, package, elements, shot_frame=None, idle=None):
        self.package = package
        self.elements = elements
        self.frame = shot_frame
        self.idle = idle  # the screen went quiet before this read (wait_idle), None: unknown


class Waiter:
    """Runs one wait on a driver. `ocr(shot)` reads a treeless screen's text (None: no OCR);
    `blocked(package)` stops a wait on an app the task may not touch (its text is never read)."""

    def __init__(self, driver, cancel: threading.Event, *, ocr: Callable[[Screenshot], list[UIElement]] | None = None,
                 blocked: Callable[[str | None], bool] | None = None, clock=time.monotonic):
        self.driver = driver
        self.cancel = cancel
        self.ocr = ocr
        self.blocked = blocked or (lambda _p: False)
        self.clock = clock

    # --- reads ---------------------------------------------------------------------------------

    def _look(self, frames: bool, idle: bool | None = None, text: bool = False) -> _Look:
        package = self.driver.current_app()
        if self.blocked(package):
            return _Look(package, [], idle=idle)
        elements = list(self.driver.ui_elements())
        shot_frame = None
        if frames or not labeled(elements):
            shot = self.driver.screenshot()
            shot_frame = frame(shot)
            if text and self.ocr is not None and not labeled(elements):
                elements = elements + self.ocr(shot)
        return _Look(package, elements, shot_frame, idle)

    def _pause(self, deadline: float) -> bool | None:
        """Until the next poll: True/False: whether the screen went quiet (wait_idle), None: slept."""
        left = deadline - self.clock()
        if left <= 0:
            return None
        wait_idle = getattr(self.driver, "wait_idle", None)
        if wait_idle is not None:
            try:
                result = wait_idle(POLL_QUIET_MS, int(min(POLL_MAX_MS, left * 1000)))
            except DeviceError:
                result = None
            if result is not None:
                return bool(result.get("idle"))
        self.cancel.wait(min(POLL_SECONDS if self.ocr is None else OCR_POLL_SECONDS, left))
        return None

    # --- the condition -------------------------------------------------------------------------

    def _check(self, cond: Condition, look: _Look, base: _Look, prev: _Look | None) -> str | None:
        """What satisfies the condition on this look, or None."""
        els = look.elements
        if cond.until == "app_in_front":
            return f"{look.package} is in front" if look.package == cond.target else None
        if cond.until == "text_appears":
            el = find_text(cond.pattern, els)
            return f"{(el.text or el.desc)!r} is on screen" if el else None
        if cond.until == "text_disappears":
            if not labeled(els):
                return None  # nothing readable (a game without OCR, a blacked-out read): can't tell
            return None if find_text(cond.pattern, els) else f"no {cond.target!r} on screen"
        if cond.until == "element_enabled":
            el = find_enabled(cond.pattern, els)
            return f"{(el.text or el.desc)!r} is enabled" if el else None
        if cond.until == "screen_changes":
            if labeled(base.elements) or labeled(els):
                if signature(look.package, els) != signature(base.package, base.elements):
                    return "the screen changed"
                return None
            if look.package != base.package:
                return "the screen changed"
            if base.frame is not None and look.frame is not None and frame_changed(base.frame, look.frame):
                return "the screen changed"
            return None
        if cond.until == "loading_done":
            if labeled(els):
                if loading_element(els) is not None:
                    return None
                quiet = look.idle is True or (prev is not None and prev.idle is None
                                              and signature(look.package, els) == signature(prev.package, prev.elements))
                return "nothing is loading and the screen is still" if quiet else None
            if prev is not None and prev.frame is not None and look.frame is not None and frame_still(prev.frame, look.frame):
                return "the screen is still"
            return None
        raise ValueError(cond.until)

    # --- the wait ------------------------------------------------------------------------------

    def run(self, cond: Condition, baseline: tuple[str | None, list[UIElement]] | None = None,
            extra_stop: Callable[[_Look, float], str | None] | None = None,
            still_exit_s: float | None = None) -> WaitResult:
        """Wait for `cond`. `baseline` is the screen the wait is measured against (screen_changes): the
        screen the model last saw, so a change while it was deciding counts too. `still_exit_s`: a text,
        element or app condition ends early, not met, once the screen has been completely still this long
        with nothing loading on it (None: wait the full timeout)."""
        start = self.clock()
        deadline = start + cond.timeout_s

        def done(met: bool, seen: str, via: str, look: _Look | None, stopped: str | None = None) -> WaitResult:
            return WaitResult(met, int((self.clock() - start) * 1000), seen, via, look.package if look else None, stopped)

        if self.cancel.is_set():
            return done(False, "stopped", "none", None, "cancelled")
        text_cond = cond.until in ("text_appears", "text_disappears", "element_enabled")
        try:
            first = self._look(frames=False, idle=None, text=text_cond)
        except DeviceError as exc:
            return done(False, str(exc), "none", None, "disconnected")
        if baseline is not None and labeled([e for e in baseline[1] if e.cls != "OCRText"]):
            base = _Look(baseline[0], [e for e in baseline[1] if e.cls != "OCRText"])
        else:
            base = first
        via = "poll" if labeled(first.elements) else "frames"
        prev: _Look | None = None
        look = first
        phone_ok = "wait_for" in (getattr(self.driver, "capabilities", None) or ())
        still_exit = still_exit_s if cond.until in STILL_EXIT_CONDITIONS and extra_stop is None else None
        still_since = start  # when the screen last moved (or something was loading)
        while True:
            if self.blocked(look.package):
                return done(False, f"{look.package} is blocked for this task, so the wait stopped", via, look, "blocked")
            hit = self._check(cond, look, base, prev)
            if hit is not None:
                return done(True, hit, via, look)
            if extra_stop is not None and (why := extra_stop(look, self.clock() - start)):
                return done(False, why, via, look)
            if still_exit is not None:
                if prev is not None and (moved(prev, look) or loading_element(look.elements) is not None):
                    still_since = self.clock()
                elif prev is None and loading_element(look.elements) is not None:
                    still_since = self.clock()
                if self.clock() - still_since >= still_exit and self.clock() < deadline:
                    return done(False, summary(look.package, look.elements), via, look, "still")
            if self.clock() >= deadline:
                return done(False, summary(look.package, look.elements), via, look)
            if self.cancel.is_set():
                return done(False, "stopped", via, look, "cancelled")
            idle: bool | None = None
            if phone_ok and extra_stop is None and (labeled(look.elements) or cond.until == "app_in_front"):
                # With a still-screen exit, the phone waits in chunks of about that long, so stillness is checked
                # between them.
                chunk = None if still_exit is None else max(still_exit - (self.clock() - still_since), 0.25)
                answer = self._phone_wait(cond, deadline, chunk)
                if answer is None:
                    phone_ok = False  # an app that can't after all: poll from here
                else:
                    via = "phone"
                    if answer.get("met") and not answer.get("redacted"):
                        # The phone saw it. Read once more so what's reported is what the agent will see.
                        try:
                            look = self._look(frames=False, idle=True, text=text_cond)
                        except DeviceError as exc:
                            return done(False, str(exc), via, None, "disconnected")
                        return done(True, self._check(cond, look, base, None) or str(answer.get("seen") or "condition met"),
                                    via, look)
            else:
                idle = self._pause(deadline)
            prev = look
            try:
                look = self._look(frames=not labeled(prev.elements), idle=idle, text=text_cond)
            except DeviceError as exc:
                return done(False, str(exc), via, None, "disconnected")
            if via != "phone":
                via = "poll" if labeled(look.elements) else "frames"

    def _phone_wait(self, cond: Condition, deadline: float, chunk_s: float | None = None) -> dict | None:
        """One wait_for on the phone (at most PHONE_CHUNK_MS, or `chunk_s`). None: the phone can't."""
        left_ms = int((deadline - self.clock()) * 1000)
        if left_ms <= 0:
            return {"met": False}
        limit = PHONE_CHUNK_MS if chunk_s is None else min(PHONE_CHUNK_MS, int(chunk_s * 1000))
        try:
            return self.driver.wait_for(cond.wire(), min(left_ms, limit))
        except DeviceError:
            return None
