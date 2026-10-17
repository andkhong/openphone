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
  text conditions read it with OCR when the agent has OCR;
- the model's screen_changes on a screen with a large unlabeled area (a board, a play field: `canvas_region`) also
  compares colour frames of that area, so a chess move counts though no label changed; the server takes those
  frames itself (the phone has none).

The model's waits end early, not met, once the screen has been still with nothing loading for a short time
(`still_exit_for`: 2.5 s for text, element and app conditions, 3.5 s for screen_changes), on every path: the phone
judges it itself when its hello lists wait_for_still, else the server checks between shorter wait_for calls.

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
# loading_done on a labeled screen that never goes quiet (a map's moving tiles and pins, an animated feed): once
# nothing on it has said "loading" for this long, the wait ends. Measured 2026-10-17: Maps "coffee near me" waited
# its full ~9.4 s cap in every run though its results were up after ~2.5 s.
LOADING_MOVING_GRACE_S = 2.5
PHONE_CHUNK_MS = 10_000  # one wait_for call at most, so a stop is noticed between calls
OCR_POLL_SECONDS = 1.0
# Text, element and app conditions end early ("not met: the screen is still") once the screen has been completely
# still this long with nothing loading on it: a label that isn't there on a still screen isn't coming by waiting
# (a guessed "3." in a chess game). A spinner, progress bar or "Installing…" keeps the full timeout.
STILL_EXIT_S = 2.5
# screen_changes ends early the same way, after a little longer: the screen hasn't changed and nothing is loading, so
# it may already be ready (measured 2026-10-17 on the owner's Samsung: Tetris's menu, ready and still, held a
# screen_changes wait for its whole 15-20 s timeout in every run). 3.5 s: a chess bot's reply and an app's answer to a
# tap came within about 1 s in the same runs, and a screen about to change on its own usually animates first (so it
# isn't still); a longer exit would mostly add waiting on screens that are already done.
SCREEN_STILL_EXIT_S = 3.5
STILL_EXIT_CONDITIONS = {"text_appears", "text_disappears", "element_enabled", "app_in_front", "screen_changes"}
# Faster mode's J3 inside a screen_changes wait on a play-area screen (Waiter.run's `ready`): first asked after
# READY_FIRST_S of the wait, then at most every READY_EVERY_S, on a look that didn't move.
READY_FIRST_S = 1.0
READY_EVERY_S = 1.0


def still_exit_for(cond: "Condition") -> float | None:
    """How long a still screen ends this condition's wait early (None: never)."""
    if cond.until == "screen_changes":
        return SCREEN_STILL_EXIT_S
    return STILL_EXIT_S if cond.until in STILL_EXIT_CONDITIONS else None

FRAME_SIZE = (18, 32)  # a frame for comparing treeless screens: 18 x 32 grayscale cells
FRAME_CHANGED_CELL = 24  # a cell moved this much (of 255)...
FRAME_CHANGED_SHARE = 0.02  # ...in this share of the cells: the screen changed
FRAME_STILL_CELL = 12  # no cell moved more than this between two frames: the screen is still

