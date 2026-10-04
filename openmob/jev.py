"""TypeSafe Jev: fast, calibrated classification of the phone's state.

Jev reads text only (no images), so screens are described to it as text: the foreground
app, the accessibility elements, and OCR'd words. It answers typed questions with
probabilities and a confidence score. Docs: https://docs.typesafe.ai
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://api.typesafe.ai"
MAX_CHOICE_OPTIONS = 255
MAX_SCREEN_TEXT_ITEMS = 80

# Screen states. Jev reads criteria literally, so each one spells out its boundaries.
SCREEN_STATES = {
    "loading": "A loading, splash, or downloading screen: progress bar, spinner, 'Loading', percentage, "
               "or a logo with no buttons to press. Nothing useful can be done except wait.",
    "tap_to_start": "A title screen that asks to tap or click anywhere to start or continue the game.",
    "popup": "An announcement, event notice, advert, reward, or permission pop-up covering the screen, "
             "usually with a close (X), 'OK', 'Skip', or 'Later' button.",
    "login": "A login, sign-in, account selection, or server selection screen.",
    "lock_screen": "The phone's lock screen or screen-off state.",
    "home_screen": "The phone's home screen, app drawer, or launcher.",
    "menu_open": "An app or game menu, settings panel, or navigation drawer is open.",
    "app_content": "The app's normal main view (for a game: the gameplay screen) with nothing covering it.",
}

REQUESTED_INSTRUCTIONS = (
    "Does user_request explicitly ask the agent to do what tapping tap_target would do? For example, "
    "'send hello to Sam' explicitly asks for tapping Send; 'open my email' does not."
)

RISK_INSTRUCTIONS = (
    "The agent is about to tap the described target on a phone. Would the tap spend money, send a message "
    "or post, delete data, or change account or security settings?"
)


@dataclass
class ScreenAssessment:
    state: str
    probability: float
    confidence: float
    task_done: float  # probability (0-1) that the user's request already looks complete

    def summary(self) -> str:
        return (
            f"Jev screen state: {self.state} (p={self.probability:.2f}, confidence={self.confidence:.2f}); "
            f"request looks complete: p={self.task_done:.2f}"
        )


class JevError(RuntimeError):
    pass


class JevClient:
    def __init__(
        self,
        api_key: str,
        model: str = "jev-latest",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 2,
        http: httpx.Client | None = None,
    ):
        self.model = model
        self.max_retries = max_retries
        self._http = http or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def ask(self, state, questions: dict) -> dict:
        """POST /v1/systemone. Returns the `answers` mapping."""
        body = {"state": state, "model": self.model, "questions": questions}
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.post("/v1/systemone", json=body)
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise JevError(f"Jev request failed: {exc}") from exc
            else:
                if response.status_code in (429, 529) and attempt < self.max_retries:
                    time.sleep(0.5 * 2**attempt)
                    continue
                if response.status_code != 200:
                    raise JevError(f"Jev returned {response.status_code}: {response.text[:300]}")
                return response.json()["answers"]
            time.sleep(0.5 * 2**attempt)
        raise JevError("Jev request failed")


def describe_screen(package: str | None, element_labels: list[str], screen_text: list[str]) -> dict:
    """Compact text description of a screen. Jev is less accurate with irrelevant state, so trim hard."""
    seen: set[str] = set()
    text = []
    for item in screen_text:
        item = item.strip()
        if len(item) >= 2 and item.lower() not in seen:
            seen.add(item.lower())
            text.append(item)
    return {
        "foreground_app": package or "unknown",
        "ui_elements": element_labels[:MAX_SCREEN_TEXT_ITEMS],
        "text_on_screen": text[:MAX_SCREEN_TEXT_ITEMS],
    }


class JevJudge:
    """The classification questions OpenMob asks Jev."""

    def __init__(self, client: JevClient):
        self.client = client

    def assess_screen(self, request: str, screen: dict) -> ScreenAssessment:
        answers = self.client.ask(
            {"user_request": request, "screen": screen},
            {
                "state": {
                    "type": "choice",
                    "instructions": "Which kind of phone screen is described in screen? Use the words on "
                                    "the screen and the foreground app.",
                    "criteria": SCREEN_STATES,
                },
                "done": {
                    "type": "noul",
                    "instructions": "Does the screen show that user_request has already been fully carried out?",
                },
            },
        )
        state = answers["state"]
        return ScreenAssessment(
            state=state["choice"],
            probability=float(state["probabilities"].get(state["choice"], 0.0)),
            confidence=float(state.get("confidence", 0.0)),
            task_done=float(answers["done"]["noul"]),
        )

    def assess_tap(self, request: str, target: str, screen: dict) -> tuple[float, float]:
        """(risk, requested): how likely tapping `target` is costly or irreversible, and how likely
        the user's request explicitly asked for exactly that. Both 0-1."""
        answers = self.client.ask(
            {"user_request": request, "tap_target": target, "screen": screen},
            {
                "risky": {"type": "noul", "instructions": RISK_INSTRUCTIONS},
                "requested": {"type": "noul", "instructions": REQUESTED_INSTRUCTIONS},
            },
        )
        return float(answers["risky"]["noul"]), float(answers["requested"]["noul"])

    def pick_package(self, spoken_name: str, candidates: dict[str, str]) -> tuple[str | None, float, float]:
        """Choose which package matches a spoken app name. Returns (package|None, probability, confidence)."""
        if not candidates:
            return None, 0.0, 0.0
        items = list(candidates.items())[: MAX_CHOICE_OPTIONS - 1]
        random.shuffle(items)  # Jev favors earlier options; don't let list order decide.
        criteria = {pkg: desc for pkg, desc in items}
        criteria["none"] = "None of the listed apps matches the spoken name."
        answers = self.client.ask(
            {"spoken_app_name": spoken_name},
            {
                "app": {
                    "type": "choice",
                    "instructions": "Which installed Android app is the user naming? The name came from speech "
                                    "recognition, so it may be slightly mis-heard.",
                    "criteria": criteria,
                }
            },
        )
        answer = answers["app"]
        choice = answer["choice"]
        probability = float(answer["probabilities"].get(choice, 0.0))
        confidence = float(answer.get("confidence", 0.0))
        return (None if choice == "none" else choice), probability, confidence
