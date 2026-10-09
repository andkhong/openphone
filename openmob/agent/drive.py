"""The drive level's plumbing (docs/design/mcp-drive.md): an outside agent's commands to the agent loop, one
at a time, and the view of the screen it gets back.

A drive session is a task on the phone's Coordinator whose decisions come from outside instead of from a
model. `Agent._drive` (loop.py) runs on the coordinator's worker thread and takes commands from a
DriveChannel; the MCP tools (platform/mcp_drive.py) put them there through V1Service. Every act a command
names runs through `Agent._execute`, exactly like the loop's own tool calls. Nothing in this module touches
the phone.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from dataclasses import dataclass, field

from ..phone.adb import Screenshot, UIElement, format_elements

SOURCE = "drive"  # the steps table's `source` for an act an outside agent decided
TIER = "drive"  # the task's tier in the ledger and the developer console
CREDITS_PER_STEP = 1
REDACTED = "<redacted>"
ESCAPE_KEYS = {"BACK", "HOME"}  # the only acts accepted while a blocked app is in front (the loop's own way out)
KEYS = {"BACK", "HOME", "ENTER"}  # the keys a drive session may press
# The loop tools a drive session may call (every one runs through Agent._execute; `done` ends the session).
TOOLS = {"tap_element", "tap", "long_press", "scroll", "swipe", "type_text", "press_key", "launch_app", "open_url",
         "open_link", "open_settings_page", "wait_until", "list_apps", "propose_plan", "done"}
BLOCKED_TEXT = ("This app is blocked for API tasks on this phone (its owner can allow it). Nothing is read while it "
                "is in front; press BACK or HOME to leave it.")


class DriveBusy(Exception):
    """A command is already in flight (running, or waiting for the owner)."""


class DriveEnded(Exception):
    """The session has ended; no more commands."""


@dataclass
class Command:
    """One command from the outside agent: an act (a loop tool and its arguments), a look, or the end."""

    op: str  # act | observe | end
    name: str | None = None  # the loop's tool for an act ("done" for end)
    args: dict = field(default_factory=dict)
    screenshot: bool = False  # the reply carries the screen's pixels too
    max_side: int = 1024
    id: int = 0
    created: float = field(default_factory=time.monotonic)
    status: str = "queued"  # queued | running | done
    done: threading.Event = field(default_factory=threading.Event)
    outcome: dict | None = None  # {text, is_error, refused} for an act
    view: dict | None = None  # the screen after it (screen_view's structured half)
    text: str = ""  # …and its text half (what the loop's model would read)
    jpeg: bytes | None = None  # the screenshot after it, when asked; dropped once the reply is read

    def wait(self, seconds: float) -> bool:
        return self.done.wait(max(0.0, seconds))

    def take_jpeg(self) -> bytes | None:
        jpeg, self.jpeg = self.jpeg, None
        return jpeg


class DriveChannel:
    """The one slot between the outside agent (any thread, through V1Service) and the loop (the coordinator's
    worker thread). One command is in flight at a time; the loop replies with the act's outcome and the screen
    after it. The channel also keeps the latest screen (sizes only, never pixels) so `get_session` can show it
    without touching the phone, and the session's end (why, and the result)."""

    def __init__(self, idle_seconds: float, max_seconds: float, max_steps: int, requester: str):
        self.idle_seconds = float(idle_seconds)
        self.max_seconds = float(max_seconds)
        self.max_steps = int(max_steps)
        self.requester = requester  # the key's name: the feed and the phone say who is driving
        self._slot: queue.Queue[Command] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._next_id = 1
        self.pending: Command | None = None  # the command in flight (queued or running)
        self.last: Command | None = None  # the last finished command
        self.view: dict | None = None  # the latest screen the loop published
        self.view_text: str = ""
        self.steps = 0  # acts run (end_session included), the task's step count
        self.started: float | None = None  # monotonic, when the loop picked the session up
        self.last_activity: float = time.monotonic()  # the last command taken or replied to (idle timeout)
        self.active = threading.Event()  # the loop is running the session
        self.ended = threading.Event()
        self.reason: str | None = None  # ended | idle_timeout | step_cap | max_duration | stopped | phone_error…
        self.result: dict | None = None  # {success, summary} once ended

    # --- the outside agent's side (V1Service, any thread) ------------------------------------------

    def status(self) -> str:
        if self.ended.is_set():
            return "ended"
        return "active" if self.active.is_set() else "starting"

    def submit(self, command: Command) -> Command:
        """Hand the loop a command. DriveBusy while one is in flight; DriveEnded once the session is over."""
        with self._lock:
            if self.ended.is_set():
                raise DriveEnded()
            if self.pending is not None and not self.pending.done.is_set():
                raise DriveBusy()
            command.id = self._next_id
            self._next_id += 1
            try:
                self._slot.put_nowait(command)
            except queue.Full:  # the loop hasn't taken the previous one yet: still in flight
                raise DriveBusy() from None
            self.pending = command
        return command

    def in_flight(self) -> Command | None:
        with self._lock:
            return self.pending if self.pending is not None and not self.pending.done.is_set() else None

    # --- the loop's side (the coordinator's worker thread) ---------------------------------------------

    def opened(self) -> None:
        self.started = self.last_activity = time.monotonic()
        self.active.set()

    def next(self, cancel: threading.Event) -> Command | None:
        """The next command, or None when the session must end: no command within idle_seconds, the session
        older than max_seconds, or cancelled (Stop on the phone or dashboard, cancel_task, a revoked key).
        `reason` says which; a cancel leaves it to the loop."""
        while True:
            if cancel.is_set():
                return None
            now = time.monotonic()
            if self.started is not None and now - self.started >= self.max_seconds:
                self.reason = "max_duration"
                return None
            if now - self.last_activity >= self.idle_seconds:
                self.reason = "idle_timeout"
                return None
            try:
                command = self._slot.get(timeout=0.25)
            except queue.Empty:
                continue
            command.status = "running"
            self.last_activity = time.monotonic()
            return command

    def publish(self, view: dict, text: str) -> None:
        self.view, self.view_text = view, text

    def reply(self, command: Command, outcome: dict | None, view: dict, text: str, jpeg: bytes | None = None) -> None:
        """The loop's answer to a command: the act's outcome (None for a look) and the screen after it."""
        command.outcome, command.view, command.text, command.jpeg = outcome, view, text, jpeg
        command.status = "done"
        with self._lock:
            if command.op != "observe":  # a look doesn't replace the last step's record
                self.last = command
        self.last_activity = time.monotonic()
        command.done.set()

    def close(self, reason: str, success: bool, summary: str) -> None:
        """The session is over. A command still in flight is answered with the end, so no caller hangs."""
        with self._lock:
            self.reason = reason
            self.result = {"success": success, "summary": summary}
            self.ended.set()
            pending = self.pending if self.pending is not None and not self.pending.done.is_set() else None
            while True:  # a command the loop never took
                try:
                    self._slot.get_nowait()
                except queue.Empty:
                    break
        if pending is not None:
            if pending.outcome is None and pending.op == "act":
                pending.outcome = {"text": f"Not run: the session ended ({reason}) first.", "is_error": True,
                                   "refused": False}
            if pending.view is None:
                pending.view, pending.text = self.view, self.view_text
            pending.status = "done"
            pending.done.set()