# The play area: a large part of the screen no label describes (a chess board, a play field, a canvas). What's drawn
# there isn't in the element list, so comparing labels can't see a move (measured 2026-10-17: a Chess.com bot's reply
# went unseen, "Back" and "Chess.com" being among the labels compared). It is the largest rectangle no labeled element
# overlaps (`canvas_region`), found on a grid CANVAS_GRID_COLS cells wide:
# - at least CANVAS_MIN_SHARE of the screen. A chess board is ~45% of a 20:9 phone screen (the owner's Samsung, from
#   the tap coordinates of 2026-10-17) and ~56% of a 16:9 one, less the grid's rounding where labels touch it; a 16:9
#   video player in portrait is ~25%, a list thumbnail far less, so neither counts;
# - each side at least CANVAS_MIN_SIDE of the screen's width (not a thin strip between rows);
# - a labeled element covering at least CANVAS_CONTAINER_SHARE of the screen is a container whose label names the
#   whole ("Game view", Tetris's only element), not something drawn in it, so it doesn't cover the area;
# - text fields count as labels (a cursor blinking in an empty field isn't the play area); OCR words don't (they are
#   read off the area itself).
CANVAS_GRID_COLS = 24
CANVAS_MIN_SHARE = 0.30
CANVAS_MIN_SIDE = 0.25
CANVAS_CONTAINER_SHARE = 0.30
# Comparing the play area: its frame is REGION_CELLS colour cells on the short side (a chess board spanning it has
# 8 x 8 cells a square), at most REGION_MAX_CELLS on the long one, each the mean of its pixels. A change is at least
# REGION_CHANGED_CELLS cells moving more than REGION_CHANGED_CELL (of 255, in any channel). Measured on synthetic
# frames (tests/test_game_waits.py; a 1080 x 2340 screen): one pawn move on a full-width board moves 26-36 cells, the
# hardest case (a white pawn from a light square to a light square, no highlight) included; on a half-width board
# 6-8; a clock's digits ("9:59" -> "10:00") 0-2; a 1-cell flicker 1; a square pulsing 15 levels brighter 0 (12 would
# count it: 24 leaves room for JPEG noise and glow).
REGION_CELLS = 64
REGION_MAX_CELLS = 192
REGION_CHANGED_CELL = 24
REGION_CHANGED_CELLS = 6
# Still (the still exit): at most REGION_STILL_CELLS cells moved more than REGION_STILL_CELL (a cursor may blink).
REGION_STILL_CELL = 16
REGION_STILL_CELLS = 4

# Not a bare "spinner": android.widget.Spinner is Android's dropdown (Maps' "Relevance" and "Price" filters), and it
# made every Maps results screen look loading (owner's Samsung, 2026-10-17). Loading spinners are ProgressBars, or
# custom views named for loading ("LoadingSpinnerView").
LOADING_CLASS = re.compile(r"progress|loading|shimmer", re.I)
LOADING_ID = re.compile(r"progress|loading|shimmer|skeleton", re.I)
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

    def wire(self, still_ms: int | None = None) -> dict:
        """The condition as the phone's wait_for takes it. `still_ms`: the phone ends the wait, not met, once its
        screen has shown the same labels with nothing loading this long (phones whose hello lists wait_for_still)."""
        out = {"until": self.until}
        if self.until == "app_in_front":
            out["package"] = self.target
        elif self.pattern is not None:
            out["pattern"] = self.pattern.pattern
        if self.until == "loading_done":
            out["quiet_ms"] = LOADING_QUIET_MS
        if still_ms is not None:
            out["still_ms"] = max(int(still_ms), 1)
        return out


@dataclass
class WaitResult:
    met: bool
    waited_ms: int
    seen: str  # what matched, or what the screen shows at the timeout
    via: str  # phone (wait_for) | poll (element list) | frames (treeless screen) | none (stopped before reading)
    package: str | None = None
    stopped: str | None = None  # why it ended without a verdict: cancelled | blocked | disconnected | still | ready

    def text(self, cond: Condition) -> str:
        secs = self.waited_ms / 1000
        if self.stopped == "cancelled":
            return "Stopped while waiting."
        if self.stopped == "blocked":
            return f"Stopped waiting after {secs:.1f}s: {self.seen}"
        if self.stopped in ("still", "ready") and cond.until == "screen_changes":
            why = ("the screen hasn't changed and nothing is loading" if self.stopped == "still" else
                   "the screen hasn't changed and a quick screenshot check says it's ready, not loading")
            return (f"Not met after {secs:.1f}s: {why}: it may already be ready. Now: {self.seen}. Look at the "
                    "screen and decide what to do next (if you expect something slow, such as an opponent "
                    "thinking or a download, wait again).")
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


# --- the play area (a board, a play field, a canvas) ---------------------------------------------------------


