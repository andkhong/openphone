"""A tighter loop, from a real run ("install chess and play 5 moves against a bot" on a Samsung, Android 15):

- an archived app (Android 15 keeps it listed with no launchable activity) cost three steps: list_apps showed it
  installed, and launch_app failed twice. Now it's marked, and launch_app says how to install it (or opens its Play
  Store page itself with install_if_missing);
- the notification permission dialog cost ~20 s of model steps (and the model tapped "Don't allow"). Now the loop
  answers it by the owner's defaults with no model call;
- a tap right after the dialog appeared failed with "The phone can't see what's there". Now the loop waits briefly
  for the screen to become readable;
- a wait_until for text that never came ran to its timeout on a still screen. Now it ends early.

Fake phones throughout; no device or adb."""

import threading
import time

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent import permission_dialogs as pd
from openmob.agent import waiting
from openmob.agent.loop import Agent
from openmob.agent.tools import SYSTEM_PROMPT, TOOL_SPECS
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError

CONTROLLER = "com.google.android.permissioncontroller"
PC = "com.android.permissioncontroller:id/"


def el(i, text, rid="", cls="Button", bounds=None, checked=None, covered=False):
    return UIElement(i, text, "", rid, cls, True, checked, bounds or (40, 200 + 150 * i, 1040, 320 + 150 * i),
                     covered=covered)


def make(phone, responses, tmp_path, task_io=None, **cfg):
    config = Config(**{"settle_delay": 0.05, "launch_settle_delay": 0.05, "settle_quiet_ms": 20, **cfg})
    io_ = task_io or RecordingIO()
    io_.steps = []
    io_.record_step = io_.steps.append
    client = ScriptedClient(responses)
    agent = Agent(client, phone, AppResolver(phone, label_cache=tmp_path / "l.json"), config, io_, use_ocr=False)
    return agent, client, io_


def last_text(request) -> str:
    return str(request["messages"])


# --- A. archived apps -----------------------------------------------------------------------------

class ArchivedPhone(FakeDriver):
    """Android 15 after the owner removed Chess.com: still listed, no launchable activity."""

    def __init__(self, **kw):
        super().__init__(packages=["com.chess", "com.android.vending", "com.whatsapp"],
                         labels={"com.chess": "Chess", "com.android.vending": "Google Play Store",
                                 "com.whatsapp": "WhatsApp"}, **kw)
        self.elements = [el(0, "Search apps")]

    def launch_app(self, package):
        if package == "com.chess":
            raise DeviceError("IllegalStateException: com.chess has no launchable activity")
        super().launch_app(package)


