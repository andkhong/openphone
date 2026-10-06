"""Round D latency features: one classifier request per step, no screenshot on fast steps, exact app
names launched directly, and the classifier's own DONE on plain navigation tasks (with its guards)."""

from conftest import RecordingIO, ScriptedClient, text_block, tool_use
from test_fast_path import DISPLAY, DONE, SETTINGS, Phone, el

from openmob.agent import fast
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.jev import JevJudge
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

TITLE = UIElement(0, "Display", "", "title", "TextView", False, None, (40, 120, 400, 200))
DISPLAY_PAGE = [TITLE, el(1, "Brightness level"), el(2, "Dark theme"), el(3, "Screen timeout"),
                el(4, "Navigation bar", bounds=(0, 2200, 1000, 2380))]
LAUNCHER = [el(0, "Phone"), el(1, "Messages"), el(2, "Camera"), el(3, "Chrome")]
PLAIN = {"choice": "app_content", "probabilities": {"app_content": 0.9}, "confidence": 0.9}


class FakeJev:
    """Stands in for the classifier service behind the real JevJudge. `script` answers the operation
    question per request: (operation, target index or None, probability); `done` the "looks done" noul
    (a number, or a list per request); `risk` / `requested` map a target label to its noul."""

    on_usage = None

    def __init__(self, script, done=0.0, risk=None, requested=None, confidence=0.9):
        self.script = list(script)
        self.done = done
        self.risk, self.requested = risk or {}, requested or {}
        self.confidence = confidence
        self.sent = []

    def ask(self, state, questions):
        self.sent.append((state, questions))
        answers = {}
        step = None
        for qid, q in questions.items():
            if qid == "state":
                answers[qid] = PLAIN
            elif qid == "done":
                done = self.done.pop(0) if isinstance(self.done, list) and len(self.done) > 1 else (
                    self.done[0] if isinstance(self.done, list) else self.done)
                answers[qid] = {"noul": done}
            elif qid == "operation":
                step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
                op, _, p = step
                answers[qid] = {"choice": op, "probabilities": {op: p}, "confidence": self.confidence}
            elif qid.startswith("risky_") or qid.startswith("requested_"):
                table = self.risk if qid.startswith("risky_") else self.requested
                answers[qid] = {"noul": table.get(q["instructions"]["tap_target"], 0.0)}
            elif qid in ("risky", "requested"):  # the separate risky-tap check
                table = self.risk if qid == "risky" else self.requested
                answers[qid] = {"noul": table.get(state["tap_target"].split()[-1], 0.0)}
        for qid in questions:
            if qid.endswith("_target") and step and step[1] is not None:
                answers[qid] = {"choice": str(step[1]), "probabilities": {str(step[1]): step[2]}, "confidence": 0.9}
        return answers

    def requests(self, kind):
        """How many requests asked `kind`: merged (operation + risk heads) or tap (the separate check)."""
        if kind == "tap":
            return sum("risky" in q for _, q in self.sent)
        return sum("operation" in q and "state" in q for _, q in self.sent)


class CountingPhone(Phone):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.shots = 0

    def screenshot(self, max_age=0.0, attempts=2):
        self.shots += 1
        return super().screenshot(max_age, attempts)


def make(responses, driver, jev, tmp_path, io=None, labels=None, **cfg):
    config = Config(**{"settle_delay": 0, "launch_settle_delay": 0, "fast_path": "on", **cfg})
    client = ScriptedClient(responses)
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    resolver.labels = dict(labels or {})
    agent = Agent(client, driver, resolver, config, io, judge=JevJudge(jev), use_ocr=False)
    return agent, client, steps


# --- 3. one classifier request per step -----------------------------------------------------


def test_a_fast_step_makes_one_classifier_request(tmp_path):
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    jev = FakeJev([("TAP", 1, 0.95), ("DONE", None, 0.95)])
    agent, client, steps = make([DONE], driver, jev, tmp_path)
    result = agent.run("open display settings")

    assert result.success and ("tap", 500, 140) in driver.actions
    assert len(jev.sent) == 2  # one per screen seen: Settings, then Display. No separate tap check.
    assert jev.requests("tap") == 0 and jev.requests("merged") == 2
    state, questions = jev.sent[0]
    assert {"state", "done", "operation", "tap_target", "risky_1"} <= set(questions)
    assert questions["risky_1"]["instructions"]["tap_target"] == "Display"
    assert not any(k.startswith("requested_") for k in questions)  # no confirm keyword on screen
    assert state["recent_actions"] == []
    # The second request already knew the tap changed the screen.
    assert jev.sent[1][0]["recent_actions"] == [{"action": "tap [1] 'Display'", "screen_changed": True}]
    assert steps[0]["jev_ms"] >= 0 and steps[0]["model_ms"] == 0