# --- the screen as the outside agent sees it -------------------------------------------------------------

def _element(el: UIElement, shot: Screenshot | None) -> dict:
    center = list(shot.to_image(*el.center)) if shot is not None else list(el.center)
    out = {"index": el.index, "class": el.cls, "text": el.text, "desc": el.desc, "resource_id": el.resource_id,
           "clickable": el.clickable, "center": center, "bounds": list(el.bounds)}
    if el.checked is not None:
        out["checked"] = el.checked
    for name in ("editable", "password", "covered", "focused"):
        if getattr(el, name, False):
            out[name] = True
    if not el.enabled:
        out["enabled"] = False
    if el.hint:
        out["hint"] = el.hint
    return out


def screen_view(obs, seq: int, blocked: bool, app: str | None, notes: list[str], owner_messages: list[str],
                assessment: str | None = None) -> tuple[dict, str]:
    """(structured, text): one look at the phone for the outside agent. The text is what the loop's model
    reads for the same screen (`Agent._obs_blocks`' header and element lines); the structured half is the same
    list as fields. A password field's text is redacted in both. A blocked app in front shows nothing at all."""
    if blocked:
        view = {"seq": seq, "package": obs.package, "app": app, "screenshot": None, "elements": [],
                "blocked_app": True, "notes": list(notes), "owner_messages": list(owner_messages)}
        return view, f"SCREEN: foreground app {obs.package or 'unknown'} is blocked for this session. {BLOCKED_TEXT}"
    shot = obs.geometry()
    elements = [dataclasses.replace(el, text=REDACTED) if getattr(el, "password", False) and el.text else el
                for el in obs.elements]
    sizes = ({"width": shot.width, "height": shot.height, "device_width": shot.device_width,
              "device_height": shot.device_height} if shot is not None else None)
    header = f"SCREEN: foreground app {obs.package or 'unknown'}"
    header += f"; screenshot is {shot.width}x{shot.height} px." if shot is not None else "."
    if elements:
        if shot is not None:
            body = ("UI elements (OCRText = words read from the screenshot; centers are screenshot pixels):\n"
                    + format_elements(elements, shot))
        else:
            body = "UI elements (centers are device pixels):\n" + "\n".join(
                f"[{el.index}] {el.cls} {el.text!r} @ {el.center}" for el in elements)
    else:
        body = "No UI element list or readable text for this screen. Work from the screenshot."
    if assessment:
        body += "\n" + assessment
    view = {"seq": seq, "package": obs.package, "app": app, "screenshot": sizes,
            "elements": [_element(el, shot) for el in elements], "blocked_app": False,
            "notes": list(notes), "owner_messages": list(owner_messages)}
    return view, f"{header}\n{body}"
