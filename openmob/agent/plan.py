"""Plan with the main model, execute with the classifier (config.plan_execute, off by default;
docs/design/fewer-model-calls.md).

On a cold run one dedicated model call before the step loop (plan_steps is its only tool) lays the request
out as sub-goals, each with a cue: a label that is on screen once that sub-goal is reached. A reply without a
valid plan changes nothing: the model then acts step by step as usual. From then on each step asks the
classifier about the current sub-goal instead of the whole request, and its answer (a tap by element, a
scroll, Back, an app launch) runs through loop._execute like any other action, so every check and
approval applies. A sub-goal is reached when its cue is on screen, or when the classifier says so with
confidence. The model takes the next step, told the plan and the progress so far, when the classifier
isn't sure, a step fails or changes nothing, a cue doesn't appear within plan_max_steps_per_goal steps,
a popup / login / lock / loading screen comes up, the next step would type (or anything else the helper
may not do), or the user corrects. Progress (a cue appearing) or a new plan_steps call resumes it.

The plan is data: sub-goal texts and cues. Nothing in it is ever run as code. This module holds the pure
parts; the loop's side is Agent._plan_step and Agent._plan_advance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..phone.adb import UIElement
from . import fast

TOOL = "plan_steps"
SOURCE = "plan"  # the steps table's source for the plan call and the steps carried out from it
NO_PLAN = "No plan given, so the request is carried out step by step as usual."
MAX_GOALS = 8
MAX_CHARS = 100
# Classifier operations the helper runs itself. Anything else (typing, waiting, escalate) goes to the model.
RUNNABLE = {"TAP", "SCROLL_UP", "SCROLL_DOWN", "BACK", "LAUNCH_APP"}


@dataclass
class Goal:
    text: str
    cue: str


@dataclass
class Plan:
    goals: list[Goal]
    index: int = 0  # the sub-goal in progress
    steps_on_goal: int = 0  # helper steps taken toward it
    retried: bool = False  # a tap refused before it ran was given one more look on this sub-goal
    retry: bool = False  # the last step was such a tap: look again without the model
    suspended: str | None = None  # why the model has the next step (None: the helper does)
    told: bool = False  # the model has been told the plan and progress since it was suspended
    fresh: bool = True  # just planned: its model call isn't an escalation (fast_path_done may still finish)
    reached: list[int] = field(default_factory=list)  # sub-goals reached, in order

    def current(self) -> Goal | None:
        return self.goals[self.index] if self.index < len(self.goals) else None

    def complete(self) -> bool:
        return self.index >= len(self.goals)

    def advance(self) -> None:
        self.reached.append(self.index)
        self.index += 1
        self.steps_on_goal = 0
        self.retried = False

    def request(self) -> str:
        """The current sub-goal as the classifier's request."""
        goal = self.current()
        return f"{goal.text} (once done, the screen shows {goal.cue!r})" if goal else ""

    def note(self) -> str:
        """The plan and progress for the model, with why the helper stopped."""
        lines = ["Plan so far (a quick helper carried out the ticked sub-goals from the element list):"]
        for i, goal in enumerate(self.goals):
            mark = "[x]" if i in self.reached else ("->" if i == self.index else "  ")
            lines.append(f"  {mark} {i + 1}. {goal.text} (cue {goal.cue!r})")
        lines.append(f"The helper stopped: {self.suspended or 'done'}. Carry on from the screen; if the plan needs "
                     f"changing, call {TOOL} again and the helper resumes.")
        return "\n".join(lines)


def _clean(value, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"each step's {what} must be a short text")
    text = " ".join(value.split())
    if not text:
        raise ValueError(f"each step needs a {what}")
    return text[:MAX_CHARS]


def parse(args: dict) -> list[Goal]:
    """The plan_steps input as goals, or ValueError: 1 to MAX_GOALS steps of {goal, cue}, each a short text."""
    steps = args.get("steps") if isinstance(args, dict) else None
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_GOALS:
        raise ValueError(f"a plan is 1 to {MAX_GOALS} steps: [{{goal, cue}}]")
    goals = []
    for raw in steps:
        if not isinstance(raw, dict):
            raise ValueError("each step is {goal, cue}")
        goals.append(Goal(_clean(raw.get("goal"), "goal"), _clean(raw.get("cue"), "cue")))
    return goals


def _norm(text: str) -> str:
    from ..phone.apps import normalize

    return normalize(text or "")


def _matches(label: str, cue: str) -> bool:
    a, b = _norm(label), _norm(cue)
    return bool(a and b) and (a == b or re.search(rf"(?<!\w){re.escape(b)}(?!\w)", a) is not None)


def _inside(inner: tuple, outer: tuple) -> bool:
    return outer != inner and outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def cue_visible(elements: list[UIElement], cue: str) -> bool:
    """The cue is on screen as the place itself: a title or selected tab (fast.shows_target), or a label
    that isn't tappable and isn't inside a tappable row (a heading, a caption). A tappable entry named
    after it is the way there, not the place, so it never counts."""
    if fast.shows_target(elements, cue) is not None:
        return True
    real = [el for el in elements if el.cls != fast.OCR_CLASS and not el.covered]
    tappable = [el.bounds for el in real if el.clickable]
    for el in real:
        if el.clickable or not _matches(el.text or el.desc, cue):
            continue
        if not any(_inside(el.bounds, b) for b in tappable):
            return True
    return False
