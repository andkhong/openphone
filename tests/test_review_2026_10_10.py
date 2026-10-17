"""Fixes from the bug and optimization review of 2026-10-10 (docs/research/bug-opt-review-2026-10-10.md), each from a
real-phone run that day: a stopped task's phone prompt held every later task; a new install didn't resolve; the
classifier tapped greyed-out options and backed out of the screen a task started on; a cold YouTube dropped the
link's query unnoticed; a keyboard Go key asked as a password fill; a model refusal read as "I stopped"."""

import json
from pathlib import Path

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, text_block, tool_use
from test_app_links import LABELS as LABELS_FOR_AMAZON, OPENS as OPENS_FOR_AMAZON, Phone, first_user_text, make
from test_fast_path import el
from test_gatekeeper_server import _Link

from openmob.agent import app_links as al
from openmob.agent import fast, gate
from openmob.agent.loop import KEYBOARD_ENTER, Agent
from openmob.config import Config
from openmob.permissions import TaskPermissions
from openmob.phone import apps
from openmob.phone.apps import AppResolver
from openmob.phone.adb import UIElement
from openmob.phone.base import GateRefused
from openmob.phone.remote import RemoteDriver

RULES = Path(__file__).parent.parent / "android/app/src/main/resources/gatekeeper/rules.json"


# --- lead 1: a stopped task's prompt is withdrawn; a new task's act waits for an open prompt ----------------------

def gk_driver(replies, caps=("gatekeeper", "gate_cancel")):
    driver = RemoteDriver(_Link(replies))
    driver.capabilities = set(caps)
    return driver


def test_a_prompt_not_approved_is_withdrawn_on_the_phone():
    driver = gk_driver([GateRefused("gate_needs_approval", "waiting", approval_id="gk1"), {"cancelled": True}])
    driver.gate_approver = lambda refusal: "cancelled"
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_no_answer" and "stopped" in str(info.value)
    assert driver.link.calls[1] == ("gate_cancel", {"approval_id": "gk1"})  # no mode or task params: not an act
    # A no from anywhere withdraws it too (one the phone already closed answers cancelled: false).
    driver = gk_driver([GateRefused("gate_needs_approval", "waiting", approval_id="gk2"), {"cancelled": False}])
    driver.gate_approver = lambda refusal: "no"
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_denied" and driver.link.calls[1][0] == "gate_cancel"
    # An older app (no gate_cancel capability) isn't sent the call.
    driver = gk_driver([GateRefused("gate_needs_approval", "waiting", approval_id="gk3")], caps=("gatekeeper",))
    driver.gate_approver = lambda refusal: "no"
    with pytest.raises(GateRefused):
        driver.tap(1, 2)
    assert [c[0] for c in driver.link.calls] == ["tap"]


def test_an_act_refused_while_another_prompt_is_open_is_tried_again():
    open_ = GateRefused("gate_prompt_open", "Waiting for the owner to answer a prompt on the phone.",
                        rule="builtin:prompt_open")
    driver = gk_driver([open_, open_, None])
    waits = []
    driver.prompt_waiter = lambda exc, waited: waits.append(waited) or True
    driver.tap(1, 2)
    assert [c[0] for c in driver.link.calls] == ["tap", "tap", "tap"] and len(waits) == 2 and waits[0] < waits[1] + 1
    # The waiter gives up: the refusal is reported.
    driver = gk_driver([open_, open_])
    driver.prompt_waiter = lambda exc, waited: len(driver.link.calls) < 2
    with pytest.raises(GateRefused) as info:
        driver.tap(1, 2)
    assert info.value.code == "gate_prompt_open"
    # A retry that now needs approval goes to the approver as usual.
    driver = gk_driver([open_, GateRefused("gate_needs_approval", "w", approval_id="gk9"), None])
    driver.prompt_waiter = lambda exc, waited: True
    driver.gate_approver = lambda refusal: "yes"
    driver.tap(1, 2)
    assert [c[0] for c in driver.link.calls] == ["tap", "tap", "tap"]


