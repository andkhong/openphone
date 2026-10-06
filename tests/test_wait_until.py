"""wait_until: waiting for something observable instead of a blind sleep (agent/waiting.py).

Fake phones whose screen changes on a schedule: a wait returns early when its condition holds, times
out with the screen as it is, never calls a model while waiting, and uses the phone's own wait_for
only when its app offers it (older apps are polled)."""

import io
import threading
import time

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from PIL import Image

from openmob.agent import waiting
from openmob.agent.loop import Agent
from openmob.agent.tools import SYSTEM_PROMPT, TOOL_SPECS, system_prompt
from openmob.config import Config
from openmob.history import History
from openmob.phone.adb import Screenshot, UIElement, parse_ui_xml
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError
from openmob.phone.protocol import decode_element, encode_element
from openmob.phone.remote import RemoteDriver
from openmob.timing_report import format_report, summarize


def el(text="", cls="TextView", rid="", enabled=True, desc="", covered=False, i=0):
    return UIElement(i, text, desc, rid, cls, True, None, (0, 100 * i, 500, 100 * i + 80), enabled=enabled,
                     covered=covered)


class TimedPhone(FakeDriver):
    """A screen that changes on a schedule: [(after_seconds, package, elements)], the last that's due shows.
    Counts reads, and (optionally) offers wait_idle like the OpenMob app."""

    fast_ui_tree = True

    def __init__(self, schedule, idle=True, color_after=None, **kw):
        super().__init__(**kw)
        self.schedule = schedule
        self.t0 = time.monotonic()
        self.reads = 0
        self.idle = idle
        self.color_after = color_after
        if not idle:
            self.wait_idle = None

    def restart(self):
        self.t0 = time.monotonic()

    def _now(self):
        age = time.monotonic() - self.t0
        due = [s for s in self.schedule if s[0] <= age]
        return due[-1] if due else self.schedule[0]

    def current_app(self):
        return self._now()[1]

    def ui_elements(self):
        self.reads += 1
        return list(self._now()[2])

    def screenshot(self, max_age=0.0, attempts=2):
        color = "white"
        if self.color_after is not None and time.monotonic() - self.t0 >= self.color_after:
            color = "black"
        buf = io.BytesIO()
        Image.new("RGB", self.image, color).save(buf, "JPEG")
        return Screenshot(buf.getvalue(), *self.image, *self.device)

    def wait_idle(self, quiet_ms, max_ms):
        time.sleep(min(quiet_ms, max_ms) / 1000)
        return {"waited_ms": quiet_ms, "idle": True}


def wait(driver, cancel=None, **args):
    cond = waiting.parse(args)
    return cond, waiting.Waiter(driver, cancel or threading.Event()).run(cond)


STORE = "com.android.vending"


def test_text_appears_returns_as_soon_as_it_is_on_screen():
    phone = TimedPhone([(0, STORE, [el("Cancel"), el("Installing…", i=1)]),
                        (0.3, STORE, [el("Uninstall"), el("Open", i=1)])])
    cond, result = wait(phone, until="text_appears", target="Open|Play", timeout_s=20)
    assert result.met and 250 <= result.waited_ms < 1500 and result.via == "poll"
    assert "'Open' is on screen" in result.text(cond)


def test_words_match_whole_labels_or_whole_words():
    pattern = waiting.compile_target("Open|Play")
    assert waiting.matches(pattern, "Open") and waiting.matches(pattern, "Play now")
    assert not waiting.matches(pattern, "Opening") and not waiting.matches(pattern, "Display")
    assert not waiting.matches(pattern, "Google Play", exact=True)  # element_enabled wants the label itself
    assert waiting.matches(waiting.compile_target("(unbalanced"), "an (unbalanced label")  # literal fallback


def test_a_timeout_reports_the_screen_as_it_is():
    phone = TimedPhone([(0, "com.example", [el("Still loading"), el("Hello", i=1)])])
    cond, result = wait(phone, until="text_appears", target="Welcome", timeout_s=1)
    assert not result.met and 900 <= result.waited_ms < 2500
    text = result.text(cond)
    assert text.startswith("Timed out after") and "'Hello'" in text and "com.example" in text


def test_screen_changes_against_the_screen_the_model_saw():
    phone = TimedPhone([(0, "a.b", [el("Home")]), (0.25, "a.b", [el("Home"), el("New message", i=1)])])
    cond = waiting.parse({"until": "screen_changes", "timeout_s": 10})
    result = waiting.Waiter(phone, threading.Event()).run(cond, baseline=("a.b", [el("Home")]))
    assert result.met and result.waited_ms < 1500
    # A clock ticking or a list settling isn't a change.
    assert waiting.signature("a", [el("10:41")]) == waiting.signature("a", [el("10:42", i=3)])
    # Already different from what the model saw: met at once.
    result = waiting.Waiter(phone, threading.Event()).run(cond, baseline=("a.b", [el("Other")]))
    assert result.met and result.waited_ms < 300