def test_merged_risk_still_asks_before_a_keyword_tap(tmp_path):
    screen = [el(0, "Message"), el(1, "Send", cls="Button"), el(2, "Attach"), el(3, "Emoji")]
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 1, 0.99)])
    agent, client, steps = make([DONE], Phone(screen), jev, tmp_path, io=io)
    agent.run("open the chat")
    assert io.questions and "Send" in io.questions[0]
    assert not any(a[0] == "tap" for a in agent.driver.actions)
    assert "requested_1" in jev.sent[0][1]  # 'Send' names a confirm keyword: its "requested" is asked too
    assert jev.requests("tap") == 0


def test_merged_risk_alone_makes_a_tap_risky(tmp_path):
    screen = [el(0, "Message"), el(1, "Confirm", cls="Button"), el(2, "Attach"), el(3, "Emoji")]
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 1, 0.99)], risk={"Confirm": 0.9})
    agent, _, _ = make([DONE], Phone(screen), jev, tmp_path, io=io)
    agent.run("open the chat")
    assert io.questions and "Confirm" in io.questions[0] and jev.requests("tap") == 0


def test_merged_requested_can_veto_but_never_grant(tmp_path):
    # A send, not a purchase: money asks in auto even when requested (see test_permissions.py).
    screen = [el(0, "Gems"), el(1, "Send gift", cls="Button"), el(2, "Back"), el(3, "Help")]
    # Asked for, and the classifier agrees: no question.
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 1, 0.99), ("DONE", None, 0.99)], requested={"Send gift": 0.9})
    agent, _, _ = make([DONE], Phone(screen), jev, tmp_path, io=io)
    agent.run("send the gift")
    assert not io.questions and ("tap", 500, 140) in agent.driver.actions
    # Asked for, but the classifier doubts it: ask (a veto).
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 1, 0.99)], requested={"Send gift": 0.1})
    agent, _, _ = make([DONE], Phone(screen), jev, tmp_path, io=io)
    agent.run("send the gift")
    assert io.questions
    # Not asked for: the classifier's "requested" can't grant it.
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 1, 0.99)], requested={"Send gift": 1.0})
    agent, _, _ = make([DONE], Phone(screen), jev, tmp_path, io=io)
    agent.run("open the shop")
    assert io.questions and jev.requests("tap") == 0


def test_a_target_without_a_risk_head_gets_the_separate_check(tmp_path):
    screen = [el(0, "Network"), el(1, "Display"), el(2, "Sound"), el(3, "Delete account")]
    io = RecordingIO(answers=["no"])
    jev = FakeJev([("TAP", 3, 0.99)], risk={"account": 0.9})
    agent, _, _ = make([DONE], Phone(screen), jev, tmp_path, io=io, jev_risk_heads=2)
    agent.run("open account settings")
    assert set(k for k in jev.sent[0][1] if k.startswith("risky_")) == {"risky_0", "risky_1"}
    assert jev.requests("tap") == 1  # [3] wasn't asked about: assess_tap, as before
    assert io.questions  # 'delete' is a confirm keyword either way


def test_a_failed_or_malformed_request_goes_to_the_vision_step(tmp_path):
    from openmob.jev import JevError

    class Broken(FakeJev):
        def __init__(self, error):
            super().__init__([("TAP", 1, 0.99)])
            self.error = error

        def ask(self, state, questions):
            if "operation" in questions:
                raise self.error
            return super().ask(state, questions)

    for error in (JevError("timeout"), KeyError("operation")):
        driver = Phone(SETTINGS)
        agent, client, steps = make([DONE], driver, Broken(error), tmp_path)
        result = agent.run("open display settings")
        assert result.success and len(client.requests) == 1 and not any(a[0] == "tap" for a in driver.actions)


def test_the_separate_requests_return_when_the_feature_is_off(tmp_path):
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    jev = FakeJev([("TAP", 1, 0.95), ("DONE", None, 0.95)])
    agent, _, _ = make([DONE], driver, jev, tmp_path, jev_one_request=False)
    agent.run("open display settings")
    # assess_screen and next_step on Settings, assess_tap for the tap, assess_screen and next_step on Display.
    assert jev.requests("merged") == 0 and jev.requests("tap") == 1 and len(jev.sent) == 5