def test_an_archived_app_is_reported_with_the_way_to_install_it(tmp_path):
    phone = ArchivedPhone()
    agent, client, io_ = make(phone, [
        [tool_use("launch_app", "a", app="Chess")],
        [tool_use("list_apps", "b", query="chess")],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path)
    agent.run("install chess and play 5 moves against a bot")
    launch, listing = io_.steps[0], io_.steps[1]
    assert launch["is_error"] and "isn't installed (archived" in launch["outcome"]
    assert "open_url market://details?id=com.chess" in launch["outcome"] and "install_if_missing" in launch["outcome"]
    assert "(com.chess) (archived: reinstall from the Play Store)" in listing["outcome"]
    assert "(com.whatsapp)" in listing["outcome"] and "whatsapp) (archived" not in listing["outcome"]


def test_install_if_missing_opens_the_store_page_in_one_step(tmp_path):
    phone = ArchivedPhone()
    agent, client, io_ = make(phone, [
        [tool_use("launch_app", "a", app="Chess", install_if_missing=True)],
        [tool_use("launch_app", "b", app="Lichess", install_if_missing=True)],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path)
    agent.run("install chess")
    assert ("url", "market://details?id=com.chess") in phone.actions
    assert not io_.steps[0]["is_error"] and "Play Store page" in io_.steps[0]["outcome"]
    assert ("url", "market://search?q=Lichess&c=apps") in phone.actions  # not installed at all: a store search
    assert len(client.requests) == 3


def test_an_app_installed_since_the_list_was_read_launches(tmp_path):
    phone = ArchivedPhone()
    agent, client, io_ = make(phone, [
        [tool_use("launch_app", "a", app="com.chess.new")],
        [tool_use("done", "c", summary="ok", success=True)],
    ], tmp_path)
    agent.resolver.packages()  # read before the install
    phone.packages.append("com.chess.new")
    agent.run("open it")
    assert ("launch", "com.chess.new") in phone.actions and not io_.steps[0]["is_error"]


def test_a_reinstalled_app_launches_again(tmp_path):
    phone = ArchivedPhone()
    resolver = AppResolver(phone, label_cache=tmp_path / "l.json")
    resolver.mark_archived("com.chess")
    assert "archived" in resolver.listing("chess")
    resolver.packages(refresh=True)  # still listed: still marked (an older phone app lists archived apps)
    assert resolver.is_archived("com.chess")
    phone.packages.remove("com.chess")
    resolver.packages(refresh=True)  # gone from the list: forgotten
    assert not resolver.is_archived("com.chess")


# --- B. permission dialogs, answered by rule ------------------------------------------------------

APP_SCREEN = [el(0, "Play as Guest"), el(1, "Log In")]
BOTS_SCREEN = [el(0, "Play Bots"), el(1, "Play Online")]


def notification_dialog():
    return [el(0, "Allow Chess.com to send you notifications?", PC + "permission_message", "TextView"),
            el(1, "Allow", PC + "permission_allow_button"),
            el(2, "Don't allow", PC + "permission_deny_button")]


def location_dialog(precise_checked=True):
    return [el(0, "Allow Chess.com to access this device's location?", PC + "permission_message", "TextView"),
            el(1, "Precise", PC + "permission_location_accuracy_radio_fine", "RadioButton", checked=precise_checked),
            el(2, "Approximate", PC + "permission_location_accuracy_radio_coarse", "RadioButton",
               checked=not precise_checked),
            el(3, "While using the app", PC + "permission_allow_foreground_only_button"),
            el(4, "Only this time", PC + "permission_allow_one_time_button"),
            el(5, "Don't allow", PC + "permission_deny_button")]


class FlowPhone(FakeDriver):
    """Screens by name; a tap on an element's center follows `moves` {(screen, label): next screen}."""

    fast_ui_tree = True

    def __init__(self, screens, start, moves, **kw):
        super().__init__(packages=["com.chess"], labels={"com.chess": "Chess"}, **kw)
        self.screens, self.screen, self.moves = screens, start, moves
        self.tapped = []
        self.ui_reads = 0

    def current_app(self):
        return self.screens[self.screen][0]

    def ui_elements(self):
        self.ui_reads += 1
        return list(self.screens[self.screen][1])

    def tap(self, x, y):
        super().tap(x, y)
        hit = next((e for e in self.screens[self.screen][1] if e.bounds[0] <= x <= e.bounds[2]
                    and e.bounds[1] <= y <= e.bounds[3]), None)
        self.tapped.append(hit.text if hit else None)
        nxt = self.moves.get((self.screen, hit.text if hit else None))
        if nxt:
            self.screen = nxt

    def wait_idle(self, quiet_ms, max_ms):
        return {"idle": True, "waited_ms": 1}

    def key(self, name):
        super().key(name)
        nxt = self.moves.get((self.screen, name))
        if nxt:
            self.screen = nxt


class SamsungPhone(FlowPhone):
    """The real failure (Samsung, Android 15/16): taps reach the permission dialog but Android drops them
    (accessibility-data-sensitive, the service isn't an accessibility tool); BACK closes it at once."""

    def tap(self, x, y):
        if self.screens[self.screen][0] == CONTROLLER:
            FakeDriver.tap(self, x, y)
            self.tapped.append("ignored")
            return
        super().tap(x, y)






def test_defaults_per_type_and_never_allow_all_the_time():
    prompts = dict(pd.DEFAULTS)

    def answer(elements, package=CONTROLLER):
        d = pd.detect(package, elements)
        plan = pd.decide(d, mode="auto", prompts=prompts, task_app_label="Chess", task_package="com.chess")
        b = pd.button(d, plan.answer, plan.grant)
        return b.text if b else None

    cam = [el(0, "Allow Chess.com to take pictures and record video?", PC + "permission_message", "TextView"),
           el(1, "While using the app", PC + "permission_allow_foreground_only_button"),
           el(2, "Only this time", PC + "permission_allow_one_time_button"),
           el(3, "Don't allow", PC + "permission_deny_button")]
    assert answer(cam) == "Only this time"
    nearby = [el(0, "Allow Chess.com to find, connect to, and determine the relative position of nearby devices?",
                 PC + "permission_message", "TextView"),
              el(1, "Allow", PC + "permission_allow_button"), el(2, "Don't allow", PC + "permission_deny_button")]
    # Only a lasting grant offered for a dangerous kind: the owner is asked.
    d = pd.detect(CONTROLLER, nearby)
    assert pd.decide(d, mode="auto", prompts=prompts, task_app_label="Chess", task_package="com.chess").answer == "ask"
    assert pd.decide(d, mode="never", prompts=prompts, task_app_label="Chess").answer == "deny"
    loose = {**prompts, "persistent_grants": "allow"}  # the owner relaxed it
    plan = pd.decide(d, mode="auto", prompts=loose, task_app_label="Chess")
    assert plan.answer == "allow" and pd.button(d, "allow", plan.grant).text == "Allow"
    photos = [el(0, "Allow Chess.com to access photos and videos on this device?", PC + "permission_message", "TextView"),
              el(1, "Allow limited access", PC + "permission_allow_selected_button"),
              el(2, "Allow all", PC + "permission_allow_all_button"),
              el(3, "Don't allow", PC + "permission_deny_button")]
    assert answer(photos) == "Allow limited access"
    assert answer([photos[0], photos[2], photos[3]]) == "Don't allow"  # only "Allow all": no
    background = [el(0, "Allow Chess.com to access this device's location?", PC + "permission_message", "TextView"),
                  el(1, "Allow all the time", PC + "permission_allow_always_button"),
                  el(2, "Don't allow", PC + "permission_deny_button")]
    assert answer(background) == "Don't allow"
    assert answer(notification_dialog()) == "Allow"
    # "Chess.com" is the Chess app's own request; another app's asks the owner.
    other = [el(0, "Allow Spyware to send you notifications?", PC + "permission_message", "TextView"),
             el(1, "Allow", PC + "permission_allow_button"), el(2, "Don't allow", PC + "permission_deny_button")]
    d = pd.detect(CONTROLLER, other)
    assert pd.decide(d, mode="auto", prompts=prompts, task_app_label="Chess", task_package="com.chess").answer == "ask"
    # Before the task has an app, every request is another app's: it asks.
    d = pd.detect(CONTROLLER, notification_dialog())
    assert pd.decide(d, mode="auto", prompts=prompts, task_app_label=None).answer == "ask"


def test_only_the_systems_dialogs_count():
    controller_ids = [el(0, "Allow Chess.com to send you notifications?", PC + "permission_message", "TextView"),
                      el(1, "Allow", PC + "permission_allow_button"), el(2, "Don't allow", PC + "permission_deny_button")]
    assert pd.detect(CONTROLLER, controller_ids) is not None
    # A web page in Chrome (HTML ids pass through) or an app setting controller-looking ids: never a dialog.
    assert pd.detect("com.android.chrome", controller_ids) is None
    assert pd.detect("com.evil", controller_ids) is None
    ocr = [el(0, "Allow Chess.com to send you notifications?", "ocr_permission_message", "OCRText"),
           el(1, "Allow", cls="OCRText"), el(2, "Don't allow", cls="OCRText"),
           el(3, "Play Bots", covered=True)]
    # The gatekeeper app's OCR of a hidden dialog: its package is missing or the controller.
    for package in (None, "com.android.permissioncontroller"):
        found = pd.detect(package, ocr, gatekeeper=True)
        assert found is not None and found.kind == "notifications" and found.buttons["allow"].text == "Allow"
    assert pd.detect("com.chess", ocr, gatekeeper=True) is None  # an app in front: its own screen
    as_app = [el(0, "Allow Chess.com to send you notifications?", "ocr_permission_message", "TextView"),
              el(1, "Allow"), el(2, "Don't allow")]
    assert pd.detect(None, as_app, gatekeeper=True) is None  # the id without the phone's OCR class
    assert pd.detect("com.evil", ocr, gatekeeper=True) is None
    assert pd.detect(None, [ocr[0], ocr[1], ocr[2]], gatekeeper=False) is not None  # (the server's own OCR, below)
    assert pd.detect(None, [el(0, "Allow Chess.com to send you notifications?", cls="OCRText"),
                            el(1, "Allow", cls="OCRText"), el(2, "Don't allow", cls="OCRText")]) is not None


def test_a_fake_dialog_on_a_web_page_is_not_auto_tapped(tmp_path):
    page = [el(0, "Allow Chess.com to send you notifications?", PC + "permission_message", "TextView"),
            el(1, "Allow", PC + "permission_allow_button"), el(2, "Don't allow", PC + "permission_deny_button")]
    for package in ("com.android.chrome", "com.evil.app"):
        phone = FlowPhone({"page": (package, page)}, "page", {})
        agent, client, io_ = make(phone, [[tool_use("done", "a", summary="ok", success=True)]], tmp_path)
        agent.run("read the news")
        assert phone.tapped == [] and all("source" not in s for s in io_.steps) and len(client.requests) == 1




def test_the_handler_can_be_turned_off_and_the_model_is_told():
    assert Config(auto_permission_dialogs=False).auto_permission_dialogs is False
    assert "permission dialogs" in SYSTEM_PROMPT and "screen_changes" in SYSTEM_PROMPT
    spec = next(t for t in TOOL_SPECS if t["name"] == "launch_app")
    assert "install_if_missing" in spec["parameters"]["properties"]


# --- C. "can't see" right after a dialog appears ------------------------------------------------------

class Unstable(DeviceError):
    code = "gate_screen_unstable"


def test_a_cant_see_refusal_waits_for_the_screen_and_tries_once_more(tmp_path):
    phone = FlowPhone({"bots": ("com.chess", BOTS_SCREEN), "game": ("com.chess", [el(0, "Resign")])}, "bots",
                      {("bots", "Play Bots"): "game"})
    real_tap = phone.tap
    state = {"refused": 0, "readable_at": None}

    def tap(x, y):
        if state["refused"] == 0:
            state["refused"] += 1
            state["readable_at"] = time.monotonic() + 0.3  # the phone's OCR finishes in a moment
            raise Unstable("The phone can't see what's there. Look again.")
        real_tap(x, y)

    def elements():
        if state["readable_at"] and time.monotonic() < state["readable_at"]:
            return [el(0, "Play Bots", covered=True), el(1, "Play Online", covered=True)]
        return list(phone.screens[phone.screen][1])

    phone.tap, phone.ui_elements = tap, elements
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0, target="Play Bots")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path, verify_taps=False)
    agent.run("play a bot")
    step = io_.steps[0]
    assert not step["is_error"] and phone.tapped == ["Play Bots"] and len(client.requests) == 2






