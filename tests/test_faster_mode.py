"""Faster mode (docs/design/faster-mode.md): Jev image checks on screens without usable text, only while the phone's
owner has it on. The agent side: what is never sent, J1 (loading means no model call, other states a hint), J3
(the wait ends when ready, and its fallbacks), timeouts, turning it off mid-task, the step record, the saved line."""

import io as iolib
import json
import threading
import time

import httpx
import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from PIL import Image

from openmob.agent import faster
from openmob.agent.drive import DriveChannel
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.history import History
from openmob.jev import IMAGES_HEADER, ImageAnswer, JevClient, JevError, JevImageJudge, prepare_image
from openmob.phone.adb import Screenshot, UIElement
from openmob.phone.apps import AppResolver
from openmob.vision.ocr import TextBox

GAME = "com.example.canvasgame"


class StepIO(RecordingIO):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.steps = []

    def record_step(self, row):
        self.steps.append(dict(row))


class FakeImages:
    """Stands in for JevImageJudge: scripted answers per question, every call recorded."""

    def __init__(self, states=(), loading=(), on_call=None, error=None):
        self.states = list(states)
        self.loadings = list(loading)
        self.calls = []
        self.on_call = on_call
        self.error = error
        self.timeout_s = 0.6

    def _next(self, kind, items, package, image):
        self.calls.append((kind, package, len(image)))
        if self.on_call:
            self.on_call(kind)
        if self.error:
            raise self.error
        value = items.pop(0) if len(items) > 1 else items[0]
        return value

    def screen_state(self, package, image):
        value = self._next("state", self.states, package, image)
        return value if isinstance(value, ImageAnswer) else ImageAnswer("state", value, 0.97, 0.95, 80.0)

    def loading(self, package, image):
        p = self._next("loading", self.loadings, package, image)
        return ImageAnswer("loading", "loading" if p >= 0.5 else "ready", max(p, 1 - p), abs(2 * p - 1), 80.0, p)


class GameDriver(FakeDriver):
    """A canvas game: no accessibility elements; the screenshot can be made black (a secure window)."""

    def __init__(self, package=GAME, black=False, elements=None):
        super().__init__(packages=[package], elements=elements or [])
        self.package = package
        self.black = black
        self.shots = 0

    def screenshot(self, max_age=0.0, attempts=2):
        self.shots += 1
        buf = iolib.BytesIO()
        Image.new("RGB", self.image, "black" if self.black else (40, 120, 200)).save(buf, "JPEG")
        return Screenshot(buf.getvalue(), *self.image, *self.device)


@pytest.fixture
def words(monkeypatch):
    """OCR on the server: the words it 'reads' off every screenshot (settable per test)."""
    box = {"words": ["Play"]}

    def recognize(data, width, height, accurate=True):
        return [TextBox(w, 0.9, 10, 10 + 40 * i, 200, 40 + 40 * i) for i, w in enumerate(box["words"])]

    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text", recognize)
    monkeypatch.setattr("openmob.agent.loop.JEV_READY_POLL_S", 0.01)
    return box


def make(tmp_path, responses, images, driver=None, on=True, io=None, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver or GameDriver()
    agent = Agent(ScriptedClient(responses), driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config,
                  io or StepIO(), use_ocr=True, images=images)
    switch = {"on": on}
    agent.faster_mode = lambda: switch["on"]
    return agent, switch


DONE = [[tool_use("done", "d", summary="ok", success=True)]]


def first_screen_text(client_requests):
    return str(client_requests[0]["messages"][0]["content"])


# --- off: nothing is sent ---------------------------------------------------------------------------------


def test_nothing_is_sent_when_faster_mode_is_off(tmp_path, words):
    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, DONE, images, on=False)
    result = agent.run("start the game")
    assert result.success and images.calls == [] and result.faster is None
    assert all(not row.get("jev_images") for row in agent.io.steps)


def test_nothing_is_sent_without_the_phone_switch_or_on_a_disabled_server(tmp_path, words):
    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, DONE, images)
    agent.faster_mode = None  # no phone-signed setting wired in (e.g. a plain CLI run): never on
    agent.run("start the game")
    agent2, _ = make(tmp_path, DONE, images, faster_mode="disabled")
    agent2.run("start the game")
    assert images.calls == []


def test_a_labeled_screen_sends_no_image(tmp_path, words):
    el = UIElement(0, "Wi-Fi", "", "", "TextView", True, None, (0, 0, 500, 100))
    images = FakeImages(states=["content"])
    agent, _ = make(tmp_path, DONE, images, driver=GameDriver(package="com.android.settings", elements=[el]))
    agent.run("open wi-fi")
    assert images.calls == []