def test_screen_changes_on_a_treeless_screen_compares_frames():
    phone = TimedPhone([(0, "com.game", [])], color_after=0.3)
    cond, result = wait(phone, until="screen_changes", timeout_s=10)
    assert result.met and result.via == "frames" and result.waited_ms < 2000


def test_app_in_front():
    phone = TimedPhone([(0, "com.android.launcher3", []), (0.3, "com.chess", [el("Play")])])
    cond, result = wait(phone, until="app_in_front", target="com.chess", timeout_s=10)
    assert result.met and result.package == "com.chess" and result.waited_ms < 1500


def test_loading_done_waits_out_a_spinner():
    phone = TimedPhone([(0, "com.chess", [el(cls="ProgressBar", rid="progress"), el("Chess.com", i=1)]),
                        (0.4, "com.chess", [el("Play Online", i=1), el("Play Bots", i=2)])])
    cond, result = wait(phone, until="loading_done", timeout_s=10)
    assert result.met and 350 <= result.waited_ms < 2000
    # "Loading…" text and a percentage count as loading; a plain screen doesn't.
    assert waiting.loading_element([el("Loading…")]) and waiting.loading_element([el("45%")])
    assert waiting.loading_element([el("Play Bots"), el("Settings")]) is None


def test_loading_done_without_wait_idle_needs_two_equal_reads():
    phone = TimedPhone([(0, "a", [el("Loading")]), (0.2, "a", [el("Ready")])], idle=False)
    cond, result = wait(phone, until="loading_done", timeout_s=10)
    assert result.met and phone.reads >= 3


def test_element_enabled_waits_for_the_greyed_out_button():
    phone = TimedPhone([(0, STORE, [el("Cancel"), el("Open", enabled=False, i=1)]),
                        (0.3, STORE, [el("Uninstall"), el("Open", i=1)])])
    cond, result = wait(phone, until="element_enabled", target="Open|Play", timeout_s=10)
    assert result.met and result.waited_ms >= 250 and "'Open' is enabled" in result.seen


def test_the_enabled_flag_crosses_the_protocol_and_adb():
    off = el("Open", enabled=False)
    assert encode_element(off)["enabled"] is False and "enabled" not in encode_element(el("Open"))
    assert decode_element(0, encode_element(off)).enabled is False
    assert decode_element(0, encode_element(el("Open"))).enabled is True  # older apps send nothing: enabled
    xml = ('<hierarchy><node text="Open" class="android.widget.Button" clickable="true" enabled="false" '
           'bounds="[0,0][100,100]"/></hierarchy>')
    assert parse_ui_xml(xml)[0].enabled is False


def test_a_stop_ends_the_wait():
    phone = TimedPhone([(0, "a", [el("x")])])
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    cond, result = wait(phone, cancel, until="text_appears", target="never", timeout_s=30)
    assert result.stopped == "cancelled" and result.waited_ms < 2000


def test_a_blocked_app_stops_the_wait_without_reading_it():
    phone = TimedPhone([(0, "a", [el("x")]), (0.2, "com.bank", [el("Balance 1,234")])])
    cond = waiting.parse({"until": "text_appears", "target": "Balance", "timeout_s": 10})
    result = waiting.Waiter(phone, threading.Event(), blocked=lambda p: p == "com.bank").run(cond)
    assert not result.met and result.stopped == "blocked" and "Balance" not in result.text(cond)


def test_bad_arguments():
    for args in ({"until": "soon"}, {"until": "text_appears"}, {"until": "app_in_front", "target": " "}):
        try:
            waiting.parse(args)
        except ValueError:
            continue
        raise AssertionError(args)
    assert waiting.parse({"until": "loading_done", "timeout_s": 999}).timeout_s == 120
    assert waiting.parse({"until": "loading_done"}).timeout_s == 30


# --- the phone's own wait_for, and older apps ------------------------------------------------------

