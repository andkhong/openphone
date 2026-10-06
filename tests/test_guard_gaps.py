"""What the guard judges is what the action really touches (docs/safety.md#what-is-guarded).

Each test is an attack that worked before these checks: a model lying about a tap's target, a swipe
that is really a tap, ENTER submitting a checkout form, a link into a banking app or a javascript:
URL, and typing into a password field or an address bar. The friction rules still hold alongside:
a requested send runs, add-to-cart and searches don't ask. A fake phone and a recording IO stand in."""

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob import permissions as perm
from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.adb import UIElement
from openmob.phone.apps import AppResolver

WA = "com.whatsapp"
SHOP = "com.amazon.mShop.android.shopping"
CHROME = "com.android.chrome"
BANK = "com.chase.sig.android"
DONE = [tool_use("done", "z", summary="ok", success=True)]


def button(i, text="", bounds=(0, 1000, 1000, 1100), desc="", rid="", cls="Button", clickable=True, **kw):
    return UIElement(i, text, desc, rid, cls, clickable, None, bounds, **kw)


def field(i, text="", bounds=(0, 400, 1000, 500), rid="", hint="", focused=True, password=False):
    el = UIElement(i, text, "", rid, "EditText", True, None, bounds, editable=True, password=password)
    el.focused, el.hint = focused, hint  # (set after, so these tests also run against older code)
    return el


def agent(steps, tmp_path, answers=(), package=SHOP, elements=(), mode="auto", driver=None, blocked=None):
    driver = driver or FakeDriver(packages=[WA, SHOP, CHROME, BANK], labels={WA: "WhatsApp", SHOP: "Amazon"})
    driver.elements = list(elements)
    driver.package = package
    io = RecordingIO(list(answers))
    resolver = AppResolver(driver, {"whatsapp": WA, "amazon": SHOP}, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    a = Agent(ScriptedClient([*steps, DONE, DONE]), driver, resolver, Config(settle_delay=0, launch_settle_delay=0),
              io, use_ocr=False)
    store = {"s": perm.normalize(perm.default_settings())}
    return a, driver, io, perm.TaskPermissions(mode, lambda: store["s"]), blocked


def run(a, perms, task, blocked=None):
    return a.run(task, permissions=perms, blocked=blocked)


def did(driver, kind):
    return [x for x in driver.actions if x[0] == kind]


# --- 1. taps are judged by what is at the point, not by the model's words ----------------------------


def test_a_model_lying_about_a_tap_target_is_judged_by_the_real_element(tmp_path):
    # "Buy now" spans y 1000-1100 on the device; the screenshot is half size, so (250, 525) hits it.
    lie = [[tool_use("tap", "a", x=250, y=525, target="Display settings")]]
    a, driver, io, perms, _ = agent(lie, tmp_path, ["no"], elements=[button(0, "Buy now")])
    run(a, perms, "change the display settings")
    assert len(io.questions) == 1 and "Buy now" in io.questions[0] and not did(driver, "tap")


def test_long_press_of_any_length_is_judged_as_a_tap(tmp_path):
    steps = [[tool_use("long_press", "a", x=250, y=525, target="a photo", duration_ms=50)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, ["no"], elements=[button(0, "Place order")])
    run(a, perms, "look at the photo")
    assert io.questions and "Place order" in io.questions[0] and not did(driver, "long_press")


def test_tap_element_uses_the_elements_own_fields_and_what_its_center_hits(tmp_path):
    # Its text says "Continue" (a step on the way), but its resource id is place_order.
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=0, target="Continue")]], tmp_path, ["no"],
                                    elements=[button(0, "Continue", rid="place_order")])
    run(a, perms, "continue")
    assert io.questions and not did(driver, "tap")
    # A clickable container with no words of its own; the "Buy now" label inside it is what is tapped.
    screen = [button(0, cls="FrameLayout"), button(1, "Buy now", bounds=(100, 1010, 900, 1090), cls="TextView",
                                                   clickable=False)]
    a, driver, io, perms, _ = agent([[tool_use("tap_element", "a", index=0, target="the product")]], tmp_path,
                                    ["no"], elements=screen)
    run(a, perms, "open the product")
    assert io.questions and "Buy now" in io.questions[0] and not did(driver, "tap")