# --- J1: loading means no model call; other states are a hint ------------------------------------------------


def test_loading_skips_the_model_and_the_wait_ends_when_ready(tmp_path, words):
    images = FakeImages(states=["loading", "menu"], loading=[0.95, 0.9, 0.05])
    agent, _ = make(tmp_path, DONE, images)
    result = agent.run("start the game")
    client = agent.client
    assert result.success and len(client.requests) == 1  # the loading look never reached the main model
    kinds = [c[0] for c in images.calls]
    assert kinds == ["state", "loading", "loading", "loading", "state"]
    text = first_screen_text(client.requests)
    assert "quick screenshot check: menu" in text and "A hint only" in text
    row = agent.io.steps[0]
    assert row["faster"] is True and row["jev_images"] == 5 and row["jev_state"] == "menu"
    assert row["wait_cond"] == "auto:jev_loading" and row["wait_via"] == "jev_image" and row["wait_met"] is True
    assert result.faster["model_calls_skipped"] == 1 and result.faster["images"] == 5
    assert "Faster mode" in result.faster["text"]
    assert any("Faster mode" in m for k, m in agent.io.events if k == "status")


def test_other_states_are_a_hint_and_go_on_the_step_record(tmp_path, words):
    images = FakeImages(states=["game_over"])
    agent, _ = make(tmp_path, DONE, images)
    agent.run("check the score")
    assert "quick screenshot check: game over" in first_screen_text(agent.client.requests)
    assert agent.io.steps[0]["jev_state"] == "game_over"
    assert [c[0] for c in images.calls] == ["state"]


def test_a_low_confidence_state_is_ignored(tmp_path, words):
    images = FakeImages(states=[ImageAnswer("state", "loading", 0.5, 0.3, 80.0)])
    agent, _ = make(tmp_path, DONE, images)
    agent.run("start the game")
    assert len(agent.client.requests) == 1 and "quick screenshot check" not in first_screen_text(agent.client.requests)
    assert [c[0] for c in images.calls] == ["state"]  # no loading wait on an unsure answer


def test_a_screen_that_stays_loading_reaches_the_model_after_two_extra_looks(tmp_path, words):
    images = FakeImages(states=["loading"], loading=[0.95])
    agent, _ = make(tmp_path, DONE, images, jev_image_loading_wait_seconds=0.3)
    result = agent.run("start the game")
    assert result.success and len(agent.client.requests) == 1
    assert [c[0] for c in images.calls].count("state") == 3  # the first look and two extra looks
    assert "quick screenshot check: loading" in first_screen_text(agent.client.requests)
    assert result.faster["model_calls_skipped"] == 2


# --- J3 fallbacks and timeouts --------------------------------------------------------------------------------


def test_j3_errors_and_unsure_answers_fall_back_without_blocking(tmp_path, words):
    class Flaky(FakeImages):
        def loading(self, package, image):
            self.calls.append(("loading", package, len(image)))
            raise JevError("Jev image answer took over 600 ms (discarded)")

    images = Flaky(states=["loading", "content"])
    agent, _ = make(tmp_path, DONE, images)
    started = time.monotonic()
    result = agent.run("start the game")
    assert result.success and time.monotonic() - started < 3
    assert agent.io.steps[0]["wait_via"] == "fallback" and agent.io.steps[0]["wait_met"] is False
    unsure = FakeImages(states=["loading", "content"], loading=[0.55])  # p 0.55: confidence 0.1, below the floor
    agent2, _ = make(tmp_path, DONE, unsure)
    agent2.run("start the game")
    assert agent2.io.steps[0]["wait_via"] == "fallback"


def test_a_slow_jev_answer_is_discarded_at_the_deadline():
    class SlowClient:
        on_usage = None

        def ask_image(self, state, questions, image, model, timeout_s, ref="screen"):
            time.sleep(1.0)
            return {"state": {"choice": "menu", "confidence": 1.0, "probabilities": {"menu": 1.0}}}

    judge = JevImageJudge(SlowClient(), "jev-1.14.0", timeout_s=0.05)
    started = time.monotonic()
    with pytest.raises(JevError, match="discarded"):
        judge.screen_state(GAME, b"x")
    assert time.monotonic() - started < 0.5


def test_a_timeout_never_blocks_the_step(tmp_path, words):
    images = FakeImages(states=["menu"], error=JevError("Jev image answer took over 600 ms (discarded)"))
    agent, _ = make(tmp_path, DONE, images)
    result = agent.run("start the game")
    assert result.success and len(agent.client.requests) == 1
    assert "quick screenshot check" not in first_screen_text(agent.client.requests)