def test_vision_taps_reuse_the_merged_risk(tmp_path):
    jev = FakeJev([("ESCALATE", None, 0.99)])
    tap = [tool_use("tap_element", "a", index=1, target="Display")]
    agent, client, _ = make([tap, DONE], Phone(SETTINGS, {"Display": DISPLAY}), jev, tmp_path)
    agent.run("open display settings")
    assert ("tap", 500, 140) in agent.driver.actions and jev.requests("tap") == 0
    # In the model's own words: the separate check, with those words, as before.
    jev = FakeJev([("ESCALATE", None, 0.99)])
    tap = [tool_use("tap_element", "a", index=1, target="the display row")]
    agent, client, _ = make([tap, DONE], Phone(SETTINGS, {"Display": DISPLAY}), jev, tmp_path)
    agent.run("open display settings")
    assert jev.requests("tap") == 1


# --- 4. no screenshot on fast steps -----------------------------------------------------------


def test_fast_steps_take_no_screenshot_and_vision_steps_do(tmp_path):
    driver = CountingPhone(SETTINGS, {"Display": DISPLAY})
    jev = FakeJev([("TAP", 1, 0.95), ("DONE", None, 0.95)])
    agent, client, steps = make([DONE], driver, jev, tmp_path)
    agent.run("open display settings")
    assert driver.shots == 1  # only for the vision step that confirmed DONE
    request = client.requests[0]["messages"][0]["content"]
    assert sum(b.get("type") == "image" for b in request) == 1  # the current screen, with its screenshot
    assert any(b.get("type") == "text" and "Brightness level" in b["text"] for b in request)
    assert steps[0]["screen_w"] in (None, 1080)  # recorded from a recent frame when there is one


def test_an_escalation_takes_the_screenshot(tmp_path):
    driver = CountingPhone(SETTINGS)
    jev = FakeJev([("ESCALATE", None, 0.99)])
    agent, client, _ = make([DONE], driver, jev, tmp_path)
    agent.run("open display settings")
    assert driver.shots == 1 and any(b.get("type") == "image" for b in client.requests[0]["messages"][0]["content"])


def test_screenshots_every_step_without_lazy_screenshots(tmp_path):
    driver = CountingPhone(SETTINGS, {"Display": DISPLAY})
    jev = FakeJev([("TAP", 1, 0.95), ("DONE", None, 0.95)])
    agent, _, _ = make([DONE], driver, jev, tmp_path, fast_path_lazy_screenshot=False)
    agent.run("open display settings")
    assert driver.shots == 2


def test_screens_without_elements_still_screenshot_for_ocr(tmp_path):
    class Game(CountingPhone):
        pass

    driver = Game([el(0, "", cls="SurfaceView")])
    jev = FakeJev([("TAP", 0, 0.99)])
    agent, client, _ = make([DONE], driver, jev, tmp_path)
    agent.use_ocr = True
    import openmob.agent.loop as loop

    seen = []
    original = loop.ocr.recognize_text
    loop.ocr.recognize_text = lambda jpeg, w, h: seen.append(len(jpeg)) or []
    try:
        agent.run("open the menu")
    finally:
        loop.ocr.recognize_text = original
    assert seen and driver.shots == 1  # OCR read the screenshot; the vision step reused it


def test_a_fast_scroll_measures_the_screen(tmp_path):
    driver = CountingPhone(SETTINGS)
    jev = FakeJev([("SCROLL_DOWN", None, 0.95), ("ESCALATE", None, 0.99)])
    agent, _, _ = make([DONE], driver, jev, tmp_path)
    agent.run("find battery settings")
    swipe = [a for a in driver.actions if a[0] == "swipe"][0]
    assert swipe[1] == 540 and swipe[2] > swipe[4]  # the real screen's center, scrolling down


# --- 2. exact app names launch without the text model -----------------------------------------


def test_an_exact_app_name_launches_without_the_text_model(tmp_path):
    driver = Phone(LAUNCHER)
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    labels = {"com.android.settings": "Settings", "com.google.android.youtube": "YouTube"}
    agent, client, steps = make([DONE], driver, jev, tmp_path, labels=labels)
    agent.run("Open Settings and go to Display")
    assert ("launch", "com.android.settings") in driver.actions
    assert all(r["model"] != agent.config.resolver_model for r in client.requests)  # no text model call
    assert steps[0]["tool"] == "launch_app" and steps[0]["fast"] and steps[0]["model_ms"] == 0


