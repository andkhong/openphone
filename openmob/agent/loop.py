"""The observe → decide → act loop that drives the phone with any model (see models.py)."""

from __future__ import annotations

import base64
import concurrent.futures
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
from ..phone.adb import Screenshot, UIElement, downscale, format_elements
from ..phone.base import DeviceError, GateRefused, OwnerDialog, PhoneLocked
from ..phone.apps import NOT_LAUNCHABLE, AppResolver
from ..usage import Usage
from ..vision import ocr
from ..permissions import ALWAYS_DAYS, FOLLOWUP_SECONDS, TaskPermissions, category_of, trusted_for
from ..phone.approval_keys import action_digest
from . import app_links, fast, gate, permission_dialogs, plan, replay, timing, waiting
from .models import ModelError, ModelRequest, ModelResponse, ModelUnavailable, router_for
from .timing import StepTimer
from .tools import (OPEN_LINK_TOOL_SPEC, OPEN_SETTINGS_TOOL_SPEC, PLAN_REQUEST_PROMPT, PLAN_STEPS_NOTE,
                    PLAN_STEPS_TOOL_SPEC, PLAN_TOOL_SPEC, TOOL_SPECS, mode_notes, system_prompt)
from ..phone import settings_pages

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
# After a link or a Settings page opens: the most to wait for the screen to leave the one it was opened from.
# On a slow phone the old screen is quiet before the new one draws, so the settle ended early, the next look
# still showed it, and the model opened the link again (measured on an emulator, 2026-10-08).
LINK_LAND_S = 2.5
DEEP_LINK_SOURCE = "deep_link"  # the steps table's `source` for a Settings page opened straight from the request
DEFAULT_QUIET_MS = 300  # config.settle_quiet_ms by default (a variant names any other value)
PLAY_STORE = "com.android.vending"
INSTALL_BUTTON = re.compile(r"install|update|get|install on (this )?(phone|device)", re.I)
INSTALLED_BUTTON = "Open|Play"  # what the Play Store's button turns into once an install is done
INSTALL_TAP_GRACE_S = 5.0  # an enabled Install button still there after this long: the tap didn't start one
BLOCKED_APP_MESSAGE = "This app is blocked for API tasks on this phone (its owner can allow it)."
LOCKED_MESSAGE = "The phone stayed locked, so I stopped. Unlock it and ask again."
DIALOG_MESSAGE = "A system dialog on the phone wasn't answered, so I stopped. Answer it and ask again."
REFUSED = "Not allowed on this phone: {why} Don't try it another way; carry on without it or call done."
# Tools that change something on the phone (ask mode asks before each; plan mode needs the plan first).
CHANGING = {"tap_element", "tap", "long_press", "type_text", "swipe", "launch_app", "open_url", "press_key",
            "open_settings_page", "open_link"}
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
    replayed: int = 0  # of those, tool calls replayed from a stored path (replay_paths)
    planned: int = 0  # of those, tool calls the classifier took toward the model's plan (plan_execute)
    done_via: str | None = None  # who said the task was done: "vision" (the main model) | "classifier" (internal)
    prompts: int = 0  # approval prompts the owner saw (permission telemetry)
    mode: str | None = None  # the permission mode the task ran in
    reason: str | None = None  # why it failed, when it's not the model's call (phone_locked)


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
        # The classifier wasn't asked about this screen: a direct first step was expected to leave it (the
        # loop asks when no such step is taken, see Agent._classify_now).
        self.unclassified = False

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
    shown: str | None = None  # what the activity feed shows instead of text (the model still gets text)
    misrouted: bool = False  # a link landed somewhere other than its app (the browser, a chooser): the model's call


