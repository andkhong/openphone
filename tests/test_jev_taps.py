"""Jev taps (Faster mode step 2, docs/design/faster-mode.md §3.1): on a routine game screen Jev decides and locates a
tap without the main model. Only with Faster mode on, never in drive sessions or under Ask every step / Plan first;
the safe tap rule, the 3-tap cap, misses, the words that rule a tap out, and the gate judging every Jev tap."""

import io as iolib
import json
import threading

import httpx
import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from PIL import Image

from openmob.agent import faster
from openmob.agent.loop import JEV_TAP_SOURCE, Agent, _TaskState
from openmob.config import Config
from openmob.history import History
from openmob.jev import (ROUTINE_ACTIONS, ImageAnswer, JevClient, JevError, JevImageJudge, RoutineAnswer,
                         prepare_image)
from openmob.permissions import TaskPermissions
from openmob.phone.adb import Screenshot
from openmob.phone.base import GateRefused
from openmob.vision.ocr import TextBox

GAME = "com.example.canvasgame"
# A Play button in the middle of the screen, as fractions of the (unpadded) image: device (432..648, 1080..1200).
PLAY_BOX = (0.4, 0.45, 0.6, 0.5)


class StepIO(RecordingIO):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.steps = []

    def record_step(self, row):
        self.steps.append(json.loads(json.dumps(row, default=str)))