def test_an_unlabeled_spot_asks_in_shopping_apps_only(tmp_path):
    steps = [[tool_use("tap", "a", x=250, y=525, target="the picture")]]
    a, driver, io, perms, _ = agent(steps, tmp_path, ["no"], elements=[button(0, cls="ImageView")])
    run(a, perms, "look at the picture")
    assert io.questions and not did(driver, "tap")
    a, driver, io, perms, _ = agent(steps, tmp_path, package=WA, elements=[button(0, cls="ImageView")])
    run(a, perms, "look at the picture")
    assert not io.questions and did(driver, "tap")


def test_the_models_words_only_add_caution(tmp_path):
    # The element is harmless, but the model calls it "Send": that asks (unrequested).
    a, driver, io, perms, _ = agent([[tool_use("tap", "a", x=250, y=525, target="Send")]], tmp_path, ["no"],
                                    package=WA, elements=[button(0, "Attach")])
    run(a, perms, "open the chat")
    assert io.questions and not did(driver, "tap")
    # A risky element the request didn't name: the model's "Send" can't make it "requested".
    a, driver, io, perms, _ = agent([[tool_use("tap", "a", x=250, y=525, target="Send")]], tmp_path, ["no"],
                                    package=WA, elements=[button(0, "Delete chat")])
    run(a, perms, "send hi to mom")
    assert io.questions and not did(driver, "tap")


# --- 2. swipes -------------------------------------------------------------------------------------


