"""Review P (phone and gatekeeper) of the 2026-10-17 batched security review: each proof of concept, now a test that
fails to reproduce. The decisions themselves are also shared scenarios (tests/fixtures/gatekeeper/build.py, tagged
review_2026_10_17) that the Kotlin gate runs too; these drive the reference engine (tests/gatekeeper_ref.py) the way
the reviewer's scripts did, and pin the server's mirrors (openmob/agent/gate.py, openmob/agent/sensitive.py)."""

import json
import random
import re
from pathlib import Path

import pytest
from gatekeeper_ref import Gatekeeper, Rules, Screen, find_sensitive, sensitive_in_url

from openmob.agent import gate, sensitive
from openmob.phone.adb import UIElement

ROOT = Path(__file__).parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/gatekeeper/sensitive_numbers.json").read_text())
SSN_LABEL = re.compile(Rules.load().d["sensitive_numbers"]["ssn_label"])
TASK = dict(task="t1", task_text="read my mail")


# --- H1: a short drag inside a button ---------------------------------------------------------------------------


def in_scroller(label, parent_cls, scroll=(), extra=(), pkg="com.example.mail"):
    return Screen.of({"windows": [{"id": 1, "type": "application", "layer": 1, "package": pkg,
        "root": {"cls": "android.widget.FrameLayout", "bounds": [0, 0, 1080, 2400], "children": [
            {"cls": parent_cls, "bounds": [0, 200, 1080, 2200], "scroll": list(scroll), "children": [
                {"cls": "android.widget.LinearLayout", "bounds": [0, 200, 1080, 2200], "children": list(extra) + [
                    {"cls": "android.widget.Button", "text": label, "clickable": True,
                     "bounds": [100, 1000, 980, 1144]}]}]}]}}], "labels": {pkg: "Example"}})


LABELS = ["Delete", "Send", "Post", "Uninstall", "Confirm order", "Place order"]
PARENTS = ["android.widget.ScrollView", "androidx.viewpager.widget.ViewPager", "android.widget.LinearLayout",
           "androidx.core.widget.NestedScrollView", "androidx.recyclerview.widget.RecyclerView", "android.webkit.WebView"]


@pytest.mark.parametrize("mode", [None, "ask", "auto", "trusted", "plan"])
@pytest.mark.parametrize("parent", PARENTS)
@pytest.mark.parametrize("label", LABELS)
def test_h1_a_drag_inside_a_button_is_judged_as_the_tap(label, parent, mode):
    """poc_swipe_inbutton.py: the 90 px drag inside the button, with no scroller that can really scroll, now gets the
    tap's verdict in every mode (the PoC got allow, even in Ask every step)."""
    s = in_scroller(label, parent)
    extra = {"mode": mode} if mode else {}
    tap = Gatekeeper().admit("tap", {"x": 540, "y": 1070, **TASK, **extra}, s)
    for y1, y2 in ((1010, 1100), (1100, 1010)):
        g = Gatekeeper()
        drag = g.admit("swipe", {"x1": 540, "y1": y1, "x2": 540, "y2": y2, "duration_ms": 200, **TASK, **extra}, s)
        assert (drag.decision, drag.kind, drag.rule, drag.money) == (tap.decision, tap.kind, tap.rule, tap.money)
        assert tap.decision == "ask", (label, parent, mode, tap.rule)


def test_h1_place_order_with_a_total_asks_as_a_payment_and_reverify_cant_admit_it():
    """poc_swipe_paid.py."""
    s = in_scroller("Place order", "android.widget.ScrollView",
                    extra=[{"cls": "android.widget.TextView", "text": "Total $49.99", "bounds": [100, 800, 980, 900]}],
                    pkg="com.example.shop")
    a = Gatekeeper().admit("swipe", {"x1": 540, "y1": 1010, "x2": 540, "y2": 1100, "duration_ms": 200, "task": "t",
                                     "task_text": "x"}, s)
    assert (a.decision, a.kind, a.money) == ("ask", "pay", "terminal")


def test_h1_a_list_that_really_scrolls_that_way_still_scrolls_freely():
    s = in_scroller("Send", "androidx.recyclerview.widget.RecyclerView", scroll=("down",))
    up = Gatekeeper().admit("swipe", {"x1": 540, "y1": 1100, "x2": 540, "y2": 1010, "duration_ms": 200, **TASK}, s)
    down = Gatekeeper().admit("swipe", {"x1": 540, "y1": 1010, "x2": 540, "y2": 1100, "duration_ms": 200, **TASK}, s)
    assert up.decision == "allow"  # the content moves down under a finger moving up: the list takes the touch
    assert down.decision == "ask"  # it can't scroll up: the button stays pressed