def _covers(el: UIElement, screen_area: float) -> bool:
    """Whether the element marks its part of the screen as described (see CANVAS_*)."""
    if el.cls == "OCRText" or not (el.text or el.desc or el.editable or el.password):
        return False
    x1, y1, x2, y2 = el.bounds
    return (x2 - x1) * (y2 - y1) < CANVAS_CONTAINER_SHARE * screen_area


def labels_its_area(el: UIElement, screen_area: float) -> bool:
    """Whether a labeled element names just its own part of the screen (a control), not a whole area it contains (a
    game's WebView or board container, 30% of the screen or more)."""
    return _covers(el, screen_area)


def _largest_rectangle(free: list[list[bool]]) -> tuple[int, int, int, int, int]:
    """(area, col1, row1, col2, row2) of the largest all-True rectangle (row-histogram method), ends exclusive."""
    best = (0, 0, 0, 0, 0)
    cols = len(free[0]) if free else 0
    heights = [0] * cols
    for r, row in enumerate(free):
        heights = [h + 1 if f else 0 for h, f in zip(heights, row)]
        stack: list[int] = []
        for c in range(cols + 1):
            h = heights[c] if c < cols else 0
            while stack and heights[stack[-1]] >= h:
                top = stack.pop()
                height = heights[top]
                left = stack[-1] + 1 if stack else 0
                area = height * (c - left)
                if area > best[0]:
                    best = (area, left, r - height + 1, c, r + 1)
            stack.append(c)
    return best


