"""The observe → decide → act loop that drives the phone with Claude."""

from __future__ import annotations

import base64
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import Config
from ..jev import JevError, JevJudge, ScreenAssessment, describe_screen
from ..memory import REMEMBER_INTENT, NoteError, render_app_notes, render_user_notes
from ..phone.adb import Screenshot, UIElement, format_elements
from ..phone.base import DeviceError
from ..phone.apps import AppResolver
from ..usage import Usage
from ..vision import ocr
from .tools import TOOLS, system_prompt

OBS_PREFIX = "SCREEN:"
OMITTED = "(earlier screen omitted)"
LOADING_POLL_SECONDS = 2.0
RECENT_REQUESTS = 5
OCR_MIN_CONFIDENCE = 0.3
BLOCKED_APP_MESSAGE = "This app is blocked for API tasks on this phone (its owner can allow it)."
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "go", "proceed", "do", "y", "correct"}


class UserIO(Protocol):
    def ask(self, question: str) -> str: ...
    def event(self, kind: str, message: str) -> None: ...


@dataclass
class AgentResult:
    success: bool
    summary: str
    steps: int
    usage: Usage = field(default_factory=Usage)


@dataclass
class Observation:
    shot: Screenshot
    elements: list[UIElement]
    package: str | None
    assessment: ScreenAssessment | None = None


@dataclass
class Outcome:
    text: str
    is_error: bool = False
    done: bool = False
    success: bool = False
    settle: float | None = None  # None means the default settle delay


@dataclass
class _TaskState:
    task: str
    cancel: threading.Event
    auto_waited: float = 0.0
    treeless_misses: dict[str, int] = field(default_factory=dict)
    approved: bool = False  # the user just said yes to an ask_user question
    max_steps: int = 0
    blocked: Callable[[str], bool] | None = None  # packages this task may not touch (API safety)
    memory: str = "on"  # on | read_only (use notes, save none) | off (incognito)
    notes_package: str | None = None  # the app whose notes were last shown
    used_notes: list[str] = field(default_factory=list)


def is_yes(answer: str) -> bool:
    words = re.sub(r"[^a-z ]", " ", answer.lower()).split()
    return bool(words) and words[0] in YES_WORDS


def _block_to_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return block.model_dump(exclude_none=True)