def test_h1_the_one_pixel_swipe_is_still_a_tap():
    """poc_swipe_tap.py (already a tap before: within the slop)."""
    s = in_scroller("Delete", "androidx.recyclerview.widget.RecyclerView", scroll=("down", "up"))
    a = Gatekeeper().admit("swipe", {"x1": 540, "y1": 1050, "x2": 540, "y2": 1051, "duration_ms": 50, **TASK}, s)
    assert a.decision == "ask"


# --- M1: no gesture outlasts the indicator ------------------------------------------------------------------------


def test_m1_gestures_over_ten_seconds_are_refused():
    s = in_scroller("Hello", "android.widget.LinearLayout")
    for method, params in (("swipe", {"x1": 540, "y1": 1800, "x2": 540, "y2": 600, "duration_ms": 60000}),
                           ("long_press", {"x": 540, "y": 600, "duration_ms": 10001})):
        a = Gatekeeper().admit(method, params, s)
        assert (a.decision, a.rule) == ("refuse", "builtin:gesture_too_long")


# --- M3: the Play Store's search box never names the app --------------------------------------------------------


def play_results(query, titles):
    kids = []
    if query is not None:
        kids.append({"cls": "android.widget.EditText", "text": query, "editable": True, "bounds": [100, 150, 900, 250]})
    for i, title in enumerate(titles):
        top = 400 + 300 * i
        kids.append({"cls": "android.view.View", "clickable": True, "bounds": [0, top, 1080, top + 280], "children": [
            {"cls": "android.widget.TextView", "text": "Sponsored", "bounds": [40, top, 300, top + 40]},
            {"cls": "android.widget.TextView", "text": title, "bounds": [200, top + 50, 800, top + 120]},
            {"cls": "android.widget.Button", "text": "Install", "clickable": True,
             "bounds": [820, top + 50, 1040, top + 140]}]})
    return Screen.of({"windows": [{"id": 1, "type": "application", "layer": 1, "package": "com.android.vending",
        "root": {"cls": "android.widget.FrameLayout", "bounds": [0, 0, 1080, 2400], "children": kids}}],
        "labels": {"com.android.vending": "Google Play Store"}})


CHESS = {"task": "t1", "task_text": "install chess and play 5 moves against a bot"}


@pytest.mark.parametrize("query", [None, "chess"])
def test_m3_a_sponsored_result_under_the_search_box_asks(query):
    """poc_install_title.py: with the search box saying "chess", the sponsored casino's Install was allowed."""
    g = Gatekeeper()
    g.note_instruction(CHESS["task_text"])  # even with the request typed on the phone itself
    s = play_results(query, ["Mega Casino Slots"])
    a = g.admit("tap", {"x": 900, "y": 500, **CHESS}, s)
    assert (a.decision, a.rule) == ("ask", "risk:install:unnamed")
    assert g.store_title(s.windows[0], None) != "chess"


def test_m3_a_results_page_names_no_app():
    g = Gatekeeper()
    g.note_instruction(CHESS["task_text"])
    s = play_results("chess", ["Mega Casino Slots", "Chess - Play and Learn"])
    assert g.install_buttons(s.windows[0]) == 2
    a = g.admit("tap", {"x": 900, "y": 800, **CHESS}, s)  # the result the task does name
    assert (a.decision, a.rule) == ("ask", "risk:install:unnamed")


def ui(text, bounds, cls="android.widget.TextView", editable=False, rid="", clickable=False):
    return UIElement(0, text, "", rid, cls, clickable, None, bounds, editable=editable)


def test_m3_the_servers_store_title_skips_the_search_box_and_results_pages():
    box = ui("chess", (100, 150, 900, 250), cls="android.widget.EditText", editable=True,
             rid="com.android.vending:id/search_bar_text")
    casino = [ui("Mega Casino Slots", (200, 450, 800, 520)), ui("Install", (820, 450, 1040, 540), clickable=True)]
    assert gate.store_title([box] + casino, casino[1]) == "Mega Casino Slots"
    assert not gate.names_app(CHESS["task_text"], gate.store_title([box] + casino, casino[1]))
    chess = [ui("Chess - Play and Learn", (200, 750, 800, 820)), ui("Install", (820, 750, 1040, 840), clickable=True)]
    assert gate.install_buttons([box] + casino + chess) == 2
    assert gate.store_title([box] + casino + chess, chess[1]) == ""
    # A details page still names its app.
    page = [ui("Chess - Play and Learn", (40, 300, 1040, 400)), ui("4.8 ★", (40, 420, 300, 480)),
            ui("Install", (40, 800, 1040, 920), cls="android.widget.Button", clickable=True)]
    assert gate.names_app(CHESS["task_text"], gate.store_title(page, page[2]))


