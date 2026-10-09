"""Replaying stored UI paths (replay_paths, off by default): what is stored, when a stored step runs without
the main model, and that every check still applies to it. Fake phones and scripted clients only."""

import time
from pathlib import Path

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from test_fast_path import DISPLAY, DONE, SETTINGS, Phone, el

from openmob.agent import replay
from openmob.agent.loop import Agent
from openmob.config import Config, load_config
from openmob.history import History
from openmob.jev import ScreenAssessment
from openmob.permissions import TaskPermissions
from openmob.phone.apps import AppResolver

TAP_DISPLAY = [tool_use("tap_element", "a", index=1, target="Display")]
TASK = "open display settings"
LAUNCHER = "com.android.launcher3"  # the fake phone's app in front


def make(responses, driver, tmp_path, judge=None, io=None, paths=None, **cfg):
    config = Config(**{"settle_delay": 0, "launch_settle_delay": 0, "replay_paths": True, **cfg})
    client = ScriptedClient(responses)
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    agent = Agent(client, driver, resolver, config, io, judge=judge, use_ocr=False, paths=paths)
    return agent, client, steps


def learned(tmp_path, store=None):
    """A store holding the path of one cold run of TASK: tap 'Display' on the Settings list."""
    store = replay.PathStore() if store is None else store  # an empty store is falsy (its length)
    agent, _, _ = make([TAP_DISPLAY, DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store)
    assert agent.run(TASK).success and len(store) == 1
    return store


def taps(driver):
    return [a for a in driver.actions if a[0] == "tap"]


# --- the switch ------------------------------------------------------------------------------


def test_off_by_default_nothing_is_stored_or_read(tmp_path):
    assert Config().replay_paths is False
    store = replay.PathStore()
    store.put(replay.goal_key(TASK), [replay.recordable("tap_element", {"index": 1}, LAUNCHER, SETTINGS, None)])
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    agent, client, steps = make([TAP_DISPLAY, DONE], driver, tmp_path, paths=store, replay_paths=False)
    result = agent.run(TASK)
    assert result.success and result.replayed == 0 and len(client.requests) == 2  # the model tapped, not the path
    assert "source" not in steps[0] and "+replay" not in steps[0]["variant"]
    assert len(store) == 1 and store.get(replay.goal_key(TASK))[0]["screen"]  # untouched
    agent, _, _ = make([TAP_DISPLAY, DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, replay_paths=False)
    assert agent.paths is None


def test_the_environment_can_turn_the_switch_on_or_off(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: claude-sonnet-5-5\n")
    monkeypatch.delenv("OPENMOB_REPLAY", raising=False)
    assert load_config(cfg_file).replay_paths is False
    monkeypatch.setenv("OPENMOB_REPLAY", "on")
    assert load_config(cfg_file).replay_paths is True
    cfg_file.write_text("replay_paths: true\n")
    monkeypatch.setenv("OPENMOB_REPLAY", "off")
    assert load_config(cfg_file).replay_paths is False


# --- storing ---------------------------------------------------------------------------------


def test_a_successful_navigation_task_stores_its_path_without_request_text(tmp_path):
    store = replay.PathStore(tmp_path / "paths.json")
    learned(tmp_path, store)
    path = store.get(replay.goal_key(TASK))
    assert [s["tool"] for s in path] == ["tap_element"]  # done is never stored
    assert path[0]["target"] == {"text": "Display", "desc": "", "resource_id": "", "cls": "TextView"}
    assert "index" not in path[0]["target"] and path[0]["package"] == LAUNCHER
    assert path[0]["screen"] == sorted(replay.fingerprint(SETTINGS)) and len(path[0]["screen"]) == 4
    text = (tmp_path / "paths.json").read_text()
    assert "display settings" not in text.lower()  # the request is a hash
    assert "Network & internet" not in text  # the other labels are hashes too; the tapped one is the anchor
    assert all(len(token) == 12 for token in path[0]["screen"])


def test_only_successful_navigation_tasks_without_typing_prompts_or_risk_are_stored(tmp_path):
    store = replay.PathStore()
    failed = [tool_use("done", "d", summary="Not there.", success=False)]
    agent, _, _ = make([TAP_DISPLAY, failed], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store)
    agent.run(TASK)
    assert len(store) == 0, "a failed task"
    agent, _, _ = make([TAP_DISPLAY, DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store)
    agent.run("turn on dark mode in display settings")
    assert len(store) == 0, "a request that changes something"
    field = el(0, "Search settings", cls="EditText", editable=True)
    agent, _, _ = make([[tool_use("tap_element", "a", index=0, target="Search")],
                        [tool_use("type_text", "b", text="display")], DONE],
                       Phone([field, el(1, "Display")]), tmp_path, paths=store)
    agent.run(TASK)
    assert len(store) == 0, "typed"
    io = RecordingIO(answers=["yes"])
    agent, _, _ = make([[tool_use("tap_element", "a", index=1, target="Send")], DONE],
                       Phone([el(0, "Chats"), el(1, "Send", cls="Button")]), tmp_path, io=io, paths=store)
    agent.run("open the chat")
    assert io.questions and len(store) == 0, "asked"


def test_isolated_tasks_replay_but_do_not_store(tmp_path):
    store = replay.PathStore()
    agent, _, _ = make([TAP_DISPLAY, DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store)
    assert agent.run(TASK, isolated=True).success and len(store) == 0  # an API key's task: not stored (owner's call)
    learned(tmp_path, store)
    agent, client, _ = make([DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store)
    assert agent.run(TASK, isolated=True).replayed == 1 and len(client.requests) == 1


# --- replaying -------------------------------------------------------------------------------


def test_a_second_run_replays_the_tap_without_the_vision_model(tmp_path):
    store = learned(tmp_path)
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    io = RecordingIO()
    agent, client, steps = make([DONE], driver, tmp_path, io=io, paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 1 and result.fast_steps == 1
    assert taps(driver) == [("tap", 500, 140)]  # the element's own center
    assert len(client.requests) == 1  # only the done
    tap, done = steps
    assert tap["source"] == "replay" and tap["fast"] is True and tap["model_ms"] == 0
    assert tap["variant"].endswith("+replay+links") and done["fast"] is False and "source" not in done
    first = client.requests[0]["messages"][0]["content"]
    assert any("quick helper" in b.get("text", "") and "tap_element" in b.get("text", "") for b in first)
    assert ("action", "tap_element: Tapped [1] 'Display'. (replayed)") in io.events


def test_the_target_is_tapped_where_it_is_now_not_where_it_was(tmp_path):
    store = learned(tmp_path)
    moved = [el(0, "Promo banner"), el(1, "Network & internet"), el(2, "Display"), el(3, "Sound"), el(4, "Battery")]
    driver = Phone(moved, {"Display": DISPLAY})
    agent, client, steps = make([DONE], driver, tmp_path, paths=store)
    result = agent.run(TASK)
    assert result.replayed == 1 and taps(driver) == [("tap", 500, 240)]
    assert steps[0]["args"] == {"index": 2, "target": "Display"}


def test_a_screen_that_does_not_match_the_path_goes_to_the_model(tmp_path):
    store = learned(tmp_path)
    other = [el(0, "Display"), el(1, "Wallpaper"), el(2, "Themes"), el(3, "Widgets")]  # 'Display' is there, the screen isn't
    driver = Phone(other, {"Display": DISPLAY})
    agent, client, steps = make([[tool_use("tap_element", "a", index=0, target="Display")], DONE], driver, tmp_path, paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 0 and len(client.requests) == 2
    assert "source" not in steps[0]


def test_a_replayed_step_that_changes_nothing_hands_over_to_the_model(tmp_path):
    store = replay.PathStore()
    store.put(replay.goal_key(TASK), [replay.recordable("scroll", {"direction": "down"}, LAUNCHER, SETTINGS, None),
                                      replay.recordable("tap_element", {"index": 1}, LAUNCHER, SETTINGS, None)])
    driver = Phone(SETTINGS, {"Display": DISPLAY})  # scrolling changes nothing on this phone
    agent, client, steps = make([TAP_DISPLAY, DONE], driver, tmp_path, paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 1 and len(client.requests) == 2
    assert steps[0]["tool"] == "scroll" and steps[0]["source"] == "replay"
    assert steps[1]["tool"] == "tap_element" and "source" not in steps[1]  # the model's tap, not the path's


def test_a_correction_mid_task_drops_the_path(tmp_path):
    store = learned(tmp_path)

    class SteeringIO(RecordingIO):
        def __init__(self):
            super().__init__()
            self.pending = ["no, go to Sound instead"]

        def take_corrections(self):
            out, self.pending = self.pending, []
            return out

    driver = Phone(SETTINGS, {"Sound": DISPLAY})
    agent, client, _ = make([[tool_use("tap_element", "a", index=2, target="Sound")], DONE], driver, tmp_path,
                            io=SteeringIO(), paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 0 and len(client.requests) == 2
    assert store.get(replay.goal_key(TASK))[0]["target"]["text"] == "Display"  # the corrected run didn't replace it


def test_no_replay_on_a_screen_the_classifier_calls_a_popup(tmp_path):
    class PopupJudge:
        def assess_screen(self, request, screen):
            return ScreenAssessment("popup", 0.95, 0.9, 0.0)

        def assess_tap(self, request, target, screen):
            return 0.0, 0.0

    store = learned(tmp_path)
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    agent, client, _ = make([TAP_DISPLAY, DONE], driver, tmp_path, judge=PopupJudge(), paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 0 and len(client.requests) == 2


# --- every check still applies -----------------------------------------------------------------


def test_a_replayed_tap_still_asks_and_a_no_stands(tmp_path):
    trash = [el(0, "Inbox"), el(1, "Delete all", cls="Button"), el(2, "Spam"), el(3, "Drafts")]
    store = replay.PathStore()
    store.put(replay.goal_key("open the trash"), [replay.recordable("tap_element", {"index": 1}, LAUNCHER, trash, None)])
    io = RecordingIO(answers=["no"])
    driver = Phone(trash)
    agent, client, steps = make([[tool_use("done", "d", summary="Left it.", success=False)]], driver, tmp_path,
                                io=io, paths=store)
    result = agent.run("open the trash")
    assert io.questions and "Delete all" in io.questions[0]
    assert not taps(driver) and result.replayed == 1
    assert steps[0]["source"] == "replay" and steps[0]["is_error"] and "did not approve" in steps[0]["outcome"]
    assert len(client.requests) == 1  # the model took over


def test_a_replayed_launch_of_a_blocked_app_is_refused(tmp_path):
    home = [el(0, "Phone"), el(1, "Messages"), el(2, "Chess"), el(3, "Chrome")]
    store = replay.PathStore()
    store.put(replay.goal_key("open chess"), [replay.recordable("launch_app", {"app": "Chess"}, LAUNCHER, home, "com.chess")])
    driver = Phone(home, packages=["com.chess", "com.android.settings"], labels={"com.chess": "Chess"})
    agent, client, steps = make([[tool_use("done", "d", summary="Blocked.", success=False)]], driver, tmp_path, paths=store)
    agent.run("open chess", blocked=lambda package: package == "com.chess")
    assert not any(a[0] == "launch" for a in driver.actions)
    assert steps[0]["source"] == "replay" and steps[0]["is_error"] and "blocked" in steps[0]["outcome"].lower()


def test_ask_mode_still_asks_before_a_replayed_step(tmp_path):
    store = learned(tmp_path)
    io = RecordingIO(answers=["yes"])
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    agent, client, _ = make([DONE], driver, tmp_path, io=io, paths=store)
    result = agent.run(TASK, permissions=TaskPermissions("ask"))
    assert io.questions == ["Next step: tap 'Display'. Should I go ahead?"]
    assert result.success and result.replayed == 1 and result.prompts == 1 and taps(driver) == [("tap", 500, 140)]


def test_plan_mode_replays_only_after_the_plan_is_approved(tmp_path):
    store = learned(tmp_path)
    io = RecordingIO(answers=["yes"])
    driver = Phone(SETTINGS, {"Display": DISPLAY})
    plan = [tool_use("propose_plan", "p", steps=[{"text": "Open the Display page"}])]
    agent, client, steps = make([plan, DONE], driver, tmp_path, io=io, paths=store)
    result = agent.run(TASK, permissions=TaskPermissions("plan"))
    assert io.questions and io.questions[0].startswith("Plan for:")
    assert result.success and result.replayed == 1 and len(client.requests) == 2
    assert [s["tool"] for s in steps] == ["propose_plan", "tap_element", "done"] and steps[1]["source"] == "replay"


# --- a tap refused before it ran gets one more look ----------------------------------------------


class Settling(Phone):
    """A phone whose list is quick to read (like the app) and still moving: each read of the elements
    returns the next list in `reads` (the last repeats), until a tap lands on Display."""

    fast_ui_tree = True

    def __init__(self, *reads):
        super().__init__(reads[0])
        self.reads = list(reads)
        self.n = 0
        self.landed = False

    def _current(self):
        return self.reads[min(max(self.n - 1, 0), len(self.reads) - 1)]

    def ui_elements(self):
        if self.landed:
            return list(DISPLAY)
        self.n += 1
        return list(self._current())

    def tap(self, x, y):
        FakeDriver.tap(self, x, y)
        for e in self._current():
            x1, y1, x2, y2 = e.bounds
            if x1 <= x < x2 and y1 <= y < y2 and e.text == "Display":
                self.landed = True


def test_a_tap_refused_before_it_ran_is_tried_once_more_without_the_model(tmp_path):
    store = learned(tmp_path)
    moved = [el(0, "Promo"), el(1, "Network & internet"), el(2, "Display"), el(3, "Sound"), el(4, "Battery")]
    settled = [el(i, text, bounds=(0, 100 * i + 100, 1000, 100 * i + 180))
               for i, text in enumerate(["Promo", "Network & internet", "Display", "Sound", "Battery"])]
    # Reads: the screen decided on, the re-check (moved), the quiet check (moved again: refused), the next look.
    driver = Settling(SETTINGS, moved, settled, settled)
    agent, client, steps = make([DONE], driver, tmp_path, paths=store)
    result = agent.run(TASK)
    assert result.success and result.replayed == 2 and len(client.requests) == 1  # no model call in between
    refused, tapped, done = steps
    assert refused["source"] == "replay" and refused["is_error"] and replay.NOT_RUN in refused["outcome"]
    assert tapped["source"] == "replay" and not tapped["is_error"] and tapped["args"]["index"] == 2
    assert taps(driver) == [("tap", 500, 340)]  # where Display had settled
    assert done["tool"] == "done"


def test_a_tap_refused_twice_goes_to_the_model(tmp_path):
    store = learned(tmp_path)
    shifting = [[el(0, "Promo", bounds=(0, 0, 1000, 80 + k)), el(1, "Network & internet"), el(2, "Display", bounds=(0, 200 + 10 * k, 1000, 280 + 10 * k)),
                 el(3, "Sound"), el(4, "Battery")] for k in range(1, 8)]
    driver = Settling(SETTINGS, *shifting)
    agent, client, steps = make([[tool_use("tap_element", "a", index=2, target="Display")], DONE], driver, tmp_path, paths=store)
    result = agent.run(TASK)
    assert result.replayed == 2 and all(s["is_error"] for s in steps[:2]) and len(client.requests) == 2
    assert steps[2]["tool"] == "tap_element" and "source" not in steps[2]


# --- the pure parts ----------------------------------------------------------------------------


def test_request_keys_and_fingerprints_hold_no_text():
    key = replay.goal_key(TASK)
    assert key == replay.goal_key("  Open Display Settings! ") == replay.goal_key("please open display settings")
    assert key != replay.goal_key("open sound settings") and len(key) == 16 and "display" not in key
    a = replay.fingerprint([el(0, "3 apps"), el(1, "Display"), el(2, "", cls="SurfaceView"), el(3, "Shop", cls="OCRText")])
    b = replay.fingerprint([el(0, "7 apps"), el(1, "Display")])
    assert a == b and len(a) == 2 and all(len(t) == 12 for t in a)  # digits masked; unlabeled views and OCR words out
    assert replay.overlap(a, replay.fingerprint([el(0, "Display")])) == 0.5 and replay.overlap(frozenset(), a) == 0.0


def test_what_a_path_may_hold():
    screen = [el(0, "Display"), el(1, "", cls="View"), el(2, "Buy", cls="OCRText"), el(3, "Sound")]

    def rec(name, args, launched=None):
        return replay.recordable(name, args, "com.x", screen, launched)

    assert rec("tap_element", {"index": 0})["target"] == {"text": "Display", "desc": "", "resource_id": "", "cls": "TextView"}
    assert rec("scroll", {"direction": "down"})["target"] == {"direction": "down"}
    assert rec("press_key", {"key": "back"})["target"] == {"key": "BACK"}
    assert rec("launch_app", {"app": "Settings"}, "com.android.settings")["target"] == {"app": "com.android.settings"}
    assert rec("tap_element", {"index": 3})["screen"] == sorted(replay.fingerprint(screen))
    never = [("tap_element", {"index": 1}), ("tap_element", {"index": 2}), ("tap_element", {"index": 9}),
             ("tap_element", {"index": "x"}), ("tap", {"x": 1, "y": 2}), ("long_press", {"x": 1, "y": 2}),
             ("swipe", {"x1": 0, "y1": 0, "x2": 1, "y2": 1}), ("type_text", {"text": "hi"}), ("press_key", {"key": "ENTER"}),
             ("open_url", {"url": "https://x"}), ("wait_until", {"until": "loading_done"}), ("ask_user", {"question": "?"}),
             ("done", {"summary": "ok"}), ("scroll", {"direction": "sideways"}), ("launch_app", {"app": "Settings"})]
    for name, args in never:
        assert rec(name, args) is None, name


def test_a_stored_tap_resolves_to_the_one_element_with_its_identity():
    step = replay.recordable("tap_element", {"index": 0}, "com.x", [el(0, "Display")], None)
    assert replay.resolve(step, "com.x", [el(0, "Promo"), el(1, "Display")]) == ("tap_element", {"index": 1, "target": "Display"})
    assert replay.resolve(step, "com.y", [el(0, "Display")]) is None  # another app
    assert replay.resolve(step, "com.x", [el(0, "Display"), el(1, "Display")]) is None  # ambiguous
    assert replay.resolve(step, "com.x", [el(0, "Display", covered=True)]) is None
    assert replay.resolve(step, "com.x", [el(0, "Display", cls="Button")]) is None  # a different element
    assert replay.resolve({"tool": "type_text", "target": {"text": "x"}, "package": "com.x"}, "com.x", []) is None


def test_match_picks_the_best_fitting_remaining_step():
    first = replay.recordable("scroll", {"direction": "down"}, "com.x", SETTINGS, None)
    scrolled = [el(0, "Battery"), el(1, "Display"), el(2, "Storage"), el(3, "Security"), el(4, "Privacy")]
    second = replay.recordable("tap_element", {"index": 1}, "com.x", scrolled, None)
    path = [first, second]
    assert replay.match(path, 0, "com.x", SETTINGS, 0.7) == (0, "scroll", {"direction": "down"})
    assert replay.match(path, 0, "com.x", scrolled, 0.7) == (1, "tap_element", {"index": 1, "target": "Display"})
    assert replay.match(path, 1, "com.x", SETTINGS, 0.7) is None  # the scroll is behind the cursor
    assert replay.match(path, 0, "com.x", [el(0, "Display"), el(1, "Sound")], 0.7) is None  # too little of either screen
    assert replay.match(path, 0, "com.y", SETTINGS, 0.7) is None


# --- the store ---------------------------------------------------------------------------------


def test_the_store_keeps_the_newest_path_per_request_caps_requests_and_expires_them(tmp_path):
    file = tmp_path / "paths.json"
    store = replay.PathStore(file, max_goals=2, retention_days=1)
    step = {"tool": "press_key", "target": {"key": "BACK"}, "package": "a", "screen": []}
    store.put("g1", [step])
    store.put("g1", [step, step])
    assert len(store) == 1 and len(store.get("g1")) == 2
    store.put("g2", [step])
    store.put("g3", [step])
    assert len(store) == 2 and store.get("g1") is None and store.get("g3")  # the oldest went first
    again = replay.PathStore(file, max_goals=2, retention_days=1)  # the file survives a new store
    path = again.get("g2")
    path[0]["tool"] = "x"  # the caller's own copy
    assert again.get("g2")[0]["tool"] == "press_key"
    again._goals["g2"]["saved"] = time.time() - 2 * 86400
    assert again.get("g2") is None and again.get("g3")  # expired on read
    again.put("g4", [])
    again.put("g5", [step] * (replay.MAX_STEPS + 1))
    assert again.get("g4") is None and again.get("g5") is None  # nothing empty or too long
    forever = replay.PathStore(retention_days=0)
    forever.put("g", [step])
    forever._goals["g"]["saved"] = 0
    assert forever.get("g")
    file.write_text("{not json")
    broken = replay.PathStore(file)
    assert len(broken) == 0  # an unreadable file is an empty store…
    broken.put("g6", [step])
    assert replay.PathStore(file).get("g6")  # …that the next path stored rewrites


def test_the_store_lives_next_to_the_phones_label_cache():
    assert replay.store_path(Path("/x/labels-app-3f2a9c.json")) == Path("/x/paths-app-3f2a9c.json")
    assert replay.store_path(Path("/x/labels.json")) == Path("/x/paths-local.json")
    assert replay.store_path(None).name == "paths-local.json"


def test_replayed_steps_count_in_the_timings_report(tmp_path):
    h = History(tmp_path / "h.sqlite")
    chat = h.create_chat(None, "p", "Pixel", "hi")
    h.record_step(chat, "t1", {"idx": 1, "tool": "tap_element", "is_error": False, "source": "replay",
                               "total_ms": 500, "model_ms": 0, "fast": True})
    h.record_step(chat, "t1", {"idx": 2, "tool": "press_key", "is_error": False, "source": "auto_dialog", "total_ms": 300})
    rows = h.step_timings()
    assert [r["idx"] for r in rows] == [1] and rows[0]["fast"] == 1


def test_benchmarks_can_opt_in_to_storing_api_tasks(tmp_path):
    store = replay.PathStore()
    agent, _, _ = make([TAP_DISPLAY, DONE], Phone(SETTINGS, {"Display": DISPLAY}), tmp_path, paths=store,
                       replay_store_api=True)
    assert agent.run(TASK, isolated=True).success and len(store) == 1
