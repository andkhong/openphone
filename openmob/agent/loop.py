"""The observe → decide → act loop that drives the phone with any model (see models.py)."""

from __future__ import annotations

import base64
import contextlib
import inspect
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import quote

from ..config import Config
from ..jev import JevError, JevJudge, ScreenAssessment, StepChoice, describe_screen
from ..memory import REMEMBER_INTENT, NoteError, render_app_notes, render_user_notes
from ..phone.adb import Screenshot, UIElement, downscale, format_elements
from ..phone.base import DeviceError
from ..phone.apps import NOT_LAUNCHABLE, AppResolver
from ..usage import Usage
from ..vision import ocr
from ..permissions import ALWAYS_DAYS, FOLLOWUP_SECONDS, TaskPermissions, category_of, trusted_for
from ..phone.approval_keys import action_digest
from . import fast, gate, permission_dialogs, waiting
from .models import ModelError, ModelRequest, ModelResponse, ModelUnavailable, router_for
from .timing import StepTimer
from .tools import PLAN_TOOL_SPEC, TOOL_SPECS, mode_notes, system_prompt

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
PLAY_STORE = "com.android.vending"
INSTALL_BUTTON = re.compile(r"install|update|get|install on (this )?(phone|device)", re.I)
INSTALL_ONLY = re.compile(r"install|install on (this )?(phone|device)", re.I)  # the tap that installs an app
INSTALL_REQUESTED = re.compile(r"\b(re)?install", re.I)  # a request that asks to install something
INSTALLED_BUTTON = "Open|Play"  # what the Play Store's button turns into once an install is done
INSTALL_TAP_GRACE_S = 5.0  # an enabled Install button still there after this long: the tap didn't start one
BLOCKED_APP_MESSAGE = "This app is blocked for API tasks on this phone (its owner can allow it)."
REFUSED = "Not allowed on this phone: {why} Don't try it another way; carry on without it or call done."
# Tools that change something on the phone (ask mode asks before each; plan mode needs the plan first).
CHANGING = {"tap_element", "tap", "long_press", "type_text", "swipe", "launch_app", "open_url", "press_key"}
# Tools after which the element list of the screen they acted on may be out of date.
MOVES_SCREEN = CHANGING | {"scroll"}
# ENTER in these apps sends what was typed.
SENDING_CATEGORIES = {"messaging", "email", "social"}
OPEN_CHECK_SECONDS = 2.0  # after open_url on a phone that can't resolve links first: how long to watch the app
CHANGED_DURING_APPROVAL = ("Didn't {what}: the screen changed while the owner was being asked, so the approval "
                           "doesn't cover what is there now. Look at the new screen.")
DECLINE_WORDS = ("no", "nope", "deny", "denied", "stop", "cancel", "reject")
# vision_brief: replies are mostly the tool call, so the step ends sooner (the thought still reaches
# the dashboard as a "thought" event).
BRIEF_REPLIES = ("\nReply briefly: at most one short sentence about what you see or why, then the tool call. "
                 "No lists, no restating the request.\n")
# Per-step model cost, recorded with each step (the steps table, `openmob timings`).
STEP_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "vision_image_side")
# A screen that can't be read yet (a dialog Android hides from accessibility, the phone's OCR still reading it): poll
# this long for it to become readable before the model looks at it or an action is reported as failed.
READABLE_WAIT_S = 1.5
READABLE_POLL_S = 0.15
# A refusal that means "the phone can't read what's there yet", not "no" (the gatekeeper's code).
UNREADABLE_CODES = {"gate_screen_unstable"}
DIALOG_TAP_REFUSED = ("Android doesn't let OpenMob press buttons in permission dialogs (the tap would be ignored), so "
                      "nothing was tapped. Press BACK (press_key BACK) to dismiss it; the app won't get the permission. "
                      "If the request needs the permission, tell the owner to allow it on the phone themselves.")
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
    prompts: int = 0  # approval prompts the owner saw (permission telemetry)
    mode: str | None = None  # the permission mode the task ran in


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
    until: str | None = None  # the settle after it waits for this too (launch_app, open_url: "loading_done")
    wait: dict | None = None  # a wait_until's condition and outcome, for the step record (wait_cond, wait_met…)


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
    settle_wait: dict | None = None  # the last settle's own wait (auto loading_done), for the step it delayed
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
    # Permission modes (openmob/permissions.py):
    perms: TaskPermissions = field(default_factory=TaskPermissions)
    task_app: str | None = None  # the app the task works in: the first it launched, else the first it acted in
    last_package: str | None = None  # the app of the last guarded action (a switch ends a payment follow-up)
    exempt_used: set[str] = field(default_factory=set)  # auto: requested words already used once
    money_followup: dict | None = None  # {package, at}: one follow-up commit tap after an approved payment
    plan: list[dict] | None = None  # plan mode: the approved steps ({text, app, used})
    step_approved: bool = False  # ask mode: the owner approved the action now running
    typing_approved: bool = False  # ask mode: the approved typing covers the ENTER that submits it
    prompts: int = 0
    acted_on: Observation | None = None  # the observation an action already changed (its elements may be stale)
    tapped_field: UIElement | None = None  # the text field the last tap landed on (typing goes there)
    app_in_front: str | None = None  # the last app seen in front (not a permission dialog): "the task's app"
    unreadable_seen: tuple | None = None  # the unreadable screen last waited on (each is waited on once)
    dialogs_given_up: set = field(default_factory=set)  # permission dialogs BACK didn't close: (package, message)


def is_yes(answer: str) -> bool:
    words = re.sub(r"[^a-z ]", " ", answer.lower()).split()
    return bool(words) and words[0] in YES_WORDS