@pytest.mark.parametrize("move, duration", [((5, 0), 300), ((300, 0), 80)])
def test_a_swipe_that_is_really_a_tap_is_judged_as_one(tmp_path, move, duration):
    # Under the touch slop (or quicker than a drag): Android delivers it to "Buy now" as a tap.
    steps = [[tool_use("swipe", "a", x1=100, y1=525, x2=100 + move[0], y2=525 + move[1], duration_ms=duration)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, ["no"], elements=[button(0, "Buy now")])
    run(a, perms, "scroll the page")
    assert io.questions and "Buy now" in io.questions[0] and not did(driver, "swipe")


def test_a_slide_to_pay_slider_asks_but_scrolling_doesnt(tmp_path):
    slider = button(0, "Slide to pay", cls="SeekBar", bounds=(0, 2000, 1000, 2100))
    steps = [[tool_use("swipe", "a", x1=20, y1=1025, x2=480, y2=1025, duration_ms=600)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, ["no"], elements=[slider])
    run(a, perms, "look at the order")
    assert io.questions and not did(driver, "swipe")
    feed = [button(0, cls="RecyclerView", clickable=False, bounds=(0, 0, 1080, 2400)), button(1, "Buy now")]
    steps = [[tool_use("scroll", "a", direction="down")],
             [tool_use("swipe", "b", x1=270, y1=900, x2=270, y2=300, duration_ms=400)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, elements=feed)
    run(a, perms, "scroll the page")
    assert not io.questions and len(did(driver, "swipe")) == 2


# --- 4. ENTER commits ------------------------------------------------------------------------------


def checkout_form():
    return [field(0, "4111 1111 1111 1111", rid="card"), button(1, "Order total: $12.99", cls="TextView"),
            UIElement(2, "", "", "", "WebView", False, None, (0, 200, 1080, 2300))]


def test_enter_in_a_checkout_form_asks_as_money(tmp_path):
    a, driver, io, perms, _ = agent([[tool_use("press_key", "a", key="ENTER")]], tmp_path, ["no"], package=CHROME,
                                    elements=checkout_form())
    run(a, perms, "fill in the form")
    assert io.questions and "$12.99" in io.questions[0] and ("key", "ENTER") not in driver.actions
    # type_text's submit is the same ENTER.
    a, driver, io, perms, _ = agent([[tool_use("type_text", "a", text="123", submit=True)]], tmp_path, ["no"],
                                    package=CHROME, elements=checkout_form())
    run(a, perms, "fill in the form")
    assert io.questions and not did(driver, "type") and ("key", "ENTER") not in driver.actions


def test_enter_sends_in_messaging_apps_and_a_requested_send_runs(tmp_path):
    steps = [[tool_use("type_text", "a", text="hi mom", submit=True)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, package=WA, elements=[field(0, rid="entry")])
    run(a, perms, "send hi to mom")
    assert not io.questions and ("key", "ENTER") in driver.actions  # asked for: no prompt
    a, driver, io, perms, _ = agent(steps, tmp_path, ["no"], package=WA, elements=[field(0, rid="entry")])
    run(a, perms, "open my chat with mom")
    assert len(io.questions) == 1 and "send" in io.questions[0] and ("key", "ENTER") not in driver.actions


def test_enter_in_a_search_box_doesnt_ask(tmp_path):
    steps = [[tool_use("type_text", "a", text="usb cable", submit=True)]]
    a, driver, io, perms, _ = agent(steps, tmp_path, elements=[field(0, hint="Search Amazon"),
                                                               button(1, "Order total: $12.99", cls="TextView")])
    run(a, perms, "find a usb cable")
    assert not io.questions and ("key", "ENTER") in driver.actions


# --- 5. links -------------------------------------------------------------------------------------


class ResolvingDriver(FakeDriver):
    """A phone whose app can say which app a link opens (resolve_url)."""

    def __init__(self, handlers, **kw):
        super().__init__(**kw)
        self.handlers = handlers

    def resolve_url(self, url):
        for prefix, package in self.handlers.items():
            if url.startswith(prefix):
                return {"package": package, "candidates": [package]}
        return {"package": CHROME, "candidates": [CHROME]}


class OpeningDriver(FakeDriver):
    """An older app: no resolve_url, and the link really opens its app; BACK leaves it."""

    def __init__(self, opens, **kw):
        super().__init__(**kw)
        self.opens = opens

    def open_url(self, url):
        super().open_url(url)
        self.previous, self.package = self.package, self.opens

    def key(self, name):
        super().key(name)
        if name == "BACK":
            self.package = self.previous


def blocks_bank(package):
    return package == BANK


@pytest.mark.parametrize("url", ["javascript:alert(document.cookie)", "  JavaScript:alert(1)", "intent://pay#Intent;end",
                                 "file:///sdcard/secret.txt", "content://contacts/people", "data:text/html,hi",
                                 "chase://transfer?to=evil"])
def test_open_url_refuses_dangerous_and_custom_schemes(tmp_path, url):
    a, driver, io, perms, _ = agent([[tool_use("open_url", "a", url=url)]], tmp_path, package=CHROME)
    run(a, perms, "open the link")
    assert not did(driver, "url") and not io.questions


def test_open_url_into_a_blocked_app_is_refused_before_opening(tmp_path):
    driver = ResolvingDriver({"https://secure.chase.com": BANK}, packages=[CHROME, BANK])
    steps = [[tool_use("open_url", "a", url="https://secure.chase.com/transfer")],
             [tool_use("open_url", "b", url="https://example.com")]]
    a, driver, io, perms, _ = agent(steps, tmp_path, package=CHROME, driver=driver)
    run(a, perms, "open the links", blocked=blocks_bank)
    assert did(driver, "url") == [("url", "https://example.com")]


def test_open_url_on_an_older_app_backs_out_of_a_blocked_app(tmp_path):
    driver = OpeningDriver(BANK, packages=[CHROME, BANK])
    a, driver, io, perms, _ = agent([[tool_use("open_url", "a", url="https://secure.chase.com")]], tmp_path,
                                    package=CHROME, driver=driver)
    run(a, perms, "open the link", blocked=blocks_bank)
    assert ("key", "BACK") in driver.actions and driver.package == CHROME


# --- 6. typing ------------------------------------------------------------------------------------


def test_javascript_typed_into_the_address_bar_is_refused(tmp_path):
    bar = field(0, "https://news.example.com", rid="url_bar")
    a, driver, io, perms, _ = agent([[tool_use("type_text", "a", text="javascript:alert(1)", submit=True)]],
                                    tmp_path, package=CHROME, elements=[bar])
    run(a, perms, "go to the page")
    assert not did(driver, "type") and ("key", "ENTER") not in driver.actions
    # The same text in a chat is just text.
    a, driver, io, perms, _ = agent([[tool_use("type_text", "a", text="data: 5 GB left")]], tmp_path, package=WA,
                                    elements=[field(0, rid="entry")])
    run(a, perms, "tell mom")
    assert did(driver, "type")


def test_enter_in_the_address_bar_runs_the_link_checks(tmp_path):
    driver = ResolvingDriver({"https://secure.chase.com": BANK}, packages=[CHROME, BANK])
    bar = field(0, "secure.chase.com/transfer", rid="url_bar")
    a, driver, io, perms, _ = agent([[tool_use("press_key", "a", key="ENTER")]], tmp_path, package=CHROME,
                                    elements=[bar], driver=driver)
    run(a, perms, "open it", blocked=blocks_bank)
    assert ("key", "ENTER") not in driver.actions
    # A search typed into the address bar is just a search.
    a, driver, io, perms, _ = agent([[tool_use("type_text", "a", text="weather today", submit=True)]], tmp_path,
                                    package=CHROME, elements=[field(0, "", rid="url_bar")])
    run(a, perms, "check the weather")
    assert not io.questions and ("key", "ENTER") in driver.actions


def test_typing_into_a_password_field_is_refused_in_every_mode(tmp_path):
    for mode in ("auto", "ask"):
        a, driver, io, perms, _ = agent([[tool_use("type_text", "a", text="hunter2")]], tmp_path, ["yes"], package=WA,
                                        elements=[field(0, password=True)], mode=mode)
        run(a, perms, "log in")
        assert not did(driver, "type") and not io.questions
    # Right after the tap that focused it (the same step, before the screen is read again).
    steps = [[tool_use("tap_element", "a", index=0), tool_use("type_text", "b", text="hunter2")]]
    a, driver, io, perms, _ = agent(steps, tmp_path, package=WA, elements=[field(0, password=True, focused=False),
                                                                           field(1, focused=False)])
    run(a, perms, "log in")
    assert did(driver, "tap") and not did(driver, "type")


# --- friction still holds ----------------------------------------------------------------------------


def test_friction_rules_still_hold(tmp_path):
    cart = [button(0, "Add to cart")]
    a, driver, io, perms, _ = agent([[tool_use("tap", "a", x=250, y=525, target="Add to cart")]], tmp_path,
                                    elements=cart)
    run(a, perms, "put it in my cart")
    assert not io.questions and did(driver, "tap")
    send = [button(0, desc="Send", rid="send")]
    a, driver, io, perms, _ = agent([[tool_use("tap", "a", x=250, y=525, target="Send")]], tmp_path, package=WA,
                                    elements=send)
    run(a, perms, "send hi to mom")
    assert not io.questions and did(driver, "tap")
    wifi = [button(0, "Network & internet", cls="TextView", bounds=(0, 60, 600, 140)), button(1, "Wi-Fi")]
    a, driver, io, perms, _ = agent([[tool_use("tap", "a", x=250, y=525, target="Wi-Fi")]], tmp_path,
                                    package="com.android.settings", elements=wifi)
    run(a, perms, "turn on wifi")
    assert not io.questions and did(driver, "tap")