# --- D. waits that can never be met ------------------------------------------------------------------

class StillPhone(FakeDriver):
    fast_ui_tree = True

    def __init__(self, elements, package="com.chess"):
        super().__init__(packages=[package])
        self.elements, self.package = elements, package

    def wait_idle(self, quiet_ms, max_ms):
        time.sleep(min(quiet_ms, max_ms) / 1000)
        return {"idle": True, "waited_ms": quiet_ms}


def test_a_text_wait_on_a_still_screen_ends_early():
    phone = StillPhone([el(0, "1. e4 e5"), el(1, "2. Nf3")])
    cond = waiting.parse({"until": "text_appears", "target": r"3\.", "timeout_s": 20})
    result = waiting.Waiter(phone, threading.Event()).run(cond, still_exit_s=waiting.STILL_EXIT_S)
    assert not result.met and result.stopped == "still" and 2400 <= result.waited_ms < 4500
    assert "the screen has been still" in result.text(cond) and "screen_changes" in result.text(cond)


def test_a_wait_keeps_its_timeout_while_something_loads():
    phone = StillPhone([el(0, "Installing…"), el(1, "Cancel")], package="com.android.vending")
    cond = waiting.parse({"until": "text_appears", "target": "Open", "timeout_s": 3.5})
    result = waiting.Waiter(phone, threading.Event()).run(cond, still_exit_s=waiting.STILL_EXIT_S)
    assert not result.met and result.stopped is None and result.waited_ms >= 3400