class Agent:
    _tools: list[dict] = TOOL_SPECS  # this task's tools (plan mode adds propose_plan); set in run

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
        permissions: TaskPermissions | None = None,
    ) -> AgentResult:
        """`model` and `max_steps` override this phone's settings for one task; `blocked` names
        packages the task must not open or operate (the task stops if one comes to the front).
        `memory`: on | read_only | off. `isolated`: a caller other than the phone's chat (e.g. an
        API key) that must neither see nor add to the chat's recent requests. `permissions`: the
        task's permission mode against the phone's live settings (default: config's mode, ceiling auto)."""
        self.usage = Usage()
        self.timer.reset()
        self._step_tokens = self._shown_side = None
        self.task_model = model or self.model
        perms = permissions or TaskPermissions(self.config.default_permission_mode)
        if perms.requested == "never":  # no prompts: sensitive apps are off-limits for every task then
            blocked = self._with_sensitive(blocked)
        state = _TaskState(task=task, cancel=cancel or threading.Event(),
                           max_steps=max_steps or self.config.max_steps, blocked=blocked,
                           memory=memory if self.memory is not None else "off", perms=perms)
        state.fast = self._fast_enabled()
        self._task_variant = self._variant(state.fast)
        result = self._run(state, go_home, isolated)
        result.usage = self.usage
        result.timings = self.timer.task_totals()
        result.fast_steps = state.fast_steps
        result.prompts = state.prompts
        result.mode = perms.mode()
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

        mode = state.perms.mode()
        system = (system_prompt(self.platform) + mode_notes(mode) + (BRIEF_REPLIES if self.config.vision_brief else "")
                  + self._user_notes(state))
        self._tools = TOOL_SPECS + [PLAN_TOOL_SPEC] if mode == "plan" else TOOL_SPECS
        for step in range(1, state.max_steps + 1):
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step - 1)
            if self._is_blocked(state, obs.package):
                return AgentResult(False, BLOCKED_APP_MESSAGE, step - 1)
            if obs.package and not permission_dialogs.PACKAGES.search(obs.package):
                state.app_in_front = obs.package
            for _ in range(3):  # Android's permission dialogs: answered by rule, before any model sees them
                if not self._permission_dialog(state, obs, messages):
                    break
                self._settle(self.config.settle_delay, state)
                obs = self._observe_settled(state)
                self._show_later(state, messages, obs)
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step - 1)

            self._apply_corrections(state, messages)
            if state.fast and not state.vision_next and not self._needs_plan(state):
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
            until: str | None = None
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
                    until = outcome.until or until
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

            self._settle(settle, state, launched, quiet_ms, until)
            obs = self._observe_after_action(state, obs)
            # The new screen goes after the tool results, not inside them: Anthropic rejects images
            # inside a tool_result marked is_error, and OpenAI tool messages hold text only.
            messages.append({"role": "user", "content": results})
            self._show_later(state, messages, obs)

        return AgentResult(False, f"I gave up after {state.max_steps} steps.", state.max_steps)

    # --- the vision step's model call ---------------------------------------------

    def _vision_request(self, route, system: str, messages: list[dict], max_tokens: int) -> ModelRequest:
        return ModelRequest(model=self.task_model, system=system, tools=self._tools, messages=messages,
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
        if self._unreadable(obs.package, obs.elements):
            seen = waiting.signature(obs.package, obs.elements)
            if seen != state.unreadable_seen:  # once per screen: one that never gets readable costs one wait
                state.unreadable_seen = seen
                if self._await_readable(state):
                    obs = self._observe(state, before, pending)  # it became readable: look again
        else:
            state.unreadable_seen = None
        while self._is_loading(obs) and state.auto_waited < self.config.jev_auto_wait_seconds:
            if state.cancel.is_set():
                break
            self.io.event("status", "Loading screen; waiting…")
            # Until the screen changes (then the classifier looks again), at most LOADING_POLL_SECONDS.
            started = time.monotonic()
            with self.timer.phase("wait"):
                self._waiter(state).run(waiting.Condition("screen_changes", timeout_s=LOADING_POLL_SECONDS),
                                        baseline=(obs.package, obs.elements))
            state.auto_waited += max(time.monotonic() - started, 0.1)
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
                 done_via: str = "vision", source: str | None = None, note: str | None = None) -> Outcome:
        started = time.monotonic()
        try:
            with self.timer.phase("action"):
                try:
                    outcome = self._dispatch(name, args, obs, state)
                except DeviceError as exc:
                    if getattr(exc, "code", None) not in UNREADABLE_CODES:
                        raise
                    outcome = self._when_readable(name, args, obs, state, exc)
        except (DeviceError, KeyError, ValueError, TypeError, IndexError) as exc:
            outcome = Outcome(f"Error: {exc}", is_error=True)
        if note and not outcome.is_error:
            outcome.text = note
        if outcome.done:
            outcome.done_via = done_via
        outcome.timings = self.timer.take()
        outcome.tokens, self._step_tokens = self._step_tokens, None
        state.step_index += 1
        if name in MOVES_SCREEN and not outcome.is_error:
            state.acted_on = obs
        state.actions.append({"action": self._action_text(name, args, obs), "screen_changed": None})
        self._record_step(state, name, args, obs, outcome, time.monotonic() - started, fast_step, source)
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
                     fast_step: bool = False, source: str | None = None) -> None:
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
                **({"source": source} if source else {}),
                **(outcome.wait or state.settle_wait or {}),
            }
            record(row)
        except Exception as exc:  # the trajectory record must never break a task
            self.io.event("error", f"Step not recorded: {exc}")

    def _dispatch(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        state.step_approved = False
        # Refused in every mode, before anything asks: links outside the scheme allowlist, typing into
        # a password field, and dangerous URLs typed into an address bar.
        if name == "open_url" and (why := gate.url_refusal(gate.web_url(str(args.get("url", ""))))):
            return self._refuse(str(args.get("url", ""))[:80], why)
        if name == "type_text" and (refused := self._typing_refusal(str(args.get("text", "")),
                                                                    self._field(state, obs), obs)):
            return refused
        if name in CHANGING:
            if self._needs_plan(state) and not (name == "press_key" and args.get("key") != "ENTER"):
                return Outcome("Plan first: call propose_plan with a short numbered plan for the owner to approve "
                               "before you change anything on the phone.", is_error=True, settle=0)
            if state.perms.mode() == "ask":
                if blocked := self._step_gate(name, args, obs, state):
                    return blocked
            if not (name == "type_text" or (name == "press_key" and args.get("key") == "ENTER")):
                state.typing_approved = False
        if name == "propose_plan":
            return self._propose_plan(args, state)
        if name == "launch_app":
            return self._launch_app(args, obs, state)

        if name == "list_apps":
            # Fresh from the phone: an app installed or removed since the last look shows as it is now.
            self.resolver.packages(refresh=True)
            text = self.resolver.listing(args.get("query", ""), limit=15 if args.get("query") else None)
            return Outcome(text or "No apps found.", settle=0)

        if name == "open_url":
            url = gate.web_url(str(args["url"]))
            refused, resolved = self._check_url(url, state)
            if refused:
                return refused
            state.money_followup = None  # leaving the app ends a payment's follow-up
            self.driver.open_url(url)
            if not resolved and (backed := self._back_out_if_blocked(state, obs.package)):
                return backed
            return Outcome(f"Opened {url}.", settle=self.config.launch_settle_delay, until="loading_done")

        if name in ("tap_element", "tap", "long_press") and self._dialog_on(obs.package, obs.elements) is not None:
            # Android drops OpenMob's touches on permission dialogs (accessibility-data-sensitive): don't send one.
            return Outcome(DIALOG_TAP_REFUSED, is_error=True, settle=0)
        if name == "tap_element":
            index = int(args["index"])
            if not 0 <= index < len(obs.elements):
                return Outcome(f"No element [{index}] on the current screen.", is_error=True)
            element = obs.elements[index]
            if element.covered:
                return Outcome(f"Element [{index}] {element.label!r} is covered by another window (a dialog or the "
                               "keyboard), so a tap would land on that. Deal with what covers it first.",
                               is_error=True, settle=0)
            # Judged by the element's own fields (and whatever its center lands on), never by the
            # model's words for it; those can only add caution. The step's one classifier request asked
            # about the element by its label: when the model described it in other words, the check
            # asks again with those words included.
            hint = str(args.get("target", "")).strip()
            inside = gate.hit([e for e in obs.elements if e is not element and gate.within(e, element)],
                              *element.center)
            words = " ".join(w for w in (gate.element_words(element), inside.words) if w)
            shown = element.text or element.desc or inside.shown or gate.readable_id(element.resource_id)
            action = self._action("tap", obs.package, element)
            prompts = state.prompts
            if blocked := self._guard(words, state, obs, element, hint=hint, action=action,
                                      cached=hint in ("", element.label), unlabeled=not words,
                                      label=repr(shown) if shown else None,
                                      recheck=lambda: self._action("tap", self.driver.current_app(),
                                                                   self._same_element(element))):
                return blocked
            target = self._recheck_target(element)
            if isinstance(target, Outcome):
                return target
            if target is not element:
                # It settled elsewhere. _recheck_target returns only the very same element (same text,
                # description, resource id and class, the only one on screen, quiet and uncovered), so the
                # guard's verdict above carries over by construction. Two things don't:
                # - an approval asked above (or ask mode's step approval) signed the old place: its digest
                #   binds the bounds, so the moved tap isn't covered;
                # - should the identity ever differ, the new element is judged again by the guard.
                if state.prompts != prompts or state.step_approved:
                    return Outcome(CHANGED_DURING_APPROVAL.format(what=f"tap [{index}] {element.label!r}"),
                                   is_error=True, settle=0)
                if self._identity(target) != self._identity(element):
                    if blocked := self._guard(gate.element_words(target), state, obs, None, hint=hint,
                                              action=self._action("tap", obs.package, target),
                                              cached=False, unlabeled=not gate.element_words(target)):
                        return blocked
            self.driver.tap(*target.center)
            state.tapped_field = target if fast.is_text_field(target) or target.password else None
            text = f"Tapped [{index}] {element.label!r}."
            if obs.package == PLAY_STORE and INSTALL_BUTTON.fullmatch((element.text or element.desc).strip()):
                return self._wait_for_install(text, state)
            return Outcome(text)

        if name in ("tap", "long_press"):
            # A long press is judged exactly like a tap, whatever its duration.
            x, y = obs.shot.to_device(args["x"], args["y"])
            if blocked := self._guard_point(name, x, y, str(args.get("target", "")), state, obs):
                return blocked
            if name == "tap":
                self.driver.tap(x, y)
                return Outcome(f"Tapped {args.get('target') or 'point'} at ({args['x']},{args['y']}).")
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
            move = moves[args["direction"]]
            if blocked := self._guard_swipe("scroll", *move, 400, "", state, obs):
                return blocked
            self.driver.swipe(*move, 400)
            return Outcome(f"Scrolled {args['direction']}.")

        if name == "swipe":
            x1, y1 = obs.shot.to_device(args["x1"], args["y1"])
            x2, y2 = obs.shot.to_device(args["x2"], args["y2"])
            duration = int(args.get("duration_ms", 300))
            if blocked := self._guard_swipe("swipe", x1, y1, x2, y2, duration, str(args.get("target", "")), state, obs):
                return blocked
            self.driver.swipe(x1, y1, x2, y2, duration)
            return Outcome("Swiped.")

        if name == "type_text":
            text = str(args["text"])
            field = self._field(state, obs)
            if refused := self._typing_refusal(text, field, obs):
                return refused
            if args.get("submit") and (blocked := self._guard_enter(state, obs, field, typed=text)):
                return blocked
            state.typed = True
            self.driver.type_text(text)
            if args.get("submit"):
                self.driver.key("ENTER")
            # A search box: give its suggestions a moment longer to arrive before the next look.
            search = any(fast.is_search_field(el) for el in obs.elements)
            return Outcome(f"Typed {args['text']!r}.",
                           quiet_ms=self.config.settle_quiet_ms + SEARCH_EXTRA_QUIET_MS if search else None)

        if name == "press_key":
            key = str(args["key"]).upper()
            if key == "ENTER" and (blocked := self._guard_enter(state, obs, self._field(state, obs))):
                return blocked
            self.driver.key(args["key"])
            return Outcome(f"Pressed {args['key']}.")

        if name == "wait":
            seconds = min(max(float(args["seconds"]), 0.5), 30.0)
            self.timer.timed("settle", self._sleep, seconds, state.cancel)
            return Outcome(f"Waited {seconds:g}s.", settle=0,
                           wait={"wait_cond": "sleep", "wait_met": None, "wait_via": "sleep",
                                 "waited_ms": int(seconds * 1000)})

        if name == "wait_until":
            return self._wait_until(args, obs, state)

        if name == "remember":
            return self._remember(args, obs, state)

        if name == "ask_user":
            answer = self.timer.timed("wait", self.io.ask, args["question"]).strip()
            state.approved = is_yes(answer)
            return Outcome(f"User answered: {answer or '(no answer)'}", settle=0)

        if name == "done":
            return Outcome(args.get("summary", "Done."), done=True, success=bool(args.get("success", True)))

        return Outcome(f"Unknown tool {name}.", is_error=True)

    # --- opening apps ------------------------------------------------------------------------------

    def _launch_app(self, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        """launch_app. An app that isn't installed, or is only archived (Android 15 keeps an archived app listed
        with no launchable activity), is reported as not installed with the way to install it; with
        install_if_missing its Play Store page opens instead (the same checks as open_url)."""
        app = str(args["app"])
        install = bool(args.get("install_if_missing"))
        resolution = self.resolver.resolve(app)
        if resolution is None:
            self.resolver.packages(refresh=True)  # installed since the list was read?
            resolution = self.resolver.resolve(app)
        if resolution is None:
            if install:
                return self._open_store(f"market://search?q={quote(app)}&c=apps", obs, state,
                                        f"No installed app matches {app!r}, so I opened a Play Store search for it.")
            closest = ", ".join(f"{name} ({pkg})" for name, pkg in self.resolver.installed_apps(app)[:5])
            return Outcome(
                f"No installed app clearly matches {app!r}. Closest: {closest}. Retry launch_app with "
                "an exact package if one of these is right, or check list_apps. To install it, launch_app with "
                "install_if_missing. Otherwise press HOME, open the app drawer, and search there.",
                is_error=True,
                settle=0,
            )
        package = resolution.package
        if self._is_blocked(state, package):
            return Outcome(f"{BLOCKED_APP_MESSAGE} Don't try to open it another way; call done.", is_error=True, settle=0)
        state.money_followup = None  # an app switch ends a payment's follow-up
        try:
            self.driver.launch_app(package)
        except DeviceError as exc:
            if not NOT_LAUNCHABLE.search(str(exc)) or getattr(exc, "code", None):
                raise
            self.resolver.mark_archived(package)
            store = f"market://details?id={package}"
            if install:
                return self._open_store(store, obs, state, f"{package} isn't installed (archived), so I opened its "
                                                           "Play Store page.")
            return Outcome(f"{package} isn't installed (archived: Android removed it but keeps it listed). Install "
                           f"it: launch_app with install_if_missing, or open_url {store}.", is_error=True, settle=0)
        self.resolver.mark_archived(package, False)
        if state.task_app is None and not gate.is_launcher(package):
            state.task_app = package
        return Outcome(
            f"Launched {package} (matched by {resolution.method}).",
            settle=self.config.launch_settle_delay,
            launched=package,
            until="loading_done",
        )

    def _open_store(self, url: str, obs: Observation, state: _TaskState, text: str) -> Outcome:
        """launch_app's install_if_missing: the app's Play Store page, through open_url's checks. In Ask every step
        the owner approved opening the app, not the store: the model is told the link instead."""
        if state.perms.mode() == "ask":
            return Outcome(f"{text.split(',')[0]}. To install it, open_url {url}.", is_error=True, settle=0)
        refused, resolved = self._check_url(url, state)
        if refused:
            return refused
        self.driver.open_url(url)
        if not resolved and (backed := self._back_out_if_blocked(state, obs.package)):
            return backed
        return Outcome(text, settle=self.config.launch_settle_delay, until="loading_done")

    # --- waiting for something observable (agent/waiting.py) ---------------------------------------

    def _waiter(self, state: _TaskState) -> waiting.Waiter:
        """Reads only (no model calls): elements, the app in front, small frames, and OCR on a treeless
        screen when the agent has OCR. A wait stops on an app this task may not touch."""
        return waiting.Waiter(self.driver, state.cancel,
                              ocr=(lambda shot: self._ocr_elements(shot, 0)) if self.use_ocr else None,
                              blocked=lambda package: self._is_blocked(state, package))

    @staticmethod
    def _wait_record(cond: waiting.Condition, result: waiting.WaitResult, auto: bool = False) -> dict:
        return {"wait_cond": ("auto:" if auto else "") + cond.describe()[:120], "wait_met": result.met,
                "wait_via": result.via, "waited_ms": result.waited_ms}

    def _wait_until(self, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        """The wait_until tool: return as soon as the condition holds (or at its timeout). Its time is
        settle time; the step records the condition and the outcome."""
        try:
            cond = waiting.parse(args)
        except ValueError as exc:
            return Outcome(f"Error: {exc}", is_error=True, settle=0)
        if cond.until == "app_in_front" and "." not in cond.target:
            resolution = self.resolver.resolve(cond.target)  # an app's name: its package
            if resolution is None:
                return Outcome(f"No installed app clearly matches {cond.target!r}; give its package.",
                               is_error=True, settle=0)
            cond.target = resolution.package
        self.io.event("status", f"Waiting until {cond.describe()} (up to {cond.timeout_s:g}s)…")
        with self.timer.phase("settle"):
            result = self._waiter(state).run(cond, baseline=(obs.package, obs.elements),
                                             still_exit_s=waiting.STILL_EXIT_S)
        if result.stopped == "disconnected":
            raise DeviceError(result.seen)
        return Outcome(result.text(cond), settle=0, wait=self._wait_record(cond, result))

    def _wait_for_install(self, tapped: str, state: _TaskState) -> Outcome:
        """After an Install tap in the Play Store: wait (no model calls) until its button reads Open or
        Play again, enabled, up to install_wait_seconds. Stops early when the tap didn't start an install
        (Install is still there), a dialog or another app came up, or it's the screen of an app the phone
        can't read quickly (adb, iOS: the model waits with wait_until instead)."""
        if not (self.config.adaptive_settle and getattr(self.driver, "fast_ui_tree", False)) \
                or self.config.install_wait_seconds <= 0:
            return Outcome(tapped)
        cond = waiting.Condition("element_enabled", INSTALLED_BUTTON, waiting.compile_target(INSTALLED_BUTTON),
                                 float(self.config.install_wait_seconds))

        def interrupted(look, elapsed: float) -> str | None:
            if look.package and look.package != PLAY_STORE:
                return f"{look.package} came to the front"
            if any(el.covered for el in look.elements):
                return "a dialog opened"
            if elapsed >= INSTALL_TAP_GRACE_S and waiting.find_enabled(INSTALL_BUTTON, look.elements):
                return "the Install button is still there, so no install started"
            return None

        self.io.event("status", "Installing; waiting until it's done…")
        with self.timer.phase("settle"):
            result = self._waiter(state).run(cond, extra_stop=interrupted)
        if result.stopped == "cancelled":
            return Outcome(tapped, wait=self._wait_record(cond, result, auto=True))
        secs = result.waited_ms / 1000
        note = (f" Waited {secs:.1f}s for the install: {result.seen}." if result.met else
                f" Stopped waiting for the install after {secs:.1f}s: {result.seen}.")
        return Outcome(tapped + note, settle=0 if result.met else None,
                       wait=self._wait_record(cond, result, auto=True))

    # --- Android permission dialogs: dismissed with Back (agent/permission_dialogs.py) ----------------------

    def _dialog_on(self, package: str | None, elements: list[UIElement]):
        """The system runtime-permission dialog on screen (B1-safe recognition), or None."""
        return permission_dialogs.detect(package, elements, gatekeeper=bool(getattr(self.driver, "gatekeeper", False)))

    def _permission_dialog(self, state: _TaskState, obs: Observation, messages: list[dict]) -> bool:
        """A runtime permission dialog on screen is dismissed with BACK, with no model call. Android 14+ marks these
        dialogs accessibility-data-sensitive and drops touches injected by an accessibility service that isn't an
        accessibility tool, so their buttons can't be pressed (a real Samsung ignored 11 taps over 24 s); BACK closes
        one at once. The app doesn't get the permission. The step is recorded with source "auto_dialog", and the
        model is told. If BACK twice doesn't close it, the loop stops handling that dialog and tells the model.
        True: one was dismissed (the caller looks again)."""
        if not getattr(self.config, "auto_permission_dialogs", True):
            return False
        dialog = self._dialog_on(obs.package, obs.elements)
        if dialog is None:
            return False
        key = (obs.package or "", dialog.message)
        if key in state.dialogs_given_up:
            return False
        if state.task_app is None and state.app_in_front and not gate.is_launcher(state.app_in_front):
            state.task_app = state.app_in_front  # the dialog is not the task's app
        app = dialog.app or (self._app_label(state.app_in_front) if state.app_in_front else "The app")
        what = permission_dialogs.WORDS.get(dialog.kind, "data")
        request = (f"{app}'s {what} request" if dialog.kind == "notifications"
                   else f"{app}'s request to use your {what}")
        text = (f"Dismissed {request} with Back (Android doesn't let OpenMob press permission buttons; the app didn't "
                "get the permission).")
        self.io.event("status", f"Permission dialog ({dialog.kind}, {app}): dismissed with Back.")
        closed = False
        for attempt in range(2):
            outcome = self._execute("press_key", {"key": "BACK"}, obs, state, fast_step=True, source="auto_dialog",
                                    note=text if attempt == 0 else f"{text} (Back again: the first didn't close it.)")
            state.fast_steps += 1
            if outcome.is_error:
                break
            if self._dialog_closed(state, key):
                closed = True
                break
        note = f"(The phone: {text}"
        if permission_dialogs.request_wants_permission(state.task):
            note += (" The request asks to allow a permission, but Android requires the owner to tap Allow "
                     "themselves: say so in your summary.")
        if not closed:
            state.dialogs_given_up.add(key)
            note += (" Back didn't close the dialog, so it's still up and OpenMob can't answer it: tell the owner to "
                     "answer it on the phone.")
        self._note_for_model(messages, note + ")")
        return True

    def _dialog_closed(self, state: _TaskState, key: tuple) -> bool:
        """After BACK: poll (no model calls) up to READABLE_WAIT_S for that dialog to be gone."""
        deadline = time.monotonic() + READABLE_WAIT_S
        while True:
            try:
                package, elements = self.driver.current_app(), self.driver.ui_elements()
            except DeviceError:
                return False
            dialog = self._dialog_on(package, elements)
            if dialog is None or (package or "", dialog.message) != key:
                return True
            if time.monotonic() >= deadline or state.cancel.is_set():
                return False
            with self.timer.phase("settle"):
                state.cancel.wait(READABLE_POLL_S)

    @staticmethod
    def _note_for_model(messages: list[dict], text: str) -> None:
        last = messages[-1]
        if last["role"] != "user":
            messages.append({"role": "user", "content": []})
            last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": text})

    # --- a screen that can't be read yet -------------------------------------------------------------

    @staticmethod
    def _unreadable(package: str | None, elements: list[UIElement]) -> bool:
        """Something on top that can't be read yet: an unidentified window (no package) or a permission
        controller with nothing labeled, or a screen whose every labeled element is covered by a higher window."""
        labeled = [e for e in elements if e.text or e.desc]
        if not labeled:
            return package is None or bool(permission_dialogs.PACKAGES.search(package))
        return all(e.covered for e in labeled)

    def _await_readable(self, state: _TaskState, budget: float = READABLE_WAIT_S) -> bool:
        """Poll (no model calls) up to `budget` for an unreadable screen to become readable: the phone's OCR of a
        hidden dialog takes 0.1-0.5 s. Only on phones whose element list is quick to read. True: readable now."""
        if not getattr(self.driver, "fast_ui_tree", False):
            return False
        deadline = time.monotonic() + budget
        with self.timer.phase("settle"):
            while time.monotonic() < deadline and not state.cancel.is_set():
                state.cancel.wait(READABLE_POLL_S)
                try:
                    if not self._unreadable(self.driver.current_app(), self.driver.ui_elements()):
                        return True
                except DeviceError:
                    return False
        return False

    def _when_readable(self, name: str, args: dict, obs: Observation, state: _TaskState, exc: DeviceError) -> Outcome:
        """The phone couldn't read where the action lands yet ("The phone can't see what's there"): wait briefly
        for the screen to become readable, then run the action once more if the screen is still the one it was
        decided on. If it changed (a dialog opened over it), nothing is done and the next look shows why (a
        permission dialog is then answered by rule)."""
        readable = self._await_readable(state)
        if not readable or state.perms.mode() == "ask":
            return Outcome(f"Error: {exc}", is_error=True, settle=0)
        try:
            package, elements = self.driver.current_app(), self.driver.ui_elements()
        except DeviceError:
            return Outcome(f"Error: {exc}", is_error=True, settle=0)
        same = (waiting.signature(package, elements) == waiting.signature(obs.package, obs.elements)
                and sum(e.covered for e in elements) == sum(e.covered for e in obs.elements))
        if not same:
            return Outcome("Didn't do it: the screen changed while the phone couldn't read it (a dialog may have "
                           "opened). Look at the new screen.", is_error=True, settle=0)
        self.io.event("status", "The screen is readable now; trying that once more.")
        return self._dispatch(name, args, obs, state)

    def _keyword_hits(self, text: str) -> list[str]:
        target = text.lower()
        return [k for k in self.config.confirm_keywords if re.search(rf"\b{re.escape(k.lower())}\b", target)]

    def _guard(self, description: str, state: _TaskState, obs: Observation, element: UIElement | None = None, *,
               hint: str = "", action: dict | None = None, cached: bool = True, unlabeled: bool = False,
               force: bool = False, money_forced: bool = False, verb: str = "tap", kind_hint: str | None = None,
               recheck: Callable[[], dict | None] | None = None, label: str | None = None) -> Outcome | None:
        """Before an action that may commit something: refuse it, ask the owner, or let it run
        (docs/safety.md#permission-modes and #what-is-guarded).

        `description` is what the action really touches: the target element's own text, description
        and resource id (never the model's words for it). `hint` is the model's description, which can
        only add caution: its risky words make the action risky, but it never makes anything "requested",
        "covered" or an everyday Setting. `unlabeled`: nothing at the target says what it is, which asks
        in shopping, payment, food, travel and ride apps (`force` and `money_forced` ask likewise, the
        latter as money). `action` (tool, app, target identity, typed text) is what an approval binds:
        its digest goes with the prompt, the phone signs it, and after a yes `recheck` must still
        describe the same action, or the screen changed while the owner was asked.

        Refused in every mode: security-critical Settings pages (and a blocked app's info page); under
        `never` also money (unless the owner allowed purchases with it: never_money) and autofill/credential
        suggestions.
        Risky: a confirm or money keyword (minus on-the-way phrases like "Add to cart"), an autofill
        suggestion, a Settings change outside the everyday allowlist, or the classifier's risk.
        Requested: Jev judges that the request explicitly asked for it, or (without Jev) the matched
        keyword appears in the request; the classifier may veto "requested", never grant it. Whether a
        risky action asks then depends on the mode (see _covered). For a tap on `element` (`cached`),
        the step's one classifier request may already hold its risk (and, when it can matter, its
        "requested")."""
        cfg = self.config
        mode = state.perms.mode()
        desc = " ".join(description.split())
        hint = " ".join((hint or "").split())
        extra = hint if hint and hint.lower() not in desc.lower() else ""
        caution = f"{desc} {extra}".strip()
        package = obs.package
        if package and package != state.last_package:
            state.money_followup = None  # an app switch ends a payment's follow-up
            state.last_package = package
        if state.task_app is None and package:
            state.task_app = package
        # A Play Store Install tap installs an app (design §5): it asks, unless the request asked to install.
        shown_label = " ".join(((element.text or element.desc) if element is not None else desc).split())
        if package == PLAY_STORE and INSTALL_ONLY.fullmatch(shown_label) and not INSTALL_REQUESTED.search(state.task):
            force, kind_hint = True, kind_hint or "install"
        tier = gate.settings_tier(cfg, package, desc, element, obs.elements, self._blocked_label(state))
        if extra:  # the model's words can make a Settings tap refused or asked, never everyday
            hint_tier = gate.settings_tier(cfg, package, extra, None, obs.elements, self._blocked_label(state))
            if hint_tier == "refuse" or (hint_tier == "ask" and tier != "refuse"):
                tier = hint_tier
        shown = desc or (f"an unlabeled spot the agent calls {hint!r}" if hint else "an unlabeled spot")
        if tier == "refuse":
            return self._refuse(shown, "this Settings page is security-critical, so agents can't change it.")
        credential = gate.is_credential(cfg, caution)
        if credential and mode == "never":
            return self._refuse(shown, "autofill and saved-credential suggestions are off while agents act "
                                       "without asking.")
        real_hits = self._keyword_hits(gate.strip_skips(desc, cfg.confirm_skip_phrases))
        hint_hits = self._keyword_hits(gate.strip_skips(extra, cfg.confirm_skip_phrases)) if extra else []
        hits = real_hits + [h for h in hint_hits if h not in real_hits]
        money = gate.words(gate.strip_skips(caution, cfg.confirm_skip_phrases), cfg.money_keywords)
        unlabeled_money = unlabeled and gate.is_money_app(package)
        if money_forced or (unlabeled_money and gate.payment_context(obs.elements)):
            money = money or ["payment"]
        risky = bool(hits or money or credential or force or unlabeled_money)
        if tier == "allow":
            # Everyday Settings (Wi-Fi, Bluetooth, dark mode…) never ask, unless the model's own words
            # for the target are risky.
            risky = bool(hint_hits or force)
        elif tier == "ask":
            risky = True
        # "Requested" comes from the target's own words. The model's words count only when they are all
        # that makes the action risky (then trusting them is no looser than ignoring them).
        basis = real_hits or ([] if force or unlabeled_money or money_forced or tier == "ask" else hint_hits)
        requested = bool(basis) and any(re.search(rf"\b{re.escape(k.lower())}\b", state.task.lower())
                                        for k in basis)
        if self.judge:
            try:
                known = cached and element is not None and element.index in obs.risks and (
                    not requested or element.index in obs.requested)
                if known:
                    risk, asked = obs.risks[element.index], obs.requested.get(element.index, 0.0)
                else:
                    risk, asked = self.timer.timed("jev", self.judge.assess_tap, state.task, caution or shown,
                                                   self._describe(obs))
                risky = risky or risk >= cfg.jev_risk_threshold
                # The classifier only ever adds caution. It reads on-screen text, so a screen could
                # talk it into "the user asked for this"; it may veto "requested", never grant it.
                requested = requested and asked >= 0.5
            except JevError as exc:
                self.io.event("error", str(exc))
        if risky:
            state.risky = True
        kind = "settings" if tier == "ask" else (gate.kind_of(hits, money, credential) or kind_hint)
        share = gate.is_share(hits)
        settings = state.perms.current_settings()
        if mode == "never" or cfg.confirm_actions == "never":
            # Purchases run unasked only when the owner turned on "Also allow purchases" with Don't ask
            # (signed on the phone, ending with that window). A per-app pay entry doesn't count here.
            if money and not settings.get("never_money"):
                return self._refuse(shown, "purchases are off while agents act without asking (the owner can "
                                           "turn on \"Also allow purchases\" with Don't ask, on the phone).")
            return None
        if mode == "ask" and state.step_approved:
            return None  # the owner just approved this very action
        if not risky:
            return None
        if self._covered(state, mode, desc, hits, money, kind, share, requested, package, settings):
            return None
        if state.approved:
            state.approved = False
            return None
        context = gate.money_context(obs.elements) if money else ""
        where = f" in {self._app_label(package)}" if package and not gate.is_launcher(package) else ""
        what = label if label is not None else (repr(desc) if desc else shown)
        question = f"I'm about to {verb} {what}{where}{f' ({context})' if context else ''}. Should I go ahead?"
        details = {"kind": "action", "action_kind": kind, "package": package}
        digest = None
        if action is not None:
            digest = action_digest(action)
            details.update(action=action, digest=digest)
        always = self._always_offer(mode, kind, package, obs, desc if share else None)
        if always:
            details["always"] = always
        answer = self._ask_owner(state, question, details)
        if is_yes(answer):
            if refused := self._approval_mismatch(digest, recheck, f"{verb} {what}"):
                return refused
            if money and not gate.words(desc, cfg.money_terminal_words):
                state.money_followup = {"package": package, "at": time.monotonic()}  # a terminal tap mints nothing
            return None
        return Outcome(
            f"The user did not approve this ({verb} {what}; they said {answer!r}). "
            "Don't do it. Pick another approach or call done.",
            is_error=True,
            settle=0,
        )

    def _approval_mismatch(self, digest: str | None, recheck: Callable[[], dict | None] | None,
                           what: str) -> Outcome | None:
        """After a yes: an approval signed on the phone names the action it approved (its digest). It
        must be the action asked about, and, re-read now, the screen must still show that action."""
        take = getattr(self.io, "take_approval_digest", None)
        approved = take() if callable(take) else None
        if approved is None:
            return None  # approved without a phone signature (adb, local or an owner's API key): as before
        if digest is None or approved != digest:
            return Outcome(f"Didn't {what}: the approval was for a different action. Ask again if it's still "
                           "needed.", is_error=True, settle=0)
        if recheck is None or not getattr(self.driver, "fast_ui_tree", False):
            return None
        try:
            with self.timer.phase("observe"):
                now = recheck()
        except DeviceError:
            now = None
        if now is None or action_digest(now) != digest:
            return Outcome(CHANGED_DURING_APPROVAL.format(what=what), is_error=True, settle=0)
        return None

    # --- what an action really touches ------------------------------------------------

    @staticmethod
    def _action(tool: str, package: str | None, element: UIElement | None = None,
                point: tuple[int, int] | None = None, typed: str = "") -> dict:
        """The action an approval binds (approval_keys.action_digest): the tool, the app, the target
        element's identity (or the point, with nothing there) and any typed text, URL or key."""
        bounds = list(element.bounds) if element is not None else (list(point) if point else "")
        return {"tool": tool, "package": package or "", "text": element.text if element else "",
                "desc": element.desc if element else "", "resource_id": element.resource_id if element else "",
                "bounds": bounds, "input": typed}

    def _same_element(self, element: UIElement) -> UIElement | None:
        """The element, re-read: the one with the same identity on the screen now, or None."""
        same = (element.text, element.desc, element.resource_id, element.cls, element.bounds)
        return next((e for e in self.driver.ui_elements()
                     if (e.text, e.desc, e.resource_id, e.cls, e.bounds) == same), None)

    def _elements(self, state: _TaskState, obs: Observation, fresh: bool = False) -> list[UIElement]:
        """The screen's elements: the observation's, unless an earlier action this step changed the
        screen (or `fresh`), then re-read."""
        if fresh or state.acted_on is obs:
            with self.timer.phase("observe"):
                return self.driver.ui_elements()
        return obs.elements

    def _hit(self, state: _TaskState, obs: Observation, x: int, y: int) -> gate.Hit:
        """What a touch at device (x, y) lands on (gate.hit), re-reading the screen when nothing is
        found in a list that may be out of date."""
        found = gate.hit(self._elements(state, obs), x, y)
        if found.element is None and obs.elements and state.acted_on is not obs:
            found = gate.hit(self._elements(state, obs, fresh=True), x, y)
        return found

    def _guard_point(self, tool: str, x: int, y: int, hint: str, state: _TaskState, obs: Observation,
                     verb: str | None = None) -> Outcome | None:
        """A tap (or long press, or a swipe that is really a tap) at device (x, y): judged by what is
        actually there, the model's words for it only adding caution."""
        element, words, is_labeled, shown = self._hit(state, obs, x, y)
        action = self._action(tool, obs.package, element, point=(x, y))

        def recheck() -> dict:
            now = gate.hit(self.driver.ui_elements(), x, y).element
            return self._action(tool, self.driver.current_app(), now, point=(x, y))

        verb = verb or ("long-press" if tool == "long_press" else "tap")
        blocked = self._guard(words, state, obs, element, hint=hint, action=action, unlabeled=not is_labeled,
                              force=gate.is_slider(element, words), verb=verb, recheck=recheck,
                              label=repr(shown) if shown else None)
        if blocked is None and tool in ("tap", "long_press"):
            state.tapped_field = element if element is not None and (
                fast.is_text_field(element) or element.password) else None
        return blocked

    def _guard_swipe(self, tool: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int, hint: str,
                     state: _TaskState, obs: Observation) -> Outcome | None:
        """A swipe that Android delivers as a tap is judged as a tap at its start. Any other swipe is
        judged at each end that is a commit control: a slider ("slide to pay") or a risky label. (A
        scroll is judged on the list it was decided from, without re-reading: it never acts as a tap.)"""
        elements = obs.elements if tool == "scroll" else self._elements(state, obs)
        if gate.swipe_is_tap(elements, x1, y1, x2, y2, duration_ms):
            return self._guard_point(tool, x1, y1, hint, state, obs, verb="tap (by a short swipe)")
        seen = []
        for x, y in ((x1, y1), (x2, y2)):
            element, words, _, shown = gate.hit(elements, x, y)
            if element is None or element in seen:
                continue
            seen.append(element)
            commit = self._keyword_hits(gate.strip_skips(words, self.config.confirm_skip_phrases)) or gate.words(
                gate.strip_skips(words, self.config.confirm_skip_phrases), self.config.money_keywords)
            if not (commit or gate.is_slider(element, words)):
                continue
            action = self._action(tool, obs.package, element, typed=f"{x1},{y1}->{x2},{y2}")
            if blocked := self._guard(words, state, obs, element, hint=hint, action=action,
                                      force=gate.is_slider(element, words), verb="swipe",
                                      label=repr(shown) if shown else None,
                                      recheck=lambda e=element: self._action(tool, self.driver.current_app(),
                                                                             self._same_element(e),
                                                                             typed=f"{x1},{y1}->{x2},{y2}")):
                return blocked
        return None

    def _field(self, state: _TaskState, obs: Observation) -> UIElement | None:
        """The text field typing and ENTER go to, as far as we can tell (None: unknown)."""
        if state.acted_on is obs:  # e.g. the tap that focused it, this same step
            return state.tapped_field
        return gate.focused_field(obs.elements) or state.tapped_field

    def _typing_refusal(self, text: str, field: UIElement | None, obs: Observation) -> Outcome | None:
        """Never typed, in any mode: into a password field, or a javascript:/intent:/file:/content:/data:
        URL into an address bar (or anywhere in a browser, when the field isn't known)."""
        if field is not None and field.password:
            return self._refuse("a password field", "agents never type passwords; the owner types them.")
        scheme = gate.dangerous_text(text)
        if scheme and (gate.is_url_bar(field, obs.package) or (
                field is None and gate.is_browser(obs.package, obs.elements))):
            return self._refuse(f"{scheme}: in the address bar", f"{scheme}: links can run code or reach into "
                                                                 "apps, so they're never typed.")
        return None

    def _guard_enter(self, state: _TaskState, obs: Observation, field: UIElement | None,
                     typed: str | None = None) -> Outcome | None:
        """ENTER (press_key, or type_text's submit) commits what the field holds:
        - in a browser's address bar it opens a URL: the open_url checks run on it;
        - in messaging, email and social apps it sends: judged as a "send";
        - in a browser or WebView form, or on a payment screen, it submits: it asks per the mode
          (as money when money words are on screen). Search boxes never ask."""
        cfg = self.config
        package = obs.package
        elements = obs.elements
        browser = gate.is_browser(package, elements)
        editables = [e for e in elements if fast.is_text_field(e)]
        if field is None and browser and editables and all(
                gate.is_url_bar(e, package) or gate.is_search_field(e) for e in editables):
            field = next((e for e in editables if gate.is_url_bar(e, package)), None)  # address bar or search
            if field is None:
                return None
        if gate.is_url_bar(field, package) or (field is None and browser and typed and gate.looks_like_url(typed)):
            url = typed if typed is not None else (field.text if field is not None else "")
            if not gate.looks_like_url(url):
                return None  # a search from the address bar
            refused, _ = self._check_url(gate.web_url(url), state)
            return refused
        typed_input = "" if typed is None else typed
        action = self._action("enter", package, field, typed=typed_input)
        if category_of(package, cfg.app_categories) & SENDING_CATEGORIES:
            return self._guard("send", state, obs, field, action=action, verb="press Enter to send",
                               kind_hint="send", label="what was typed")
        if gate.is_search_field(field):
            return None
        money = gate.money_screen(cfg, elements)
        if browser or money or gate.payment_context(elements):
            words = gate.element_words(field)
            return self._guard(words, state, obs, field, action=action, force=True, money_forced=money,
                               verb="press Enter to submit", kind_hint=None,
                               label=f"the form ({words!r})" if words else "the form")
        return None

    def _check_url(self, url: str, state: _TaskState) -> tuple[Outcome | None, bool]:
        """open_url's checks, also run on a URL typed into an address bar: the scheme allowlist, then
        the app that will handle it, resolved on the phone before opening (newer apps, adb), against
        the task's blocked apps. (refusal or None, whether the handler was resolved)."""
        if why := gate.url_refusal(url):
            return self._refuse(url[:80], why), False
        resolve = getattr(self.driver, "resolve_url", None)
        target = None
        if callable(resolve):
            try:
                target = resolve(url)
            except DeviceError:
                target = None
        if target is None:
            return None, False
        handlers = [target["package"]] if target.get("package") else list(target.get("candidates") or [])
        if any(self._is_blocked(state, p) for p in handlers):
            return Outcome(f"{BLOCKED_APP_MESSAGE} That link opens in it. Don't try to open it another way; "
                           "call done.", is_error=True, settle=0), True
        return None, True

    def _back_out_if_blocked(self, state: _TaskState, before: str | None) -> Outcome | None:
        """A phone that can't resolve links first: watch which app the link opened, and back out of a
        blocked one at once (BACK, then HOME)."""
        if not state.blocked:
            return None
        deadline = time.monotonic() + OPEN_CHECK_SECONDS
        package = self.driver.current_app()
        while package == before and time.monotonic() < deadline and not state.cancel.is_set():
            self._sleep(0.2, state.cancel)
            package = self.driver.current_app()
        if not self._is_blocked(state, package):
            return None
        self.driver.key("BACK")
        if self._is_blocked(state, self.driver.current_app()):
            self.driver.key("HOME")
        return Outcome(f"{BLOCKED_APP_MESSAGE} The link opened it, so I went back. Don't try to open it another "
                       "way; call done.", is_error=True, settle=0)

    def _covered(self, state: _TaskState, mode: str, desc: str, hits: list[str], money: list[str], kind: str | None,
                 share: bool, requested: bool, package: str | None, settings: dict) -> bool:
        """A risky action that runs without asking in this mode:
        - money: the one follow-up commit tap after an approved payment (terminal words only, same app,
          within FOLLOWUP_SECONDS, no app switch since), or a trusted pay entry for this app (auto, trusted);
        - a live trusted entry for this app and kind (auto, trusted; category entries in trusted only);
        - plan: an unused approved plan step naming the action (its risky words) in this app;
        - auto/trusted: a requested action, once per word, in the task's own app (never money)."""
        cfg = self.config
        if money:
            follow = state.money_followup
            if (follow and follow["package"] == package and time.monotonic() - follow["at"] <= FOLLOWUP_SECONDS
                    and gate.words(desc, cfg.money_terminal_words) and not gate.words(desc, cfg.money_start_words)):
                state.money_followup = None
                return True
            return mode in ("auto", "trusted") and trusted_for(settings, package, "pay", cfg.app_categories, False)
        if mode in ("auto", "trusted") and kind and kind != "settings" and trusted_for(
                settings, package, kind, cfg.app_categories, mode == "trusted", target=desc if share else None):
            return True
        if mode == "plan":
            return self._plan_covers(state, hits, package)
        if mode in ("auto", "trusted") and cfg.confirm_actions == "unrequested" and requested and kind != "settings":
            fresh = [h for h in hits if h not in state.exempt_used]
            if fresh and package == state.task_app:
                state.exempt_used.update(hits)
                return True
        return False

    def _always_offer(self, mode: str, kind: str | None, package: str | None, obs: Observation,
                      target: str | None) -> dict | None:
        """The "Always allow this" option a prompt may carry: none for Settings (no page identity), for
        autofill in browsers or WebViews, in ask mode, or without a kind and an app."""
        if mode not in ("auto", "trusted", "plan") or kind not in ALWAYS_DAYS or not package:
            return None
        if gate.is_launcher(package) or (kind == "fill" and gate.is_browser(package, obs.elements)):
            return None
        days = ALWAYS_DAYS[kind]
        offer = {"app": package, "kind": kind, "days": days,
                 "text": gate.grant_text(kind, self._app_label(package), days, target),
                 "confirm_twice": kind == "pay"}
        if target:
            offer["target"] = " ".join(target.split()).lower()[:80]
        return offer

    def _ask_owner(self, state: _TaskState, question: str, details: dict) -> str:
        """One approval prompt (the owner's screen lock, on an app phone). Counts it, and records its
        decision and how long the owner took, for the prompt budget (`openmob prompts`)."""
        confirm = getattr(self.io, "confirm", None)
        started = time.monotonic()
        if confirm is None:
            answer = self.timer.timed("wait", self.io.ask, question)
        else:
            try:
                takes_details = "details" in inspect.signature(confirm).parameters
            except (TypeError, ValueError):
                takes_details = False
            answer = (self.timer.timed("wait", confirm, question, details) if takes_details
                      else self.timer.timed("wait", confirm, question))
        state.prompts += 1
        low = answer.strip().lower()
        decision = ("always" if is_yes(answer) and "always" in low else "approved" if is_yes(answer)
                    else "denied" if not low or low.split()[0] in DECLINE_WORDS else "changes")
        record = getattr(self.io, "record_prompt", None)
        if record is not None:
            try:
                record({"t": time.time(), "kind": details.get("kind", "action"),
                        "action_kind": details.get("action_kind"), "mode": state.perms.mode(),
                        "decision": decision, "latency_ms": int((time.monotonic() - started) * 1000),
                        "package": details.get("package")})
            except Exception as exc:  # telemetry must never break a task
                self.io.event("error", f"Prompt not recorded: {exc}")
        return answer

    def _refuse(self, desc: str, why: str) -> Outcome:
        self.io.event("status", f"Refused {desc!r}: {why}")
        return Outcome(REFUSED.format(why=why[0].upper() + why[1:]), is_error=True, settle=0)

    def _blocked_label(self, state: _TaskState):
        """A Settings app-info page names its app by label: is that an app this task may not touch?"""
        if not state.blocked:
            return None

        def check(text: str) -> bool:
            try:
                package = self.resolver.exact(text)
            except Exception:
                return False
            return bool(package and state.blocked(package))
        return check

    def _app_label(self, package: str | None) -> str:
        if not package:
            return "this app"
        try:
            return self.resolver.display_name(package) or package
        except Exception:
            return package

    def _with_sensitive(self, blocked):
        patterns = [re.compile(p, re.IGNORECASE) for p in self.config.sensitive_apps]
        return lambda package: any(p.search(package) for p in patterns) or bool(blocked and blocked(package))

    # --- ask every step / plan first ------------------------------------------------

    def _step_gate(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome | None:
        """Ask mode: the owner approves every changing action. Not asked: tapping a text field (it only
        focuses it), a long swipe (a scroll), keys other than ENTER, and the ENTER that submits text the
        owner just approved (typing and its submit are one prompt)."""
        what = None
        action = None
        if name in ("tap_element", "tap", "long_press"):
            element = None
            if name == "tap_element" and str(args.get("index", "")).lstrip("-").isdigit():
                i = int(args["index"])
                element = obs.elements[i] if 0 <= i < len(obs.elements) else None
                if element is not None and fast.is_text_field(element):
                    return None
            elif name != "tap_element" and all(isinstance(args.get(k), (int, float)) for k in ("x", "y")):
                element = self._hit(state, obs, *obs.shot.to_device(args["x"], args["y"])).element  # what's there
            label = gate.element_words(element) or args.get("target") or "that point"
            what = f"{'long-press' if name == 'long_press' else 'tap'} {label.strip()!r}"
            action = self._action(name, obs.package, element)
        elif name == "type_text":
            text = str(args.get("text", ""))
            shown = text if len(text) <= 80 else text[:79] + "…"
            what = f"type {shown!r}" + (" and press Enter" if args.get("submit") else "")
            action = self._action("type_text", obs.package, self._field(state, obs), typed=text)
        elif name == "press_key":
            if args.get("key") != "ENTER":
                return None
            if state.typing_approved:
                state.typing_approved = False
                state.step_approved = True  # the owner approved typing this text and submitting it
                return None
            what = "press Enter"
            action = self._action("enter", obs.package, self._field(state, obs))
        elif name == "swipe":
            frame = obs.geometry()
            w, h = (frame.width, frame.height) if frame else (1080, 2400)
            if not gate.short_swipe(args, w, h):
                try:
                    x1, y1 = obs.shot.to_device(args["x1"], args["y1"])
                    x2, y2 = obs.shot.to_device(args["x2"], args["y2"])
                    tap_like = gate.swipe_is_tap(obs.elements, x1, y1, x2, y2, int(args.get("duration_ms", 300)))
                except (KeyError, TypeError, ValueError):
                    tap_like = False
                if not tap_like:
                    return None
            what = f"tap {args.get('target') or 'by swiping'}"
            action = self._action("swipe", obs.package, typed=str(args.get("target") or ""))
        elif name == "launch_app":
            what = f"open {args.get('app')!r}"
            action = self._action("launch_app", obs.package, typed=str(args.get("app")))
        elif name == "open_url":
            what = f"open {args.get('url')!r}"
            action = self._action("open_url", obs.package, typed=str(args.get("url")))
        if what is None:
            return None
        digest = action_digest(action) if action is not None else None
        answer = self._ask_owner(state, f"Next step: {what}. Should I go ahead?",
                                 {"kind": "step", "package": obs.package,
                                  **({"action": action, "digest": digest} if action is not None else {})})
        if not is_yes(answer):
            return Outcome(f"The user did not approve this step ({what}; they said {answer!r}). Don't do it. "
                           "Pick another approach or call done.", is_error=True, settle=0)
        if refused := self._approval_mismatch(digest, None, what):
            return refused
        state.step_approved = True
        state.typing_approved = name == "type_text"
        return None

    def _needs_plan(self, state: _TaskState) -> bool:
        return state.plan is None and state.perms.mode() == "plan"

    def _propose_plan(self, args: dict, state: _TaskState) -> Outcome:
        """Plan mode: show the plan for one approval. Yes: carry it out. No: the task ends. Anything
        else is the owner's changes: propose again."""
        if state.perms.mode() != "plan":
            return Outcome("This task doesn't need a plan approved; carry on.", settle=0)
        steps = []
        for raw in (args.get("steps") or [])[:12]:
            item = raw if isinstance(raw, dict) else {"text": str(raw)}
            text = " ".join(str(item.get("text", "")).split())[:200]
            if text:
                steps.append({"text": text, "app": (str(item.get("app") or "").strip() or None), "used": False})
        if not steps:
            return Outcome("A plan needs 1 to 12 steps: [{text, app}].", is_error=True, settle=0)
        lines = "\n".join(f"{i}. {s['text']}" + (f" ({s['app']})" if s["app"] else "") for i, s in enumerate(steps, 1))
        answer = self._ask_owner(state, f"Plan for: {state.task}\n{lines}\nApprove this plan?",
                                 {"kind": "plan", "plan": [s["text"] for s in steps]})
        if is_yes(answer):
            state.plan = steps
            return Outcome("The owner approved the plan. Carry it out step by step; anything risky the plan "
                           "doesn't name will still ask.", settle=0)
        low = answer.strip().lower()
        if not low or low.split()[0] in DECLINE_WORDS:
            return Outcome("The owner rejected the plan, so the task ends here.", done=True, success=False, settle=0)
        return Outcome(f"The owner wants changes to the plan: {answer!r}. Call propose_plan again with a revised "
                       "plan.", settle=0)

    def _plan_covers(self, state: _TaskState, hits: list[str], package: str | None) -> bool:
        """An approved plan step covers one risky action: every risky word of the action appears in the
        step's text, and the step's app (if it names one) is the app in front. Each step covers once."""
        if not state.plan or not hits:
            return False
        for step in state.plan:
            if step["used"]:
                continue
            text = step["text"].lower()
            if not all(re.search(rf"\b{re.escape(h.lower())}\b", text) for h in hits):
                continue
            app = step["app"]
            if app and package:
                try:
                    resolved = self.resolver.exact(app)
                except Exception:
                    resolved = None
                if (resolved != package and app.lower() not in package.lower()
                        and app.lower() != self._app_label(package).lower()):
                    continue
            step["used"] = True
            return True
        return False

    @staticmethod
    def _identity(element: UIElement) -> tuple:
        """What makes two reads of the screen the same element, wherever it is (_recheck_target)."""
        return element.text, element.desc, element.resource_id, element.cls

    def _recheck_target(self, element: UIElement) -> UIElement | Outcome:
        """Just before a tap_element: re-read the elements and return where to tap, or refuse.

        Still in place: tap it. Moved, but the very same element (same text, description, id and
        class, and only one such element) is on screen, e.g. a list still settling after an app
        opened: wait for the screen to go quiet, confirm it stayed put, and tap it there. The guard
        already judged that element: a returned element always has the same identity (_identity), so
        tap_element's re-judging of a different identity is only a safeguard (but an approval signed
        for the old place doesn't carry over: its digest binds the bounds). Anything else (gone,
        ambiguous, covered, still moving) is refused. Only on phones whose element list is quick to read (the OpenMob app); adb and iOS
        dumps take a second or more."""
        if not self.config.verify_taps or element.cls == fast.OCR_CLASS or not getattr(self.driver, "fast_ui_tree", False):
            return element
        with self.timer.phase("observe"):
            fresh = self.driver.ui_elements()
        if not fresh:
            return element  # nothing to compare with (a tree that's briefly empty): tap as decided
        identity = self._identity(element)
        if any((el.text, el.desc, el.resource_id, el.cls, el.bounds) == identity + (element.bounds,) for el in fresh):
            return element
        refused = Outcome(f"Didn't tap [{element.index}] {element.label!r}: the screen changed and it is no longer "
                          "where it was. Look at the new screen.", is_error=True, settle=0)
        if not (element.text or element.desc or element.resource_id):
            return refused  # nothing to recognise it by
        matches = [el for el in fresh if self._identity(el) == identity]
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
            again = [el for el in self.driver.ui_elements() if self._identity(el) == identity]
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
        self._settle(settle, state, last.launched, last.quiet_ms, last.until)
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

    def _settle(self, seconds: float, state: _TaskState, launched: str | None = None, quiet_ms: int | None = None,
                until: str | None = None) -> None:
        """Let the screen catch up after an action. `seconds` is the most to wait (the fixed delay).

        Adaptive (config.adaptive_settle): a phone whose app offers wait_idle returns as soon as no
        window or content change has arrived for settle_quiet_ms. An adb phone ends a launch once the
        app is in front plus the short settle. Anything else (older apps, iOS): the fixed sleep.
        After launch_app or open_url (`until` "loading_done"), on a phone whose element list is quick to
        read: once quiet, if a spinner or "Loading" is still on screen (or the launched app isn't in front
        yet), keep waiting until it's gone, up to launch_wait_seconds in all (agent/waiting.py).
        Recorded as settle time: the real time waited."""
        state.settle_idle = None
        state.settle_wait = None
        if seconds <= 0:
            return
        if until == "loading_done" and self.config.adaptive_settle and getattr(self.driver, "fast_ui_tree", False) \
                and self.config.launch_wait_seconds > 0:
            started = time.monotonic()
            self._settle(seconds, state, launched, quiet_ms)
            idle = state.settle_idle
            with self.timer.phase("settle"):
                self._settle_loading(state, launched, self.config.launch_wait_seconds - (time.monotonic() - started))
            state.settle_idle = idle
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

    def _settle_loading(self, state: _TaskState, launched: str | None, budget: float) -> None:
        """The launch's settle is over: keep waiting only while something is visibly still loading."""
        if budget <= 0.2 or state.cancel.is_set():
            return
        try:
            package = self.driver.current_app()
            elements = self.driver.ui_elements()
        except DeviceError:
            return
        if self._is_blocked(state, package):
            return
        if launched and package != launched:
            cond = waiting.Condition("app_in_front", launched, timeout_s=budget)
        elif waiting.labeled(elements) and waiting.loading_element(elements) is not None:
            cond = waiting.Condition("loading_done", timeout_s=budget)
        else:
            return  # nothing is loading: the settle was enough
        result = self._waiter(state).run(cond)
        if cond.until == "app_in_front" and result.met and budget - result.waited_ms / 1000 > 0.2:
            cond2 = waiting.Condition("loading_done", timeout_s=budget - result.waited_ms / 1000)
            try:
                elements = self.driver.ui_elements()
            except DeviceError:
                elements = []
            if waiting.labeled(elements) and waiting.loading_element(elements) is not None:
                more = self._waiter(state).run(cond2)
                result = waiting.WaitResult(more.met, result.waited_ms + more.waited_ms, more.seen, more.via,
                                            more.package, more.stopped)
                cond = cond2
        state.settle_wait = self._wait_record(cond, result, auto=True)