class FakeLink:
    """A PhoneLink stand-in: answers calls from a TimedPhone; wait_for like a current app would."""

    closed = False

    def __init__(self, phone, broken_wait_for=False):
        self.phone = phone
        self.calls = []
        self.broken = broken_wait_for

    def call(self, method, params):
        self.calls.append(method)
        if method == "wait_for":
            if self.broken:
                raise DeviceError("Unknown method wait_for")
            cond = params["condition"]
            start = time.monotonic()
            pattern = waiting.compile_target(cond.get("pattern", ""))
            while (time.monotonic() - start) * 1000 < params["timeout_ms"]:
                if waiting.find_text(pattern, self.phone.ui_elements()):
                    return {"met": True, "waited_ms": int((time.monotonic() - start) * 1000), "seen": "found",
                            "package": self.phone.current_app()}
                time.sleep(0.02)
            return {"met": False, "waited_ms": params["timeout_ms"], "seen": "", "package": None}
        if method == "ui_elements":
            return [encode_element(e) for e in self.phone.ui_elements()]
        if method == "current_app":
            return self.phone.current_app()
        if method == "wait_idle":
            return self.phone.wait_idle(params["quiet_ms"], params["max_ms"])
        raise AssertionError(method)


def remote(phone, caps, broken=False):
    link = FakeLink(phone, broken)
    driver = RemoteDriver(link)
    driver.capabilities = set(caps)
    return driver, link


def test_a_current_app_waits_on_the_phone():
    phone = TimedPhone([(0, STORE, [el("Cancel")]), (0.3, STORE, [el("Open")])])
    driver, link = remote(phone, {"wait_idle", "wait_for"})
    cond, result = wait(driver, until="text_appears", target="Open", timeout_s=10)
    assert result.met and result.via == "phone"
    assert link.calls.count("wait_for") == 1 and link.calls.count("ui_elements") == 2  # before, and the result


def test_older_apps_are_polled():
    phone = TimedPhone([(0, STORE, [el("Cancel")]), (0.3, STORE, [el("Open")])])
    driver, link = remote(phone, {"wait_idle"})  # no wait_for in its hello
    cond, result = wait(driver, until="text_appears", target="Open", timeout_s=10)
    assert result.met and result.via == "poll" and "wait_for" not in link.calls
    assert link.calls.count("ui_elements") >= 2 and "wait_idle" in link.calls


def test_an_app_that_fails_wait_for_falls_back_to_polling():
    phone = TimedPhone([(0, STORE, [el("Cancel")]), (0.3, STORE, [el("Open")])])
    driver, link = remote(phone, {"wait_idle", "wait_for"}, broken=True)
    cond, result = wait(driver, until="text_appears", target="Open", timeout_s=10)
    assert result.met and result.via == "poll" and "wait_for" not in driver.capabilities
    assert link.calls.count("wait_for") == 1


# --- in the agent ---------------------------------------------------------------------------------

class CountingJudge:
    """A screen classifier that only counts its calls (each is a model call)."""

    def __init__(self):
        self.calls = 0

    def assess_screen(self, request, screen):
        from openmob.jev import ScreenAssessment

        self.calls += 1
        return ScreenAssessment("app_content", 0.9, 0.9, 0.1)

    def assess_tap(self, request, target, screen):
        return 0.0, 0.0


def make(phone, responses, tmp_path, judge=None, **cfg):
    config = Config(**{"settle_delay": 0.05, "launch_settle_delay": 0.1, "settle_quiet_ms": 20, **cfg})
    io_ = RecordingIO()
    io_.steps = []
    io_.record_step = io_.steps.append
    client = ScriptedClient(responses)
    agent = Agent(client, phone, AppResolver(phone, label_cache=tmp_path / "l.json"), config, io_, judge=judge,
                  use_ocr=False)
    return agent, client, io_


def test_the_agent_waits_without_model_calls_and_records_it(tmp_path):
    phone = TimedPhone([(0, STORE, [el("Cancel"), el("Open", enabled=False, i=1)]),
                        (60, STORE, [el("Open", i=1)])])  # moved to "now" below, once the wait has begun
    judge = CountingJudge()
    agent, client, io_ = make(phone, [
        [tool_use("wait_until", "a", until="element_enabled", target="Open|Play", timeout_s=30)],
        [tool_use("done", "b", summary="Installed.", success=True)],
    ], tmp_path, judge=judge)
    real_read = phone.ui_elements

    def read():
        if phone.reads == 3:
            phone.schedule[1] = (0, STORE, [el("Open", i=1)])
        return real_read()
    phone.ui_elements = read
    result = agent.run("install chess")
    assert result.success
    assert len(client.requests) == 2  # one decision per step: none during the wait
    assert judge.calls == 2  # one classification per observation, none while waiting
    step = io_.steps[0]
    assert step["tool"] == "wait_until" and step["wait_cond"] == "element_enabled:Open|Play"
    assert step["wait_met"] is True and step["wait_via"] == "poll" and step["waited_ms"] < 5000
    assert step["settle_ms"] >= 0 and phone.reads > 3
    assert "met after" in str(client.requests[1]["messages"])