class Agent:
    def __init__(
        self,
        client,
        driver,
        resolver: AppResolver,
        config: Config,
        io: UserIO,
        judge: JevJudge | None = None,
        use_ocr: bool | None = None,
    ):
        self.client = client
        self.driver = driver
        self.resolver = resolver
        self.config = config
        self.io = io
        self.judge = judge
        self.use_ocr = (config.ocr and ocr.available()) if use_ocr is None else use_ocr
        self.recent: deque[tuple[str, str]] = deque(maxlen=RECENT_REQUESTS)
        self.usage = Usage()  # tokens for the task in progress (or the last one)
        self.platform = "android"  # set by the DeviceManager from the phone's hello (android | ios)
        self.memory = None  # Memory store (pinned notes), set by the DeviceManager; None: no memory
        self.account_id: str | None = None  # whose notes to use (the phone's owner)
        self.model = config.model  # chosen per phone from the dashboard
        self.task_model = config.model  # locked in when a task starts, so a mid-task switch waits
        if judge is not None and hasattr(judge, "client"):
            judge.client.on_usage = lambda tokens: self.usage.add_jev(tokens)

    # --- public ----------------------------------------------------------

    def run(
        self,
        task: str,
        cancel: threading.Event | None = None,
        go_home: bool = False,
        model: str | None = None,
        max_steps: int | None = None,
        blocked: Callable[[str], bool] | None = None,
        memory: str = "on",
        isolated: bool = False,
    ) -> AgentResult:
        """`model` and `max_steps` override this phone's settings for one task; `blocked` names
        packages the task must not open or operate (the task stops if one comes to the front).
        `memory`: on | read_only | off. `isolated`: a caller other than the phone's chat (e.g. an
        API key) that must neither see nor add to the chat's recent requests."""
        self.usage = Usage()
        self.task_model = model or self.model
        state = _TaskState(task=task, cancel=cancel or threading.Event(),
                           max_steps=max_steps or self.config.max_steps, blocked=blocked,
                           memory=memory if self.memory is not None else "off")
        result = self._run(state, go_home, isolated)
        result.usage = self.usage
        if state.used_notes:
            self.memory.mark_used(state.used_notes)
        if not isolated:
            self.recent.append((task, result.summary))
        return result

    def _run(self, state: _TaskState, go_home: bool, isolated: bool = False) -> AgentResult:
        task = state.task
        self.driver.wake_unlock()
        if go_home:
            self.driver.key("HOME")
            time.sleep(self.config.settle_delay)

        obs = self._observe_settled(state)
        messages: list[dict] = [{
            "role": "user",
            "content": [{"type": "text", "text": self._request_text(task, isolated)}, *self._obs_blocks(obs, self._app_notes(state, obs))],
        }]
        nudged = False

        system = system_prompt(self.platform) + self._user_notes(state)
        for step in range(1, state.max_steps + 1):
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step - 1)
            if self._is_blocked(state, obs.package):
                return AgentResult(False, BLOCKED_APP_MESSAGE, step - 1)

            self._trim_history(messages)
            response = self.client.messages.create(
                model=self.task_model,
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            # Count under the model the API says served this call, which verifies the model choice.
            self.usage.add_claude(getattr(response, "usage", None), getattr(response, "model", None) or self.task_model)
            content = [_block_to_dict(b) for b in response.content]
            messages.append({"role": "assistant", "content": content})

            for block in content:
                if block["type"] == "text" and block["text"].strip():
                    self.io.event("thought", block["text"].strip())

            tool_uses = [b for b in content if b["type"] == "tool_use"]
            if not tool_uses:
                text = " ".join(b["text"] for b in content if b["type"] == "text").strip()
                if nudged or response.stop_reason == "refusal":
                    return AgentResult(False, text or "I stopped without finishing.", step)
                nudged = True
                messages.append({
                    "role": "user",
                    "content": "Continue by calling a tool. Call done when the request is complete.",
                })
                continue

            results: list[dict] = []
            finished: Outcome | None = None
            settle = self.config.settle_delay
            for tool_use in tool_uses:
                if finished or state.cancel.is_set():
                    outcome = Outcome("Skipped.", is_error=True)
                else:
                    outcome = self._execute(tool_use["name"], tool_use["input"], obs, state)
                    self.io.event("error" if outcome.is_error else "action", f"{tool_use['name']}: {outcome.text}")
                    if outcome.settle is not None:
                        settle = outcome.settle
                    if outcome.done:
                        finished = outcome
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": [{"type": "text", "text": outcome.text}],
                    **({"is_error": True} if outcome.is_error else {}),
                })

            if finished:
                return AgentResult(finished.success, finished.text, step)
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step)

            self._sleep(settle, state.cancel)
            obs = self._observe_settled(state)
            # The new screen goes after the tool results, not inside them: the API rejects
            # images inside a tool_result marked is_error.
            messages.append({"role": "user", "content": results + self._obs_blocks(obs, self._app_notes(state, obs))})

        return AgentResult(False, f"I gave up after {state.max_steps} steps.", state.max_steps)

    @staticmethod
    def _is_blocked(state: _TaskState, package: str | None) -> bool:
        return bool(state.blocked and package and state.blocked(package))

    def _request_text(self, task: str, isolated: bool = False) -> str:
        if not self.recent or isolated:
            return f"Request: {task}"
        earlier = "\n".join(f"- {t!r} -> {summary}" for t, summary in self.recent)
        return f"Earlier requests this session (oldest first):\n{earlier}\n\nRequest: {task}"

    # --- observation -----------------------------------------------------

    def _observe(self, state: _TaskState) -> Observation:
        package = self.driver.current_app()
        shot = self.driver.screenshot()
        elements: list[UIElement] = []
        mode = self.config.ui_tree
        # Games make uiautomator slow and useless; stop asking after two empty dumps.
        if mode == "always" or (mode == "auto" and state.treeless_misses.get(package or "", 0) < 2):
            elements = self.driver.ui_elements()
            useful = any(el.text or el.desc for el in elements)
            key = package or ""
            state.treeless_misses[key] = 0 if useful else state.treeless_misses.get(key, 0) + 1
        if self.use_ocr and not any(el.text or el.desc for el in elements):
            elements = elements + self._ocr_elements(shot, start=len(elements))
        obs = Observation(shot, elements, package)
        if self.judge:
            try:
                obs.assessment = self.judge.assess_screen(state.task, self._describe(obs))
            except JevError as exc:
                self.io.event("error", str(exc))
        return obs

    def _observe_settled(self, state: _TaskState) -> Observation:
        """Observe, and sit out loading screens without spending a Claude call on each one."""
        obs = self._observe(state)
        while self._is_loading(obs) and state.auto_waited < self.config.jev_auto_wait_seconds:
            if state.cancel.is_set():
                break
            self.io.event("status", "Loading screen; waiting…")
            state.cancel.wait(LOADING_POLL_SECONDS)
            state.auto_waited += LOADING_POLL_SECONDS
            obs = self._observe(state)
        return obs

    def _is_loading(self, obs: Observation) -> bool:
        a = obs.assessment
        return bool(a and a.state == "loading" and a.probability >= 0.8 and a.confidence >= self.config.jev_min_confidence)

    def _ocr_elements(self, shot: Screenshot, start: int) -> list[UIElement]:
        try:
            boxes = ocr.recognize_text(shot.jpeg, shot.width, shot.height)
        except Exception as exc:
            self.io.event("error", f"OCR failed: {exc}")
            return []
        elements = []
        for box in boxes:
            if box.confidence < OCR_MIN_CONFIDENCE or not box.text.strip():
                continue
            x1, y1 = shot.to_device(box.x1, box.y1)
            x2, y2 = shot.to_device(box.x2, box.y2)
            if x2 <= x1 or y2 <= y1:
                continue
            elements.append(UIElement(start + len(elements), box.text.strip(), "", "", "OCRText", True, None, (x1, y1, x2, y2)))
        return elements

    @staticmethod
    def _describe(obs: Observation) -> dict:
        labels = [el.label for el in obs.elements if el.cls != "OCRText"]
        words = [el.text for el in obs.elements if el.cls == "OCRText"]
        return describe_screen(obs.package, labels, words)

    # --- memory (plain text, so it works the same with any model) --------------

    def _user_notes(self, state: _TaskState) -> str:
        if state.memory == "off":
            return ""
        notes = self.memory.user_notes(self.account_id)
        state.used_notes.extend(n["id"] for n in notes)
        return render_user_notes(notes)

    def _app_notes(self, state: _TaskState, obs: Observation) -> str:
        """The user's notes about the app in front, once each time that app comes to the front."""
        if state.memory == "off" or not obs.package or obs.package == state.notes_package:
            return ""
        state.notes_package = obs.package
        notes = self.memory.app_notes(self.account_id, obs.package)
        state.used_notes.extend(n["id"] for n in notes)
        return render_app_notes(obs.package, notes)

    def _event(self, kind: str, message: str, data: dict) -> None:
        """An event with structured details for IOs that take them (the dashboard's Coordinator)."""
        try:
            self.io.event(kind, message, data=data)
        except TypeError:  # a plain UserIO (terminal, tests): kind and message only
            self.io.event(kind, message)

    def _remember(self, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        if state.memory != "on":
            return Outcome("Memory is off for this task, so nothing was saved.", settle=0)
        package = obs.package if args.get("about_app") else None
        # The user's own words asked for it: save now. Otherwise (the agent inferred it, possibly from
        # screen text) it's only a proposal the user approves, so nothing on screen can plant a note.
        status = "active" if REMEMBER_INTENT.search(state.task) else "proposed"
        try:
            note = self.memory.add(self.account_id, args["text"], package=package, status=status, source="agent" if status == "proposed" else "user")
        except NoteError as exc:
            return Outcome(f"Not saved: {exc}", is_error=True, settle=0)
        where = f" (about {package})" if package else ""
        if note["status"] == "active":
            self._event("memory", f"Remembered{where}: {note['text']}", {"note_id": note["id"], "status": "active"})
            return Outcome(f"Saved to memory{where}: {note['text']}", settle=0)
        self._event("memory", f"Remember this{where}? {note['text']}", {"note_id": note["id"], "status": "proposed"})
        return Outcome(f"Suggested for memory{where}; the user will confirm it: {note['text']}", settle=0)

    def _obs_blocks(self, obs: Observation, notes: str = "") -> list[dict]:
        header = (
            f"{OBS_PREFIX} foreground app {obs.package or 'unknown'}; "
            f"screenshot is {obs.shot.width}x{obs.shot.height} px."
        ) + notes
        if obs.elements:
            body = "UI elements (OCRText = words read from the screenshot; centers are screenshot pixels):\n" + format_elements(obs.elements, obs.shot)
        else:
            body = "No UI element list or readable text for this screen. Work from the screenshot."
        if obs.assessment:
            body += "\n" + obs.assessment.summary()
        return [
            {"type": "text", "text": f"{header}\n{body}"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(obs.shot.jpeg).decode(),
                },
            },
        ]

    def _trim_history(self, messages: list[dict]) -> None:
        """Keep only the most recent screenshots (and element lists) in the history."""
        slots: list[tuple[list, int]] = []
        for message in messages:
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            containers = [message["content"]] + [
                b["content"] for b in message["content"]
                if b.get("type") == "tool_result" and isinstance(b.get("content"), list)
            ]
            for container in containers:
                for i, block in enumerate(container):
                    if block.get("type") == "image" or (
                        block.get("type") == "text" and block["text"].startswith(OBS_PREFIX)
                    ):
                        slots.append((container, i))
        keep = 2 * self.config.keep_screenshots
        for container, i in slots[:-keep] if keep else slots:
            container[i] = {"type": "text", "text": OMITTED}

    # --- actions ---------------------------------------------------------

    def _execute(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        try:
            return self._dispatch(name, args, obs, state)
        except (DeviceError, KeyError, ValueError, TypeError, IndexError) as exc:
            return Outcome(f"Error: {exc}", is_error=True)

    def _dispatch(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        shot = obs.shot
        if name == "launch_app":
            resolution = self.resolver.resolve(args["app"])
            if resolution is None:
                closest = ", ".join(f"{name} ({pkg})" for name, pkg in self.resolver.installed_apps(args["app"])[:5])
                return Outcome(
                    f"No installed app clearly matches {args['app']!r}. Closest: {closest}. Retry launch_app with "
                    "an exact package if one of these is right, or check list_apps. Otherwise press HOME, open the "
                    "app drawer, and search there.",
                    is_error=True,
                    settle=0,
                )
            if self._is_blocked(state, resolution.package):
                return Outcome(f"{BLOCKED_APP_MESSAGE} Don't try to open it another way; call done.", is_error=True, settle=0)
            self.driver.launch_app(resolution.package)
            return Outcome(
                f"Launched {resolution.package} (matched by {resolution.method}).",
                settle=self.config.launch_settle_delay,
            )

        if name == "list_apps":
            apps = self.resolver.installed_apps(args.get("query", ""))
            if args.get("query"):
                apps = apps[:15]
            return Outcome("\n".join(f"{n} ({p})" for n, p in apps) or "No apps found.", settle=0)

        if name == "open_url":
            self.driver.open_url(args["url"])
            return Outcome(f"Opened {args['url']}.", settle=self.config.launch_settle_delay)

        if name == "tap_element":
            index = int(args["index"])
            if not 0 <= index < len(obs.elements):
                return Outcome(f"No element [{index}] on the current screen.", is_error=True)
            element = obs.elements[index]
            if blocked := self._guard(f"{args.get('target', '')} {element.label}", state, obs):
                return blocked
            self.driver.tap(*element.center)
            return Outcome(f"Tapped [{index}] {element.label!r}.")

        if name == "tap":
            if blocked := self._guard(args.get("target", ""), state, obs):
                return blocked
            x, y = shot.to_device(args["x"], args["y"])
            self.driver.tap(x, y)
            return Outcome(f"Tapped {args.get('target') or 'point'} at ({args['x']},{args['y']}).")

        if name == "long_press":
            if blocked := self._guard(args.get("target", ""), state, obs):
                return blocked
            x, y = shot.to_device(args["x"], args["y"])
            self.driver.long_press(x, y, int(args.get("duration_ms", 800)))
            return Outcome(f"Long-pressed {args.get('target') or 'point'}.")

        if name == "scroll":
            w, h = shot.device_width, shot.device_height
            cx, cy = w // 2, h // 2
            dx, dy = int(w * 0.3), int(h * 0.25)
            moves = {
                "down": (cx, cy + dy, cx, cy - dy),
                "up": (cx, cy - dy, cx, cy + dy),
                "right": (cx + dx, cy, cx - dx, cy),
                "left": (cx - dx, cy, cx + dx, cy),
            }
            self.driver.swipe(*moves[args["direction"]], 400)
            return Outcome(f"Scrolled {args['direction']}.")

        if name == "swipe":
            x1, y1 = shot.to_device(args["x1"], args["y1"])
            x2, y2 = shot.to_device(args["x2"], args["y2"])
            self.driver.swipe(x1, y1, x2, y2, int(args.get("duration_ms", 300)))
            return Outcome("Swiped.")

        if name == "type_text":
            self.driver.type_text(args["text"])
            if args.get("submit"):
                self.driver.key("ENTER")
            return Outcome(f"Typed {args['text']!r}.")

        if name == "press_key":
            self.driver.key(args["key"])
            return Outcome(f"Pressed {args['key']}.")

        if name == "wait":
            seconds = min(max(float(args["seconds"]), 0.5), 30.0)
            self._sleep(seconds, state.cancel)
            return Outcome(f"Waited {seconds:g}s.", settle=0)

        if name == "remember":
            return self._remember(args, obs, state)

        if name == "ask_user":
            answer = self.io.ask(args["question"]).strip()
            state.approved = is_yes(answer)
            return Outcome(f"User answered: {answer or '(no answer)'}", settle=0)

        if name == "done":
            return Outcome(args.get("summary", "Done."), done=True, success=bool(args.get("success", True)))

        return Outcome(f"Unknown tool {name}.", is_error=True)

    def _guard(self, description: str, state: _TaskState, obs: Observation) -> Outcome | None:
        """Ask before risky taps ('Buy', 'Send', ...) the user didn't ask for.

        Risky: matches a confirm keyword, or Jev rates it risky. Requested: Jev judges that the
        request explicitly asked for it, or (without Jev) the matched keyword appears in the request.
        """
        policy = self.config.confirm_actions
        if policy == "never":
            return None
        target = description.lower()
        hits = [k for k in self.config.confirm_keywords if re.search(rf"\b{re.escape(k.lower())}\b", target)]
        risky = bool(hits)
        requested = any(re.search(rf"\b{re.escape(k.lower())}\b", state.task.lower()) for k in hits)
        if self.judge:
            try:
                risk, asked = self.judge.assess_tap(state.task, description.strip(), self._describe(obs))
                risky = risky or risk >= self.config.jev_risk_threshold
                requested = asked >= 0.5
            except JevError as exc:
                self.io.event("error", str(exc))
        if not risky or (policy == "unrequested" and requested):
            return None
        if state.approved:
            state.approved = False
            return None
        # A separate channel when the IO has one, so callers can tell a risky-action
        # confirmation apart from an ordinary question.
        confirm = getattr(self.io, "confirm", self.io.ask)
        answer = confirm(f"I'm about to tap {description.strip()!r}. Should I go ahead?")
        if is_yes(answer):
            return None
        return Outcome(
            f"The user did not approve tapping {description.strip()!r} (they said {answer!r}). "
            "Don't do it. Pick another approach or call done.",
            is_error=True,
            settle=0,
        )

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event) -> None:
        cancel.wait(seconds)