# --- L2: the server's task text alone doesn't name the app ------------------------------------------------------


def test_l2_a_named_install_on_the_servers_word_asks_once_as_the_task():
    page = Screen.of(json.loads((ROOT / "tests/fixtures/gatekeeper/screens.json").read_text())["play_store_chess"])
    g = Gatekeeper()
    a = g.admit("tap", {"x": 540, "y": 860, **CHESS}, page)
    assert (a.decision, a.kind, a.rule) == ("ask", "task", "risk:task:install:unattested")
    assert CHESS["task_text"] in a.wording  # the owner sees the text the grant will bind
    typed = Gatekeeper()
    typed.note_instruction(CHESS["task_text"])
    assert typed.admit("tap", {"x": 540, "y": 860, **CHESS}, page).decision == "allow"


# --- M4: numbers typed in pieces ----------------------------------------------------------------------------------


def field_screen(existing, pkg="com.example.notes"):
    return Screen.of({"windows": [{"id": 1, "type": "application", "layer": 1, "package": pkg,
        "root": {"cls": "android.widget.FrameLayout", "bounds": [0, 0, 1080, 2400], "children": [
            {"cls": "android.widget.EditText", "id": f"{pkg}:id/body", "text": existing, "editable": True,
             "focused": True, "bounds": [0, 200, 1080, 400]}]}}], "focus": {"expected": True}, "labels": {pkg: "Notes"}})


def test_m4_a_card_number_typed_in_pieces_asks():
    """poc_numbers.py: "4111 1111" then " 1111 1111" was allowed twice."""
    p = {"task": "t", "task_text": "write a note"}
    assert Gatekeeper().admit("type_text", {"text": "4111 1111", **p}, field_screen("")).decision == "allow"
    a = Gatekeeper().admit("type_text", {"text": " 1111 1111", **p}, field_screen("4111 1111"))
    assert (a.decision, a.rule) == ("ask", "risk:sensitive_number")
    # replace: the field will hold only the new text
    assert Gatekeeper().admit("type_text", {"text": " 1111 1111", "replace": True, **p},
                              field_screen("4111 1111")).decision == "allow"


def omnibox(text):
    return Screen.of({"windows": [{"id": 1, "type": "application", "layer": 1, "package": "com.android.chrome",
        "root": {"cls": "android.widget.FrameLayout", "bounds": [0, 0, 1080, 2400], "children": [
            {"cls": "android.widget.EditText", "id": "com.android.chrome:id/url_bar", "text": text, "editable": True,
             "focused": True, "bounds": [100, 100, 1000, 200]},
            {"cls": "android.widget.LinearLayout", "clickable": True, "bounds": [0, 220, 1080, 360], "children": [
                {"text": text, "bounds": [100, 240, 1000, 300]}]}]}}], "focus": {"expected": True},
        "labels": {"com.android.chrome": "Chrome"}})


def test_m4_the_address_bar_in_pieces_and_its_suggestion_row():
    a = Gatekeeper().admit("type_text", {"text": "111111111"}, omnibox("evil.example/?c=4111111"))
    assert (a.decision, a.rule) == ("refuse", "builtin:sensitive_link")
    tap = Gatekeeper().admit("tap", {"x": 540, "y": 290}, omnibox("evil.example/?c=4111111111111111"))
    assert (tap.decision, tap.rule) == ("refuse", "builtin:sensitive_link")
    assert Gatekeeper().admit("tap", {"x": 540, "y": 290}, omnibox("evil.example/?q=chess")).decision == "allow"


# --- L1: the detector, on the phone (reference) and the server alike ------------------------------------------