# --- off mid-task ---------------------------------------------------------------------------------------------


def test_turning_it_off_mid_task_takes_effect_at_the_next_look(tmp_path, words):
    holder = {}
    images = FakeImages(states=["menu"])
    steps = [[tool_use("wait", "w1", seconds=0.01)], [tool_use("wait", "w2", seconds=0.01)]] + DONE
    agent, switch = make(tmp_path, steps, images)
    holder["switch"] = switch
    images.on_call = lambda kind: holder["switch"].update(on=False)  # the owner taps Turn off during the first one
    agent.run("start the game")
    assert len(images.calls) == 1  # the first answer was discarded and nothing more was sent
    assert "quick screenshot check" not in first_screen_text(agent.client.requests)
    assert agent.io.steps[0]["jev_image_skip"] == "off_discarded"
    assert agent.io.steps[1].get("faster") in (None, False) and not agent.io.steps[1].get("jev_images")


# --- what is never sent ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("case, expected", [
    ("blocked", faster.BLOCKED_APP),
    ("sensitive_app", faster.SENSITIVE_APP),
    ("card", faster.SENSITIVE_NUMBER),
    ("ssn", faster.SENSITIVE_NUMBER),
    ("password", faster.PASSWORD_FIELD),
    ("black", faster.BLACK_FRAME),
    ("unknown_window", faster.UNKNOWN_WINDOW),
    ("permission_dialog", faster.SYSTEM_DIALOG),
])
def test_never_sent_screens(tmp_path, words, case, expected):
    images = FakeImages(states=["menu"])
    driver = GameDriver()
    blocked = None
    if case == "blocked":
        blocked = lambda package: package == GAME  # noqa: E731
    elif case == "sensitive_app":
        driver = GameDriver(package="com.chase.sig.android")
    elif case == "card":
        words["words"] = ["Your card", "4111 1111 1111 1111"]
    elif case == "ssn":
        words["words"] = ["SSN", "123 45 6789"]
    elif case == "password":
        driver = GameDriver(elements=[UIElement(0, "", "", "pw", "EditText", True, None, (0, 0, 100, 50),
                                                editable=True, password=True)])
    elif case == "black":
        driver = GameDriver(black=True)
        words["words"] = []
    elif case == "unknown_window":
        driver = GameDriver(package=None)
    elif case == "permission_dialog":
        driver = GameDriver(package="com.google.android.permissioncontroller")
    agent, _ = make(tmp_path, DONE, images, driver=driver)
    agent.run("start the game", blocked=blocked)
    assert images.calls == []
    skips = [row.get("jev_image_skip") for row in agent.io.steps]
    assert expected in skips or case in ("blocked",)  # a blocked app ends the task before any step is recorded


def test_no_ocr_means_no_text_check_and_nothing_sent(tmp_path):
    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, DONE, images)
    agent.use_ocr = False
    agent.run("start the game")
    assert images.calls == [] and agent.io.steps[0]["jev_image_skip"] == faster.NO_TEXT_CHECK


def test_drive_sessions_send_nothing_unless_the_owner_allowed_the_key(tmp_path, words):
    from openmob.agent.loop import _TaskState

    images = FakeImages(states=["menu"])
    agent, _ = make(tmp_path, [], images)
    assert DriveChannel(5, 10, 3, "bot").faster_images is False  # the default for every key
    state = _TaskState(task="x", cancel=threading.Event(), drive=True, drive_images=False)
    assert agent._faster_blocked(state) == faster.DRIVE
    obs_elements: list = []
    assert agent._faster_image(state, GAME, obs_elements, GameDriver().screenshot(), True) is None
    state.drive_images = True  # the owner allowed this key (KeyPatch faster_drive)
    assert agent._faster_blocked(state) is None
    # A real drive session: the loop's own looks send nothing for a key that wasn't allowed.
    channel = DriveChannel(1, 5, 3, "bot")
    agent.run("drive it", drive=channel)
    assert images.calls == []