def test_the_agent_waits_for_an_open_prompt_then_stops_waiting(tmp_path):
    agent = Agent(ScriptedClient([]), FakeDriver(), None, Config(gate_prompt_wait_seconds=0.3), RecordingIO(),
                  use_ocr=False)

    class State:
        import threading
        cancel = threading.Event()

    refusal = GateRefused("gate_prompt_open", "w", rule="builtin:prompt_open")
    assert agent._prompt_wait(State, refusal, 0.0) is True
    assert agent._prompt_wait(State, refusal, 0.3) is False  # the limit
    State.cancel.set()
    assert agent._prompt_wait(State, refusal, 0.0) is False  # a stopped task doesn't wait
    assert agent._prompt_wait(None, refusal, 0.0) is False


def test_gate_cancel_is_a_meta_method_on_the_phone():
    rules = json.loads(RULES.read_text())
    assert "gate_cancel" in rules["methods"]["meta"]


# --- lead 3: an app installed since the list was read resolves ------------------------------------------------------

def test_a_new_install_resolves_after_one_more_read(tmp_path, monkeypatch):
    driver = FakeDriver(packages=["com.android.settings"])
    r = AppResolver(driver, label_cache=tmp_path / "l.json")
    assert r.find("Wikipedia") is None
    driver.packages.append("org.wikipedia")  # installed since
    assert r.resolve("Wikipedia") is None  # the cached list
    assert r.find("Wikipedia").package == "org.wikipedia"
    # And the list isn't trusted forever.
    driver.packages.append("com.spotify.music")
    monkeypatch.setattr(apps, "PACKAGES_TTL_S", 0.0)
    assert "com.spotify.music" in r.packages()


# --- lead 5 and 6: the classifier's table ---------------------------------------------------------------------------

def test_greyed_out_elements_are_marked_and_never_fast_tapped():
    light = el(3, "Light", clickable=True)
    light.enabled = False
    dark = el(6, "Dark", clickable=True)
    assert "disabled" in fast.describe(light) and "disabled" not in fast.describe(dark)
    assert set(fast.targets([light, dark])["TAP"]) == {"6"}


# --- lead 8: a search link that lands without its query --------------------------------------------------------------

def test_echo_shown():
    assert al.echo_shown("lofi beats", ["Home", "lofi beats", "Shorts"])
    assert al.echo_shown("lofi beats", ["Search lofi beats"])
    assert al.echo_shown("a very long query about sourdough starters", ["a very long query about"])  # cut short
    assert not al.echo_shown("lofi beats", ["Home", "Shorts", "Subscriptions"])
    assert al.echo_shown("", ["Home"])
    assert al.BY_ID["youtube.search"].echo == "q"


def test_a_youtube_landing_without_the_query_hands_to_the_model(tmp_path):
    agent, driver, _, steps, _ = make([[tool_use("done", "z", summary="ok", success=True)]], "search YouTube for lofi",
                                      driver=Phone(elements=[el(0, "Home"), el(1, "Shorts")]), tmp_path=tmp_path)
    assert steps[0]["source"] == "link" and "doesn't show the search" in first_user_text(agent.client)
    agent, driver, _, steps, _ = make([[tool_use("done", "z", summary="ok", success=True)]], "search YouTube for lofi",
                                      driver=Phone(elements=[el(0, "lofi"), el(1, "Filters")]), tmp_path=tmp_path)
    assert "doesn't show the search" not in first_user_text(agent.client)


# --- lead 9: common words don't name the store page's app ------------------------------------------------------------

def test_names_app_ignores_common_words_and_taglines():
    assert gate.names_app("install chess and play a game", "Chess - Play and Learn")
    assert gate.names_app("install tap titans", "Tap Titans 2: Clicker RPG")
    assert not gate.names_app("tap install on this page", "Tap Titans 2: Clicker RPG")
    assert not gate.names_app("install this", "This War of Mine")
    assert not gate.names_app("install a music app", "Spotify: Music and Podcasts")
    assert not gate.names_app("install google lens", "Google Pay")


def test_the_servers_word_lists_match_the_phones():
    install = json.loads(RULES.read_text())["install"]
    assert gate._GENERIC == set(install["generic"]) and gate._STOP == set(install["stop"])


# --- lead 10: the keyboard's Go key is pressed as ENTER ----------------------------------------------------------------

def test_keyboard_enter_words():
    for target in ("Go key on keyboard", "keyboard Search button", "Enter key", "Done key"):
        assert KEYBOARD_ENTER.search(target), target
    for target in ("Go button", "Search icon", "Send key on keyboard", "Keyboard settings"):
        assert not KEYBOARD_ENTER.search(target), target