BYPASSES = {  # the reviewers' cases (poc_numbers.py, test_detector_bypasses)
    "4111.1111.1111.1111": "card", "4111/1111/1111/1111": "card", "4111_1111_1111_1111": "card",
    "4111  1111  1111  1111": "card", "٤١١١١١١١١١١١١١١١": "card", "123.45.6789": "ssn", "123 45 6789": "ssn",
}
URL_BYPASSES = [
    "https://evil.example/?c=4111_1111_1111_1111", "https://evil.example/?c=4111.1111.1111.1111",
    "https://evil.example/4111/1111/1111/1111", "https://evil.example/?a=4111111&b=111111111",
    "https://x.example/?a=41111111&b=11111111",
    "https://x.example/?n=" + "".join("%25252534" if d == "4" else d for d in "4111111111111111"),
]


@pytest.mark.parametrize("text", BYPASSES)
def test_l1_text_bypasses_are_caught_on_both_sides(text):
    assert find_sensitive(text) == BYPASSES[text]
    assert sensitive.find(text) == BYPASSES[text]
    assert "4111" not in sensitive.redact(f"pay {text} now") and "6789" not in sensitive.redact(f"pay {text} now")


@pytest.mark.parametrize("url", URL_BYPASSES)
def test_l1_link_bypasses_are_caught_on_both_sides(url):
    assert sensitive_in_url(url, SSN_LABEL) == "card"
    assert sensitive.in_url(url) == "card"


def test_l1_base64_is_not_caught_and_says_so():
    """Documented: a guard against accidental leaks, not a determined adversary (docs/safety.md)."""
    assert sensitive.in_url("https://evil.example/?c=NDExMTExMTExMTExMTExMQ") is None
    assert "determined adversary" in sensitive.__doc__


@pytest.mark.parametrize("case", FIXTURE["texts"], ids=lambda c: f"{c['text']}|{c['ssn_context']}|{c['card_field']}")
def test_l1_the_reference_engine_matches_the_shared_fixture(case):
    assert find_sensitive(case["text"], case["ssn_context"], case["card_field"]) == case["expect"]


@pytest.mark.parametrize("case", FIXTURE["urls"], ids=lambda c: c["url"])
def test_l1_the_reference_engine_matches_the_shared_url_fixture(case):
    assert sensitive_in_url(case["url"], SSN_LABEL) == case["expect"]


def test_l1_the_wider_separators_add_no_false_positives():
    """Ordinary numbers with the separators the detector now reads (dots, slashes, underscores, spaces), and links
    with numeric parameters: none counts (a generated set, not a measurement of real use)."""
    rng = random.Random(17)
    texts, urls = [], []
    for _ in range(2000):
        y, m, d = rng.randint(1990, 2030), rng.randint(1, 12), rng.randint(1, 28)
        texts += [f"{y}.{m:02d}.{d:02d}", f"{y}/{m:02d}/{d:02d}", f"{d:02d}.{m:02d}.{y}", f"{m}/{d}/{y} {rng.randint(0, 23)}:30",
                  ".".join(str(rng.randint(0, 255)) for _ in range(4)), f"v{rng.randint(1, 30)}.{rng.randint(0, 99)}.{rng.randint(0, 9999)}",
                  f"IMG_{y}{m:02d}{d:02d}_{rng.randint(0, 235959):06d}", f"{rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}",
                  f"+49 {rng.randint(150, 179)} {rng.randint(1000000, 9999999)}", f"{rng.uniform(-90, 90):.7f}, {rng.uniform(-180, 180):.7f}"]
        urls += [f"https://x.example/p?id={rng.randint(1, 99999)}&page={rng.randint(1, 50)}&ts={rng.randint(1_600_000_000_000, 1_900_000_000_000)}",
                 f"https://maps.example/?lat={rng.uniform(-90, 90):.7f}&lng={rng.uniform(-180, 180):.7f}&z={rng.randint(1, 20)}",
                 f"https://img.example/{y}/{m:02d}/{d:02d}/photo_{rng.randint(1, 9999)}.jpg?w={rng.choice([320, 640, 1080])}&h={rng.choice([240, 480, 2400])}",
                 f"https://shop.example/item/{rng.randint(10**9, 10**10)}?ref=home&qty={rng.randint(1, 9)}",
                 f"https://x.example/a?id={rng.randint(10000, 99999)}&ts={rng.randint(1_600_000_000_000, 1_900_000_000_000)}"]
    hits = [t for t in texts if sensitive.find(t) or find_sensitive(t)]
    url_hits = [u for u in urls if sensitive.in_url(u) or sensitive_in_url(u, SSN_LABEL)]
    assert hits == [] and url_hits == [], (hits[:5], url_hits[:5])