def canvas_region(elements: list[UIElement], screen: tuple[int, int] | None) -> tuple[int, int, int, int] | None:
    """The play area in device pixels (x1, y1, x2, y2): the largest rectangle no labeled element overlaps, when it is
    big enough (CANVAS_*); None otherwise. A screen with no labels at all is all play area."""
    if not screen or screen[0] <= 0 or screen[1] <= 0:
        return None
    w, h = screen
    cols = CANVAS_GRID_COLS
    rows = max(1, round(cols * h / w))
    cw, ch = w / cols, h / rows
    free = [[True] * cols for _ in range(rows)]
    for el in elements:
        if not _covers(el, w * h):
            continue
        x1, y1, x2, y2 = el.bounds
        x1, x2 = max(x1, 0), min(x2, w)
        y1, y2 = max(y1, 0), min(y2, h)
        if x2 <= x1 or y2 <= y1:
            continue
        c1, c2 = int(x1 // cw), min(cols, int(-(-x2 // cw)))
        r1, r2 = int(y1 // ch), min(rows, int(-(-y2 // ch)))
        for r in range(r1, r2):
            row = free[r]
            for c in range(c1, c2):
                row[c] = False
    area, c1, r1, c2, r2 = _largest_rectangle(free)
    if area < CANVAS_MIN_SHARE * rows * cols:
        return None
    if min((c2 - c1) * cw, (r2 - r1) * ch) < CANVAS_MIN_SIDE * w:
        return None
    return round(c1 * cw), round(r1 * ch), round(c2 * cw), round(r2 * ch)


def region_frame(shot: Screenshot, region: tuple[int, int, int, int]) -> list[int]:
    """The play area of `shot` as colour cells (REGION_CELLS on its short side), each its pixels' mean, flattened
    R, G, B. Colour, not grey: a white piece on a light square and a yellow move highlight barely differ in grey
    (Chess.com's light square is ~233 grey, its highlight ~229) but differ in blue by 40-100."""
    from PIL import Image

    img = Image.open(io.BytesIO(shot.jpeg))
    x1, y1 = shot.to_image(region[0], region[1])
    x2, y2 = shot.to_image(region[2], region[3])
    x2, y2 = max(x2, x1 + 1), max(y2, y1 + 1)
    short, long_ = sorted((x2 - x1, y2 - y1))
    n_long = min(REGION_MAX_CELLS, max(REGION_CELLS, round(REGION_CELLS * long_ / max(short, 1))))
    size = (REGION_CELLS, n_long) if x2 - x1 <= y2 - y1 else (n_long, REGION_CELLS)
    scale = min((x2 - x1) / size[0], (y2 - y1) / size[1]) / 4  # JPEG: decode smaller, still 4+ px a cell
    if scale >= 2:
        img.draft("RGB", (int(img.width / scale), int(img.height / scale)))
        fx, fy = img.width / shot.width, img.height / shot.height
        x1, y1, x2, y2 = int(x1 * fx), int(y1 * fy), max(int(x2 * fx), int(x1 * fx) + 1), max(int(y2 * fy), int(y1 * fy) + 1)
    img = img.convert("RGB").crop((x1, y1, x2, y2))
    return list(img.resize(size, Image.BOX).tobytes())


def _cells(shot: Screenshot, region: tuple[int, int, int, int]) -> list[int] | None:
    try:
        return region_frame(shot, region)
    except (OSError, ValueError):  # an undecodable frame: no play-area comparison for this look
        return None


def region_moved_cells(a: list[int], b: list[int], by: int) -> int:
    """How many cells moved more than `by` in any colour channel between two play-area frames."""
    return sum(1 for i in range(0, min(len(a), len(b)) - 2, 3)
               if max(abs(a[i] - b[i]), abs(a[i + 1] - b[i + 1]), abs(a[i + 2] - b[i + 2])) > by)


def region_changed(a: list[int], b: list[int]) -> bool:
    """A change that counts in the play area: a piece moved, a menu opened (not a digit, a cursor, a pulse)."""
    return len(a) == len(b) and region_moved_cells(a, b, REGION_CHANGED_CELL) >= REGION_CHANGED_CELLS


def region_still(a: list[int], b: list[int]) -> bool:
    return len(a) == len(b) and region_moved_cells(a, b, REGION_STILL_CELL) <= REGION_STILL_CELLS


def region_detail(cells: list[int]) -> float:
    """How much is drawn in the area: the standard deviation of its cells' brightness (a blank area is ~0, a board
    or a menu 20 or more)."""
    grey = [sum(cells[i:i + 3]) / 3 for i in range(0, len(cells) - 2, 3)]
    if not grey:
        return 0.0
    mean = sum(grey) / len(grey)
    return (sum((c - mean) ** 2 for c in grey) / len(grey)) ** 0.5


def moved(a: "_Look", b: "_Look") -> bool:
    """Whether the screen moved between two looks: another app, other labels (signature), or, on a screen read as
    frames, any visible motion (in the play area, when the wait compares one)."""
    if a.package != b.package:
        return True
    if a.region_frame is not None and b.region_frame is not None and not region_still(a.region_frame, b.region_frame):
        return True
    if labeled(a.elements) or labeled(b.elements):
        if signature(a.package, a.elements) != signature(b.package, b.elements):
            return True
        # OCR text (a treeless screen read for a text condition) counts too: a game board's moves.
        ocr = lambda els: sorted(el.text for el in els if el.cls == "OCRText")  # noqa: E731
        if ocr(a.elements) != ocr(b.elements):
            return True
    if a.region_frame is not None and b.region_frame is not None:
        return False  # the play area was compared above (it covers the whole screen when nothing is labeled)
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

    def __init__(self, package, elements, shot_frame=None, idle=None, region_frame=None, shot=None):
        self.package = package
        self.elements = elements
        self.frame = shot_frame
        self.idle = idle  # the screen went quiet before this read (wait_idle), None: unknown
        self.region_frame = region_frame  # the play area's cells, when the wait compares one
        self.shot = shot  # the screenshot this look took, if it took one


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
        self.region: tuple[int, int, int, int] | None = None  # the play area this wait compares (screen_changes)

    # --- reads ---------------------------------------------------------------------------------

    def _look(self, frames: bool, idle: bool | None = None, text: bool = False) -> _Look:
        package = self.driver.current_app()
        if self.blocked(package):
            return _Look(package, [], idle=idle)
        elements = list(self.driver.ui_elements())
        shot = shot_frame = cells = None
        if frames or not labeled(elements) or self.region is not None:
            shot = self.driver.screenshot()
            if frames or not labeled(elements):
                shot_frame = frame(shot)
            if self.region is not None:
                cells = _cells(shot, self.region)
            if text and self.ocr is not None and not labeled(elements):
                elements = elements + self.ocr(shot)
        return _Look(package, elements, shot_frame, idle, cells, shot)

    def _screen(self, given: tuple[int, int] | None, elements: list[UIElement]) -> tuple[int, int] | None:
        """The screen's size in device pixels: given, else a recent capture's, else what the elements span."""
        if given:
            return given
        latest = getattr(self.driver, "latest_capture", None)
        try:
            shot = latest() if latest else None
        except Exception:
            shot = None
        if shot is not None and getattr(shot, "device_width", 0) > 0:
            return shot.device_width, shot.device_height
        if elements:
            return max(e.bounds[2] for e in elements), max(e.bounds[3] for e in elements)
        return None

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
            # The play area's frames first: a move there is a change even when the labels say nothing changed.
            if look.package == base.package and base.region_frame is not None and look.region_frame is not None \
                    and region_changed(base.region_frame, look.region_frame):
                return "the screen changed (in its unlabeled area: a board, a play field or a canvas)"
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
            still_exit_s: float | None = None, *, play_area: bool = False,
            screen: tuple[int, int] | None = None, baseline_shot: Screenshot | None = None,
            ready: Callable[[_Look], str | None] | None = None) -> WaitResult:
        """Wait for `cond`. `baseline` is the screen the wait is measured against (screen_changes): the
        screen the model last saw, so a change while it was deciding counts too. `still_exit_s`: the condition
        ends early, not met, once the screen has been completely still this long with nothing loading on it
        (None: wait the full timeout; `still_exit_for` gives each condition's).

        `play_area` (screen_changes): on a screen with a large unlabeled area (`canvas_region`; `screen` is its size
        in device pixels), frames of that area are compared too, and a change there counts even when the labels
        didn't change; the server takes the frames, so the phone's wait_for isn't used. `baseline_shot`: the
        screenshot the model last saw, the frame a change is measured against. `ready(look)`: asked on a still
        look of such a screen (at most every READY_EVERY_S, from READY_FIRST_S on); a reason ends the wait, not
        met (Faster mode's J3 "ready", agent/loop.py)."""
        start = self.clock()
        deadline = start + cond.timeout_s

        def done(met: bool, seen: str, via: str, look: _Look | None, stopped: str | None = None) -> WaitResult:
            return WaitResult(met, int((self.clock() - start) * 1000), seen, via, look.package if look else None, stopped)

        if self.cancel.is_set():
            return done(False, "stopped", "none", None, "cancelled")
        text_cond = cond.until in ("text_appears", "text_disappears", "element_enabled")
        self.region = None
        try:
            first = self._look(frames=False, idle=None, text=text_cond)
            if play_area and cond.until == "screen_changes" and not self.blocked(first.package):
                given = baseline[1] if baseline is not None and baseline[0] == first.package else first.elements
                labels = [e for e in given if e.cls != "OCRText"]
                self.region = canvas_region(labels, self._screen(screen, labels))
                if self.region is not None:
                    first = self._look(frames=False, idle=None, text=text_cond)
        except DeviceError as exc:
            return done(False, str(exc), "none", None, "disconnected")
        if baseline is not None and labeled([e for e in baseline[1] if e.cls != "OCRText"]):
            base = _Look(baseline[0], [e for e in baseline[1] if e.cls != "OCRText"])
        else:
            base = first
        if self.region is not None:
            base.region_frame = first.region_frame
            if baseline_shot is not None and (baseline is None or baseline[0] == first.package):
                base.region_frame = _cells(baseline_shot, self.region) or first.region_frame
        via = "poll" if labeled(first.elements) else "frames"
        prev: _Look | None = None
        look = first
        # The phone has no frames: a wait that compares the play area takes them itself.
        phone_ok = "wait_for" in (getattr(self.driver, "capabilities", None) or ()) and self.region is None
        phone_still = "wait_for_still" in (getattr(self.driver, "capabilities", None) or ())
        still_exit = still_exit_s if cond.until in STILL_EXIT_CONDITIONS and extra_stop is None else None
        still_since = start  # when the screen last moved (or something was loading)
        unloaded_since: float | None = None  # loading_done: since when a labeled screen has shown nothing loading
        asked_at: float | None = None  # ready(): when it was last asked
        while True:
            if self.blocked(look.package):
                return done(False, f"{look.package} is blocked for this task, so the wait stopped", via, look, "blocked")
            hit = self._check(cond, look, base, prev)
            if hit is not None:
                return done(True, hit, via, look)
            if cond.until == "loading_done" and labeled(look.elements):
                if loading_element(look.elements) is not None:
                    unloaded_since = None
                elif unloaded_since is None:
                    unloaded_since = self.clock()
                elif self.clock() - unloaded_since >= LOADING_MOVING_GRACE_S:
                    return done(True, "nothing is loading (the screen keeps moving)", via, look)
            if extra_stop is not None and (why := extra_stop(look, self.clock() - start)):
                return done(False, why, via, look)
            moving = prev is not None and moved(prev, look)
            if still_exit is not None:
                if moving or loading_element(look.elements) is not None:
                    still_since = self.clock()
                if self.clock() - still_since >= still_exit and self.clock() < deadline:
                    return done(False, summary(look.package, look.elements), via, look, "still")
            if ready is not None and self.region is not None and prev is not None and not moving \
                    and look.shot is not None and self.clock() - start >= READY_FIRST_S \
                    and (asked_at is None or self.clock() - asked_at >= READY_EVERY_S):
                asked_at = self.clock()
                if why := ready(look):
                    return done(False, why, "jev_image", look, "ready")
            if self.clock() >= deadline:
                return done(False, summary(look.package, look.elements), via, look)
            if self.cancel.is_set():
                return done(False, "stopped", via, look, "cancelled")
            idle: bool | None = None
            if phone_ok and extra_stop is None and (labeled(look.elements) or cond.until == "app_in_front"):
                # With a still-screen exit, a phone that can judge stillness itself (wait_for_still) is told how
                # much longer the screen must stay still; an older one waits in chunks of about that long, so
                # stillness is checked here between them.
                still_left = None if still_exit is None else max(still_exit - (self.clock() - still_since), 0.25)
                if still_left is not None and phone_still:
                    answer = self._phone_wait(cond, deadline, None, still_ms=int(still_left * 1000))
                else:
                    answer = self._phone_wait(cond, deadline, still_left)
                if answer is None:
                    phone_ok = False  # an app that can't after all: poll from here
                else:
                    via = "phone"
                    if answer.get("still") and not answer.get("redacted") and still_exit is not None \
                            and self.clock() < deadline:
                        try:
                            look = self._look(frames=False, idle=True, text=text_cond)
                        except DeviceError as exc:
                            return done(False, str(exc), via, None, "disconnected")
                        if (hit := self._check(cond, look, base, None)) is not None:
                            return done(True, hit, via, look)
                        return done(False, summary(look.package, look.elements), via, look, "still")
                    if "still_for_ms" in answer and still_exit is not None:
                        # The phone saw the screen move during that call: still since then, by its clock.
                        still_since = min(self.clock(), max(still_since, self.clock() - answer["still_for_ms"] / 1000))
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

    def _phone_wait(self, cond: Condition, deadline: float, chunk_s: float | None = None,
                    still_ms: int | None = None) -> dict | None:
        """One wait_for on the phone (at most PHONE_CHUNK_MS, or `chunk_s`; `still_ms`: it ends once its screen has
        been still that long). None: the phone can't."""
        left_ms = int((deadline - self.clock()) * 1000)
        if left_ms <= 0:
            return {"met": False}
        limit = PHONE_CHUNK_MS if chunk_s is None else min(PHONE_CHUNK_MS, int(chunk_s * 1000))
        try:
            return self.driver.wait_for(cond.wire(still_ms), min(left_ms, limit))
        except DeviceError:
            return None