def test_an_install_tap_waits_for_open_without_the_model(tmp_path):
    phone = TimedPhone([(0, STORE, [el("Install", cls="Button")]),
                        (0.4, STORE, [el("Cancel"), el("Open", enabled=False, i=1)]),
                        (0.8, STORE, [el("Uninstall"), el("Open", i=1)])], device=(1080, 2400))
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0, target="Install")],
        [tool_use("done", "b", summary="Installed.", success=True)],
    ], tmp_path)
    original = phone.tap
    phone.tap = lambda x, y: (original(x, y), phone.restart())
    agent.run("install chess")
    tap = io_.steps[0]
    assert tap["wait_cond"] == "auto:element_enabled:Open|Play" and tap["wait_met"] is True
    assert 700 <= tap["waited_ms"] < 5000 and "Waited" in tap["outcome"]
    assert len(client.requests) == 2


def test_an_install_tap_that_starts_nothing_stops_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr("openmob.agent.loop.INSTALL_TAP_GRACE_S", 0.3)
    phone = TimedPhone([(0, STORE, [el("Install", cls="Button")])])
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0, target="Install")],
        [tool_use("done", "b", summary="no", success=False)],
    ], tmp_path)
    agent.run("install chess")
    tap = io_.steps[0]
    assert tap["wait_met"] is False and tap["waited_ms"] < 3000 and "no install started" in tap["outcome"]


def test_a_launch_waits_out_its_loading_screen(tmp_path):
    phone = TimedPhone([(0, "com.android.launcher3", [el("Chess")]),
                        (0.05, "com.chess", [el(cls="ProgressBar"), el("chess.com", i=1)]),
                        (0.5, "com.chess", [el("Play Bots", i=1)])], packages=["com.chess"])
    agent, client, io_ = make(phone, [
        [tool_use("launch_app", "a", app="com.chess")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path)
    phone.launch_app = lambda package: phone.restart()
    agent.run("open chess")
    done = io_.steps[1]
    assert done["wait_cond"] == "auto:loading_done" and done["wait_met"] is True
    assert "Play Bots" in str(client.requests[1]["messages"])  # the model saw the loaded screen


def test_a_launch_with_nothing_loading_adds_no_wait(tmp_path):
    phone = TimedPhone([(0, "com.chess", [el("Play Bots")])], packages=["com.chess"])
    agent, client, io_ = make(phone, [
        [tool_use("launch_app", "a", app="com.chess")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path)
    agent.run("open chess")
    assert io_.steps[1].get("wait_cond") is None and io_.steps[1]["settle_ms"] < 1000


def test_app_in_front_by_name_and_bad_args(tmp_path):
    phone = TimedPhone([(0, "com.chess", [el("x")])], packages=["com.chess"], labels={"com.chess": "Chess"})
    agent, client, io_ = make(phone, [
        [tool_use("wait_until", "a", until="app_in_front", target="Chess", timeout_s=5)],
        [tool_use("wait_until", "b", until="text_appears")],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path)
    agent.run("open chess")
    assert io_.steps[0]["wait_met"] is True and io_.steps[0]["wait_cond"] == "app_in_front:com.chess"
    assert io_.steps[1]["is_error"] and "needs a target" in io_.steps[1]["outcome"]


def test_the_model_is_told_to_prefer_wait_until():
    names = [t["name"] for t in TOOL_SPECS]
    assert "wait_until" in names and "wait" in names
    for prompt in (SYSTEM_PROMPT, system_prompt("ios")):
        assert "wait_until element_enabled \"Open|Play\"" in prompt and "loading_done" in prompt
        assert "only when nothing observable will change" in prompt
    spec = next(t for t in TOOL_SPECS if t["name"] == "wait_until")
    assert spec["parameters"]["properties"]["timeout_s"]["maximum"] == 120


def test_timings_show_the_waits(tmp_path):
    h = History(tmp_path / "h.db")
    chat = h.create_chat(None, "p", "Pixel", "install chess")
    h.record(chat, {"kind": "heard", "message": "install chess", "task": "t1", "time": 2})
    base = {"is_error": False, "total_ms": 1000, "settle_ms": 500, "model": "m", "jev": False}
    h.record_step(chat, "t1", {**base, "idx": 1, "tool": "wait", "wait_cond": "sleep", "wait_met": None,
                               "wait_via": "sleep", "waited_ms": 15000})
    h.record_step(chat, "t1", {**base, "idx": 2, "tool": "wait_until", "wait_cond": "element_enabled:Open|Play",
                               "wait_met": True, "wait_via": "phone", "waited_ms": 4200})
    rows = h.step_timings()
    assert {r["wait_cond"] for r in rows} == {"sleep", "element_enabled:Open|Play"}
    report = format_report(summarize(rows))
    assert "waits · sleep: 1" in report and "waits · wait_until: 1, 1 met their condition" in report