def test_names_fold_case_and_diacritics(tmp_path):
    driver = Phone(LAUNCHER, packages=["com.cafe.app", "com.android.settings"])
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    agent, client, _ = make([DONE], driver, jev, tmp_path, labels={"com.cafe.app": "Café Délice"})
    agent.run("open cafe delice")
    assert ("launch", "com.cafe.app") in driver.actions and len(client.requests) == 1


def test_an_ambiguous_name_asks_the_text_model(tmp_path):
    driver = Phone(LAUNCHER, packages=["com.samsung.music", "com.google.music"])
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    labels = {"com.samsung.music": "Music", "com.google.music": "Music"}
    agent, client, _ = make([[text_block('{"app": "com.google.music"}')], DONE], driver, jev, tmp_path, labels=labels)
    agent.run("open music")
    assert client.requests[0]["model"] == agent.config.resolver_model  # the text model chose
    assert ("launch", "com.google.music") in driver.actions


def test_a_name_that_matches_no_label_asks_the_text_model(tmp_path):
    driver = Phone(LAUNCHER)
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    agent, client, _ = make([[text_block('{"app": "YouTube"}')], DONE], driver, jev, tmp_path,
                            labels={"com.google.android.youtube": "YouTube"})
    agent.run("play some videos")
    assert client.requests[0]["model"] == agent.config.resolver_model


def test_direct_launch_can_be_switched_off(tmp_path):
    driver = Phone(LAUNCHER)
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    agent, client, _ = make([[text_block('{"app": "Settings"}')], DONE], driver, jev, tmp_path,
                            labels={"com.android.settings": "Settings"}, fast_path_direct_launch=False)
    agent.run("open settings")
    assert client.requests[0]["model"] == agent.config.resolver_model


def test_a_blocked_app_stays_blocked_on_a_direct_launch(tmp_path):
    driver = Phone(LAUNCHER)
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.99)])
    agent, client, steps = make([DONE], driver, jev, tmp_path, labels={"com.android.settings": "Settings"})
    agent.run("open settings", blocked=lambda p: p == "com.android.settings")
    assert not any(a[0] == "launch" for a in driver.actions) and steps[0]["is_error"]


# --- 1. the classifier's DONE -----------------------------------------------------------------


def done_run(tmp_path, task="Open Settings and go to Display", page=DISPLAY_PAGE, script=None, done=0.95,
             responses=None, io=None, **cfg):
    driver = Phone(SETTINGS, {"Display": page})
    jev = FakeJev(script or [("TAP", 1, 0.95), ("DONE", None, 0.95)], done=done)
    agent, client, steps = make(responses or [[tool_use("done", "v", summary="Vision says done.", success=True)]],
                                driver, jev, tmp_path, io=io, **{"fast_path_done": True, **cfg})
    result = agent.run(task)
    return result, client, steps


def test_a_plain_navigation_task_finishes_on_the_classifier(tmp_path):
    result, client, steps = done_run(tmp_path, done=[0.1, 0.95])
    assert result.success and result.done_via == "classifier" and result.summary == "Display is open."
    assert client.requests == []  # no vision step at all
    tap, done = steps
    assert done["tool"] == "done" and done["fast"] and done["done_via"] == "classifier"
    assert done["variant"].endswith("+fastdone")


def test_vision_confirms_when_fast_done_is_off(tmp_path):
    result, client, steps = done_run(tmp_path, fast_path_done=False)
    assert len(client.requests) == 1 and result.done_via == "vision" and steps[-1]["done_via"] == "vision"


def test_side_effect_words_need_vision(tmp_path):
    # Each would pass every other check (the last clause still asks for Display).
    for task in ("Open Settings to send feedback and go to Display", "Open Settings to pay and go to Display",
                 "Open Settings, turn off wifi, and go to Display", "Book a table, then go to Display"):
        result, client, _ = done_run(tmp_path, task=task)
        assert len(client.requests) == 1 and result.done_via == "vision", task


def test_low_probability_needs_vision(tmp_path):
    result, client, _ = done_run(tmp_path, done=0.7)  # the "looks complete" head is unsure
    assert len(client.requests) == 1 and result.done_via == "vision"
    result, client, _ = done_run(tmp_path, script=[("TAP", 1, 0.95), ("DONE", None, 0.8)])  # DONE itself
    assert len(client.requests) == 1