class Scenes(FakeDriver):
    """A canvas game whose screens follow each other on every tap: each scene is (colour, OCR words, J1 state).
    `stuck`: taps change nothing."""

    def __init__(self, scenes, package=GAME, stuck=False, refuse=None):
        super().__init__(packages=[package], elements=[])
        self.package = package
        self.scenes = list(scenes)
        self.at = 0
        self.stuck = stuck
        self.refuse = refuse

    @property
    def scene(self):
        return self.scenes[min(self.at, len(self.scenes) - 1)]

    def screenshot(self, max_age=0.0, attempts=2):
        buf = iolib.BytesIO()
        img = Image.new("RGB", self.image, self.scene[0])
        img.paste((250, 250, 250), (0, 0, self.image[0] // 3, self.image[1] // 3))  # something drawn
        img.save(buf, "JPEG")
        return Screenshot(buf.getvalue(), *self.image, *self.device)

    def tap(self, x, y):
        if self.refuse:
            raise GateRefused(self.refuse, "the phone's gatekeeper said no", kind="action")
        super().tap(x, y)
        if not self.stuck:
            self.at += 1


class FakeImages:
    """Stands in for JevImageJudge: J1 from the driver's scene, routine answers scripted."""

    def __init__(self, driver, routines=()):
        self.driver = driver
        self.routines = list(routines)
        self.calls = []
        self.timeout_s = 0.6

    def screen_state(self, package, image):
        self.calls.append("state")
        return ImageAnswer("state", self.driver.scene[2], 0.97, 0.95, 80.0)

    def loading(self, package, image):
        self.calls.append("loading")
        return ImageAnswer("loading", "ready", 0.95, 0.9, 80.0, 0.05)

    def routine(self, package, request, image, actions):
        self.calls.append(("routine", request, tuple(actions)))
        r = self.routines.pop(0) if len(self.routines) > 1 else self.routines[0]
        if isinstance(r, Exception):
            raise r
        return r


def routine(action="start", p=0.95, conf=0.9, box=PLAY_BOX, box_conf=0.9, visible=0.95):
    return RoutineAnswer(action, p, conf, {action: {"box": box, "confidence": box_conf, "visible": visible}}, 90.0)


@pytest.fixture
def ocr_words(monkeypatch, request):
    """OCR on the server: the words of the driver's current scene, each at its own image-pixel box."""
    holder = {}

    def recognize(data, width, height, accurate=True):
        driver = holder.get("driver")
        if driver is None:
            return []
        return [TextBox(w, 0.9, *box) for w, box in driver.scene[1]]

    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text", recognize)
    monkeypatch.setattr("openmob.agent.loop.JEV_READY_POLL_S", 0.01)
    return holder


MENU = ((40, 120, 200), [("SPACE BLOCKS", (150, 200, 390, 240)), ("Play", (240, 545, 300, 590))], "menu")
POPUP = ((200, 40, 40), [("Daily bonus", (150, 300, 390, 340)), ("X", (430, 300, 460, 330))], "dialog")
GAMEPLAY = ((10, 160, 60), [("SCORE 0", (20, 20, 160, 60))], "content")
# The adversarial screen: a big "Play" button whose own words are a purchase.
FAKE_PLAY = ((20, 20, 120), [("PLAY", (240, 520, 300, 560)), ("Buy 500 gems $4.99", (220, 565, 330, 595))], "menu")
FAKE_PLAY_NO_PRICE = ((20, 20, 120), [("PLAY", (240, 520, 300, 560)), ("Buy 500 gems", (220, 565, 330, 595))], "menu")
FAKE_BOX = (0.41, 0.46, 0.61, 0.5)  # Jev boxed the button's "Buy 500 gems" line: device center (540, 1152)
# A "Continue" next to a "Watch ad" button: no money word on screen, but the Box sits on the ad.
WATCH_AD = ((60, 60, 60), [("Level complete", (150, 300, 390, 340)), ("Watch ad", (230, 540, 310, 580)),
                           ("Continue", (230, 700, 310, 740))], "game_over")


def make(tmp_path, ocr_words, scenes, routines, responses, on=True, io=None, driver=None, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver or Scenes(scenes)
    ocr_words["driver"] = driver
    images = FakeImages(driver, routines)
    from openmob.phone.apps import AppResolver

    agent = Agent(ScriptedClient(responses), driver, AppResolver(driver, label_cache=tmp_path / "l.json"), config,
                  io or StepIO(), use_ocr=True, images=images)
    switch = {"on": on}
    agent.faster_mode = lambda: switch["on"]
    return agent, driver, images, switch


DONE = [[tool_use("done", "d", summary="ok", success=True)]]


def jev_rows(agent):
    return [r for r in agent.io.steps if r.get("source") == JEV_TAP_SOURCE]


def last_row(agent, idx):
    return [r for r in agent.io.steps if r["idx"] == idx][-1]


# --- the happy path -------------------------------------------------------------------------------------------


def test_a_routine_start_is_tapped_without_the_main_model(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [routine()], DONE)
    result = agent.run("start a new game")
    assert result.success
    assert len(agent.client.requests) == 1  # only the final "done": the menu step never reached the main model
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert taps == [("tap", 540, 1140)]  # the Box's center in device pixels
    assert ("routine", "start a new game", ("start", "continue", "dismiss")) in images.calls
    row = last_row(agent, jev_rows(agent)[0]["idx"])
    tap = row["jev_tap"]
    assert tap["choice"] == "start" and tap["rule"] == "pass" and tap["tap"] == "tapped"
    assert tap["verify"].startswith(("state:", "frame", "labels")) and tap["box_conf"] == 0.9
    assert row["fast"] is True and row["tool"] == "tap"
    assert result.faster["jev_taps"] == 1 and result.faster["model_calls_skipped"] == 1
    assert "replaced by Jev taps" in result.faster["text"]
    # The main model was told what the helper did.
    assert "quick screenshot helper (Faster mode)" in json.dumps(agent.client.requests[0]["messages"])


def test_a_popup_is_dismissed_then_the_game_started(tmp_path, ocr_words):
    close = routine("dismiss", box=(0.79, 0.25, 0.86, 0.28))
    agent, driver, _, _ = make(tmp_path, ocr_words, [POPUP, MENU, GAMEPLAY], [close, routine()], DONE)
    result = agent.run("start a new game")
    assert result.success and len(agent.client.requests) == 1
    assert len([a for a in driver.actions if a[0] == "tap"]) == 2
    assert result.faster["jev_taps"] == 2


# --- never with Faster mode off, in drive sessions, in ask and plan modes, or with jev_taps off ---------------


def test_no_jev_tap_with_faster_mode_off(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [routine()], DONE, on=False)
    agent.run("start a new game")
    assert images.calls == [] and not [a for a in driver.actions if a[0] == "tap"]
    assert len(agent.client.requests) == 1


def test_no_jev_tap_when_the_switch_is_off(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [routine()], DONE, jev_taps=False)
    agent.run("start a new game")
    assert images.calls == ["state"] and not [a for a in driver.actions if a[0] == "tap"]


@pytest.mark.parametrize("mode", ["ask", "plan"])
def test_no_jev_tap_under_ask_every_step_or_plan_first(tmp_path, ocr_words, mode):
    agent, driver, images, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [routine()], DONE)
    state = _TaskState(task="start a new game", cancel=threading.Event(), perms=TaskPermissions(mode))
    obs = agent._observe_settled(state)
    assert obs.image_state is not None and obs.image_state.value == "menu"
    assert agent._jev_tap_off(state, obs) == "mode"
    assert agent._jev_tap_step(state, obs) is None
    assert not any(isinstance(c, tuple) for c in images.calls) and driver.actions == []


def test_no_jev_tap_in_drive_sessions(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [routine()], DONE)
    state = _TaskState(task="x", cancel=threading.Event(), drive=True, drive_images=True)  # even an allowed key
    obs = agent._observe_settled(state)
    assert agent._jev_tap_off(state, obs) == ""
    assert agent._jev_tap_step(state, obs) is None and driver.actions == []
    from openmob.agent.drive import DriveChannel

    channel = DriveChannel(1, 5, 3, "bot")
    channel.faster_images = True
    agent.run("drive it", drive=channel)
    assert not any(isinstance(c, tuple) for c in images.calls)


@pytest.mark.parametrize("why, kw", [
    ("sensitive app", {"package": "com.chase.sig.android"}),
    ("blocked app", {}),
])
def test_no_jev_tap_on_never_sent_screens(tmp_path, ocr_words, why, kw):
    driver = Scenes([MENU, GAMEPLAY], package=kw.get("package", GAME))
    agent, _, images, _ = make(tmp_path, ocr_words, None, [routine()], DONE, driver=driver)
    blocked = (lambda p: p == GAME) if why == "blocked app" else None
    agent.run("start a new game", blocked=blocked)
    assert images.calls == [] and not [a for a in driver.actions if a[0] == "tap"]


def test_no_jev_tap_on_a_black_secure_frame(tmp_path, ocr_words):
    black = ((0, 0, 0), [], "menu")
    agent, driver, images, _ = make(tmp_path, ocr_words, [black], [routine()], DONE)

    def shot(max_age=0.0, attempts=2):
        buf = iolib.BytesIO()
        Image.new("RGB", driver.image, "black").save(buf, "JPEG")
        return Screenshot(buf.getvalue(), *driver.image, *driver.device)

    driver.screenshot = shot
    agent.run("start a new game")
    assert images.calls == [] and driver.actions[1:] == []


def test_no_jev_tap_on_content_or_low_confidence_state(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [GAMEPLAY], [routine()], DONE)
    agent.run("start a new game")
    assert images.calls == ["state"]  # gameplay: no routine question at all
    agent2, driver2, images2, _ = make(tmp_path, ocr_words, [MENU], [routine()], DONE)
    images2.screen_state = lambda package, image: ImageAnswer("state", "menu", 0.6, 0.4, 80.0)
    agent2.run("start a new game")
    assert not [a for a in driver2.actions if a[0] == "tap"]


# --- the decision: the vocabulary, its confidence, the safe tap rule -------------------------------------------


@pytest.mark.parametrize("answer, skip", [
    (routine("none"), "none"),
    (routine(p=0.79), "unsure"),
    (routine(conf=0.69), "unsure"),
    (routine(box_conf=0.49), "rule"),
    (routine(visible=0.45), "rule"),
    (routine(box=(0.0, 0.0, 0.9, 0.9)), "box_shape"),
    (JevError("Jev returned 500 for an image"), "no_answer"),
])
def test_the_tap_needs_a_sure_action_and_the_safe_tap_rule(tmp_path, ocr_words, answer, skip):
    agent, driver, _, _ = make(tmp_path, ocr_words, [MENU, GAMEPLAY], [answer], DONE + DONE)
    agent.run("start a new game")
    assert not [a for a in driver.actions if a[0] == "tap"]
    assert len(agent.client.requests) == 1  # the main model decided the menu step
    assert agent.io.steps[0]["jev_tap"]["skip"] == skip
    if skip == "rule":
        assert agent.io.steps[0]["jev_tap"]["rule"] == "fail"


def test_the_safe_tap_rule_at_its_threshold():
    assert faster.safe_tap(0.5, 0.5, 0.5) and faster.safe_tap(0.9, 0.5, 0.5)
    assert not faster.safe_tap(0.49, 0.99, 0.5) and not faster.safe_tap(0.99, 0.49, 0.5)


def test_the_criteria_send_purchases_offers_and_ads_to_none():
    none = ROUTINE_ACTIONS["none"].lower()
    for word in ("purchase", "price", "offer", "subscription", "coins or gems for money", "watch an ad", "shop"):
        assert word in none
    assert set(ROUTINE_ACTIONS) == {"start", "continue", "dismiss", "none"}
    assert "not selling anything" in ROUTINE_ACTIONS["dismiss"].lower()


def test_config_refuses_a_looser_tap_rule(tmp_path):
    from openmob.config import load_config

    for bad in ("jev_tap_min_box: 0.3\n", "jev_tap_actions: [buy]\n", "jev_tap_states: [login]\n"):
        path = tmp_path / "c.yaml"
        path.write_text(bad)
        with pytest.raises(ValueError):
            load_config(path)
    cfg = Config()
    assert cfg.jev_taps and cfg.jev_tap_min_box == 0.5 and cfg.jev_tap_max_consecutive == 3


# --- limits: 3 in a row, misses, the gate asking -------------------------------------------------------------


def test_at_most_three_jev_taps_in_a_row(tmp_path, ocr_words):
    menus = [((40 + 30 * i, 120, 200), MENU[1], "menu") for i in range(6)]
    agent, driver, _, _ = make(tmp_path, ocr_words, menus, [routine()],
                               [[tool_use("wait", "w", seconds=0.5)]] + DONE)
    agent.run("start a new game")
    # 3 Jev taps, then the main model's turn (a wait), then Jev may tap again (the 6th is a miss: the last screen).
    first = json.dumps(agent.client.requests[0]["messages"])
    assert first.count("quick screenshot helper (Faster mode)") == 3
    assert any((r.get("jev_tap") or {}).get("skip") == "cap" for r in agent.io.steps)
    assert len([a for a in driver.actions if a[0] == "tap"]) == 6 and len(agent.client.requests) == 2


def test_a_miss_goes_to_the_main_model_and_that_screen_gets_no_second_jev_tap(tmp_path, ocr_words):
    driver = Scenes([MENU], stuck=True)
    agent, _, images, _ = make(tmp_path, ocr_words, None, [routine()],
                               [[tool_use("wait", "w", seconds=0.5)]] + DONE, driver=driver)
    result = agent.run("start a new game")
    taps = [a for a in driver.actions if a[0] == "tap"]
    assert len(taps) == 1  # the miss, then the main model; the same screen isn't tapped by Jev again
    rows = agent.io.steps
    assert last_row(agent, jev_rows(agent)[0]["idx"])["jev_tap"]["verify"] == "miss"
    assert any((r.get("jev_tap") or {}).get("skip") == "failed_here" for r in rows)
    assert result.faster["jev_tap_misses"] == 1 and result.faster["jev_taps"] == 0
    assert len(agent.client.requests) == 2


def test_misses_stop_jev_taps_for_the_task(tmp_path, ocr_words):
    agent, driver, _, _ = make(tmp_path, ocr_words, [MENU], [routine()], DONE)
    state = _TaskState(task="start", cancel=threading.Event(), jev_tap_misses=2)
    obs = agent._observe_settled(state)
    assert agent._jev_tap_off(state, obs) == "misses"


def test_no_jev_tap_in_an_app_where_the_gate_asked(tmp_path, ocr_words):
    agent, driver, _, _ = make(tmp_path, ocr_words, [MENU], [routine()], DONE)
    state = _TaskState(task="start", cancel=threading.Event(), gate_asked={GAME})
    obs = agent._observe_settled(state)
    assert agent._jev_tap_off(state, obs) == "gate_asked"


# --- the malicious screen: a fake "Play" that is really a purchase ---------------------------------------------


def test_a_fake_play_that_is_a_purchase_is_never_jev_tapped(tmp_path, ocr_words):
    agent, driver, images, _ = make(tmp_path, ocr_words, [FAKE_PLAY, GAMEPLAY], [routine()], DONE)
    agent.run("start a new game")
    assert not [a for a in driver.actions if a[0] == "tap"]
    assert agent.io.steps[0]["jev_tap"]["skip"] == "screen_price"  # ruled out before any Jev question
    assert not any(isinstance(c, tuple) for c in images.calls)


def test_a_purchase_word_anywhere_on_screen_rules_a_jev_tap_out(tmp_path, ocr_words):
    # No price on screen, but "Buy 500 gems" under the "PLAY".
    agent, driver, images, _ = make(tmp_path, ocr_words, [FAKE_PLAY_NO_PRICE, GAMEPLAY], [routine(box=FAKE_BOX)], DONE)
    agent.run("start a new game")
    assert not [a for a in driver.actions if a[0] == "tap"]
    assert agent.io.steps[0]["jev_tap"]["skip"] == "screen_money"
    assert not any(isinstance(c, tuple) for c in images.calls)


def test_the_words_in_the_box_rule_an_ad_out(tmp_path, ocr_words):
    # Jev said "continue" but boxed "Watch ad" (a wrong-button Box): its words keep the tap from happening.
    agent, driver, _, _ = make(tmp_path, ocr_words, [WATCH_AD, GAMEPLAY],
                               [routine("continue", box=(0.42, 0.44, 0.58, 0.49))], DONE)
    agent.run("continue to the next level")
    assert not [a for a in driver.actions if a[0] == "tap"]
    assert agent.io.steps[0]["jev_tap"]["skip"] == "box_word:watch"
    # The right Box (on "Continue") is tapped.
    agent2, driver2, _, _ = make(tmp_path, ocr_words, [WATCH_AD, GAMEPLAY],
                                 [routine("continue", box=(0.42, 0.575, 0.58, 0.62))], DONE)
    agent2.run("continue to the next level")
    assert len([a for a in driver2.actions if a[0] == "tap"]) == 1


def test_box_words_catch_offers_and_ads():
    assert faster.tap_words_refusal("Watch ad for 50 gems") == "word:watch"
    assert faster.tap_words_refusal("Claim") == "word:claim"
    assert faster.tap_words_refusal("Special offer") == "word:offer"
    assert faster.tap_words_refusal("€0,99") == "price"
    assert faster.tap_words_refusal("Play") is None and faster.tap_words_refusal("Next level") is None


def test_the_gate_judges_a_jev_tap_like_any_tap(tmp_path, ocr_words, monkeypatch):
    """Even with Jev's own word checks out of the way, the gate sees the OCR words under the point and asks."""
    monkeypatch.setattr(Agent, "_jev_tap_words", lambda self, words, box: None)
    io = StepIO(answers=["no"])
    agent, driver, _, _ = make(tmp_path, ocr_words, [FAKE_PLAY_NO_PRICE, GAMEPLAY], [routine(box=FAKE_BOX)], DONE,
                               io=io)
    result = agent.run("start a new game")
    assert io.questions and "Buy 500 gems" in io.questions[0]  # the owner was asked, by the target's own words
    assert not [a for a in driver.actions if a[0] == "tap"]  # declined: nothing tapped
    row = last_row(agent, jev_rows(agent)[0]["idx"])
    assert row["jev_tap"]["tap"] == "asked" and row["is_error"]
    assert result.faster["jev_tap_misses"] == 1
    assert len(agent.client.requests) == 1  # the main model took over (and finished)


def test_dont_ask_mode_refuses_a_purchase_jev_tap(tmp_path, ocr_words, monkeypatch):
    monkeypatch.setattr(Agent, "_jev_tap_words", lambda self, words, box: None)
    agent, driver, _, _ = make(tmp_path, ocr_words, [FAKE_PLAY_NO_PRICE], [routine(box=FAKE_BOX)], DONE)
    state = _TaskState(task="start a new game", cancel=threading.Event(),
                       perms=TaskPermissions("never", settings=lambda: {"never": True, "never_until": 2e10}))
    obs = agent._observe_settled(state)
    if state.perms.mode() != "never":
        pytest.skip("this build's permission defaults don't allow Don't ask in a unit test")
    tapped = agent._jev_tap_step(state, obs)
    assert tapped is not None and tapped["result"] == "refused"
    assert not [a for a in driver.actions if a[0] == "tap"] and GAME in state.gate_asked


def test_the_phone_gatekeeper_refusing_a_jev_tap_falls_back_to_the_model(tmp_path, ocr_words):
    driver = Scenes([MENU, GAMEPLAY], refuse="gate_denied")
    agent, _, _, _ = make(tmp_path, ocr_words, None, [routine()], DONE, driver=driver)
    result = agent.run("start a new game")
    row = last_row(agent, jev_rows(agent)[0]["idx"])
    assert row["jev_tap"]["tap"] == "refused" and "Refused on the phone" in row["outcome"]
    assert result.faster["jev_tap_misses"] == 1 and len(agent.client.requests) == 1


# --- the request, the image mapping, the record ------------------------------------------------------------------


def test_the_routine_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        start = body.index(b'{"model"')
        seen["request"] = json.loads(body[start:body.index(b"\r\n", start)])
        return httpx.Response(200, json={"answers": {
            "action": {"type": "choice", "choice": "start", "confidence": 0.9, "probabilities": {"start": 0.93}},
            "box_start": {"type": "box", "box": {"left": 0.4, "top": 0.45, "right": 0.6, "bottom": 0.5},
                          "confidence": 0.88},
            "vis_start": {"type": "noul", "noul": 0.97}}})

    client = JevClient("k", http=httpx.Client(base_url="https://api.typesafe.ai", transport=httpx.MockTransport(handler)))
    judge = JevImageJudge(client, "jev-1.14.0", timeout_s=2)
    jpeg = prepare_image(Scenes([MENU]).screenshot().jpeg, 768).jpeg
    answer = judge.routine(GAME, "start a new game", jpeg, ("start",))
    assert answer.action == "start" and answer.probability == 0.93
    assert answer.boxes["start"] == {"box": (0.4, 0.45, 0.6, 0.5), "confidence": 0.88, "visible": 0.97}
    req = seen["request"]
    assert req["model"] == "jev-1.14.0" and req["state"]["user_request"] == "start a new game"
    assert set(req["questions"]) == {"action", "box_start", "vis_start"}
    assert set(req["questions"]["action"]["criteria"]) == {"start", "none"}
    assert req["questions"]["box_start"]["type"] == "box"


def test_padding_is_removed_when_mapping_a_box_back():
    buf = iolib.BytesIO()
    Image.new("RGB", (100, 600), "white").save(buf, "JPEG")
    prepared = prepare_image(buf.getvalue(), 768)  # 100 x 600 (never enlarged), padded to 280 wide
    assert prepared.width == 280 and prepared.off_x == 90
    x, y = prepared.to_source(0.5, 0.5)
    assert abs(x - 50) < 1 and abs(y - 300) < 1
    x, y = prepared.to_source(90 / 280, 0.0)
    assert abs(x) < 1 and abs(y) < 1


def test_history_keeps_the_jev_tap_record(tmp_path):
    history = History(tmp_path / "h.sqlite")
    chat = history.create_chat(None, "dev", "Pixel", "start the game")
    record = {"choice": "start", "rule": "pass", "tap": "tapped", "verify": "frame"}
    history.record_step(chat, "turn1", {"idx": 1, "tool": "tap", "args": {}, "faster": True, "jev_tap": record,
                                        "source": JEV_TAP_SOURCE})
    rows = history.steps(None, "turn1")
    assert json.loads(rows[0]["jev_tap"]) == record and rows[0]["source"] == JEV_TAP_SOURCE


# --- the gap the emulator check found: a model tap on a drawn purchase ----------------------------------------


def scene_words_near(driver):
    """Agent._ocr_near standing in for a crop OCR: the scene's words, in device pixels."""
    from openmob.phone.adb import UIElement

    def near(shot, x, y):
        out = []
        for word, (x1, y1, x2, y2) in driver.scene[1]:
            a, b = shot.to_device(x1, y1)
            c, d = shot.to_device(x2, y2)
            out.append(UIElement(-1, word, "", "", "OCRText", False, None, (a, b, c, d)))
        return out

    return staticmethod(near)


def test_the_crop_ocr_maps_words_back_to_device_pixels(monkeypatch):
    def recognize(data, width, height, accurate=True):
        assert (width, height) == (486, 252)  # the crop around the point, not the whole frame
        return [TextBox("Buy 500 gems", 0.9, 10, 20, 110, 40)]

    monkeypatch.setattr("openmob.agent.loop.ocr.recognize_text", recognize)
    shot = Scenes([MENU]).screenshot()  # 540 x 1200 image of a 1080 x 2400 screen
    words = Agent._ocr_near(shot, 540, 1200)  # image (270, 600); the crop starts at image (27, 474)
    assert [(w.text, w.bounds) for w in words] == [("Buy 500 gems", (74, 988, 274, 1028))]


def test_a_model_tap_on_a_drawn_purchase_asks(tmp_path, ocr_words, monkeypatch):
    """A sparse screen (Chrome's WebView 'Brick Drop' over a canvas): the model taps the drawn PLAY whose button also
    says 'Buy 500 gems $4.99'. The accessibility tree only says 'Brick Drop'; the OCR words near the point make the
    gate ask (2026-10-17: before this, the emulator's offer page was bought twice without a prompt)."""
    from openmob.phone.adb import UIElement

    webview = UIElement(0, "", "Brick Drop", "", "android.webkit.WebView", False, None, (0, 0, 1080, 2400))
    driver = Scenes([FAKE_PLAY, GAMEPLAY])
    driver.elements = [webview]
    monkeypatch.setattr(Agent, "_ocr_near", scene_words_near(driver))
    io = StepIO(answers=["no"])
    tap = [[tool_use("tap", "t", x=270, y=540, target="PLAY")]]
    agent, _, _, _ = make(tmp_path, ocr_words, None, [routine()], tap + DONE, driver=driver, io=io, on=False)
    agent.run("start a new game")
    assert io.questions and "'PLAY'" in io.questions[0] and "4.99" in io.questions[0]  # as a purchase
    assert not [a for a in driver.actions if a[0] == "tap"]
    # Off: the old behaviour (no drawn words), for the record.
    agent2, driver2, _, _ = make(tmp_path, ocr_words, None, [routine()], tap + DONE,
                                 driver=Scenes([FAKE_PLAY, GAMEPLAY]), io=StepIO(), on=False, gate_canvas_words=False)
    agent2.driver.elements = [webview]
    monkeypatch.setattr(Agent, "_ocr_near", scene_words_near(agent2.driver))
    agent2.run("start a new game")
    assert agent2.io.questions == [] and [a for a in driver2.actions if a[0] == "tap"]