def test_a_moving_screen_resets_the_still_clock():
    phone = StillPhone([el(0, "Your move")])
    t0 = time.monotonic()

    def read():
        age = time.monotonic() - t0
        if age < 2.0:
            return [el(0, f"Thinking {'.' * (int(age * 4) % 3 + 1)}")]  # the bot "thinks": labels change
        return [el(0, "Your move")]

    phone.ui_elements = read
    cond = waiting.parse({"until": "text_appears", "target": "Checkmate", "timeout_s": 20})
    result = waiting.Waiter(phone, threading.Event()).run(cond, still_exit_s=waiting.STILL_EXIT_S)
    assert result.stopped == "still" and result.waited_ms >= 4300  # 2 s of motion, then 2.5 s still


def test_the_agents_wait_until_uses_the_still_exit(tmp_path):
    phone = StillPhone([el(0, "1. e4 e5")])
    agent, client, io_ = make(phone, [
        [tool_use("wait_until", "a", until="text_appears", target=r"3\.", timeout_s=8)],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path)
    agent.run("play 5 moves against a bot")
    step = io_.steps[0]
    assert step["wait_met"] is False and step["waited_ms"] < 4500 and "still" in step["outcome"]




# --- Play Store installs ------------------------------------------------------------------------------

STORE_PAGE = [el(0, "Chess - Play and Learn", cls="TextView"), el(1, "Install")]


def test_an_unrequested_install_asks_and_a_requested_one_does_not(tmp_path):
    for task, answers, asked in (("look at the chess app's reviews", ["no"], True),
                                 ("install chess and play 5 moves against a bot", [], False)):
        phone = FlowPhone({"store": ("com.android.vending", STORE_PAGE)}, "store", {})
        agent, client, io_ = make(phone, [
            [tool_use("tap_element", "a", index=1, target="Install")],
            [tool_use("done", "b", summary="ok", success=True)],
        ], tmp_path, task_io=RecordingIO(answers=answers), install_wait_seconds=0)
        agent.run(task)
        assert bool(io_.questions) is asked
        assert phone.tapped == ([] if asked else ["Install"])


# --- B (revised). Permission dialogs are dismissed with Back ------------------------------------------------

def guest_flow(phone_cls=SamsungPhone, dialog=None, back_closes=True):
    screens = {"guest": ("com.chess", APP_SCREEN), "dialog": (CONTROLLER, dialog or notification_dialog()),
               "bots": ("com.chess", BOTS_SCREEN)}
    moves = {("guest", "Play as Guest"): "dialog"}
    if back_closes:
        moves[("dialog", "BACK")] = "bots"
    return phone_cls(screens, "guest", moves)


def test_regression_samsung_taps_ignored_back_closes_the_dialog(tmp_path):
    phone = guest_flow()
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0, target="Play as Guest")],
        [tool_use("done", "b", summary="On the bots screen.", success=True)],
    ], tmp_path)
    result = agent.run("install chess and play 5 moves against a bot")
    assert result.success
    assert phone.tapped == ["Play as Guest"]  # no tap was ever sent to the dialog
    assert [a for a in phone.actions if a[0] == "key"] == [("key", "BACK")]
    assert len(client.requests) == 2  # the dialog cost no model call
    auto = io_.steps[1]
    assert auto["source"] == "auto_dialog" and auto["tool"] == "press_key" and auto["args"] == {"key": "BACK"}
    assert auto["outcome"].startswith("Dismissed Chess.com's notifications request with Back")
    assert "didn't get the permission" in auto["outcome"]
    seen = str(client.requests[1]["messages"])
    assert "Play Bots" in seen and "Dismissed Chess.com's notifications request" in seen
    assert not io_.questions