def test_a_screen_without_the_title_needs_vision(tmp_path):
    # 'Display' only as a tappable row (a way there, not the place), or no 'Display' at all.
    row = UIElement(0, "", "", "row", "LinearLayout", True, None, (0, 100, 1000, 200))
    label = UIElement(1, "Display", "", "title", "TextView", False, None, (40, 120, 400, 180))
    bottom = el(4, "Help", bounds=(0, 2200, 1000, 2380))
    for page in ([row, label, el(2, "Sound"), el(3, "Battery"), bottom], DISPLAY):
        result, client, _ = done_run(tmp_path, page=page)
        assert len(client.requests) == 1 and result.done_via == "vision"


def test_a_risky_step_needs_vision(tmp_path):
    driver = Phone(SETTINGS, {"Display": DISPLAY_PAGE})
    jev = FakeJev([("TAP", 1, 0.95), ("DONE", None, 0.95)], risk={"Display": 0.9}, done=0.95)
    io = RecordingIO(answers=["yes"])
    agent, client, _ = make([[tool_use("done", "v", summary="ok", success=True)]], driver, jev, tmp_path, io=io,
                            fast_path_done=True)
    result = agent.run("Open Settings and go to Display")
    assert io.questions and len(client.requests) == 1 and result.done_via == "vision"


def test_typing_needs_vision(tmp_path):
    box = el(0, "Search settings", cls="EditText", editable=True)
    driver = Phone([box, el(1, "Network"), el(2, "Sound"), el(3, "Battery")])
    driver.screens = {}
    jev = FakeJev([("TYPE_TEXT", 0, 0.95), ("DONE", None, 0.95)], done=0.95)

    class Typing(Phone):
        pass

    agent, client, _ = make([[text_block('{"text": "display"}')], [tool_use("done", "v", summary="ok")]],
                            driver, jev, tmp_path, fast_path_done=True)
    driver.type_text = lambda text: (driver.actions.append(("type", text)), setattr(driver, "elements", DISPLAY_PAGE))
    result = agent.run("Open Display")
    assert ("type", "display") in driver.actions
    assert result.done_via == "vision" and len(client.requests) == 2  # the text model, then vision


def test_an_escalated_task_needs_vision(tmp_path):
    tap = [tool_use("tap_element", "a", index=1, target="Display")]  # the vision model took a step
    driver = Phone(SETTINGS, {"Display": DISPLAY_PAGE})
    jev = FakeJev([("ESCALATE", None, 0.99), ("DONE", None, 0.95)], done=0.95)
    agent, client, _ = make([tap, [tool_use("done", "v", summary="ok")]], driver, jev, tmp_path, fast_path_done=True)
    result = agent.run("Open Settings and go to Display")
    assert len(client.requests) == 2 and result.done_via == "vision"


def test_a_correction_needs_vision(tmp_path):
    class IO(RecordingIO):
        def __init__(self):
            super().__init__()
            self.pending = ["actually the display one"]

        def take_corrections(self):
            out, self.pending = self.pending, []
            return out

    result, client, _ = done_run(tmp_path, io=IO())
    assert len(client.requests) == 1 and result.done_via == "vision"


def test_open_an_app_finishes_when_it_is_in_front(tmp_path):
    driver = Phone(LAUNCHER)
    jev = FakeJev([("LAUNCH_APP", None, 0.95), ("DONE", None, 0.95)], done=0.95)
    driver_screens = {"com.android.settings": SETTINGS}

    def launch(package):
        driver.actions.append(("launch", package))
        driver.package = package
        driver.elements = list(driver_screens[package])

    driver.launch_app = launch
    agent, client, _ = make([DONE], driver, jev, tmp_path, labels={"com.android.settings": "Settings"},
                            fast_path_done=True)
    result = agent.run("open Settings")
    assert result.success and result.done_via == "classifier" and client.requests == []
    assert result.summary == "Settings is open."


# --- the request and screen checks ------------------------------------------------------------


def test_reading_the_request():
    kw = Config().confirm_keywords
    assert fast.navigation_only("Open Settings and go to Display", kw)
    assert fast.navigation_only("please open the camera", kw)
    for task in ("Open WhatsApp and send hi to Sam", "turn on wifi", "Open settings and change the font",
                 "search youtube for cats", "Open Settings\nUser correction: the other one", "", "what time is it"):
        assert not fast.navigation_only(task, kw), task
    assert fast.key_target("Open Settings and go to Display") == "Display"
    assert fast.key_target("go to Display in Settings") == "Display"
    assert fast.key_target("open the Display settings") == "Display settings"
    assert fast.app_phrases("Open Settings and go to Display") == ["Settings", "Display"]
    assert "Settings" in fast.app_phrases("go to Display in Settings")
    assert "Spotify" in fast.app_phrases("launch the Spotify app")