@dataclass
class _TaskState:
    task: str
    cancel: threading.Event
    auto_waited: float = 0.0
    treeless_misses: dict[str, int] = field(default_factory=dict)
    approved: bool = False  # the user just said yes to an ask_user question
    max_steps: int = 0
    blocked: Callable[[str], bool] | None = None  # packages this task may not touch (API safety)
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
    locked_out: bool = False  # the phone stayed locked past locked_wait_seconds
    task_outcome: str | None = None  # task_declined / task_no_answer: the owner didn't approve the task
    dialog_out: bool = False  # a system dialog only the owner can answer stayed open past owner_dialog_wait_seconds
    app_in_front: str | None = None  # the last app seen in front (not a permission dialog): "the task's app"
    typing_approved: bool = False  # ask mode: the approved typing covers the ENTER that submits it
    prompts: int = 0
    acted_on: Observation | None = None  # the observation an action already changed (its elements may be stale)
    tapped_field: UIElement | None = None  # the text field the last tap landed on (typing goes there)
    unreadable_seen: tuple | None = None  # the unreadable screen last waited on (each is waited on once)
    dialogs_given_up: set = field(default_factory=set)  # permission dialogs BACK didn't close: (package, message)
    # Replaying a stored path (config.replay_paths, agent/replay.py):
    replay: list[dict] | None = None  # the stored path for this request, if any
    replay_cursor: int = 0  # stored steps before this one have run or been passed over
    replay_tries: dict[int, int] = field(default_factory=dict)  # stored step -> attempts this task
    replay_retry: bool = False  # the last replayed tap was refused before it ran: once more at the next look
    replayed: int = 0  # replayed tool calls
    path: list[dict] = field(default_factory=list)  # this task's own replayable steps, stored if it succeeds
    # Plan with the model, execute with the classifier (config.plan_execute, agent/plan.py):
    plan_exec: plan.Plan | None = None  # the model's sub-goals, while the helper carries them out
    planned: int = 0  # tool calls the helper took toward the plan
    # The one direct first step a task may take without the classifier or the main model: an in-app link
    # (config.app_links_fast, agent/app_links.py) or a Settings page (config.deep_links_fast).
    direct_tried: bool = False
    link_cue: app_links.Link | None = None  # the entry just opened, when its cue must show on the next look
    url_targets: dict[str, dict | None] = field(default_factory=dict)  # a link's handler, resolved once before opening


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
        paths: replay.PathStore | None = None,
    ):
        self.client = client
        self.driver = driver
        self.resolver = resolver
        self.config = config
        self.io = io
        self.judge = judge
        self.use_ocr = (config.ocr and ocr.available()) if use_ocr is None else use_ocr
        # Stored UI paths (replay_paths): the given store, else memory only for the life of this agent.
        self.paths = paths if paths is not None else (replay.PathStore() if config.replay_paths else None)
        self.recent: deque[tuple[str, str]] = deque(maxlen=RECENT_REQUESTS)
        self.usage = Usage()  # tokens for the task in progress (or the last one)
        self.platform = "android"  # set by the DeviceManager from the phone's hello (android | ios)
        self.model = config.model  # chosen per phone from the dashboard
        self.task_model = config.model  # locked in when a task starts, so a mid-task switch waits
        self.timer = StepTimer()  # where each step's time goes (measurement only)
        self._frame: Screenshot | None = None  # the last screenshot the agent took (for sizes)
        self._decision_effort: str | None = None  # this vision call's effort (canvas screens; _effort_for)
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
        isolated: bool = False,
        permissions: TaskPermissions | None = None,
    ) -> AgentResult:
        """`model` and `max_steps` override this phone's settings for one task; `blocked` names
        packages the task must not open or operate (the task stops if one comes to the front).
        `isolated`: a caller other than the phone's chat (e.g. an
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
                           perms=perms)
        state.fast = self._fast_enabled()
        if self._replay_on() and replay.eligible(task, self.config.confirm_keywords):
            state.replay = self.paths.get(replay.goal_key(task)) or None
        self._task_variant = self._variant(state.fast)
        self._gate_setup(state)
        try:
            result = self._run(state, go_home, isolated)
        finally:
            self._gate_setup(None)
        if state.locked_out and not result.success:
            result.reason = "phone_locked"
        elif state.dialog_out and not result.success:
            result.reason = "needs_owner_dialog"
        elif state.task_outcome and not result.success:
            result.reason = state.task_outcome
        result.usage = self.usage
        result.timings = self.timer.task_totals()
        result.fast_steps = state.fast_steps
        result.replayed = state.replayed
        result.planned = state.planned
        result.prompts = state.prompts
        result.mode = perms.mode()
        # A path worth replaying: the task succeeded, only navigated (the request as it stands, corrections
        # included), typed nothing, asked nothing and tapped nothing risky, and came from the phone's own chat
        # (not an isolated caller such as an API key: the owner's call; replay_store_api opts in, for benchmarks).
        if (self._replay_on() and result.success and state.path and state.prompts == 0 and not state.typed
                and not state.risky and (not isolated or self.config.replay_store_api)
                and replay.eligible(state.task, self.config.confirm_keywords)):
            self.paths.put(replay.goal_key(task), state.path)
        if not isolated:
            self.recent.append((task, result.summary))
        return result

    def _run(self, state: _TaskState, go_home: bool, isolated: bool = False) -> AgentResult:
        task = state.task
        with self.timer.phase("action"):  # phone actions before the first step: the wake, and HOME when asked
            self.driver.wake_unlock()
            # HOME when the launcher is already in front changes nothing: one read spares the press and its settle.
            go_home = go_home and not self._home_in_front()
            if go_home:
                self.driver.key("HOME")
        if go_home:
            self._settle(self.config.settle_delay, state)

        try:
            route = router_for(self.client, self.config).route(self.task_model)
        except ModelUnavailable as exc:
            return AgentResult(False, str(exc), 0)
        # A request that already names its first action (a link, a Settings page) doesn't ask the classifier about
        # the screen it is leaving; the look after that step asks as usual.
        obs = self._observe_settled(state, classify=not self._first_action_known(state))
        messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": self._request_text(task, isolated)}]}]
        self._show_later(state, messages, obs)
        if (self._plan_enabled() and not state.replay and not state.cancel.is_set()
                and not self._is_blocked(state, obs.package)):
            self._plan_request(route, obs, state, isolated)  # a cold run: the model plans before the step loop
        nudged = False

        mode = state.perms.mode()
        batch = max(1, int(self.config.max_actions_per_turn or 1))
        planning = self._plan_enabled()
        deep_links = self._can_open_settings()
        links = self._app_links_on()
        system = (system_prompt(self.platform, batch, settings_pages=deep_links, app_links=links) + mode_notes(mode)
                  + (PLAN_STEPS_NOTE if planning else "") + (BRIEF_REPLIES if self.config.vision_brief else ""))
        self._tools = TOOL_SPECS + [PLAN_TOOL_SPEC] if mode == "plan" else TOOL_SPECS
        if deep_links:  # only a phone that can open Settings pages directly is offered the tool
            self._tools = self._tools + [OPEN_SETTINGS_TOOL_SPEC]
        if links:  # the catalog of in-app links (agent/app_links.py)
            self._tools = self._tools + [OPEN_LINK_TOOL_SPEC]
        if planning:
            self._tools = self._tools + [PLAN_STEPS_TOOL_SPEC]
        for step in range(1, state.max_steps + 1):
            if state.cancel.is_set():
                return AgentResult(False, "Stopped.", step - 1)
            if getattr(self.driver, "locked", False) and not state.locked_out:
                if self._lock_wait(state):  # a secure lock came up between steps: pause, then look again
                    obs = self._observe_settled(state)
            if state.locked_out:
                return AgentResult(False, LOCKED_MESSAGE, step - 1)
            if getattr(self.driver, "owner_dialog", None) and not state.dialog_out:
                if self._dialog_wait(state):  # a system dialog only the owner can answer: pause, then look again
                    obs = self._observe_settled(state)
            if state.dialog_out:
                return AgentResult(False, DIALOG_MESSAGE, step - 1)
            if state.task_outcome:
                return AgentResult(False, "The owner declined this task on the phone." if state.task_outcome ==
                                   "task_declined" else "Nobody approved this task on the phone in time.", step - 1)
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
            if not state.direct_tried and not state.vision_next and not self._needs_plan(state):
                # A request that plainly names one in-app destination or one Settings page opens it as the first
                # action, with no classifier or main-model decision.
                state.direct_tried = True
                taken = self._link_step(state, obs) if self._app_links_fast_on() else None
                if taken is not None:
                    obs = self._after_fast_step(state, obs, taken, messages)
                    self._link_landing(state, obs, messages)
                    continue
                taken = self._settings_step(state, obs) if self._deep_links_fast_on() else None
                if taken is not None:
                    obs = self._after_fast_step(state, obs, taken, messages)
                    continue
            if obs.unclassified:  # no direct step after all: the classifier's usual look at this screen
                self._classify_now(state, obs)
            if state.replay and not state.vision_next and not self._needs_plan(state):
                taken = self._replay_step(state, obs)
                if taken is not None:
                    obs = self._after_fast_step(state, obs, taken, messages, retry=state.replay_retry)
                    continue
            if state.plan_exec is not None and not state.vision_next and not self._needs_plan(state):
                taken = self._plan_step(state, obs)
                if taken is not None:
                    obs = self._after_fast_step(state, obs, taken, messages, retry=state.plan_exec.retry)
                    if state.vision_next and state.plan_exec is not None:  # it failed or changed nothing
                        self._plan_suspend(state, "the last step failed" if taken[-1][2].is_error
                                           else "the last step changed nothing")
                    continue
            if state.fast and not state.vision_next and not self._needs_plan(state):
                taken = self._fast_step(state, obs)
                if taken is not None:
                    last = taken[-1][2]
                    if last.done:
                        return AgentResult(last.success, last.text, step, done_via=last.done_via)
                    obs = self._after_fast_step(state, obs, taken, messages)
                    continue
            state.vision_next = False
            was_escalated = state.escalated
            state.escalated = True
            self._plan_tell(state, messages)
            self._show(state, messages)
            self._trim_history(messages)
            early: dict[str, Outcome] = {}  # tool calls already run while the reply streamed (vision_stream)

            def act_early(call) -> None:
                if any(o.done for o in early.values()) or state.cancel.is_set():
                    return  # the loop below marks it skipped
                early[call.id] = self._act(call, obs, state)

            # Streaming actions run each call as it arrives, with no look in between: one-per-turn only.
            self._decision_effort = self._effort_for(route, obs)
            response, streamed = self._vision_call(route, system, messages, act_early if batch == 1 else None)
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
            seen = obs  # the screen the model decided on: its indices and coordinates refer to it
            ran: Outcome | None = None  # the previous action in this turn (batch > 1)
            stop: str | None = None  # why the rest of this turn is skipped
            for n, tool_use in enumerate(tool_uses):
                if batch > 1 and ran is not None and not stop:
                    stop = self._batch_stop(tool_use, n, batch)
                    if not stop:  # look before the next action, without asking the model
                        self._settle(self.config.settle_delay if ran.settle is None else ran.settle, state,
                                     ran.launched, ran.quiet_ms, ran.until)
                        fresh = self._observe_after_action(state, obs)
                        stop = self._batch_unexpected(seen, obs, fresh, state)
                        obs = fresh
                if tool_use.id in early and not finished:
                    outcome = early[tool_use.id]
                elif stop:
                    outcome = Outcome(f"Not run: {stop} Look at the new screen and decide again.", is_error=True)
                elif finished or state.cancel.is_set():
                    outcome = Outcome("Skipped.", is_error=True)
                else:
                    prompts = state.prompts
                    outcome = self._act(tool_use, seen, state)
                    if batch > 1:
                        ran = outcome
                        if outcome.is_error:
                            stop = "the previous action failed."
                        elif state.prompts != prompts:
                            stop = "the previous action asked the owner."
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

            if state.plan_exec is not None and state.plan_exec.fresh:
                # A reply that only planned decided nothing on the phone: not an escalation, so the classifier
                # may still finish a plain navigation task the helper carried out (fast_path_done).
                state.plan_exec.fresh = False
                if len(tool_uses) == 1:
                    state.escalated = was_escalated
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

    # --- several actions from one reply (max_actions_per_turn) ----------------------

    @staticmethod
    def _batch_stop(call, n: int, batch: int) -> str | None:
        """Why the n-th call of a reply mustn't run, before looking at the screen."""
        if n >= batch:
            return f"at most {batch} actions run per turn."
        if call.name == "done":
            return "call done alone, after you've seen the result of your actions."
        return None

    @staticmethod
    def _batch_unexpected(seen: Observation, before: Observation, after: Observation, state: _TaskState) -> str | None:
        """Why the screen after an action doesn't fit carrying on with the model's plan: another app,
        a labeled screen that didn't change (the action did nothing visible), or one mostly replaced (a
        new page: the model's next target may not be there). Screens without labels (games, canvases)
        can't be judged, so they carry on; every tap is still judged by the real element first."""
        if after.package != seen.package:
            return "the previous action opened another app."
        labels = lambda o: {el.label for el in o.elements if fast.labeled(el)}
        then, now = labels(before), labels(after)
        if then and state.actions and state.actions[-1].get("screen_changed") is False:
            return "the previous action didn't change the screen."
        if then and now and len(then & now) < 0.5 * min(len(then), len(now)):
            return "the screen changed more than expected."
        return None

    # --- the vision step's model call ---------------------------------------------

    def _vision_request(self, route, system: str, messages: list[dict], max_tokens: int) -> ModelRequest:
        return ModelRequest(model=self.task_model, system=system, tools=self._tools, messages=messages,
                            max_tokens=max_tokens, cache_history=self.config.history_cache,
                            effort=self._decision_effort)

    def _effort_for(self, route, obs: Observation) -> str | None:
        """canvas_effort for a Claude decision on a screen with no readable labels (a game, a chess board): there,
        reading the picture is the hard part. Labeled screens keep the model's own effort (low for Haiku 5.5).
        A switch resets the provider's cache for the conversation, so a task pays it about once (entering the game)."""
        if not self.config.canvas_effort or route.spec.provider != "anthropic":
            return None
        return None if any(fast.labeled(el) for el in obs.elements) else self.config.canvas_effort

    def _act(self, call, obs: Observation, state: _TaskState) -> Outcome:
        """Run one of the main model's tool calls and report it."""
        outcome = self._execute(call.name, call.input, obs, state, source=plan.SOURCE if call.name == plan.TOOL else None)
        self._event("error" if outcome.is_error else "action", f"{call.name}: {outcome.shown or outcome.text}",
                    {"timings": outcome.timings, "jev": self.judge is not None})
        return outcome

    def _vision_call(self, route, system: str, messages: list[dict], act=None):
        """The main model decides this step: (response, streamed). With vision_max_tokens, a reply that
        hit that cap before finishing its tool call is asked again with the model's full max_tokens.
        With vision_stream (and a provider that streams), `act` runs each tool call as soon as its
        block is complete, before the reply ends."""
        full = route.spec.max_tokens
        cap = int(route.spec.options.get("vision_max_tokens", self.config.vision_max_tokens))
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

    def _observe(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = (),
                 classify: bool = True) -> Observation:
        with self.timer.phase("observe"):
            return self._observe_untimed(state, before, pending, classify)

    def _observe_pool(self):
        """One worker for screenshots taken alongside the other reads (parallel_observe)."""
        pool = getattr(self, "_shot_pool", None)
        if pool is None:
            pool = self._shot_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="observe")
        return pool

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

    def _observe_untimed(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = (),
                         classify: bool = True) -> Observation:
        # The fast path decides from the element list; its screenshot waits until a vision step needs it.
        # parallel_observe: the screenshot is taken while the app and the elements are read.
        # classify=False: the classifier isn't asked (a direct first step is expected; _classify_now asks later if not).
        pending_shot = None
        if not self._lazy_shots(state) and self.config.parallel_observe:
            pending_shot = self._observe_pool().submit(self.driver.screenshot)
        package = self.driver.current_app()
        shot = None if self._lazy_shots(state) or pending_shot else self._screenshot()
        elements: list[UIElement] = []
        mode = self.config.ui_tree
        # Games make uiautomator slow and useless; stop asking after two empty dumps.
        if mode == "always" or (mode == "auto" and state.treeless_misses.get(package or "", 0) < 2):
            elements = self.driver.ui_elements()
            useful = any(el.text or el.desc for el in elements)
            key = package or ""
            state.treeless_misses[key] = 0 if useful else state.treeless_misses.get(key, 0) + 1
        if pending_shot is not None:
            shot = self._frame = pending_shot.result()  # its error, if any, surfaces here as before
        if self.use_ocr and not any(el.text or el.desc for el in elements):
            shot = shot or self._screenshot()  # OCR reads the screenshot (no element list: a vision step anyway)
            elements = elements + self._ocr_elements(shot, start=len(elements))
        obs = Observation(shot, elements, package, capture=self._screenshot,
                          geometry=None if shot else self._recent_frame())
        if before is not None:  # whether the last actions changed the screen (the fast path's loop check)
            changed = fast.signature(obs.package, obs.elements) != fast.signature(before.package, before.elements)
            for action in pending:
                action["screen_changed"] = changed
        if state.plan_exec is not None:  # sub-goals whose cue is on screen are reached, before the classifier is asked
            self._plan_advance(state, obs)
        if classify:
            self._classify_now(state, obs)
        else:
            obs.unclassified = self.judge is not None
        return obs

    def _classify_now(self, state: _TaskState, obs: Observation) -> None:
        """The classifier's look at `obs`: the one request per step (jev_one_request) on screens the fast path
        can use, else assess_screen. Every observation gets it, except the first look of a task whose first
        action the request already names (then it's asked here only if that step isn't taken after all)."""
        obs.unclassified = False
        if not self.judge:
            return
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

    def _one_request(self, state: _TaskState, obs: Observation) -> bool:
        """Ask the screen classifier everything for this step at once: on fast-path tasks, on screens the
        fast path can use (jev_one_request)."""
        return ((state.fast or state.plan_exec is not None) and self.config.jev_one_request
                and hasattr(self.judge, "observe_step") and fast.eligible(obs.elements, self.config.fast_path_min_elements))

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
        verdict = self.timer.timed("jev", self.judge.observe_step, self._jev_request(state),
                                   fast.screen_table(obs.package, obs.elements), state.actions[-5:], heads,
                                   risk, requested)
        obs.assessment, obs.choice = verdict.assessment, verdict.choice
        obs.risks = {int(k): v for k, v in verdict.risks.items()}
        obs.requested = {int(k): v for k, v in verdict.requested.items()}
        obs.unclassified = False

    def _observe_settled(self, state: _TaskState, before: Observation | None = None, pending: list[dict] = (),
                         classify: bool = True) -> Observation:
        """Observe, and sit out loading screens without spending a Claude call on each one."""
        obs = self._observe(state, before, pending, classify)
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

    # --- events and screen context ------------------------------------------

    def _event(self, kind: str, message: str, data: dict) -> None:
        """An event with structured details for IOs that take them (the dashboard's Coordinator)."""
        try:
            self.io.event(kind, message, data=data)
        except TypeError:  # a plain UserIO (terminal, tests): kind and message only
            self.io.event(kind, message)

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

    def _obs_blocks(self, obs: Observation) -> list[dict]:
        self._right_size(obs)
        header = (
            f"{OBS_PREFIX} foreground app {obs.package or 'unknown'}; "
            f"screenshot is {obs.shot.width}x{obs.shot.height} px."
        )
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
        last["content"].extend(self._obs_blocks(obs))

    # --- mid-task corrections ("steer") ----------------------------------------

    def _apply_corrections(self, state: _TaskState, messages: list[dict]) -> None:
        """What the user said while the task ran (Coordinator.steer) joins the next user message.
        It's the user's own words, so it also counts as part of the request for risky-tap checks."""
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
                 done_via: str = "vision", handled_by: str | None = None, source: str | None = None,
                 note: str | None = None) -> Outcome:
        started = time.monotonic()
        try:
            with self.timer.phase("action"):
                try:
                    outcome = self._dispatch(name, args, obs, state)
                except DeviceError as exc:
                    # "The phone can't see what's there" (gate_screen_unstable): wait for it to be readable first.
                    if getattr(exc, "code", None) not in UNREADABLE_CODES:
                        raise
                    outcome = self._when_readable(name, args, obs, state, exc)
        except OwnerDialog:
            state.dialog_out = True  # the dialog stayed open past owner_dialog_wait_seconds (the wait happened)
            outcome = Outcome(DIALOG_MESSAGE, is_error=True)
        except PhoneLocked:
            state.locked_out = True  # stayed locked past locked_wait_seconds (the wait already happened)
            outcome = Outcome("The phone stayed locked, so the task stopped. Unlock it and ask again.", is_error=True)
        except GateRefused as exc:
            # The phone refused it: report with its stable code, and don't retry the same call.
            if exc.kind == "task" and exc.code in ("gate_denied", "gate_no_answer"):
                state.task_outcome = "task_declined" if exc.code == "gate_denied" else "task_no_answer"
            self.io.event("status", f"Refused on the phone ({exc.code}): {exc}")
            outcome = Outcome(f"Refused on the phone ({exc.code}): {exc} Don't retry this; pick another approach "
                              "or call done.", is_error=True, settle=0)
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
        if name != plan.TOOL:  # a plan isn't an action on the phone
            state.actions.append({"action": self._action_text(name, args, obs), "screen_changed": None})
        self._record_step(state, name, args, obs, outcome, time.monotonic() - started, fast_step, handled_by, source)
        if self._replay_on() and not outcome.is_error and source in (None, replay.SOURCE, plan.SOURCE):
            step = replay.recordable(name, args, obs.package, obs.elements, outcome.launched)
            if step is not None:
                state.path.append(step)
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
                     fast_step: bool = False, handled_by: str | None = None, source: str | None = None) -> None:
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
            elif name == "open_link" and isinstance(args.get("params"), dict):
                # A link's parameters (a search, a number) are the user's words, like typed text.
                safe["params"] = {str(k): REDACTED for k in args["params"]}
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
                **({"handled_by": handled_by} if handled_by else {}),
                **({"source": source} if source else {}),
                **(outcome.wait or state.settle_wait or {}),
            }
            record(row)
        except Exception as exc:  # the trajectory record must never break a task
            self.io.event("error", f"Step not recorded: {exc}")

    def _dispatch(self, name: str, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        state.step_approved = False
        if name in ("tap", "tap_element", "long_press", "swipe", "scroll") and (
                locked := self._keyguard_check(state, obs)):
            return locked  # never a gesture at the lock screen
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
        if name == plan.TOOL:
            return self._plan_steps(args, state)
        if name == "launch_app":
            return self._launch_app(args, obs, state)
        if name == "open_settings_page":
            return self._open_settings(args, obs, state)
        if name == "open_link":
            return self._open_link(args, obs, state)

        if name == "list_apps":
            # Fresh from the phone: an app installed or removed since the last look shows as it is now.
            self.resolver.packages(refresh=True)
            text = self.resolver.listing(args.get("query", ""), limit=15 if args.get("query") else None)
            # The feed shows a count, not the phone's app list (the owner's apps are nobody's business on screen).
            count = len([line for line in (text or "").splitlines() if line.strip()])
            shown = f"Checked the installed apps ({count} found)." if count else "No apps found."
            return Outcome(text or "No apps found.", settle=0, shown=shown)

        if name == "open_url":
            url = gate.web_url(str(args["url"]))
            if self._app_links_on():
                # As an in-app destination: the handler is reported, a page the app should have taken is said so.
                return self._open_destination(url, obs, state, app_links.app_for_url(url), label=url,
                                              shown=app_links.without_query(url))
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
            # A scroll flings the list, which keeps moving well past the usual cap: wait it out (adaptive
            # settle still returns as soon as the list stops), or the next look reads moving positions and
            # its tap is refused as "moved", costing another look and a model call.
            return Outcome(f"Scrolled {args['direction']}.", settle=max(self.config.settle_delay, self.config.scroll_settle_delay))

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
            # A search or address box keeps the last query (seen on a Samsung: typing "chess games" into a Play
            # Store search still holding "Spotify" gave "Spotifychess games"), so its text is replaced; elsewhere
            # typing inserts at the cursor (a draft the owner started stays) unless the model asks to replace.
            replace = bool(args.get("replace")) or (field is not None and fast.is_query_field(field))
            self.driver.type_text(text, replace=replace)
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

    # --- Settings deep links (docs/design/deep-links.md) ---------------------------------------------

    def _can_open_settings(self) -> bool:
        """The phone opens Settings pages directly: the app advertised "open_settings", or it's an adb phone,
        and deep_links is on."""
        return self.config.deep_links and bool(getattr(self.driver, "can_open_settings", False))

    def _settings_page_tier(self, page: settings_pages.SettingsPage) -> str:
        """"refuse", "ask" or "allow" for opening `page` directly: the stricter of the table's tier and this
        server's own Settings word lists (the ones that judge taps on Settings pages), so a deep link never
        reaches a page a tap couldn't, and never loosens an existing rule."""
        cfg = self.config
        title = page.title.lower()
        if gate.words(title, cfg.settings_refuse_terms):
            return "refuse"
        if page.tier == settings_pages.ASK or gate.words(title, cfg.settings_ask_terms):
            return "ask"
        return "allow"

    def _open_settings(self, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        """open_settings_page: the phone opens one Settings page from its fixed key table. The page's tier is
        judged here before the phone is asked (its gatekeeper judges it again on gatekeeper builds): everyday
        pages open, ask pages ask the owner (refused under Don't ask), and an app's own pages are refused for
        an app this task may not touch. A phone without the page says so, and the agent navigates instead."""
        key = str(args.get("page", "")).strip()
        page = settings_pages.PAGES.get(key)
        if page is None:
            return Outcome(f"No Settings page {key!r}. Pages: {', '.join(settings_pages.PAGE_KEYS)}. For any other "
                           "page, open Settings with launch_app and navigate there.", is_error=True, settle=0)
        if not self._can_open_settings():
            return Outcome("This phone's app can't open Settings pages directly (an older OpenMob app). Open "
                           "Settings with launch_app and navigate there instead.", is_error=True, settle=0)
        package = None
        if page.needs_package:
            wanted = str(args.get("package", "")).strip()
            resolution = self.resolver.resolve(wanted) if wanted else None
            if resolution is None:
                return Outcome(f"The {page.title} page needs an installed app: pass its name or package "
                               f"(got {wanted!r}). Use list_apps to find it.", is_error=True, settle=0)
            package = resolution.package
            if package == settings_pages.OWN_PACKAGE or self._is_blocked(state, package):
                return Outcome(f"{BLOCKED_APP_MESSAGE} Its Settings pages are off-limits too. Don't try another "
                               "way; call done.", is_error=True, settle=0)
        shown = f"the {page.title} Settings page" + (f" of {self._app_label(package)}" if package else "")
        tier = self._settings_page_tier(page)
        if tier == "refuse":
            return self._refuse(shown, "this Settings page is security-critical, so agents can't open it.")
        if tier == "ask" and not state.step_approved:
            mode = state.perms.mode()
            if mode == "never" or self.config.confirm_actions == "never":
                return self._refuse(shown, "this Settings page asks the owner first, and nobody can be asked "
                                           "while agents act without asking.")
            state.risky = True
            if not state.approved:
                action = self._action("open_settings", obs.package, typed=f"{key} {package or ''}".strip())
                answer = self._ask_owner(state, f"I'm about to open {shown}. Should I go ahead?",
                                         {"kind": "action", "action_kind": "settings", "package": obs.package,
                                          "action": action, "digest": action_digest(action)})
                if not is_yes(answer):
                    return Outcome(f"The user did not approve opening {shown} (they said {answer!r}). Don't do "
                                   "it. Pick another approach or call done.", is_error=True, settle=0)
                if refused := self._approval_mismatch(action_digest(action), None, f"open {shown}"):
                    return refused
            state.approved = False
        state.money_followup = None  # leaving the app ends a payment's follow-up
        try:
            opened = self.driver.open_settings(key, package) or {}
        except DeviceError as exc:
            if getattr(exc, "code", None):  # a gatekeeper refusal: reported with its code by _execute
                raise
            return Outcome(f"Couldn't open {shown} directly ({exc}). Open Settings with launch_app and navigate "
                           "there instead.", is_error=True, settle=0)
        launched = opened.get("package") if isinstance(opened, dict) else None
        if state.task_app is None and launched:
            state.task_app = launched
        self._await_new_screen(state, obs)
        # Settle until the page is quiet and done loading, without waiting for a given app in front: an OEM may
        # hand a page to another app (on a Samsung, Battery opens Device care, not Settings), and waiting for
        # Settings there sat out the whole launch wait (measured ~9 s per task).
        # Some phones show a page in their own app (a Samsung opens Battery in Device care, "Battery activity"):
        # measured, the model then distrusted the page and navigated by hand (29 s instead of ~3 s).
        return Outcome(f"Opened {shown}. Some phones show this page in their own app (a Samsung shows Battery "
                       f"in Device care): if the screen is about {page.title.lower()}, you're on it.",
                       shown=f"Opened {shown}.", settle=self.config.launch_settle_delay, until="loading_done")

    def _deep_links_fast_on(self) -> bool:
        """The direct Settings step (config.deep_links_fast), on a phone that can open pages directly."""
        return bool(self.config.deep_links_fast) and self._can_open_settings()

    def _direct_settings_page(self, state: _TaskState) -> dict | None:
        """The fast path's deep link: open_settings_page's arguments for the Settings page a plain navigation
        request names exactly ("Open Settings and go to Display"; for an app's own page, "open Chrome's
        notification settings and tell me whether notifications are allowed", the app resolved by its exact
        name, never a guess), when this phone can open it directly; else None (the usual launch, or the main
        model). A page that asks or is refused, an app the task may not touch or OpenMob itself, and a name
        that isn't exactly an installed app's are the main model's call."""
        if not self._can_open_settings():
            return None
        found = fast.direct_settings_request(state.task, self.config.confirm_keywords)
        if found is None:
            return None
        key, name = found
        if self._settings_page_tier(settings_pages.PAGES[key]) != "allow":
            return None
        args = {"page": key}
        if name is not None:
            package = self.resolver.exact(name) if self.resolver is not None else None
            if package is None or package == settings_pages.OWN_PACKAGE or self._is_blocked(state, package):
                return None
            args["package"] = package
        return args

    def _settings_step(self, state: _TaskState, obs: Observation) -> list[tuple[str, dict, Outcome]] | None:
        """The direct Settings step (deep_links_fast): a request that plainly names one Settings page, or an app's
        own page, opens it now, with no classifier or main-model decision, through open_settings_page's every
        check (the page's tier, blocked apps, Ask every step, Plan first, the phone's gatekeeper). Returns what
        ran, or None: the usual path."""
        args = self._direct_settings_page(state)
        if args is None:
            return None
        outcome = self._execute("open_settings_page", args, obs, state, fast_step=True, source=DEEP_LINK_SOURCE)
        state.fast_steps += 1
        self._event("error" if outcome.is_error else "action", f"open_settings_page: {outcome.shown or outcome.text}",
                    {"timings": outcome.timings, "jev": self.judge is not None, "fast": True})
        return [("open_settings_page", args, outcome)]

    def _first_action_known(self, state: _TaskState) -> bool:
        """Whether the request alone names the task's first action: a plain link request (app_links_fast) or a
        plain Settings-page request (deep_links_fast). Only the request-side checks are made here; the step may
        still step aside (a link a browser would take, a value the text model can't give), and the classifier is
        then asked about the screen as usual."""
        if state.vision_next or self._needs_plan(state):
            return False
        if self._app_links_fast_on() and self._link_match(state) is not None:
            return True
        return self._deep_links_fast_on() and self._direct_settings_page(state) is not None

    def _home_in_front(self) -> bool:
        """Whether the launcher is in front (a HOME press would change nothing)."""
        try:
            return gate.is_launcher(self.driver.current_app())
        except DeviceError:
            return False

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

    # --- in-app links (docs/design/app-links.md, agent/app_links.py) -------------------------------------

    def _app_links_on(self) -> bool:
        """The link catalog (open_link) and open_url's in-app behaviour: config.app_links, on Android phones."""
        return bool(self.config.app_links) and self.platform == "android"

    def _app_links_fast_on(self) -> bool:
        return self._app_links_on() and bool(self.config.app_links_fast)

    def _await_new_screen(self, state: _TaskState, before: Observation) -> None:
        """Wait (up to LINK_LAND_S) until the screen differs from `before`, the one a link or a Settings page was
        opened from: by its contents, not its app, since a page may open in Settings, in another app (a Samsung's
        Device care) or as another page of the app in front. The usual settle follows. Only on phones whose element
        list is quick to read; a page that was already showing costs the whole wait (rare: the link would be moot)."""
        if not getattr(self.driver, "fast_ui_tree", False):
            return
        seen = waiting.signature(before.package, before.elements)
        deadline = time.monotonic() + LINK_LAND_S
        with self.timer.phase("settle"):
            while time.monotonic() < deadline and not state.cancel.is_set():
                try:
                    if waiting.signature(self.driver.current_app(), self.driver.ui_elements()) != seen:
                        return
                except DeviceError:
                    return
                state.cancel.wait(READABLE_POLL_S)

    def _open_link(self, args: dict, obs: Observation, state: _TaskState) -> Outcome:
        """open_link: a catalog destination inside an app (app_links.LINKS). The URL is built from the entry's
        template and judged like any link (the scheme allowlist, blocked apps, the phone's gatekeeper). A `view`
        or `draft` entry then opens as open_url does; an `action` entry asks the owner first (refused under Don't
        ask). Nothing here skips an ask a tap would get: the taps that follow are judged as always."""
        link = app_links.find(str(args.get("app", "")), str(args.get("intent", "")))
        if link is None:
            return Outcome(f"No link {str(args.get('app'))[:40]!r} / {str(args.get('intent'))[:40]!r}. The catalog: "
                           f"{app_links.summary()}. For anything else, open_url a web link or navigate in the app.",
                           is_error=True, settle=0)
        if not self._app_links_on():
            return Outcome("In-app links are off on this server. Use open_url, or navigate in the app.",
                           is_error=True, settle=0)
        try:
            url = app_links.build(link, args.get("params") or {})
        except ValueError as exc:
            return Outcome(f"Can't open {link.label}: {exc}", is_error=True, settle=0)
        if any(self._is_blocked(state, p) for p in link.packages):
            return Outcome(f"{BLOCKED_APP_MESSAGE} That link opens in it. Don't try to open it another way; call done.",
                           is_error=True, settle=0)
        if link.side_effect == app_links.ACTION and not state.step_approved:
            mode = state.perms.mode()
            if mode == "never" or self.config.confirm_actions == "never":
                return self._refuse(link.label, "this link acts as soon as it opens, so it asks the owner first, and "
                                                "nobody can be asked while agents act without asking.")
            state.risky = True
            if not state.approved:
                action = self._action("open_url", obs.package, typed=url)
                answer = self._ask_owner(state, f"I'm about to open {link.label}, which acts as soon as it opens. "
                                                "Should I go ahead?",
                                         {"kind": "action", "action_kind": "link", "package": obs.package,
                                          "action": action, "digest": action_digest(action)})
                if not is_yes(answer):
                    return Outcome(f"The user did not approve opening {link.label} (they said {answer!r}). Don't do "
                                   "it. Pick another approach or call done.", is_error=True, settle=0)
                if refused := self._approval_mismatch(action_digest(action), None, f"open {link.label}"):
                    return refused
            state.approved = False
        return self._open_destination(url, obs, state, app_links.APPS[link.app], label=link.label, shown=link.label)

    def _open_destination(self, url: str, obs: Observation, state: _TaskState, app: app_links.App | None, *,
                          label: str, shown: str) -> Outcome:
        """Open a link as a step into its app: the checks open_url always had (the scheme allowlist, the handler
        resolved on the phone against the task's blocked apps; the gatekeeper judges it again on the phone), then
        settle until the page is done loading and say where it landed. `app`: the catalog app the link belongs
        to, so a landing in the browser is reported as such (the app isn't installed, or doesn't take these links
        on this phone) and the model can adjust. Never waits for a particular package in front: an OEM may hand a
        link to another app. `label` is for the model, `shown` for the feed (never the user's words)."""
        if why := gate.url_refusal(url):
            return self._refuse(url[:80], why)
        target = state.url_targets.pop(url) if url in state.url_targets else self._url_target(url)
        if refused := self._blocked_handler(target, state):
            return refused
        state.money_followup = None  # leaving the app ends a payment's follow-up
        self.driver.open_url(url)
        if target is None and (backed := self._back_out_if_blocked(state, obs.package)):
            return backed
        # The step ends once the destination's window is up (the screen left the one the link was opened from)
        # and the app in front is the one expected (the entry's app, or a browser where that is fine): the
        # settle until the page is quiet and done loading is then the next step's, as after a launch, and is
        # recorded on the step it delays. When that quick look can't tell (the home screen, or an app that is
        # neither: the old screen may just have redrawn), the step settles fully first and looks again, as before.
        self._await_new_screen(state, obs)
        landed = self._landed()
        quick = (landed is not None and not gate.is_launcher(landed)
                 and (app is None or landed in app.packages or (app.browser_ok and app_links.is_browser(landed))))
        if not quick:
            self._settle(self.config.launch_settle_delay, state, until="loading_done")
            landed = self._landed()
        if state.task_app is None and landed and not gate.is_launcher(landed):
            state.task_app = landed
        where = self._app_label(landed) if landed else None
        wrong = False
        if landed and gate.is_launcher(landed):
            wrong = True
            text = (f"Opened {label}, but the home screen is still in front: the link may not have opened. Look at "
                    "the screen.")
        elif app is not None and app.packages:
            if landed in app.packages or (app.browser_ok and app_links.is_browser(landed)):
                text = f"Opened {label} in {where if landed not in app.packages else app.label}."
            elif app_links.is_browser(landed):
                wrong = True
                if any(p in self.resolver.packages() for p in app.packages):
                    text = (f"Opened {label} in {where}, not the {app.label} app: this phone sends these links to the "
                            f"browser. If the web page serves the request, carry on there; otherwise open {app.label} "
                            "with launch_app and navigate.")
                else:
                    text = (f"Opened {label} in {where}: the {app.label} app isn't installed. Carry on on the web, or "
                            f"launch_app {app.label!r} with install_if_missing.")
            else:
                wrong = True
                text = (f"Opened {label}, but {where or 'something else'} is in front rather than {app.label}. Look at "
                        "the screen: a chooser or a dialog may be up, or the link may not have opened.")
        else:
            text = f"Opened {label}" + (f" in {where}." if where else ".")
        return Outcome(text, settle=self.config.launch_settle_delay if quick else 0, until="loading_done" if quick else None,
                       shown=f"Opened {shown}" + (f" in {where}." if where else "."), misrouted=wrong)

    def _landed(self) -> str | None:
        try:
            return self.driver.current_app()
        except DeviceError:
            return None

    def _link_match(self, state: _TaskState) -> app_links.Match | None:
        """The one `view` catalog entry the request plainly asks for (app_links.match, or a Play Store page by app
        name), unless its app is blocked for this task: the request-side half of the fast link step."""
        match = app_links.match(state.task, self.config.confirm_keywords)
        if match is None and (name := app_links.play_page_name(state.task)):
            page = app_links.BY_ID["play_store.app"]
            package = self.resolver.exact(name) if self.resolver else None  # installed: its id, no guessing
            match = app_links.Match(page, {"package": package} if package else {}, () if package else page.params)
        if match is None:
            return None
        if match.link.side_effect != app_links.VIEW or any(self._is_blocked(state, p) for p in match.link.packages):
            return None
        return match

    def _link_step(self, state: _TaskState, obs: Observation) -> list[tuple[str, dict, Outcome]] | None:
        """The fast link step: a request that plainly asks for one `view` catalog entry opens it now, with no
        main-model decision (app_links.match). A value the request doesn't give plainly comes from the small text
        model. Not taken (None: the usual path) when the entry's app is blocked for this task, or when the phone
        says the link wouldn't open in that app (not installed, a browser would take it, a chooser would show):
        the main model then decides from the screen as always. Returns what ran."""
        match = self._link_match(state)
        if match is None:
            return None
        link, params = match.link, dict(match.params)
        for param in match.missing:
            value = self._fast_value(state, param.name, {"link": link.label, "parameter": {"name": param.name, "holds": param.description},
                                                         "today": time.strftime("%Y-%m-%d")},
                                     system=app_links.LINK_TEXT_SYSTEM)
            if value is None:
                return None
            params[param.name] = value
        try:
            url = app_links.build(link, params)
        except ValueError as exc:
            self.io.event("status", f"Link not taken: {exc}")
            return None
        target = self._url_target(url)
        if target is not None and link.packages:
            handler = target.get("package")
            if handler not in link.packages and not (app_links.APPS[link.app].browser_ok and app_links.is_browser(handler)):
                why = ("a chooser would show" if not handler else
                       f"this phone opens these links in {self._app_label(handler)}, not {app_links.APPS[link.app].label}")
                self.io.event("status", f"Link not taken ({why}); deciding the usual way.")
                return None
            state.url_targets[url] = target  # resolved once: _open_destination needn't ask the phone again
        args = {"app": link.app, "intent": link.intent, "params": params}
        outcome = self._execute("open_link", args, obs, state, fast_step=True, source=app_links.SOURCE)
        state.fast_steps += 1
        self._event("error" if outcome.is_error else "action", f"open_link: {outcome.shown or outcome.text}",
                    {"timings": outcome.timings, "jev": self.judge is not None, "fast": True})
        if outcome.misrouted:
            state.vision_next = True  # it landed elsewhere: the main model looks and decides
        state.link_cue = link if link.cue and not outcome.is_error else None
        return [("open_link", args, outcome)]

    def _link_landing(self, state: _TaskState, obs: Observation, messages: list[dict]) -> None:
        """After the link step's look: an entry with a screen cue must show it, else the main model takes the
        next step, told what was expected."""
        link, state.link_cue = state.link_cue, None
        if link is None or state.vision_next:
            return
        if not app_links.cue_shown(link, [el.label for el in obs.elements if fast.labeled(el)]):
            state.vision_next = True
            self._note_for_model(messages, f"(The screen after opening {link.label} doesn't show what was expected "
                                           f"there ({link.cue}): check it and carry on, or navigate in the app.)")

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
            outcome = self._execute("press_key", {"key": "BACK"}, obs, state, fast_step=True,
                                    handled_by="permission_rule", source="auto_dialog",
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
        # Consequence-based risk (by owner request; the phone's gatekeeper decides the same way):
        # - Subscribe / Join are reversible, and money only with a price nearby;
        if not gate.money_context(obs.elements) and not gate.is_money_app(package):
            money = [m for m in money if m not in gate.PRICE_ONLY_MONEY]
            hits = [h for h in hits if h not in gate.PRICE_ONLY_MONEY]
            real_hits = [h for h in real_hits if h not in gate.PRICE_ONLY_MONEY]
        # - a public link shared to yourself (a share target that is you while a link is on screen) is low risk.
        if not money and gate.to_self(desc, obs, self._owner_names()) and not credential:
            hits, real_hits, hint_hits = [], [], []
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
        # "Don't confirm everyday actions" (signed on the phone): only money and dangerous actions are confirmed.
        if mode != "ask" and settings.get("everyday") and not money and not credential and tier != "ask":
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
        details = {"kind": "action", "action_kind": kind, "package": package,
                   # Only concrete words or rules make the phone ask; the classifier's unease alone doesn't (a
                   # fingerprint is for money and dangerous actions, never basic app flow).
                   "hint_ok": bool(hits or money or credential or force or unlabeled_money or tier == "ask")}
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
        target = self._url_target(url)
        if target is None:
            return None, False
        return self._blocked_handler(target, state), True

    def _url_target(self, url: str) -> dict | None:
        """Which app would open `url` ({"package", "candidates"}), resolved on the phone before opening (newer
        apps, adb), or None when this phone can't say."""
        resolve = getattr(self.driver, "resolve_url", None)
        if not callable(resolve):
            return None
        try:
            return resolve(url)
        except DeviceError:
            return None

    def _blocked_handler(self, target: dict | None, state: _TaskState) -> Outcome | None:
        """The link's resolved handler (or, with a chooser, any candidate) is an app the task may not open."""
        if target is None:
            return None
        handlers = [target["package"]] if target.get("package") else list(target.get("candidates") or [])
        if any(self._is_blocked(state, p) for p in handlers):
            return Outcome(f"{BLOCKED_APP_MESSAGE} That link opens in it. Don't try to open it another way; "
                           "call done.", is_error=True, settle=0)
        return None

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

    # --- gatekeeper phones (docs/design/phone-gatekeeper.md) and the lock screen ---------------------

    @property
    def _gatekeeper(self) -> bool:
        return bool(getattr(self.driver, "gatekeeper", False))

    def _gate_setup(self, state: _TaskState | None) -> None:
        """Wire the driver to this task: its mode goes with each act call (the phone takes the tighter of it and
        its owner's ceiling), the phone's own prompts wait here, and a secure lock pauses the task."""
        self._gate_state = state
        if not hasattr(self.driver, "gate_approver"):
            return
        self.driver.gate_mode = state.perms.mode() if state else None
        self.driver.gate_task = str(getattr(self.io, "current_task", "") or "") if state else ""
        # Task approval, just in time: the phone asks for the whole task at the first step that needs a tap, worded
        # from this request (and not at all when no step does). Not in Ask every step.
        task_ok = state is not None and state.perms.mode() in ("auto", "trusted", "plan")
        try:
            if task_ok and (state.perms.current_settings() or {}).get("task_approval") is False:
                task_ok = False
        except Exception:
            pass
        source = getattr(self.io, "task_source", None)
        self.driver.gate_task_text = state.task if task_ok else ""
        self.driver.gate_task_source = (source() if callable(source) else "An agent") if task_ok else ""
        self.driver.gate_approver = self._gate_approval if state else None
        self.driver.lock_waiter = (lambda exc: self._lock_wait(self._gate_state)) if state else None
        if hasattr(self.driver, "dialog_waiter"):
            self.driver.dialog_waiter = (lambda exc: self._dialog_wait(self._gate_state)) if state else None

    def _owner_names(self) -> list[str]:
        """The owner's own names ("self" recipients): the paired account's email, if the IO knows it."""
        names = getattr(self.io, "owner_names", None)
        try:
            return list(names() if callable(names) else names or [])
        except Exception:
            return []

    def _task_approval(self, state: _TaskState) -> AgentResult | None:
        """Task approval (by owner request): on a gatekeeper phone, in Ask for risky / Trusted / Plan first, ask the
        owner once, on the phone, to approve this task; its non-money risky steps then run without more prompts.
        Declined: the task stops (task_declined). Not asked in Ask every step or Don't ask, or when turned off."""
        task_grant = getattr(self.driver, "task_grant", None)
        if task_grant is None or not self._gatekeeper or state.perms.mode() not in ("auto", "trusted", "plan"):
            return None
        try:
            if (state.perms.current_settings() or {}).get("task_approval") is False:
                return None
        except Exception:
            pass
        source = getattr(self.io, "task_source", None)
        try:
            granted = task_grant(str(getattr(self.io, "current_task", "") or ""), state.task,
                                 source() if callable(source) else "An agent")
        except GateRefused as exc:
            if exc.code in ("gate_denied", "gate_no_answer"):
                state.task_outcome = "task_declined" if exc.code == "gate_denied" else "task_no_answer"
                return AgentResult(False, "The owner declined this task on the phone." if exc.code == "gate_denied"
                                   else "Nobody approved this task on the phone in time, so I didn't start.", 0)
            self.io.event("status", f"No task approval ({exc.code}): risky steps will ask one by one.")
            return None
        except DeviceError:
            return None
        if granted:
            self.io.event("status", "Task approved on the phone: its steps run without more prompts (purchases "
                                    "still ask).")
        return None

    def _gate_approval(self, refusal) -> str:
        """The phone asked its owner (gate_needs_approval): the task waits for that answer, worded by the phone.
        The API and dashboard can decline it, never approve it (approval_on_phone)."""
        state = self._gate_state
        if state is None:
            return False
        question = refusal.question or "Approve this action on your phone?"
        what = "of this task " if refusal.kind == "task" else ""
        answer = self._ask_owner(state, f"Waiting for your approval {what}on the phone: {question}",
                                 {"kind": "gate", "approver": "phone", "approval_id": refusal.approval_id,
                                  "package": refusal.package, "action_kind": refusal.kind})
        if is_yes(answer):
            return "yes"
        # "no answer": the prompt ended without a decision (timed out, or interrupted and never answered).
        return "no_answer" if answer.strip().lower() == "no answer" else "no"

    def _dialog_wait(self, state: _TaskState | None) -> bool:
        """A system dialog the phone can't read is up (Android 14+ permission dialogs): the task pauses with the
        phone's message ("Kalshi is asking for location: choose on your phone…") until the owner answers it (True)
        or owner_dialog_wait_seconds pass (False: the task ends with reason needs_owner_dialog). Never tapped."""
        wait = float(getattr(self.config, "owner_dialog_wait_seconds", 0) or 0)
        waiter = getattr(self.driver, "wait_owner_dialog", None)
        message = getattr(self.driver, "owner_dialog", None) or "A system dialog needs your answer on the phone."
        self.io.event("status", message)
        closed = False
        if waiter is not None and wait > 0:
            deadline = time.monotonic() + wait
            while not closed and time.monotonic() < deadline and not (state and state.cancel.is_set()):
                closed = self.timer.timed("wait", waiter, min(5.0, deadline - time.monotonic()))
        if closed:
            self.io.event("status", "The dialog was answered on the phone. Carrying on.")
            return True
        if state is not None:
            state.dialog_out = True
        return False

    def _lock_wait(self, state: _TaskState | None) -> bool:
        """A secure lock screen came up mid-task: pause with "Unlock your phone to continue" until the owner
        unlocks it (True), or locked_wait_seconds pass (False: the task ends with reason phone_locked). The agent
        never taps or swipes at the lock screen itself."""
        wait = float(getattr(self.config, "locked_wait_seconds", 0) or 0)
        waiter = getattr(self.driver, "wait_unlocked", None)
        self.io.event("status", "Unlock your phone to continue.")
        unlocked = False
        if waiter is not None and wait > 0:
            deadline = time.monotonic() + wait
            while not unlocked and time.monotonic() < deadline and not (state and state.cancel.is_set()):
                unlocked = self.timer.timed("wait", waiter, min(5.0, deadline - time.monotonic()))
        if unlocked:
            self.io.event("status", "The phone is unlocked. Carrying on.")
            return True
        if state is not None:
            state.locked_out = True
        return False

    def _keyguard_check(self, state: _TaskState, obs: Observation) -> Outcome | None:
        """Before a tap or swipe: never at the lock screen. A swipe-only lock is woken and dismissed by the phone
        (wake_unlock); a secure one pauses the task as phone_locked."""
        if not (getattr(self.driver, "locked", False) or obs.package == "com.android.systemui"):
            return None
        try:
            lock = self.driver.device_state() if hasattr(self.driver, "device_state") else None
        except DeviceError:
            lock = None
        if not lock or not lock.get("locked"):
            return None
        if not lock.get("secure") and not getattr(self.driver, "locked", False):  # not after the owner's power press
            self.driver.wake_unlock()
            return Outcome("The lock screen was up; the phone dismissed it. Look at the screen again.", is_error=True)
        if self._lock_wait(state):
            return Outcome("The phone was locked; it's unlocked now. Look at the screen again before acting.",
                           is_error=True)
        return Outcome("The phone stayed locked, so the task stopped. Unlock it and ask again.", is_error=True)

    def _ask_owner(self, state: _TaskState, question: str, details: dict) -> str:
        """One approval prompt (the owner's screen lock, on an app phone). Counts it, and records its
        decision and how long the owner took, for the prompt budget (`openmob prompts`).

        On a gatekeeper phone the phone owns approval (D9): the server's checks only add caution. Instead of an
        approval_request, the next act call carries a hint; the phone asks once, covering both detectors."""
        if self._gatekeeper and details.get("kind") in ("action", "step", "plan"):
            if details.get("kind") == "action" and details.get("hint_ok", True):
                self.driver.gate_hint(question)
            return "yes"  # not a yes from anyone: the phone decides, and asks its owner if it must
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
                    else "no_answer" if low == "no answer"
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
        elif name == "open_link":
            link = app_links.find(str(args.get("app", "")), str(args.get("intent", "")))
            try:
                url = app_links.build(link, args.get("params") or {}) if link is not None else None
            except ValueError:
                url = None
            if url is None:
                return None  # it errors without touching the phone
            what = f"open {link.label}"
            action = self._action("open_url", obs.package, typed=url)  # the prompt shows the exact link
        elif name == "open_settings_page":
            page = settings_pages.PAGES.get(str(args.get("page", "")))
            if page is None:
                return None  # an unknown key errors without touching the phone
            package = str(args.get("package") or "")  # an installed app's package shows as its name; a name as given
            known = bool(package) and self.resolver is not None and package in self.resolver.packages()
            of_app = self._app_label(package) if known else repr(package)
            what = f"open the {page.title} Settings page" + (f" of {of_app}" if package else "")
            action = self._action("open_settings", obs.package,
                                  typed=f"{args.get('page')} {args.get('package') or ''}".strip())
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
        """What makes two reads of the screen the same element, wherever it is (_recheck_target). Volatile numbers
        (live odds, prices, counts, times) are blanked: "DUN · 42%" and "DUN · 47%" are the same button. A money
        commit approved on the phone is still bound to its exact amounts (the phone's permit, the approval digest)."""
        return gate.volatile(element.text), gate.volatile(element.desc), element.resource_id, element.cls

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
        if any(self._identity(el) == identity and el.bounds == element.bounds for el in fresh):
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

    # --- plan with the model, execute with the classifier (see plan.py) ----------------------

    def _plan_enabled(self) -> bool:
        return bool(self.config.plan_execute and self.judge is not None and hasattr(self.judge, "next_step"))

    def _jev_request(self, state: _TaskState) -> str:
        """What the classifier is asked to make progress on: the plan's current sub-goal while the helper
        carries it out, else the request."""
        p = state.plan_exec
        return p.request() if p is not None and not p.suspended and not p.complete() else state.task

    def _plan_advance(self, state: _TaskState, obs: Observation) -> None:
        """At each look: sub-goals whose cue is on screen are reached. Progress while the model had the next
        step hands it back to the helper; once the last sub-goal is reached the plan is done and the usual
        path (the classifier's DONE with its checks, else the model) finishes the task."""
        p = state.plan_exec
        moved = False
        while not p.complete() and plan.cue_visible(obs.elements, p.current().cue):
            p.advance()
            moved = True
        if moved and p.suspended:
            p.suspended = None
            p.told = False
        if p.complete():
            self.io.event("status", "Plan carried out; checking the result.")
            state.plan_exec = None

    def _plan_suspend(self, state: _TaskState, why: str) -> None:
        """The model takes the next step, told why with the plan and progress (_plan_tell)."""
        p = state.plan_exec
        p.suspended = why
        p.told = False
        state.vision_next = True
        self.io.event("status", f"Plan paused: {why}.")

    def _plan_tell(self, state: _TaskState, messages: list[dict]) -> None:
        p = state.plan_exec
        if p is not None and p.suspended and not p.told:
            self._note_for_model(messages, p.note())
            p.told = True

    def _plan_steps(self, args: dict, state: _TaskState) -> Outcome:
        """The plan_steps tool: the model's sub-goals and cues, validated as data, arm the helper (or re-arm it
        with a new plan after it stopped). Never run as anything but a list of texts to match."""
        if not self._plan_enabled():
            return Outcome(f"Unknown tool {plan.TOOL}.", is_error=True)
        if not isinstance(args, dict) or args.get("steps") is None:  # the planning request's reply held no plan
            return Outcome(plan.NO_PLAN, is_error=True, settle=0)
        try:
            goals = plan.parse(args)
        except ValueError as exc:
            return Outcome(f"Error: {exc}", is_error=True, settle=0)
        state.plan_exec = plan.Plan(goals)
        self.io.event("status", f"Plan: {len(goals)} sub-goal{'s' if len(goals) != 1 else ''}; a quick helper carries them out.")
        return Outcome("Plan noted. A quick helper carries it out from the element list; you'll hear back, with the "
                       "progress so far, if a screen doesn't fit.", settle=0)

    def _plan_request(self, route, obs: Observation, state: _TaskState, isolated: bool) -> None:
        """plan_execute on a cold run: one dedicated model call before the step loop, offering only plan_steps,
        with the request and the screen as the first vision step would see them. A reply with a valid plan
        arms the helper, and the classifier is asked once more about this screen, for its first sub-goal;
        any other reply (the model saying there is nothing to plan, or acting) is dropped and the task
        carries on as usual. No forced tool choice: not every provider or model takes one. Recorded as a plan
        step with the call's model time. The main conversation doesn't carry the exchange: the helper's
        notes, and the plan on a handover, do."""
        messages = [{"role": "user", "content": [{"type": "text", "text": self._request_text(state.task, isolated)},
                                                  *self._obs_blocks(obs)]}]
        request = ModelRequest(model=self.task_model, system=PLAN_REQUEST_PROMPT, tools=[PLAN_STEPS_TOOL_SPEC],
                               messages=messages, max_tokens=route.spec.max_tokens, cache_history=False)
        call = None
        try:
            with self.timer.phase("model"):
                response = route.adapter.complete(request)
        except ModelError as exc:
            self.io.event("error", f"No plan: {exc}")
        else:
            self._count_vision(response)
            for text in response.text:
                if text.strip():
                    self.io.event("thought", text.strip())
            call = next((c for c in response.tool_calls if c.name == plan.TOOL), None)
        outcome = self._execute(plan.TOOL, call.input if call is not None else {}, obs, state, source=plan.SOURCE)
        if outcome.is_error:
            self.io.event("status", f"No plan; carrying on as usual ({outcome.text}).")
            return
        state.plan_exec.fresh = False  # not a vision step's reply: the escalation rule for those doesn't apply
        # The screen was looked at before the plan existed: reach any sub-goal it already shows, and ask the
        # classifier about it once more, for the first sub-goal still to do.
        self._plan_advance(state, obs)
        if state.plan_exec is not None and self.judge is not None and self._one_request(state, obs):
            try:
                self._classify_step(state, obs)
            except (JevError, KeyError, TypeError, ValueError) as exc:
                self.io.event("error", f"Fast step skipped: {exc}")

    def _plan_choice(self, state: _TaskState, obs: Observation, again: bool = False):
        """The classifier's choice for the current sub-goal: the step's one request already holds it (asked
        with the sub-goal as the request, _jev_request); `again`, or without that request, one next_step
        call. (choice, the sub-goal's "looks done" probability or None, the target heads)."""
        heads = obs.heads if obs.heads is not None else fast.targets(obs.elements)
        if not again and obs.heads is not None:
            return obs.choice, (obs.assessment.task_done if obs.assessment is not None else None), heads
        try:
            choice = self.timer.timed("jev", self.judge.next_step, state.plan_exec.request(),
                                      fast.screen_table(obs.package, obs.elements), state.actions[-5:], heads)
        except (JevError, KeyError, TypeError, ValueError) as exc:
            self.io.event("error", f"Plan step skipped: {exc}")
            return None, None, heads
        return choice, None, heads

    def _plan_step(self, state: _TaskState, obs: Observation) -> list[tuple[str, dict, Outcome]] | None:
        """One step toward the plan's current sub-goal, without the main model, or None: the model's turn
        (_plan_suspend says why; the model is told the plan and progress) or nothing left to do. The
        classifier's answer for the sub-goal becomes a tap by element, a scroll, Back or an app launch,
        run through _execute like any other action, so every check and approval applies. Its DONE (or a
        confident "looks done") reaches the sub-goal. Anything else it proposes, an unsure answer, a cue
        that hasn't appeared after plan_max_steps_per_goal steps, a repeat, a popup / login / lock /
        loading screen, a screen without a usable element list, or a correction hands over to the model.
        A tap refused before it ran (its element moved) gets one more look per sub-goal."""
        c, p = self.config, state.plan_exec
        if p.suspended or p.complete():
            return None
        if fast.CORRECTION_PREFIX in state.task:
            return self._plan_suspend(state, "the user corrected the request")
        a = obs.assessment
        if a is not None and a.state in replay.BLOCKING_STATES and a.confidence >= c.jev_min_confidence:
            return self._plan_suspend(state, f"a {a.state.replace('_', ' ')} screen came up")
        if not fast.eligible(obs.elements, c.fast_path_min_elements):
            return self._plan_suspend(state, "this screen has no usable element list")
        p.retry = False
        goal = p.current()
        choice, looks_done, heads = self._plan_choice(state, obs)
        if choice is None:
            return self._plan_suspend(state, "the classifier didn't answer")

        def sure(prob: float, conf: float) -> bool:
            return prob >= c.fast_path_min_probability and conf >= c.fast_path_min_confidence

        # The classifier says the sub-goal is reached (its cue may read differently on this phone): move on,
        # and ask once more for the next one.
        if (choice.operation == "DONE" and sure(choice.probability, choice.confidence)) or (
                looks_done is not None and a is not None and looks_done >= c.fast_path_done_min_probability
                and a.confidence >= c.fast_path_min_confidence):
            p.advance()
            if p.complete():
                self.io.event("status", "Plan carried out; checking the result.")
                state.plan_exec = None
                return None
            goal = p.current()
            choice, _, heads = self._plan_choice(state, obs, again=True)
            if choice is None or choice.operation == "DONE":
                return self._plan_suspend(state, "two sub-goals looked done on one screen; check the plan")
        if choice.operation == "ESCALATE" or not sure(choice.probability, choice.confidence):
            return self._plan_suspend(state, "the helper wasn't sure what to do on this screen")
        if choice.operation not in plan.RUNNABLE:
            return self._plan_suspend(state, "the next step is typing" if choice.operation == "TYPE_TEXT"
                                      else "the next step isn't one the helper may take")
        if choice.operation == "TAP":
            picked = choice.targets.get("TAP")
            if not picked or picked[0] not in heads["TAP"] or not sure(picked[1], picked[2]):
                return self._plan_suspend(state, "the helper wasn't sure what to tap")
            element = obs.elements[int(picked[0])]
            name, args = "tap_element", {"index": element.index, "target": element.label}
        elif choice.operation == "LAUNCH_APP":
            app = self._direct_app(state, obs)
            if app is None:
                return self._plan_suspend(state, "the app to open isn't named in the request")
            name, args = "launch_app", {"app": app}
        else:
            name, args = fast.SIMPLE_OPERATIONS[choice.operation]
            args = dict(args)
        if p.steps_on_goal >= c.plan_max_steps_per_goal:
            return self._plan_suspend(state, f"the cue {goal.cue!r} didn't appear after {p.steps_on_goal} steps")
        first = self._action_text(name, args, obs)
        recent = [x["action"] for x in state.actions[-4:]]
        if recent[-1:] == [first] or recent.count(first) >= 2:
            return self._plan_suspend(state, "the helper would repeat itself")
        p.steps_on_goal += 1
        outcome = self._execute(name, args, obs, state, fast_step=True, source=plan.SOURCE)
        state.fast_steps += 1
        state.planned += 1
        self._event("error" if outcome.is_error else "action",
                    f"{name}: {outcome.shown or outcome.text} (plan: {goal.text})",
                    {"timings": outcome.timings, "jev": True, "fast": True, "plan": True})
        if outcome.is_error and replay.NOT_RUN in outcome.text and not p.retried:
            p.retried = p.retry = True  # its element moved before the tap: one more look, no model
        return [(name, args, outcome)]

    # --- replaying a stored path (see replay.py) ----------------------------------------

    def _replay_on(self) -> bool:
        return bool(self.config.replay_paths and self.paths is not None)

    def _replay_step(self, state: _TaskState, obs: Observation) -> list[tuple[str, dict, Outcome]] | None:
        """One step from the stored path, without the main model, or None: no remaining stored step fits
        this screen (the next decision is the fast path's or the vision model's; the path stays, so replay
        resumes on a later screen that fits). A step fits when the app and enough of the screen's labels
        match and its target is on screen exactly once (replay.match); the call then runs through
        _execute like any other, so every check and approval applies. A tap refused before it ran (its
        element moved) is tried once more at the next look; anything else moves on. A correction from
        the user drops the path, and no step runs on a screen the classifier confidently calls a popup,
        login, lock or loading screen."""
        c = self.config
        if fast.CORRECTION_PREFIX in state.task or state.replayed >= replay.MAX_TRIES * len(state.replay):
            state.replay = None
            return None
        state.replay_retry = False
        a = obs.assessment
        if a is not None and a.state in replay.BLOCKING_STATES and a.confidence >= c.jev_min_confidence:
            return None
        found = replay.match(state.replay, state.replay_cursor, obs.package, obs.elements, c.replay_min_overlap)
        if found is None:
            return None
        at, name, args = found
        tries = state.replay_tries.get(at, 0) + 1
        state.replay_tries[at] = tries
        outcome = self._execute(name, args, obs, state, fast_step=True, source=replay.SOURCE)
        state.fast_steps += 1
        state.replayed += 1
        self._event("error" if outcome.is_error else "action", f"{name}: {outcome.shown or outcome.text} (replayed)",
                    {"timings": outcome.timings, "jev": self.judge is not None, "fast": True, "replay": True})
        if outcome.is_error and replay.NOT_RUN in outcome.text and tries < replay.MAX_TRIES:
            state.replay_retry = True  # the same stored step again at the next look
        else:
            state.replay_cursor = at + 1
        return [(name, args, outcome)]

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
        if name == "open_settings_page":
            return f"{name} {args.get('page')}"
        if name == "open_link":
            return f"{name} {args.get('app')}.{args.get('intent')}"  # never its parameters
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
            # A request for one Settings page goes straight there (a deep link) instead of opening Settings.
            if page := self._direct_settings_page(state):
                calls = [("open_settings_page", page)]
            else:
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
            shown = f"{name}: {'Typed ' + REDACTED + '.' if name == 'type_text' and not outcome.is_error else outcome.shown or outcome.text}"
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

    def _fast_value(self, state: _TaskState, key: str, context: dict, system: str = fast.TEXT_SYSTEM) -> str | None:
        """A small, fast text model writes one value ({"text": …}, {"app": …}, or a link's parameter); None if
        it can't."""
        model = self.config.fast_path_text_model or self.config.resolver_model
        router = router_for(self.client, self.config)
        try:
            route = router.route(model)
            with self.timer.phase("model"):
                response = route.adapter.complete(ModelRequest(
                    model=model, system=system, tools=[], max_tokens=200,
                    messages=[{"role": "user", "content": json.dumps({"user_request": state.task, "want": key, **context})}],
                ))
        except (ModelUnavailable, ModelError) as exc:
            self.io.event("error", f"Fast step skipped: {exc}")
            return None
        self.usage.add_model(response.usage, response.model or model)
        self._note_tokens(response.usage)
        return fast.parse_value("".join(response.text), key)

    def _after_fast_step(self, state: _TaskState, obs: Observation, taken: list[tuple[str, dict, Outcome]],
                         messages: list[dict], retry: bool = False) -> Observation:
        """Settle, look again, and tell the main model what happened (as text in the conversation, so
        any provider takes it). The new screen follows when the main model next decides (_show_later).
        `retry`: a replayed tap was refused before it ran and gets one more look (_replay_step), so its
        lack of progress doesn't send the next step to the vision model."""
        last = taken[-1][2]
        settle = self.config.settle_delay if last.settle is None else last.settle
        self._settle(settle, state, last.launched, last.quiet_ms, last.until)
        new = self._observe_after_action(state, obs)
        if not retry and (any(o.is_error for _, _, o in taken) or state.actions[-1]["screen_changed"] is False):
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
        +launch (exact app names launch directly), +fastdone (the classifier may finish); +replay when
        stored paths may be replayed (replay_paths); +links when a plain in-app link request opens
        first without the model (app_links_fast); +pages when a plain Settings-page request does
        (deep_links_fast)."""
        c = self.config
        out = "adaptive" if c.adaptive_settle else "fixed"
        if c.adaptive_settle and c.settle_quiet_ms != DEFAULT_QUIET_MS:
            out += f"+q{c.settle_quiet_ms}"
        if fast_path:
            out += "+fast"
            for on, tag in ((c.fast_path_lazy_screenshot, "noshot"), (c.jev_one_request, "1req"),
                            (c.fast_path_direct_launch, "launch"), (c.fast_path_done, "fastdone")):
                out += f"+{tag}" if on else ""
        if c.replay_paths:
            out += "+replay"
        if c.plan_execute:
            out += "+plan"
        if c.app_links and c.app_links_fast:
            out += "+links"
        if c.deep_links and c.deep_links_fast:
            out += "+pages"
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
                        timing.trace(f"wait_idle quiet={int(quiet_ms or self.config.settle_quiet_ms)} cap={int(seconds * 1000)}: "
                                     f"phone waited {result.get('waited_ms')} ms, idle={result.get('idle')}, "
                                     f"round trip {int((time.monotonic() - started) * 1000)} ms")
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
