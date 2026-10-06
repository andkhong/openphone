"""The observe → decide → act loop that drives the phone with any model (see models.py)."""

from __future__ import annotations

import base64
import contextlib
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..config import Config
from ..jev import JevError, JevJudge, ScreenAssessment, StepChoice, describe_screen
from ..memory import REMEMBER_INTENT, NoteError, render_app_notes, render_user_notes
from ..phone.adb import Screenshot, UIElement, downscale, format_elements
from ..phone.base import DeviceError
from ..phone.apps import AppResolver
from ..usage import Usage
from ..vision import ocr
from . import fast
from .models import ModelError, ModelRequest, ModelResponse, ModelUnavailable, router_for
from .timing import StepTimer
from .tools import TOOL_SPECS, system_prompt

OBS_PREFIX = "SCREEN:"
OMITTED = "(earlier screen omitted)"
LOADING_POLL_SECONDS = 2.0
RECENT_REQUESTS = 5
OCR_MIN_CONFIDENCE = 0.3
REDACTED = "<redacted>"
POINT_ARGS = {"tap": (("x", "y"),), "long_press": (("x", "y"),), "swipe": (("x1", "y1"), ("x2", "y2"))}
FOCUS_QUIET_MS = 150  # fast TYPE_TEXT: after tapping the field, before typing
SEARCH_EXTRA_QUIET_MS = 200  # after typing into a search field (jev-ultrafast waits 200 ms for suggestions)
RECHECK_QUIET_MS = 200  # a tap target that moved (a list settling): wait this quiet before trusting its new place
RECHECK_MAX_MS = 800
DEFAULT_QUIET_MS = 300  # config.settle_quiet_ms by default (a variant names any other value)
BLOCKED_APP_MESSAGE = "This app is blocked for API tasks on this phone (its owner can allow it)."
# vision_brief: replies are mostly the tool call, so the step ends sooner (the thought still reaches
# the dashboard as a "thought" event).
BRIEF_REPLIES = ("\nReply briefly: at most one short sentence about what you see or why, then the tool call. "
                 "No lists, no restating the request.\n")
# Per-step model cost, recorded with each step (the steps table, `openmob timings`).
STEP_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "vision_image_side")
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
    timings: dict = field(default_factory=dict)  # the task's time per phase in ms (see timing.py)
    fast_steps: int = 0  # tool calls the fast path made (no main model)
    done_via: str | None = None  # who said the task was done: "vision" (the main model) | "classifier" (internal)


class Observation:
    """One look at the phone. The screenshot can be deferred (`capture`): a fast step decides from the
    element list alone, and the screenshot is taken only when something needs it (a vision step)."""

    def __init__(self, shot: Screenshot | None, elements: list[UIElement], package: str | None,
                 assessment: ScreenAssessment | None = None, capture: Callable[[], Screenshot] | None = None,
                 geometry: Screenshot | None = None):
        self._shot = shot
        self.elements = elements
        self.package = package
        self.assessment = assessment
        self.capture = capture
        self._geometry = geometry  # a recent frame of the same phone: its sizes, when no screenshot was taken
        # From the one-request classifier call (jev_one_request), when it ran on this screen:
        self.heads: dict | None = None  # the fast path's target heads it was asked
        self.choice: StepChoice | None = None  # its fast-path decision
        self.risks: dict[int, float] = {}  # element index -> tap risk
        self.requested: dict[int, float] = {}  # element index -> "the request asked for this tap"

    @property
    def shot(self) -> Screenshot:
        if self._shot is None:
            if self.capture is None:
                raise DeviceError("No screenshot of this screen")
            self._shot = self.capture()
        return self._shot

    @property
    def captured(self) -> bool:
        return self._shot is not None

    def geometry(self) -> Screenshot | None:
        """This screen's screenshot if taken, else a recent frame's (for device sizes only)."""
        return self._shot or self._geometry


@dataclass
class Outcome:
    text: str
    is_error: bool = False
    done: bool = False
    success: bool = False
    settle: float | None = None  # None means the default settle delay (the cap, with adaptive settle)
    timings: dict | None = None  # this step's time per phase in ms (set by _execute)
    launched: str | None = None  # the package launch_app started (adb's launch settle waits for it)
    quiet_ms: int | None = None  # adaptive settle: a longer quiet time (e.g. search suggestions)
    done_via: str | None = None  # for done: "vision" | "classifier"
    tokens: dict | None = None  # the model calls since the last step (set by _execute; see _note_tokens)


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
    step_index: int = 0  # tool calls executed so far (the steps table's idx)
    settle_idle: bool | None = None  # the last adaptive settle: the screen went quiet (False: hit the cap)
    fast: bool = False  # the fast path is on for this task
    vision_next: bool = False  # the next step must be a vision step (the last fast step made no progress)
    fast_steps: int = 0  # tool calls the fast path made
    # The actions so far, for the fast path's loop check: {"action", "screen_changed"}.
    actions: list[dict] = field(default_factory=list)
    unshown: Observation | None = None  # the current screen, not yet in the main model's conversation
    # For the classifier's DONE (fast_path_done): anything that makes this more than plain navigation.
    escalated: bool = False  # a vision step ran, or the fast path declined or looped
    risky: bool = False  # the risky-tap check fired on some step (keywords or the classifier)
    typed: bool = False  # text was typed


def is_yes(answer: str) -> bool:
    words = re.sub(r"[^a-z ]", " ", answer.lower()).split()
    return bool(words) and words[0] in YES_WORDS