def test_the_title_check():
    assert fast.shows_target(DISPLAY_PAGE, "Display") is TITLE
    assert fast.shows_target(DISPLAY_PAGE, "Display settings") is TITLE  # "settings" is generic
    assert fast.shows_target(DISPLAY, "Display") is None
    assert fast.shows_target(SETTINGS, "Display") is None  # a tappable row
    low = UIElement(5, "Display", "", "", "TextView", False, None, (0, 2000, 400, 2080))
    assert fast.shows_target([low, el(1, "x", bounds=(0, 2300, 10, 2400))], "Display") is None  # not near the top
    switch = UIElement(0, "Dark theme", "", "switch_widget", "Switch", True, True, (0, 0, 100, 100))
    assert fast.shows_target([switch], "Dark theme") is None  # a checked switch is a setting, not a page
    tab = UIElement(0, "Library", "", "tab_library", "Tab", True, True, (0, 2200, 300, 2300))
    assert fast.shows_target([tab], "Library") is tab  # a selected tab


def test_variant_names_each_feature(tmp_path):
    driver = Phone(SETTINGS)
    jev = FakeJev([("ESCALATE", None, 0.99)])
    agent, _, steps = make([DONE], driver, jev, tmp_path, fast_path_done=True, settle_quiet_ms=200)
    agent.run("x")
    assert steps[0]["variant"] == "adaptive+q200+fast+noshot+1req+launch+fastdone"
    agent, _, steps = make([DONE], driver, jev, tmp_path, fast_path_lazy_screenshot=False, jev_one_request=False,
                           fast_path_direct_launch=False)
    agent.run("x")
    assert steps[0]["variant"] == "adaptive+fast"


def test_done_via_has_a_steps_column(tmp_path):
    from openmob.history import History

    h = History(tmp_path / "h.sqlite")
    assert "done_via" in [r[1] for r in h._db.execute("PRAGMA table_info(steps)")]


def test_round_switches_from_the_environment(tmp_path, monkeypatch):
    from openmob.config import load_config

    path = tmp_path / "c.yaml"
    path.write_text("model: claude-sonnet-5-5\n")
    cfg = load_config(path)
    assert (cfg.fast_path_lazy_screenshot, cfg.jev_one_request, cfg.fast_path_direct_launch, cfg.fast_path_done,
            cfg.settle_quiet_ms) == (True, True, True, False, 300)
    monkeypatch.setenv("OPENMOB_ROUND_D", "off")
    cfg = load_config(path)
    assert not (cfg.fast_path_lazy_screenshot or cfg.jev_one_request or cfg.fast_path_direct_launch or cfg.fast_path_done)
    monkeypatch.setenv("OPENMOB_ROUND_D", "on")
    monkeypatch.setenv("OPENMOB_SETTLE_QUIET_MS", "200")
    cfg = load_config(path)
    assert cfg.fast_path_done and cfg.jev_one_request and cfg.settle_quiet_ms == 200
    monkeypatch.setenv("OPENMOB_FAST_DONE", "off")
    assert not load_config(path).fast_path_done


def test_the_one_request_carries_every_head():
    jev = FakeJev([("TAP", 2, 0.9)], done=0.4, risk={"Pay": 0.8}, requested={"Pay": 0.2})
    verdict = JevJudge(jev).observe_step("open settings", {"foreground_app": "x", "elements": []}, [],
                                         {"TAP": {"1": "[1] a", "2": "[2] Pay"}, "TYPE_TEXT": {}},
                                         {"1": "a", "2": "Pay"}, {"2": "Pay"})
    assert len(jev.sent) == 1
    assert (verdict.assessment.state, verdict.assessment.task_done) == ("app_content", 0.4)
    assert verdict.choice.operation == "TAP" and verdict.choice.targets["TAP"][0] == "2"
    assert verdict.risks == {"1": 0.0, "2": 0.8} and verdict.requested == {"2": 0.2}
    _, questions = jev.sent[0]
    assert questions["risky_2"]["type"] == "noul" and "`tap_target`" in questions["risky_2"]["instructions"]["question"]