def test_a_dialog_back_cant_close_is_tried_twice_then_left_to_the_owner(tmp_path):
    phone = guest_flow(back_closes=False)
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0)],
        [tool_use("done", "b", summary="Waiting for you.", success=False)],
    ], tmp_path)
    agent.run("play chess")
    assert [a for a in phone.actions if a[0] == "key"] == [("key", "BACK"), ("key", "BACK")]
    assert [s.get("source") for s in io_.steps] == [None, "auto_dialog", "auto_dialog", None]
    assert "answer it on the phone" in str(client.requests[1]["messages"]) and len(client.requests) == 2


def test_the_models_tap_on_a_dialog_is_not_sent(tmp_path):
    phone = guest_flow(back_closes=False)
    phone.screen = "dialog"
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=1, target="Allow")],
        [tool_use("tap", "b", x=270, y=700, target="Allow")],
        [tool_use("done", "c", summary="no", success=False)],
    ], tmp_path, auto_permission_dialogs=False)
    agent.run("play chess")
    assert phone.tapped == [] and not [a for a in phone.actions if a[0] == "tap"]
    for step in io_.steps[:2]:
        assert step["is_error"] and "Press BACK" in step["outcome"]


def test_an_explicit_allow_request_still_gets_back_and_an_honest_note(tmp_path):
    phone = guest_flow()
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0)],
        [tool_use("done", "b", summary="Android requires you to tap Allow yourself.", success=False)],
    ], tmp_path)
    agent.run("open chess as a guest and allow notifications")
    assert [a for a in phone.actions if a[0] == "key"] == [("key", "BACK")] and phone.tapped == ["Play as Guest"]
    assert "Android requires the owner to tap Allow" in str(client.requests[1]["messages"])
    assert pd.request_wants_permission("turn on location for Maps")
    assert not pd.request_wants_permission("open chess and don't allow notifications")