class Agent:
    def __init__(
        self,
        client,  # a models.ModelRouter, or one Anthropic-shaped client (serves every Claude model)
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
        self.timer = StepTimer()  # where each step's time goes (measurement only)
        self._frame: Screenshot | None = None  # the last screenshot the agent took (for sizes)
        self._step_tokens: dict | None = None  # model tokens since the last recorded step (measurement only)
        self._shown_side: int | None = None  # long side of the newest screenshot in the conversation
        self._last_record: dict | None = None  # the newest step record (vision_stream amends its tokens)
        self._acted_early = False  # a streamed reply's tool call already ran
        self._task_variant = self._variant()  # the latency features of the task in progress (steps record it)
        if judge is not None and hasattr(judge, "client"):
            judge.client.on_usage = lambda tokens: self.usage.add_jev(tokens)
        if judge is not None:
            try:  # classifier app picking happens inside the resolver; it reports its time here
                judge.on_timing = lambda seconds: self.timer.add("jev", seconds)
            except AttributeError:
                pass

    # --- public ----------------------------------------------------------

    def model_available(self, model: str) -> bool:
        """Whether this agent can run `model`: its provider has a key here and it can operate a phone."""
        return router_for(self.client, self.config).available(model)

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
        self.timer.reset()
        self._step_tokens = self._shown_side = None
        self.task_model = model or self.model
        state = _TaskState(task=task, cancel=cancel or threading.Event(),
                           max_steps=max_steps or self.config.max_steps, blocked=blocked,
                           memory=memory if self.memory is not None else "off")
        state.fast = self._fast_enabled()
        self._task_variant = self._variant(state.fast)
        result = self._run(state, go_home, isolated)
        result.usage = self.usage
        result.timings = self.timer.task_totals()
        result.fast_steps = state.fast_steps
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
            self._settle(self.config.settle_delay, state)

        try:
            route = router_for(self.client, self.config).route(self.task_model)
        except ModelUnavailable as exc:
            return AgentResult(False, str(exc), 0)
        obs = self._observe_settled(state)
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": self._request_text(task, isolated)}]}]
        self._show_later(state, messages, obs)
        nudged = False

        system = system_prompt(self.platform) + (BRIEF_REPLIES if self.config.vision_brief else "") + self._user_notes(state)
        for step in range(1, state.max_steps + 1):
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step - 1)
            if self._is_blocked(state, obs.package):
                return AgentResult(False, BLOCKED_APP_MESSAGE, step - 1)

            self._apply_corrections(state, messages)
            if state.fast and not state.vision_next:
                taken = self._fast_step(state, obs)
                if taken is not None:
                    last = taken[-1][2]
                    if last.done:
                        return AgentResult(last.success, last.text, step, done_via=last.done_via)
                    obs = self._after_fast_step(state, obs, taken, messages)
                    continue
            state.vision_next = False
            state.escalated = True
            self._show(state, messages)
            self._trim_history(messages)
            early: dict[str, Outcome] = {}  # tool calls already run while the reply streamed (vision_stream)

            def act_early(call) -> None:
                if any(o.done for o in early.values()) or state.cancel.is_set():
                    return  # the loop below marks it skipped
                early[call.id] = self._act(call, obs, state)

            response, streamed = self._vision_call(route, system, messages, act_early)
            messages.append({"role": "assistant", "content": response.content})

            for text in [] if streamed else response.text:  # streamed thoughts went out as they came
                if text.strip():
                    self.io.event("thought", text.strip())

            tool_uses = response.tool_calls
            if not tool_uses:
                text = " ".join(response.text).strip()
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
            launched: str | None = None
            quiet_ms: int | None = None
            for tool_use in tool_uses:
                if tool_use.id in early and not finished:
                    outcome = early[tool_use.id]
                elif finished or state.cancel.is_set():
                    outcome = Outcome("Skipped.", is_error=True)
                else:
                    outcome = self._act(tool_use, obs, state)
                if outcome.timings is not None:  # it ran
                    if outcome.settle is not None:
                        settle = outcome.settle
                    launched = outcome.launched or launched
                    quiet_ms = outcome.quiet_ms or quiet_ms
                    if outcome.done:
                        finished = outcome
                results.append({
                    "type": "tool_result",
                    "call_id": tool_use.id,
                    "content": [{"type": "text", "text": outcome.text}],
                    **({"is_error": True} if outcome.is_error else {}),
                })

            if finished:
                return AgentResult(finished.success, finished.text, step, done_via=finished.done_via)
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step)

            self._settle(settle, state, launched, quiet_ms)
            obs = self._observe_after_action(state, obs)
            # The new screen goes after the tool results, not inside them: Anthropic rejects images
            # inside a tool_result marked is_error, and OpenAI tool messages hold text only.
            messages.append({"role": "user", "content": results})
            self._show_later(state, messages, obs)

        return AgentResult(False, f"I gave up after {state.max_steps} steps.", state.max_steps)

    # --- the vision step's model call ---------------------------------------------

    def _vision_request(self, route, system: str, messages: list[dict], max_tokens: int) -> ModelRequest:
        return ModelRequest(model=self.task_model, system=system, tools=TOOL_SPECS, messages=messages,
                            max_tokens=max_tokens, cache_history=self.config.history_cache)

    def _act(self, call, obs: Observation, state: _TaskState) -> Outcome:
        """Run one of the main model's tool calls and report it."""
        outcome = self._execute(call.name, call.input, obs, state)
        self._event("error" if outcome.is_error else "action", f"{call.name}: {outcome.text}",
                    {"timings": outcome.timings, "jev": self.judge is not None})
        return outcome

    def _vision_call(self, route, system: str, messages: list[dict], act=None):
        """The main model decides this step: (response, streamed). With vision_max_tokens, a reply that
        hit that cap before finishing its tool call is asked again with the model's full max_tokens.
        With vision_stream (and a provider that streams), `act` runs each tool call as soon as its
        block is complete, before the reply ends."""
        full = route.spec.max_tokens
        cap = self.config.vision_max_tokens
        limit = min(cap, full) if cap > 0 else full
        response = None
        if act is not None and self.config.vision_stream and hasattr(route.adapter, "stream"):
            response = self._vision_stream(route, system, messages, limit, act)
            if response is not None and (response.stop_reason != "max_tokens" or limit >= full
                                         or self._acted_early):
                return response, True
        if response is None:
            with self.timer.phase("model"):
                response = route.adapter.complete(self._vision_request(route, system, messages, limit))
            self._count_vision(response)
        if response.stop_reason == "max_tokens" and limit < full:
            with self.timer.phase("model"):
                response = route.adapter.complete(self._vision_request(route, system, messages, full))
            self._count_vision(response)
        return response, False

    def _vision_stream(self, route, system: str, messages: list[dict], limit: int, act) -> ModelResponse | None:
        """Stream the reply; thoughts go out and tool calls run as their blocks complete. The model's
        time is the time spent waiting on the stream (an action in between is action time). Returns
        None when the stream failed before anything ran (the caller asks again without streaming)."""
        self._acted_early = False
        seen: list[dict] = []
        record = None
        events = route.adapter.stream(self._vision_request(route, system, messages, limit))
        while True:
            try:
                with self.timer.phase("model"):
                    kind, value = next(events)
            except StopIteration:
                return None if not self._acted_early else ModelResponse(seen, "tool_use", None, None)
            except ModelError as exc:
                if not self._acted_early:
                    return None
                self.io.event("error", f"The rest of the reply was lost: {exc}")
                return ModelResponse(seen, "tool_use", None, None)
            if kind == "text":
                seen.append({"type": "text", "text": value})
                if value.strip():
                    self.io.event("thought", value.strip())
            elif kind == "tool_call":
                seen.append({"type": "tool_call", "id": value.id, "name": value.name, "input": value.input})
                self._last_record = None
                act(value)
                if not self._acted_early:
                    self._acted_early = True
                    record = self._last_record  # the step that gets this reply's tokens
            elif kind == "response":
                self._count_vision(value)
                if record is not None and self._step_tokens:
                    # The step already ran and was recorded: add the reply's tokens to its record.
                    record.update(self._step_tokens)
                    self._step_tokens = None
                    with contextlib.suppress(Exception):
                        self.io.record_step(record)
                return value

    def _count_vision(self, response) -> None:
        # Count under the model the API says served this call, which verifies the model choice.
        self.usage.add_model(response.usage, response.model or self.task_model)
        self._note_tokens(response.usage, self._shown_side)

    @staticmethod
    def _is_blocked(state: _TaskState, package: str | None) -> bool:
        return bool(state.blocked and package and state.blocked(package))

    def _request_text(self, task: str, isolated: bool = False) -> str:
        if not self.recent or isolated:
            return f"Request: {task}"
        earlier = "\n".join(f"- {t!r} -> {summary}" for t, summary in self.recent)
        return f"Earlier requests this session (oldest first):\n{earlier}\n\nRequest: {task}"

    # --- observation -----------------------------------------------------

    def _observe(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = ()) -> Observation:
        with self.timer.phase("observe"):
            return self._observe_untimed(state, before, pending)

    def _lazy_shots(self, state: _TaskState) -> bool:
        return state.fast and self.config.fast_path_lazy_screenshot

    def _screenshot(self) -> Screenshot:
        with self.timer.phase("observe"):
            shot = self.driver.screenshot()
        self._frame = shot
        return shot

    def _recent_frame(self) -> Screenshot | None:
        """A recent capture of this phone (the agent's own, else the live view's), for its sizes."""
        latest = getattr(self.driver, "latest_capture", None)
        try:
            return (latest() if latest else None) or self._frame
        except Exception:
            return self._frame

    def _observe_untimed(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = ()) -> Observation:
        package = self.driver.current_app()
        # The fast path decides from the element list; its screenshot waits until a vision step needs it.
        shot = None if self._lazy_shots(state) else self._screenshot()
        elements: list[UIElement] = []
        mode = self.config.ui_tree
        # Games make uiautomator slow and useless; stop asking after two empty dumps.
        if mode == "always" or (mode == "auto" and state.treeless_misses.get(package or "", 0) < 2):
            elements = self.driver.ui_elements()
            useful = any(el.text or el.desc for el in elements)
            key = package or ""
            state.treeless_misses[key] = 0 if useful else state.treeless_misses.get(key, 0) + 1
        if self.use_ocr and not any(el.text or el.desc for el in elements):
            shot = shot or self._screenshot()  # OCR reads the screenshot (no element list: a vision step anyway)
            elements = elements + self._ocr_elements(shot, start=len(elements))
        obs = Observation(shot, elements, package, capture=self._screenshot,
                          geometry=None if shot else self._recent_frame())
        if before is not None:  # whether the last actions changed the screen (the fast path's loop check)
            changed = fast.signature(obs.package, obs.elements) != fast.signature(before.package, before.elements)
            for action in pending:
                action["screen_changed"] = changed
        if self.judge:
            try:
                if self._one_request(state, obs):
                    try:
                        self._classify_step(state, obs)
                    except (KeyError, TypeError, ValueError) as exc:  # a malformed answer: the vision step
                        self.io.event("error", f"Fast step skipped: {exc}")
                else:
                    obs.assessment = self.timer.timed("jev", self.judge.assess_screen, state.task, self._describe(obs))
            except JevError as exc:
                self.io.event("error", str(exc))
        return obs

    def _one_request(self, state: _TaskState, obs: Observation) -> bool:
        """Ask the screen classifier everything for this step at once: on fast-path tasks, on screens the
        fast path can use (jev_one_request)."""
        return (state.fast and self.config.jev_one_request and hasattr(self.judge, "observe_step")
                and fast.eligible(obs.elements, self.config.fast_path_min_elements))

    def _classify_step(self, state: _TaskState, obs: Observation) -> None:
        """One classifier request: the screen's state and "looks done", the fast path's operation and
        targets, and the risky-tap questions for up to jev_risk_heads candidate elements (tappable
        ones first). Requested questions only for elements a confirm keyword names (the only ones
        whose "requested" can matter, see _guard)."""
        heads = fast.targets(obs.elements)
        obs.heads = heads
        candidates = sorted((el for el in obs.elements if fast.labeled(el) and not el.covered),
                            key=lambda el: not el.clickable)
        risk = {str(el.index): el.label for el in candidates[: max(self.config.jev_risk_heads, 0)]}
        requested = {k: label for k, label in risk.items() if self._keyword_hits(label)}
        verdict = self.timer.timed("jev", self.judge.observe_step, state.task,
                                   fast.screen_table(obs.package, obs.elements), state.actions[-5:], heads,
                                   risk, requested)
        obs.assessment, obs.choice = verdict.assessment, verdict.choice
        obs.risks = {int(k): v for k, v in verdict.risks.items()}
        obs.requested = {int(k): v for k, v in verdict.requested.items()}

    def _observe_settled(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = ()) -> Observation:
        """Observe, and sit out loading screens without spending a Claude call on each one."""
        obs = self._observe(state, before, pending)
        while self._is_loading(obs) and state.auto_waited < self.config.jev_auto_wait_seconds:
            if state.cancel.is_set():
                break
            self.io.event("status", "Loading screen; waiting…")
            self.timer.timed("wait", state.cancel.wait, LOADING_POLL_SECONDS)
            state.auto_waited += LOADING_POLL_SECONDS
            obs = self._observe(state, before, pending)
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

    def _rich_labels(self, obs: Observation) -> bool:
        """A screen the element list describes well: enough labeled accessibility elements, and no
        OCR words (a game or an app drawing its own UI is read from the picture)."""
        if any(el.cls == fast.OCR_CLASS for el in obs.elements):
            return False
        return sum(fast.labeled(el) for el in obs.elements) >= self.config.vision_labeled_min_elements

    def _right_size(self, obs: Observation) -> None:
        """vision_max_side_labeled: on a richly labeled screen the model mostly taps by element index, so
        a smaller screenshot does (fewer image tokens, a faster step). The observation's screenshot is
        replaced by the smaller one, so every coordinate the model gives maps back through it."""
        side = self.config.vision_max_side_labeled
        if side <= 0 or not self._rich_labels(obs):
            return
        try:
            shot = obs.shot  # a lazy screenshot is taken here (timed as observe on its own)
            with self.timer.phase("observe"):
                obs._shot = downscale(shot, side)
        except (OSError, ValueError) as exc:  # an undecodable frame: keep the full one
            self.io.event("error", f"Screenshot not resized: {exc}")

    def _obs_blocks(self, obs: Observation, notes: str = "") -> list[dict]:
        self._right_size(obs)
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
        self._shown_side = max(obs.shot.width, obs.shot.height)
        return [
            {"type": "text", "text": f"{header}\n{body}"},
            {"type": "image", "media_type": "image/jpeg", "data": base64.b64encode(obs.shot.jpeg).decode()},
        ]

    def _show_later(self, state: _TaskState, messages: list[dict], obs: Observation) -> None:
        """The main model sees `obs` at its next turn. Without lazy screenshots it goes into the
        conversation now (as before); with them, when a vision step comes (_show)."""
        state.unshown = obs
        if not self._lazy_shots(state):
            self._show(state, messages)

    def _show(self, state: _TaskState, messages: list[dict]) -> None:
        """Put the current screen (text and screenshot) at the end of the last user message."""
        obs, state.unshown = state.unshown, None
        if obs is None:
            return
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": []})
        last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].extend(self._obs_blocks(obs, self._app_notes(state, obs)))

    # --- mid-task corrections ("steer") ----------------------------------------

    def _apply_corrections(self, state: _TaskState, messages: list[dict]) -> None:
        """What the user said while the task ran (Coordinator.steer) joins the next user message.
        It's the user's own words, so it also counts as part of the request (the risky-tap and
        "remember" checks read it)."""
        take = getattr(self.io, "take_corrections", None)
        for text in take() if take else []:
            line = f"User correction: {text}"
            last = messages[-1]
            if last["role"] != "user":
                messages.append({"role": "user", "content": []})
                last = messages[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": line})
            state.task = f"{state.task}\n{line}"

    def _trim_history(self, messages: list[dict]) -> None:
        """Keep only the most recent screenshots (and element lists) in the history.

        rolling: the newest keep_screenshots every step, which rewrites an earlier message each step.
        batch: nothing changes until there are more than history_max_screenshots, then the oldest go
        down to keep_screenshots at once. Earlier messages stay byte-identical between trims, so the
        provider's prompt cache keeps covering the conversation."""
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
        if self.config.history_trim == "batch" and keep:
            ceiling = 2 * max(self.config.history_max_screenshots, self.config.keep_screenshots)
            if len(slots) <= ceiling:
                return
        for container, i in slots[:-keep] if keep else slots:
            container[i] = {"type": "text", "text": OMITTED}

    # --- actions ---------------------------------------------------------

    def _execute(self, name: str, args: dict, obs: Observation, state: _TaskState, fast_step: bool = False,
                 done_via: str = "vision") -> Outcome:
        started = time.monotonic()
        try:
            with self.timer.phase("action"):
                outcome = self._dispatch(name, args, obs, state)
        except (DeviceError, KeyError, ValueError, TypeError, IndexError) as exc:
            outcome = Outcome(f"Error: {exc}", is_error=True)
        if outcome.done:
            outcome.done_via = done_via
        outcome.timings = self.timer.take()
        outcome.tokens, self._step_tokens = self._step_tokens, None
        state.step_index += 1
        state.actions.append({"action": self._action_text(name, args, obs), "screen_changed": None})
        self._record_step(state, name, args, obs, outcome, time.monotonic() - started, fast_step)
        return outcome

    def _note_tokens(self, tokens, image_side: int | None = None) -> None:
        """Count a main-model call's tokens toward the next recorded step (when a response holds several
        tool calls, they go on the first, like its timings)."""
        if tokens is None and image_side is None:
            return
        step = self._step_tokens or dict.fromkeys(STEP_TOKEN_FIELDS)
        if tokens is not None:
            for key, value in (("input_tokens", tokens.input), ("output_tokens", tokens.output),
                               ("cache_read_tokens", tokens.cache_read), ("cache_write_tokens", tokens.cache_write)):
                step[key] = (step[key] or 0) + value
        if image_side is not None:
            step["vision_image_side"] = image_side
        self._step_tokens = step

    def _record_step(self, state: _TaskState, name: str, args: dict, obs: Observation, outcome: Outcome, seconds: float,
                     fast_step: bool = False) -> None:
        """One structured row per tool call, for IOs that keep them (the Coordinator → steps table).
        Text only: typed text is always redacted (we can't tell a password field), and no screenshot."""
        record = getattr(self.io, "record_step", None)
        if record is None:
            return
        try:
            shot = obs.geometry()  # never takes a screenshot just for the record
            safe = dict(args)
            text = outcome.text
            if name == "type_text":
                safe["text"] = REDACTED
                if not outcome.is_error:
                    text = f"Typed {REDACTED}."
            points: list[int] = []
            anchor = None
            if name == "tap_element" and str(args.get("index", "")).lstrip("-").isdigit():
                i = int(args["index"])
                if 0 <= i < len(obs.elements):
                    el = obs.elements[i]
                    anchor = {"text": el.text, "desc": el.desc, "resource_id": el.resource_id, "class": el.cls,
                              "bounds": list(el.bounds), "index": el.index}
                    points = list(el.center)
            for xk, yk in POINT_ARGS.get(name, ()):
                if shot and isinstance(args.get(xk), (int, float)) and isinstance(args.get(yk), (int, float)):
                    points += shot.to_device(args[xk], args[yk])
            # Device coordinates as 0-1 fractions: [x, y] per point (a swipe has two points).
            norm = [round(v / (shot.device_width if i % 2 == 0 else shot.device_height), 4)
                    for i, v in enumerate(points)] if shot else []
            self._last_record = row = {
                "idx": state.step_index, "t": time.time(), "tool": name, "args": safe,
                "norm_xy": norm or None, "package": obs.package,
                "screen_w": shot.device_width if shot else None, "screen_h": shot.device_height if shot else None,
                "anchor": anchor,
                "outcome": text, "is_error": outcome.is_error, "latency_ms": int(seconds * 1000),
                "model": self.task_model, "jev": self.judge is not None, **(outcome.timings or {}),
                "settle_idle": state.settle_idle, "variant": self._task_variant, "fast": fast_step,
                "done_via": outcome.done_via, **(outcome.tokens or {}),
            }
            record(row)
        except Exception as exc:  # the trajectory record must never break a task
            self.io.event("error", f"Step not recorded: {exc}")

    def _dispatch(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome:
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
                launched=resolution.package,
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
            if element.covered:
                return Outcome(f"Element [{index}] {element.label!r} is covered by another window (a dialog or the "
                               "keyboard), so a tap would land on that. Deal with what covers it first.",
                               is_error=True, settle=0)
            # The step's one classifier request asked about the element by its label. When the model
            # described the target in other words, the check asks again with those words included.
            same = str(args.get("target", "")).strip() in ("", element.label)
            if blocked := self._guard(f"{args.get('target', '')} {element.label}", state, obs, element if same else None):
                return blocked
            target = self._recheck_target(element)
            if isinstance(target, Outcome):
                return target
            self.driver.tap(*target.center)
            return Outcome(f"Tapped [{index}] {element.label!r}.")

        if name == "tap":
            if blocked := self._guard(args.get("target", ""), state, obs):
                return blocked
            x, y = obs.shot.to_device(args["x"], args["y"])
            self.driver.tap(x, y)
            return Outcome(f"Tapped {args.get('target') or 'point'} at ({args['x']},{args['y']}).")

        if name == "long_press":
            if blocked := self._guard(args.get("target", ""), state, obs):
                return blocked
            x, y = obs.shot.to_device(args["x"], args["y"])
            self.driver.long_press(x, y, int(args.get("duration_ms", 800)))
            return Outcome(f"Long-pressed {args.get('target') or 'point'}.")

        if name == "scroll":
            frame = obs.shot  # this screen's own size (a recent frame may predate a rotation)
            w, h = frame.device_width, frame.device_height
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
            x1, y1 = obs.shot.to_device(args["x1"], args["y1"])
            x2, y2 = obs.shot.to_device(args["x2"], args["y2"])
            self.driver.swipe(x1, y1, x2, y2, int(args.get("duration_ms", 300)))
            return Outcome("Swiped.")

        if name == "type_text":
            state.typed = True
            self.driver.type_text(args["text"])
            if args.get("submit"):
                self.driver.key("ENTER")
            # A search box: give its suggestions a moment longer to arrive before the next look.
            search = any(fast.is_search_field(el) for el in obs.elements)
            return Outcome(f"Typed {args['text']!r}.",
                           quiet_ms=self.config.settle_quiet_ms + SEARCH_EXTRA_QUIET_MS if search else None)

        if name == "press_key":
            self.driver.key(args["key"])
            return Outcome(f"Pressed {args['key']}.")

        if name == "wait":
            seconds = min(max(float(args["seconds"]), 0.5), 30.0)
            self.timer.timed("settle", self._sleep, seconds, state.cancel)
            return Outcome(f"Waited {seconds:g}s.", settle=0)

        if name == "remember":
            return self._remember(args, obs, state)

        if name == "ask_user":
            answer = self.timer.timed("wait", self.io.ask, args["question"]).strip()
            state.approved = is_yes(answer)
            return Outcome(f"User answered: {answer or '(no answer)'}", settle=0)

        if name == "done":
            return Outcome(args.get("summary", "Done."), done=True, success=bool(args.get("success", True)))

        return Outcome(f"Unknown tool {name}.", is_error=True)

    def _keyword_hits(self, text: str) -> list[str]:
        target = text.lower()
        return [k for k in self.config.confirm_keywords if re.search(rf"\b{re.escape(k.lower())}\b", target)]

    def _guard(self, description: str, state: _TaskState, obs: Observation, element: UIElement | None = None) -> Outcome | None:
        """Ask before risky taps ('Buy', 'Send', ...) the user didn't ask for.

        Risky: matches a confirm keyword, or Jev rates it risky. Requested: Jev judges that the
        request explicitly asked for it, or (without Jev) the matched keyword appears in the request.
        For a tap on `element`, the step's one classifier request may already hold its risk (and,
        when it can matter, its "requested"); otherwise the separate check asks now.
        """
        policy = self.config.confirm_actions
        if policy == "never":
            return None
        hits = self._keyword_hits(description)
        risky = bool(hits)
        requested = any(re.search(rf"\b{re.escape(k.lower())}\b", state.task.lower()) for k in hits)
        if self.judge:
            try:
                known = element is not None and element.index in obs.risks and (
                    not requested or element.index in obs.requested)
                if known:
                    risk, asked = obs.risks[element.index], obs.requested.get(element.index, 0.0)
                else:
                    risk, asked = self.timer.timed("jev", self.judge.assess_tap, state.task, description.strip(),
                                                   self._describe(obs))
                risky = risky or risk >= self.config.jev_risk_threshold
                # The classifier only ever adds caution. It reads on-screen text, so a screen could
                # talk it into "the user asked for this"; it may veto "requested", never grant it.
                requested = requested and asked >= 0.5
            except JevError as exc:
                self.io.event("error", str(exc))
        if risky:
            state.risky = True
        if not risky or (policy == "unrequested" and requested):
            return None
        if state.approved:
            state.approved = False
            return None
        # A separate channel when the IO has one, so callers can tell a risky-action
        # confirmation apart from an ordinary question.
        confirm = getattr(self.io, "confirm", self.io.ask)
        answer = self.timer.timed("wait", confirm, f"I'm about to tap {description.strip()!r}. Should I go ahead?")
        if is_yes(answer):
            return None
        return Outcome(
            f"The user did not approve tapping {description.strip()!r} (they said {answer!r}). "
            "Don't do it. Pick another approach or call done.",
            is_error=True,
            settle=0,
        )

    def _recheck_target(self, element: UIElement) -> UIElement | Outcome:
        """Just before a tap_element: re-read the elements and return where to tap, or refuse.

        Still in place: tap it. Moved, but the very same element (same text, description, id and
        class, and only one such element) is on screen, e.g. a list still settling after an app
        opened: wait for the screen to go quiet, confirm it stayed put, and tap it there; the guard
        already judged that element. Anything else (gone, ambiguous, covered, still moving) is
        refused. Only on phones whose element list is quick to read (the OpenMob app); adb and iOS
        dumps take a second or more."""
        if not self.config.verify_taps or element.cls == fast.OCR_CLASS or not getattr(self.driver, "fast_ui_tree", False):
            return element
        with self.timer.phase("observe"):
            fresh = self.driver.ui_elements()
        if not fresh:
            return element  # nothing to compare with (a tree that's briefly empty): tap as decided
        identity = (element.text, element.desc, element.resource_id, element.cls)
        if any((el.text, el.desc, el.resource_id, el.cls, el.bounds) == identity + (element.bounds,) for el in fresh):
            return element
        refused = Outcome(f"Didn't tap [{element.index}] {element.label!r}: the screen changed and it is no longer "
                          "where it was. Look at the new screen.", is_error=True, settle=0)
        if not (element.text or element.desc or element.resource_id):
            return refused  # nothing to recognise it by
        matches = [el for el in fresh if (el.text, el.desc, el.resource_id, el.cls) == identity]
        if len(matches) != 1:
            return refused
        wait_idle = getattr(self.driver, "wait_idle", None)
        if callable(wait_idle):
            with self.timer.phase("settle"):
                try:
                    wait_idle(RECHECK_QUIET_MS, RECHECK_MAX_MS)
                except Exception:  # an old phone without wait_idle: compare the two reads as they are
                    pass
        with self.timer.phase("observe"):
            again = [el for el in self.driver.ui_elements() if (el.text, el.desc, el.resource_id, el.cls) == identity]
        if len(again) != 1 or again[0].bounds != matches[0].bounds or getattr(again[0], "covered", False):
            return refused  # still moving, gone, duplicated or now covered
        return again[0]

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event) -> None:
        cancel.wait(seconds)

    # --- the fast path (see fast.py) -------------------------------------------------

    def _fast_enabled(self) -> bool:
        """fast_path is on (or fast_tier and this task runs on the fast tier's model) and the
        classifier is configured and enabled."""
        mode = self.config.fast_path
        if mode == "off" or self.judge is None or not hasattr(self.judge, "next_step"):
            return False
        if mode == "fast_tier":
            return self.task_model == (self.config.api_tiers.get("fast") or {}).get("model")
        return True

    @staticmethod
    def _action_text(name: str, args: dict, obs: Observation) -> str:
        """A short, text-free description of an action (typed text stays out of the classifier)."""
        if name == "tap_element" and str(args.get("index", "")).lstrip("-").isdigit():
            i = int(args["index"])
            if 0 <= i < len(obs.elements):
                return f"tap [{i}] {obs.elements[i].label[:60]!r}"
        if name in ("scroll", "press_key", "launch_app"):
            return f"{name} {args.get('direction') or args.get('key') or args.get('app')}"
        if name == "type_text":
            return "type text"
        return name

    def _fast_step(self, state: _TaskState, obs: Observation) -> list[tuple[str, dict, Outcome]] | None:
        """One step without the main model, or None: take the vision step. Returns what ran."""
        c = self.config
        if not fast.eligible(obs.elements, c.fast_path_min_elements):
            return None
        if obs.heads is not None:  # the step's one classifier request already decided (_classify_step)
            heads, choice = obs.heads, obs.choice
            if choice is None:
                return None  # that request failed: the vision step
        else:
            heads = fast.targets(obs.elements)
            try:
                choice = self.timer.timed("jev", self.judge.next_step, state.task,
                                          fast.screen_table(obs.package, obs.elements), state.actions[-5:], heads)
            except (JevError, KeyError, TypeError, ValueError) as exc:
                self.io.event("error", f"Fast step skipped: {exc}")
                return None
        op = choice.operation

        def sure(p: float, conf: float) -> bool:
            return p >= c.fast_path_min_probability and conf >= c.fast_path_min_confidence

        # DONE goes to the vision model, except a plain navigation task the classifier may finish
        # itself when every check passes (fast_path_done, see _classifier_done).
        if op == "DONE":
            return self._classifier_done(state, obs, choice)
        if op == "ESCALATE" or not sure(choice.probability, choice.confidence):
            return None
        element = None
        if op in heads:
            picked = choice.targets.get(op)
            if not picked or picked[0] not in heads[op] or not sure(picked[1], picked[2]):
                return None
            element = obs.elements[int(picked[0])]

        if op == "TAP":
            calls = [("tap_element", {"index": element.index, "target": element.label})]
        elif op == "TYPE_TEXT":
            text = self._fast_value(state, "text", {"field": fast.describe(element)})
            if text is None:
                return None
            calls = [("tap_element", {"index": element.index, "target": element.label}), ("type_text", {"text": text})]
        elif op == "LAUNCH_APP":
            app = self._direct_app(state, obs) if c.fast_path_direct_launch else None
            if app is None:
                app = self._fast_value(state, "app", {"foreground_app": obs.package or "unknown"})
            if app is None:
                return None
            calls = [("launch_app", {"app": app})]
        elif op in fast.SIMPLE_OPERATIONS:
            name, args = fast.SIMPLE_OPERATIONS[op]
            calls = [(name, dict(args))]
        else:
            return None

        # No loops: repeating the last action, or one tried twice lately, is the vision model's call.
        first = self._action_text(calls[0][0], calls[0][1], obs)
        recent = [a["action"] for a in state.actions[-4:]]
        if recent[-1:] == [first] or recent.count(first) >= 2:
            state.escalated = True
            return None

        taken = []
        for i, (name, args) in enumerate(calls):
            if i:  # typing after the tap that focused the field: let the keyboard come up first
                self._settle(c.settle_delay, state, quiet_ms=FOCUS_QUIET_MS)
            outcome = self._execute(name, args, obs, state, fast_step=True)
            state.fast_steps += 1
            shown = f"{name}: {'Typed ' + REDACTED + '.' if name == 'type_text' and not outcome.is_error else outcome.text}"
            self._event("error" if outcome.is_error else "action", shown,
                        {"timings": outcome.timings, "jev": True, "fast": True})
            taken.append((name, args, outcome))
            if outcome.is_error:
                break
        return taken

    def _direct_app(self, state: _TaskState, obs: Observation) -> str | None:
        """The package of the one app the request names exactly (an installed app's label or an alias,
        case, diacritics and spacing aside), so it opens without asking the text model. None when
        no name matches, or the names match several apps or only the app already in front."""
        found = {pkg for phrase in fast.app_phrases(state.task) if (pkg := self.resolver.exact(phrase))}
        found.discard(obs.package)
        return found.pop() if len(found) == 1 else None

    def _classifier_done(self, state: _TaskState, obs: Observation, choice: StepChoice) -> list | None:
        """Finish without the vision model's confirmation (fast_path_done), only when all of these hold;
        otherwise None (the vision step confirms, as always):
        (a) the classifier's DONE and its "request looks complete" both reach
            fast_path_done_min_probability, with its confidence at least fast_path_min_confidence;
        (b) the screen shows what the request asked to open: a title or selected tab named after it
            (or, for "open <app>", that app is in front);
        (c) the task only navigated: the request is made of navigation verbs and names no side effect,
            no tap was risky and nothing was typed;
        (d) nothing escalated to the vision model or looped."""
        c = self.config
        a = obs.assessment
        if not (c.fast_path_done and choice.probability >= c.fast_path_done_min_probability
                and choice.confidence >= c.fast_path_min_confidence
                and a is not None and a.task_done >= c.fast_path_done_min_probability):
            return None
        if state.escalated or state.risky or state.typed or any(x["screen_changed"] is False for x in state.actions):
            return None
        if not fast.navigation_only(state.task, c.confirm_keywords):
            return None
        target = fast.key_target(state.task)
        if not target:
            return None
        shown = fast.shows_target(obs.elements, target)  # sizes from the elements themselves (no stale frame)
        if shown is not None:
            summary = f"{shown.text or shown.desc} is open."
        elif obs.package and self.resolver.exact(target) == obs.package:
            summary = f"{self.resolver.display_name(obs.package)} is open."
        else:
            return None
        args = {"summary": summary, "success": True}
        outcome = self._execute("done", args, obs, state, fast_step=True, done_via="classifier")
        state.fast_steps += 1
        self._event("action", f"done: {outcome.text}", {"timings": outcome.timings, "jev": True, "fast": True})
        return [("done", args, outcome)]

    def _fast_value(self, state: _TaskState, key: str, context: dict) -> str | None:
        """A small, fast text model writes one value ({"text": …} or {"app": …}); None if it can't."""
        model = self.config.fast_path_text_model or self.config.resolver_model
        router = router_for(self.client, self.config)
        try:
            route = router.route(model)
            with self.timer.phase("model"):
                response = route.adapter.complete(ModelRequest(
                    model=model, system=fast.TEXT_SYSTEM, tools=[], max_tokens=200,
                    messages=[{"role": "user", "content": json.dumps({"user_request": state.task, "want": key, **context})}],
                ))
        except (ModelUnavailable, ModelError) as exc:
            self.io.event("error", f"Fast step skipped: {exc}")
            return None
        self.usage.add_model(response.usage, response.model or model)
        self._note_tokens(response.usage)
        return fast.parse_value("".join(response.text), key)

    def _after_fast_step(self, state: _TaskState, obs: Observation, taken: list[tuple[str, dict, Outcome]],
                         messages: list[dict]) -> Observation:
        """Settle, look again, and tell the main model what happened (as text in the conversation, so
        any provider takes it). The new screen follows when the main model next decides (_show_later)."""
        last = taken[-1][2]
        settle = self.config.settle_delay if last.settle is None else last.settle
        self._settle(settle, state, last.launched, last.quiet_ms)
        new = self._observe_after_action(state, obs)
        if any(o.is_error for _, _, o in taken) or state.actions[-1]["screen_changed"] is False:
            state.vision_next = True  # no visible progress: the vision model takes the next step
        lines = []
        for name, args, outcome in taken:
            shown = {**args, "text": REDACTED} if name == "type_text" else args
            text = f"Typed {REDACTED}." if name == "type_text" and not outcome.is_error else outcome.text
            lines.append(f"- {name} {json.dumps(shown)}: {'Error: ' if outcome.is_error else ''}{text}")
        note = ("Since your last turn, a quick helper took these steps from the element list "
                "(no reply needed; carry on from the new screen):\n" + "\n".join(lines))
        blocks = [{"type": "text", "text": note}]
        if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": "user", "content": blocks})
        self._show_later(state, messages, new)
        return new

    def _observe_after_action(self, state: _TaskState, before: Observation) -> Observation:
        """Observe after an action and note whether it changed the screen (the fast path's loop check).
        The note is made as soon as the elements are read, before the classifier sees recent actions."""
        pending = [action for action in state.actions if action["screen_changed"] is None]
        return self._observe_settled(state, before, pending)

    # --- settling after an action -------------------------------------------------

    def _variant(self, fast_path: bool = False) -> str:
        """Which latency features a task runs with, for comparing them in `openmob timings`:
        fixed | adaptive settle (+q<ms> for a quiet time other than 300 ms), then with the fast path
        +fast and its extras: +noshot (lazy screenshots), +1req (one classifier request per step),
        +launch (exact app names launch directly), +fastdone (the classifier may finish)."""
        c = self.config
        out = "adaptive" if c.adaptive_settle else "fixed"
        if c.adaptive_settle and c.settle_quiet_ms != DEFAULT_QUIET_MS:
            out += f"+q{c.settle_quiet_ms}"
        if fast_path:
            out += "+fast"
            for on, tag in ((c.fast_path_lazy_screenshot, "noshot"), (c.jev_one_request, "1req"),
                            (c.fast_path_direct_launch, "launch"), (c.fast_path_done, "fastdone")):
                out += f"+{tag}" if on else ""
        return out

    def _settle(self, seconds: float, state: _TaskState, launched: str | None = None, quiet_ms: int | None = None) -> None:
        """Let the screen catch up after an action. `seconds` is the most to wait (the fixed delay).

        Adaptive (config.adaptive_settle): a phone whose app offers wait_idle returns as soon as no
        window or content change has arrived for settle_quiet_ms. An adb phone ends a launch once the
        app is in front plus the short settle. Anything else (older apps, iOS): the fixed sleep.
        Recorded as settle time: the real time waited."""
        state.settle_idle = None
        if seconds <= 0:
            return
        with self.timer.phase("settle"):
            if self.config.adaptive_settle and not state.cancel.is_set():
                started = time.monotonic()
                wait_idle = getattr(self.driver, "wait_idle", None)
                if wait_idle is not None:
                    try:
                        result = wait_idle(int(quiet_ms or self.config.settle_quiet_ms), int(seconds * 1000))
                    except DeviceError:
                        result = None  # sleep out the rest; the next observation reports a lost phone
                    if result is not None:
                        state.settle_idle = bool(result.get("idle"))
                        return
                    seconds -= time.monotonic() - started
                wait_foreground = getattr(self.driver, "wait_foreground", None)
                if launched and wait_foreground is not None and seconds > 0:
                    if wait_foreground(launched, seconds, state.cancel):
                        left = seconds - (time.monotonic() - started)
                        self._sleep(max(min(self.config.settle_delay, left), 0), state.cancel)
                    return
            self._sleep(max(seconds, 0), state.cancel)