def test_a_tap_on_the_keyboards_go_key_is_an_enter(tmp_path):
    field = UIElement(0, "example.com/p4.html", "", "com.android.chrome:id/url_bar", "android.widget.EditText", True,
                      None, (0, 50, 1000, 130), editable=True, focused=True)
    driver = FakeDriver(elements=[field, el(1, "Go", clickable=True)])
    driver.package = "com.android.chrome"
    replies = [[tool_use("tap", "a", x=161, y=347, target="Go key on keyboard")],
               [tool_use("done", "z", summary="ok", success=True)]]
    agent = Agent(ScriptedClient(replies), driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0, fast_path="off"), io := RecordingIO(), use_ocr=False)
    result = agent.run("open the page", permissions=TaskPermissions("auto"), isolated=True)
    assert ("key", "ENTER") in driver.actions, (result.summary, io.events)
    assert not any(a[0] == "tap" for a in driver.actions)


# --- lead 11: a model refusal says so ---------------------------------------------------------------------------------

def test_a_model_refusal_is_reported_as_one(tmp_path):
    driver = FakeDriver(elements=[el(0, "Sourdough")])
    class Refusing(ScriptedClient):
        def create(self, **kwargs):
            reply = super().create(**kwargs)
            reply.stop_reason = "refusal"
            return reply

    client = Refusing([[text_block("")]])
    agent = Agent(client, driver, AppResolver(driver, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0, fast_path="off"), RecordingIO(), use_ocr=False)
    result = agent.run("explain the page", permissions=TaskPermissions("auto"), isolated=True)
    assert not result.success and result.reason == "model_refusal" and "declined to continue" in result.summary


# --- 2026-10-17 performance run: Maps' loading wait, Amazon's unsent search -------------------------------------------

def test_loading_done_ends_on_a_labeled_screen_that_keeps_moving():
    # Maps "coffee near me" waited its full cap (~9.4 s) in every run: pins and tiles keep changing, so the screen never
    # went quiet, though nothing on it said "loading". It ends once nothing has been loading for the grace time.
    from test_wait_until import TimedPhone, wait
    from openmob.agent import waiting

    names = [f"Cafe {a}{b}" for a in "ABCDEFGH" for b in "abcdefgh"]  # letters: the stillness check ignores digits
    schedule = [(i * 0.2, "com.google.android.apps.maps", [el(0, "Results"), el(1, names[i])]) for i in range(60)]
    phone = TimedPhone(schedule, idle=False)
    cond, result = wait(phone, until="loading_done", timeout_s=10)
    assert result.met and "keeps moving" in result.seen
    assert result.waited_ms < (waiting.LOADING_MOVING_GRACE_S + 1.5) * 1000
    # A screen that keeps saying "loading" still waits.
    phone = TimedPhone([(i * 0.2, "a", [el(0, "Loading…"), el(1, names[i])]) for i in range(30)], idle=False)
    cond, result = wait(phone, until="loading_done", timeout_s=4)
    assert not result.met


def test_an_amazon_search_left_unsent_in_the_box_is_submitted_without_the_model(tmp_path):
    amz = "com.amazon.mShop.android.shopping"
    box = el(0, "usb-c cables", cls="android.widget.EditText", editable=True, focused=True)
    phone = Phone(opens={**OPENS_FOR_AMAZON, "https://www.amazon.com": amz}, packages=[*LABELS_FOR_AMAZON, amz],
                  elements=[box])
    agent, driver, _, steps, _ = make([[tool_use("done", "z", summary="ok", success=True)]], "search Amazon for usb-c cables",
                                      driver=phone, tmp_path=tmp_path)
    assert ("key", "ENTER") in driver.actions
    assert [s["tool"] for s in steps[:2]] == ["open_link", "press_key"] and steps[1]["source"] == "link"
    # Results already showing (the query in an unfocused bar): nothing is pressed.
    phone = Phone(opens={**OPENS_FOR_AMAZON, "https://www.amazon.com": amz}, packages=[*LABELS_FOR_AMAZON, amz],
                  elements=[el(0, "usb-c cables"), el(1, "Results")])
    agent, driver, _, steps, _ = make([[tool_use("done", "z", summary="ok", success=True)]], "search Amazon for usb-c cables",
                                      driver=phone, tmp_path=tmp_path)
    assert ("key", "ENTER") not in driver.actions