def test_the_refusal_rules_directly():
    def el(text="", cls="TextView", **kw):
        return UIElement(0, text, "", "", cls, False, None, (0, 0, 10, 10), **kw)

    ok = dict(blocked=None, sensitive_apps=["chase", "paypal"], locked=False, ocr_ran=True)
    assert faster.refusal(GAME, [el("Play", "OCRText")], **ok) is None
    assert faster.refusal(GAME, [], **{**ok, "ocr_ran": False}) == faster.NO_TEXT_CHECK
    assert faster.refusal(GAME, [], **{**ok, "locked": True}) == faster.LOCKED
    assert faster.refusal("com.paypal.android", [], **ok) == faster.SENSITIVE_APP
    assert faster.refusal(GAME, [el("4111", "OCRText"), el("1111 1111 1111", "OCRText")], **ok) == \
        faster.SENSITIVE_NUMBER  # split across OCR boxes
    assert faster.refusal(GAME, [el("Price 1,299", "OCRText")], **ok) is None
    covered = UIElement(0, "Allow", "", "", "Button", True, None, (0, 0, 10, 10), covered=True)
    assert faster.refusal(GAME, [covered], **ok) == faster.SYSTEM_DIALOG


# --- the request itself ---------------------------------------------------------------------------------------


def test_the_image_request_shape_pins_the_model_and_sets_the_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(200, json={"answers": {"state": {"type": "choice", "choice": "menu", "confidence": 0.9,
                                                               "probabilities": {"menu": 0.95}}},
                                         "usage": {"input_tokens": 1300}})

    client = JevClient("k", http=httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(handler)))
    judge = JevImageJudge(client, "jev-1.14.0", timeout_s=2)
    jpeg = prepare_image(GameDriver().screenshot().jpeg, 768).jpeg
    answer = judge.screen_state(GAME, jpeg)
    assert answer.value == "menu" and answer.probability == 0.95
    assert seen["headers"][IMAGES_HEADER.lower()] == "true"
    body = seen["body"]
    start = body.index(b'{"model"')
    request = json.loads(body[start:body.index(b"\r\n", start)])
    assert request["model"] == "jev-1.14.0"
    ref = request["state"]["screen"]
    assert request["assets"] == {ref: {"type": "image", "part": "screen-image"}}
    assert b'name="screen-image"' in body and b"image/jpeg" in body


def test_prepare_image_is_768_on_the_long_side_and_spots_black_frames():
    shot = GameDriver().screenshot()  # 540 x 1200
    small = prepare_image(shot.jpeg, 768)
    assert max(small.width, small.height) == 768 and not small.black
    big = Screenshot(shot.jpeg, 540, 1200, 1080, 2400)
    assert prepare_image(big.jpeg, 512).height == 512
    black = prepare_image(GameDriver(black=True).screenshot().jpeg, 768)
    assert black.black
    assert min(prepare_image(shot.jpeg, 400).width, 280) == 280  # padded to Jev's minimum side


def test_config_never_lets_the_server_turn_it_on(tmp_path):
    from openmob.config import load_config

    for bad in ("faster_mode: on\n", "faster_mode: always\n", "jev_image_model: jev-preview\n"):
        path = tmp_path / "c.yaml"
        path.write_text(bad)
        with pytest.raises(ValueError):
            load_config(path)
    cfg = Config()
    assert cfg.faster_mode == "phone_opt_in" and cfg.jev_image_model == "jev-1.14.0" and cfg.jev_image_side == 768


def test_history_keeps_the_faster_columns(tmp_path):
    history = History(tmp_path / "h.sqlite")
    chat = history.create_chat(None, "dev", "Pixel", "start the game")
    history.record_step(chat, "turn1", {"idx": 1, "tool": "done", "args": {}, "faster": True, "jev_images": 3,
                                        "jev_image_ms": 241, "jev_state": "menu", "jev_image_skip": None})
    history.record_step(chat, "turn1", {"idx": 2, "tool": "done", "args": {}})
    rows = history.steps(None, "turn1")
    assert (rows[0]["faster"], rows[0]["jev_images"], rows[0]["jev_image_ms"], rows[0]["jev_state"]) == \
        (1, 3, 241, "menu")
    assert rows[1]["faster"] is None and rows[1]["jev_images"] is None


def test_the_warning_is_the_same_words_everywhere():
    import re
    from pathlib import Path

    from openmob.permissions import FASTER_WARNING

    root = Path(__file__).resolve().parent.parent
    squash = lambda s: " ".join(s.replace("\\'", "'").split())  # noqa: E731
    kotlin = (root / "android/app/src/main/java/dev/openmob/companion/gate/ModeChange.kt").read_text()
    joined = "".join(re.findall(r'"([^"]*)"', kotlin.split("FASTER_WARNING =", 1)[1].split("\n\n", 1)[0]))
    assert squash(joined) == FASTER_WARNING
    strings = (root / "android/app/src/main/res/values/strings_ui.xml").read_text()
    assert FASTER_WARNING in squash(strings)
    assert FASTER_WARNING in squash((root / "docs/safety.md").read_text())