def test_location_and_contacts_dialogs_are_dismissed_the_same_way(tmp_path):
    contacts = [el(0, "Allow Chess.com to access your contacts?", PC + "permission_message", "TextView"),
                el(1, "Allow", PC + "permission_allow_button"), el(2, "Don't allow", PC + "permission_deny_button")]
    for dialog in (location_dialog(), contacts):
        phone = guest_flow(dialog=dialog)
        agent, client, io_ = make(phone, [
            [tool_use("tap_element", "a", index=0)],
            [tool_use("done", "b", summary="ok", success=True)],
        ], tmp_path)
        agent.run("find a chess club near me")
        assert phone.tapped == ["Play as Guest"] and ("key", "BACK") in phone.actions
        assert not io_.questions and len(client.requests) == 2


def test_a_cant_see_refusal_over_a_new_dialog_does_nothing_and_the_dialog_is_dismissed(tmp_path):
    phone = FlowPhone({"bots": ("com.chess", BOTS_SCREEN), "dialog": (CONTROLLER, notification_dialog()),
                       "game": ("com.chess", [el(0, "Resign")])}, "bots", {("dialog", "BACK"): "game"})
    refused = []

    def tap(x, y):
        refused.append(1)
        phone.screen = "dialog"  # the dialog came up as the tap was sent
        raise Unstable("The phone can't see what's there. Look again.")

    phone.tap = tap
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0, target="Play Bots")],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path, verify_taps=False)
    agent.run("play a bot")
    assert io_.steps[0]["is_error"] and "screen changed" in io_.steps[0]["outcome"]
    assert io_.steps[1]["source"] == "auto_dialog" and ("key", "BACK") in phone.actions
    assert len(client.requests) == 2


def test_an_unreadable_window_is_given_a_moment_before_the_model_looks(tmp_path):
    phone = FlowPhone({"app": ("com.chess", APP_SCREEN), "hidden": (None, []),
                       "dialog": (CONTROLLER, notification_dialog()), "bots": ("com.chess", BOTS_SCREEN)}, "app",
                      {("app", "Play as Guest"): "hidden", ("dialog", "BACK"): "bots"})
    real_read = phone.ui_elements
    seen = {"at": None}

    def read():
        if phone.screen == "hidden":
            seen["at"] = seen["at"] or time.monotonic()
            if time.monotonic() - seen["at"] > 0.3:
                phone.screen = "dialog"  # OCR done: the dialog is readable
        return real_read()

    phone.ui_elements = read
    agent, client, io_ = make(phone, [
        [tool_use("tap_element", "a", index=0)],
        [tool_use("done", "b", summary="ok", success=True)],
    ], tmp_path)
    agent.run("open chess as a guest")
    assert phone.tapped == ["Play as Guest"] and ("key", "BACK") in phone.actions and len(client.requests) == 2
