"""In-app links (docs/design/app-links.md): the open_link catalog, the fast link step, and open_url as a step into an app.

Every link passes the checks any link gets (the scheme allowlist, blocked apps, the mode's asks, the phone's gatekeeper);
only `view` entries open on the fast path, with no main-model decision; a link's parameters are the user's words and
stay out of the feed and the steps table, like typed text.
"""

import datetime as dt
from pathlib import Path

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, system_text, text_block, tool_use
from test_fast_path import el

from openmob import permissions as perm
from openmob.agent import app_links as al
from openmob.agent import gate
from openmob.agent.loop import BLOCKED_APP_MESSAGE, CHANGING, REDACTED, Agent
from openmob.agent.tools import APP_LINKS_LINE, OPEN_LINK_TOOL_SPEC, system_prompt
from openmob.config import DEFAULT_CONFIRM_KEYWORDS, Config, load_config
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError

ROOT = Path(__file__).resolve().parent.parent
YT, MAPS, CHROME, WA = "com.google.android.youtube", "com.google.android.apps.maps", "com.android.chrome", "com.whatsapp"
STORE, SPOTIFY, BANK, SMS = "com.android.vending", "com.spotify.music", "com.chase.sig.android", "com.google.android.apps.messaging"
LABELS = {YT: "YouTube", MAPS: "Maps", CHROME: "Chrome", WA: "WhatsApp", STORE: "Google Play Store", SPOTIFY: "Spotify",
          BANK: "Chase", SMS: "Messages"}
# Which app takes which link on this phone (the default handler for anything else is Chrome).
OPENS = {"https://www.youtube.com": YT, "https://youtu.be": YT, "https://www.google.com/maps": MAPS, "market:": STORE,
         "https://wa.me": WA, "https://open.spotify.com": SPOTIFY, "sms:": SMS, "https://secure.chase.com": BANK}
TODAY = dt.date(2026, 10, 8)
DONE = [tool_use("done", "z", summary="ok", success=True)]
YT_SEARCH = "https://www.youtube.com/results?search_query=lofi"


class Phone(FakeDriver):
    """A phone whose links open: `opens` maps a URL prefix to the app that takes it (else Chrome). With `resolves`
    the app says so beforehand (resolve_url); without, it's an older app. `chooser`: prefixes the system would
    show its chooser for."""

    def __init__(self, opens=OPENS, resolves=True, chooser=(), packages=None, elements=None, fail=None):
        super().__init__(packages=packages or list(LABELS), labels=LABELS, elements=elements)
        self.opens, self.chooser, self.fail = opens, chooser, fail
        if not resolves:
            self.resolve_url = None

    def handler(self, url):
        return next((pkg for prefix, pkg in self.opens.items() if url.startswith(prefix) and pkg in self.packages), CHROME)

    def resolve_url(self, url):
        if any(url.startswith(p) for p in self.chooser):
            return {"package": None, "candidates": [self.handler(url), CHROME]}
        return {"package": self.handler(url), "candidates": [self.handler(url)]}

    def open_url(self, url):
        if self.fail:
            raise self.fail
        super().open_url(url)
        self.package = self.handler(url)


def make(responses, task, driver=None, io=None, tmp_path=None, mode=None, settings=None, blocked=None,
         platform="android", **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver if driver is not None else Phone()
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    agent = Agent(ScriptedClient(responses), driver, resolver, config, io, use_ocr=False)
    agent.platform = platform
    perms = perm.TaskPermissions(mode or "auto", lambda: perm.normalize(settings or perm.default_settings()))
    result = agent.run(task, permissions=perms, blocked=blocked)
    return agent, driver, io, steps, result


def tool_result(client, index=1):
    return client.requests[index]["messages"][-1]["content"][0]["content"][0]["text"]


def first_user_text(client, index=0):
    return " ".join(b.get("text", "") for b in client.requests[index]["messages"][0]["content"])


def opened(driver):
    return [a[1] for a in driver.actions if a[0] == "url"]


def link_call(tool_id="a", app="youtube", intent="search", **params):
    return [tool_use("open_link", tool_id, app=app, intent=intent, params=params)]


SAMPLE = {"text": "lofi beats", "number": "+1 555 123 4567", "package": "com.chess", "url": "https://bbc.com/news",
          "id": "dQw4w9WgXcQ", "handle": "@lofigirl", "email": "a@b.co", "date": "today", "choice": "transit"}


# --- the catalog ------------------------------------------------------------------------------------


def test_the_catalog_is_well_formed():
    """Every entry has a class, an app, a template whose placeholders are its parameters, and allowed schemes only;
    only view entries may be fast; the tool offers exactly the catalog's apps."""
    assert set(al.BY_ID) == {l.id for l in al.LINKS} and len(al.BY_ID) == len(al.LINKS)
    for link in al.LINKS:
        assert link.side_effect in al.CLASSES and link.app in al.APPS, link.id
        names = {p.name for p in link.params}
        assert {m for m in __import__("re").findall(r"\{(\w+)\}", link.template)} <= names, link.id
        assert {name for name, _ in link.query} <= names, link.id
        assert all(p.kind in al.KINDS for p in link.params), link.id
        if link.fast:
            assert link.side_effect == al.VIEW, f"{link.id}: only view entries may open without the model"
        url = al.build(link, {p.name: SAMPLE[p.kind] for p in link.params}, TODAY)
        assert gate.url_refusal(url) is None and gate.url_scheme(url) in {"https", "market", "sms", "mailto", "tel"}, link.id
        assert "{" not in url and link.label, link.id
        assert f"{link.app}: " in al.summary() and f"{link.intent}(" in al.summary(), link.id
    assert [l.side_effect for l in al.LINKS if l.side_effect != al.VIEW] == [al.DRAFT] * 3  # no action entry exists
    assert "open_link" in CHANGING  # ask mode asks before it; plan mode needs the plan first
    assert OPEN_LINK_TOOL_SPEC["parameters"]["properties"]["app"]["enum"] == al.APP_IDS
    assert al.find("YouTube", "Search") is al.BY_ID["youtube.search"] and al.find("nope", "x") is None


def test_build_fills_and_encodes_parameters():
    assert al.build(al.find("youtube", "search"), {"q": "lofi & chill"}) == "https://www.youtube.com/results?search_query=lofi%20%26%20chill"
    assert al.build(al.find("spotify", "search"), {"q": "miles davis"}) == "https://open.spotify.com/search/miles%20davis"
    assert al.build(al.find("maps", "directions"), {"destination": "SFO"}) == "https://www.google.com/maps/dir/?api=1&destination=SFO"
    assert al.build(al.find("maps", "directions"), {"destination": "SFO", "origin": "home", "mode": "Transit"}) == \
        "https://www.google.com/maps/dir/?api=1&destination=SFO&origin=home&travelmode=transit"
    assert al.build(al.find("whatsapp", "chat"), {"number": "+1 (555) 123-4567"}) == "https://wa.me/15551234567"
    assert al.build(al.find("play_store", "app"), {"package": "com.chess"}) == "market://details?id=com.chess"
    assert al.build(al.find("play_store", "search"), {"q": "chess"}) == "market://search?c=apps&q=chess"
    assert al.build(al.find("chrome", "open"), {"url": "bbc.com/news"}) == "https://bbc.com/news"
    assert al.build(al.find("youtube", "channel"), {"handle": "lofigirl"}) == "https://www.youtube.com/@lofigirl"
    assert al.build(al.find("calendar", "day"), {"date": "tomorrow"}, TODAY) == "https://calendar.google.com/calendar/u/0/r/day/2026/10/9"
    assert al.build(al.find("calendar", "day"), {"date": "2026-12-01"}) == "https://calendar.google.com/calendar/u/0/r/day/2026/12/1"
    assert al.build(al.find("gmail", "inbox"), None) == "https://mail.google.com/mail/u/0/"
    assert al.build(al.find("messages", "compose"), {"number": "555 123 4567", "body": "see you"}) == "sms:5551234567?body=see%20you"
    assert al.build(al.find("email", "compose"), {"to": "a@b.co", "subject": "hi there"}) == "mailto:a@b.co?subject=hi%20there"
    assert al.build(al.find("phone", "dial"), {"number": "+44 20 7946 0958"}) == "tel:442079460958"


@pytest.mark.parametrize("link,params,why", [
    ("youtube.search", {}, "needs q"), ("youtube.search", {"q": "   "}, "needs q"),
    ("youtube.search", {"zzz": "1"}, "no parameter 'zzz'"), ("gmail.inbox", {"q": "x"}, "no parameter 'q'"),
    ("youtube.search", {"q": "x" * 201}, "q must be"), ("whatsapp.chat", {"number": "12"}, "number must be"),
    ("whatsapp.chat", {"number": "+1 555 123 4567; rm -rf"}, "number must be"),
    ("chrome.open", {"url": "javascript:alert(1)"}, "url must be"), ("chrome.open", {"url": "intent://x#Intent;end"}, "url must be"),
    ("chrome.open", {"url": "youtube"}, "url must be"), ("chrome.open", {"url": "https://x.com/a b"}, "url must be"),
    ("play_store.app", {"package": "chrome"}, "package must be"), ("play_store.app", {"package": "com.x;id=y"}, "package must be"),
    ("maps.directions", {"destination": "SFO", "mode": "flying"}, "mode must be"),
    ("youtube.video", {"id": "../x"}, "id must be"), ("email.compose", {"to": "not an address"}, "to must be"),
    ("calendar.day", {"date": "next monday"}, "date must be"), ("youtube.search", "lofi", "must be an object"),
])
def test_build_refuses_bad_or_missing_values_without_echoing_them(link, params, why):
    with pytest.raises(ValueError, match=why) as info:
        al.build(al.BY_ID[link], params)
    for value in (params.values() if isinstance(params, dict) else [params]):
        assert str(value)[:12] not in str(info.value) or len(str(value)) < 4  # the value is the user's; not repeated


# --- matching a request ------------------------------------------------------------------------------


@pytest.mark.parametrize("task,link,params", [
    ("search YouTube for lofi", "youtube.search", {"q": "lofi"}),
    ("Search for lofi on youtube, please.", "youtube.search", {"q": "lofi"}),
    ("could you play lofi hip hop on youtube", "youtube.search", {"q": "lofi hip hop"}),
    ("open youtube and search for cats and dogs", "youtube.search", {"q": "cats and dogs"}),
    ("Open Spotify and search jazz", "spotify.search", {"q": "jazz"}),
    ("listen to miles davis on spotify", "spotify.search", {"q": "miles davis"}),
    ("directions to the airport in Maps", "maps.directions", {"destination": "the airport"}),
    ("open google maps and get directions from home to work", "maps.directions", {"origin": "home", "destination": "work"}),
    ("navigate to 1 main st on maps", "maps.directions", {"destination": "1 main st"}),
    ("find directions to the airport on maps", "maps.directions", {"destination": "the airport"}),  # not a search
    ("find coffee shops near me on google maps", "maps.search", {"q": "coffee shops near me"}),
    ("show me pharmacies on maps", "maps.search", {"q": "pharmacies"}),
    ("search google maps for coffee", "maps.search", {"q": "coffee"}),
    ("search the play store for chess", "play_store.search", {"q": "chess"}),
    ("open bbc.com", "chrome.open", {"url": "bbc.com"}),
    ("open bbc.com/news in chrome", "chrome.open", {"url": "bbc.com/news"}),
    ("open the browser and go to https://example.com", "chrome.open", {"url": "https://example.com"}),
    ("open a whatsapp chat with +1 (555) 123-4567", "whatsapp.chat", {"number": "+1 (555) 123-4567"}),
    ("open a chat with 15551234567 on whatsapp", "whatsapp.chat", {"number": "15551234567"}),
    ("search amazon for usb-c cable", "amazon.search", {"q": "usb-c cable"}),
    ("google the weather in tokyo", "google.search", {"q": "the weather in tokyo"}),
    ("look up the eiffel tower on google", "google.search", {"q": "the eiffel tower"}),
    ("open my gmail inbox", "gmail.inbox", {}),
    ("what's on my calendar today", "calendar.day", {"date": "today"}),
    ("show my calendar for 2026-10-09", "calendar.day", {"date": "2026-10-09"}),
])
def test_plain_requests_match_one_view_entry(task, link, params):
    m = al.match(task, DEFAULT_CONFIRM_KEYWORDS, TODAY)
    assert m is not None and (m.link.id, m.params, m.missing) == (link, params, ()), task


@pytest.mark.parametrize("task", [
    "open youtube", "open spotify", "open chrome", "open settings", "Open Settings and go to Display",  # launches
    "search youtube for lofi on spotify",  # two apps: ambiguous
    "navigate to the airport", "search for lofi", "check my email",  # no app named
    "send hi to mom on whatsapp", "open youtube and like the video", "open youtube and subscribe to lofi girl",
    "look up the weather on google and send it to mom",  # a side effect in the same breath
    "open youtube and find the subscribe button", "search youtube for the settings menu",  # a screen part, not content
    "message mom on whatsapp", "open a chat with mom on whatsapp", "open my calendar for next monday",  # no value in it
    "search youtube for lofi\nUser correction: no, spotify", "", "   ",
])
def test_requests_that_are_not_plain_go_to_the_model(task):
    assert al.match(task, DEFAULT_CONFIRM_KEYWORDS, TODAY) is None, task


def test_a_trailing_instruction_is_cut_from_the_value():
    m = al.match("search youtube for lofi and play the first video", DEFAULT_CONFIRM_KEYWORDS)
    assert (m.link.id, m.params) == ("youtube.search", {"q": "lofi"})
    m = al.match("search youtube for lofi, then scroll down", DEFAULT_CONFIRM_KEYWORDS)
    assert m.params == {"q": "lofi"}
    # The search opens first; what follows (a cart, a purchase) is taps the guard judges as always.
    m = al.match("search amazon for cables and add one to my cart", DEFAULT_CONFIRM_KEYWORDS)
    assert (m.link.id, m.params) == ("amazon.search", {"q": "cables"})
    assert al.match("search youtube for how to delete my account", DEFAULT_CONFIRM_KEYWORDS).params == {"q": "how to delete my account"}


def test_an_intent_without_plain_values_asks_the_text_model():
    """The app and the intent are plain, the value isn't: the small text model fills it (the loop's job)."""
    m = al.match("on youtube, could you look for some lofi beats", DEFAULT_CONFIRM_KEYWORDS)
    assert m.link.id == "youtube.search" and m.params == {} and [p.name for p in m.missing] == ["q"]
    m = al.match("buy a usb-c cable on amazon", DEFAULT_CONFIRM_KEYWORDS)  # the search opens; buying asks at its taps
    assert m.link.id == "amazon.search" and [p.name for p in m.missing] == ["q"]
    assert al.match("find a usb-c cable on amazon and buy it", DEFAULT_CONFIRM_KEYWORDS).link.id == "amazon.search"
    m = al.match("on whatsapp, message 555 123 4567", DEFAULT_CONFIRM_KEYWORDS)  # the chat opens; the send is judged later
    assert m.link.id == "whatsapp.chat" and [p.name for p in m.missing] == ["number"]
    m = al.match("install chess from the play store", DEFAULT_CONFIRM_KEYWORDS)
    assert m.link.id == "play_store.search" and [p.name for p in m.missing] == ["q"]
    # A side effect in the same breath blocks this reading; a trailing clause is the guard's business later.
    assert al.match("on youtube, could you look for lofi girl and subscribe", DEFAULT_CONFIRM_KEYWORDS) is None
    assert al.match("open youtube and find lofi girl and subscribe", DEFAULT_CONFIRM_KEYWORDS).params == {"q": "lofi girl"}


def test_app_for_url_and_the_feed_form():
    assert al.app_for_url("https://m.youtube.com/watch?v=1").id == "youtube" and al.app_for_url("https://youtu.be/x").id == "youtube"
    assert al.app_for_url("https://www.google.com/maps/search/?api=1&query=x").id == "maps"
    assert al.app_for_url("https://google.com/search?q=1").id == "google"
    assert al.app_for_url("https://en.wikipedia.org") is None and al.app_for_url("market://details?id=x") is None
    assert al.app_for_url("notaurl") is None and al.app_for_url("https://evilyoutube.com") is None
    assert al.without_query(YT_SEARCH) == "https://www.youtube.com/results?…"
    assert al.without_query("https://x.com/a#frag") == "https://x.com/a?…" and al.without_query("https://x.com/a") == "https://x.com/a"
    assert al.is_browser(CHROME) and al.is_browser("org.mozilla.firefox") and not al.is_browser(YT) and not al.is_browser(None)


# --- the tool and the prompt --------------------------------------------------------------------------


def test_the_tool_and_its_prompt_line_follow_the_switch(tmp_path):
    agent, _, _, _, _ = make([DONE], "check", tmp_path=tmp_path)
    request = agent.client.requests[0]
    assert "open_link" in {t["name"] for t in request["tools"]} and APP_LINKS_LINE.strip() in system_text(request)

    agent, _, _, _, _ = make([DONE], "check", tmp_path=tmp_path, app_links=False)
    request = agent.client.requests[0]
    assert "open_link" not in {t["name"] for t in request["tools"]} and APP_LINKS_LINE.strip() not in system_text(request)

    agent, _, _, _, _ = make([DONE], "check", tmp_path=tmp_path, platform="ios")  # the catalog is Android's
    assert "open_link" not in {t["name"] for t in agent.client.requests[0]["tools"]}
    assert "open_link" not in system_prompt() and "open_link" in system_prompt(app_links=True)
    assert "open_settings_page" in system_prompt(app_links=True, settings_pages=True)
    assert "youtube: search(q)" in OPEN_LINK_TOOL_SPEC["description"] and "[draft]" in OPEN_LINK_TOOL_SPEC["description"]


# --- the main model's open_link -----------------------------------------------------------------------


def test_open_link_opens_the_entry_in_its_app_and_is_recorded_without_the_users_words(tmp_path):
    agent, driver, io, steps, result = make([link_call(q="lofi"), DONE], "find lofi", tmp_path=tmp_path)
    assert opened(driver) == [YT_SEARCH]
    assert tool_result(agent.client) == "Opened a YouTube search in YouTube."
    assert ("action", "open_link: Opened a YouTube search in YouTube.") in io.events
    assert result.success and result.prompts == 0 and io.questions == []
    step = steps[0]
    assert step["tool"] == "open_link" and step["args"] == {"app": "youtube", "intent": "search", "params": {"q": REDACTED}}
    assert step["fast"] is False and "source" not in step and not step["is_error"]
    assert "lofi" not in str(steps) and "lofi" not in str(io.events)


def test_open_link_says_when_the_app_didnt_take_the_link(tmp_path):
    # Installed, but this phone sends youtube.com to the browser.
    agent, driver, _, _, _ = make([link_call(q="lofi"), DONE], "find lofi", driver=Phone(opens={}), tmp_path=tmp_path)
    assert opened(driver) == [YT_SEARCH]
    text = tool_result(agent.client)
    assert text.startswith("Opened a YouTube search in Chrome, not the YouTube app") and "launch_app" in text
    # Not installed at all.
    agent, driver, _, _, _ = make([link_call(q="lofi"), DONE], "find lofi", driver=Phone(opens={}, packages=[CHROME]),
                                  tmp_path=tmp_path)
    assert "the YouTube app isn't installed" in tool_result(agent.client) and "install_if_missing" in tool_result(agent.client)
    # Nothing came up (the home screen is still in front).
    still = Phone()
    still.open_url = lambda url: FakeDriver.open_url(still, url)
    agent, driver, _, _, _ = make([link_call(q="lofi"), DONE], "find lofi", driver=still, tmp_path=tmp_path)
    assert "the home screen is still in front" in tool_result(agent.client)


def test_open_link_errors_clearly_without_touching_the_phone(tmp_path):
    calls = [link_call("a", app="youtube", intent="subscribe", handle="@x"), link_call("b", q="x" * 300),
             link_call("c", app="whatsapp", intent="chat", number="12"), link_call("d", app="chrome", intent="open", url="javascript:alert(1)"),
             [tool_use("open_link", "e", app="youtube", intent="search")], DONE]
    agent, driver, io, steps, _ = make(calls, "do things", tmp_path=tmp_path)
    assert opened(driver) == [] and io.questions == []
    assert tool_result(agent.client, 1).startswith("No link 'youtube' / 'subscribe'. The catalog: youtube: search(q)")
    assert tool_result(agent.client, 2) == "Can't open a YouTube search: youtube.search: q must be what to search for."
    assert "number must be" in tool_result(agent.client, 3) and "12" not in tool_result(agent.client, 3)
    assert "url must be" in tool_result(agent.client, 4) and "javascript" not in tool_result(agent.client, 4)
    assert tool_result(agent.client, 5).startswith("Can't open a YouTube search: youtube.search needs q")
    assert all(s["is_error"] for s in steps[:-1]) and steps[-1]["tool"] == "done"


def test_open_link_into_an_app_the_task_may_not_touch_is_refused(tmp_path):
    blocked = lambda package: package in (YT, BANK)  # noqa: E731
    calls = [link_call("a", q="lofi"), link_call("b", app="chrome", intent="open", url="https://secure.chase.com/x"), DONE]
    agent, driver, _, _, _ = make(calls, "do things", tmp_path=tmp_path, blocked=blocked)
    assert opened(driver) == []
    assert tool_result(agent.client, 1).startswith(BLOCKED_APP_MESSAGE)  # the entry's own app
    assert tool_result(agent.client, 2).startswith(BLOCKED_APP_MESSAGE)  # the resolved handler of a web page


def test_draft_entries_open_without_asking_and_action_entries_ask(tmp_path, monkeypatch):
    agent, driver, io, _, _ = make([link_call("a", app="messages", intent="compose", number="5551234567", body="hi"), DONE],
                                   "text them", tmp_path=tmp_path)
    assert opened(driver) == ["sms:5551234567?body=hi"] and io.questions == []
    assert tool_result(agent.client) == "Opened a text message draft in Messages."

    # No action entry exists; one would ask the owner before opening, bound to the exact link, and be refused under Don't ask.
    acting = al.Link("youtube", "subscribe", "a YouTube subscription", "https://www.youtube.com/{handle}?sub_confirmation=1",
                     (al.Param("handle", "the channel", kind="handle"),), side_effect=al.ACTION)
    monkeypatch.setitem(al.BY_ID, acting.id, acting)
    call = link_call("a", app="youtube", intent="subscribe", handle="@lofigirl")
    agent, driver, io, _, result = make([call, DONE], "subscribe", io=RecordingIO(answers=["yes"]), tmp_path=tmp_path)
    assert io.questions == ["I'm about to open a YouTube subscription, which acts as soon as it opens. Should I go ahead?"]
    assert opened(driver) == ["https://www.youtube.com/@lofigirl?sub_confirmation=1"] and result.prompts == 1

    agent, driver, io, _, _ = make([call, DONE], "subscribe", io=RecordingIO(answers=["no"]), tmp_path=tmp_path)
    assert opened(driver) == [] and "did not approve" in tool_result(agent.client)

    never = perm.apply_patch(perm.default_settings(), {"never": "1h"}, {})[0]
    agent, driver, io, _, _ = make([call, DONE], "subscribe", tmp_path=tmp_path, mode="never", settings=never)
    assert opened(driver) == [] and io.questions == []
    assert tool_result(agent.client).startswith("Not allowed on this phone: This link acts as soon as it opens")
    # View entries still open under Don't ask without asking.
    agent, driver, io, _, _ = make([link_call(q="lofi"), DONE], "find lofi", tmp_path=tmp_path, mode="never", settings=never)
    assert opened(driver) == [YT_SEARCH] and io.questions == []


def test_ask_every_step_asks_before_a_link_and_plan_first_needs_the_plan(tmp_path):
    agent, driver, io, _, _ = make([link_call(q="lofi"), DONE], "find lofi", io=RecordingIO(answers=["yes"]),
                                   tmp_path=tmp_path, mode="ask")
    assert io.questions == ["Next step: open a YouTube search. Should I go ahead?"] and opened(driver) == [YT_SEARCH]
    agent, driver, io, _, _ = make([link_call(q="lofi"), DONE], "find lofi", io=RecordingIO(answers=["no"]),
                                   tmp_path=tmp_path, mode="ask")
    assert opened(driver) == [] and "did not approve" in tool_result(agent.client)
    agent, driver, _, _, _ = make([link_call(q="lofi"), DONE], "find lofi", tmp_path=tmp_path, mode="plan")
    assert opened(driver) == [] and tool_result(agent.client).startswith("Plan first")


def test_a_refusal_on_the_phone_reaches_the_model_as_an_error(tmp_path):
    """The gatekeeper judges the URL again on the phone; its refusal (a coded DeviceError) is reported, not retried."""
    refusal = DeviceError("Blocked on the phone: that link opens com.chase.sig.android, a sensitive app. Call done.")
    refusal.code = "gate_blocked_app"
    agent, driver, _, steps, _ = make([link_call(q="lofi"), DONE], "find lofi", driver=Phone(fail=refusal), tmp_path=tmp_path)
    assert tool_result(agent.client).startswith("Error: Blocked on the phone") and steps[0]["is_error"]


# --- open_url as a step into an app --------------------------------------------------------------------


def test_open_url_says_which_app_took_the_link_and_keeps_the_query_out_of_the_feed(tmp_path):
    calls = [[tool_use("open_url", "a", url=YT_SEARCH)], [tool_use("open_url", "b", url="https://en.wikipedia.org/wiki/Lofi")], DONE]
    agent, driver, io, steps, _ = make(calls, "look", tmp_path=tmp_path)
    assert opened(driver) == [YT_SEARCH, "https://en.wikipedia.org/wiki/Lofi"]
    assert tool_result(agent.client, 1) == f"Opened {YT_SEARCH} in YouTube."
    assert tool_result(agent.client, 2) == "Opened https://en.wikipedia.org/wiki/Lofi in Chrome."
    assert ("action", "open_url: Opened https://www.youtube.com/results?… in YouTube.") in io.events
    assert "search_query=lofi" not in str(io.events)
    # The app the link belongs to didn't take it: said so, so the model can adjust.
    agent, driver, _, _, _ = make([[tool_use("open_url", "a", url=YT_SEARCH)], DONE], "look", driver=Phone(opens={}), tmp_path=tmp_path)
    assert tool_result(agent.client).startswith(f"Opened {YT_SEARCH} in Chrome, not the YouTube app")
    # With in-app links off, open_url is exactly what it was.
    agent, driver, _, _, _ = make([[tool_use("open_url", "a", url=YT_SEARCH)], DONE], "look", tmp_path=tmp_path, app_links=False)
    assert opened(driver) == [YT_SEARCH] and tool_result(agent.client) == f"Opened {YT_SEARCH}."


def test_open_url_keeps_its_checks(tmp_path):
    blocked = lambda package: package == BANK  # noqa: E731
    calls = [[tool_use("open_url", "a", url="https://secure.chase.com/transfer")], [tool_use("open_url", "b", url="intent://x#Intent;end")], DONE]
    agent, driver, io, _, _ = make(calls, "look", tmp_path=tmp_path, blocked=blocked)
    assert opened(driver) == [] and io.questions == []
    assert tool_result(agent.client, 1).startswith(BLOCKED_APP_MESSAGE)
    assert tool_result(agent.client, 2).startswith("Not allowed on this phone: Intent: links can run code")


# --- the fast path ------------------------------------------------------------------------------------


def test_a_plain_link_request_opens_first_without_any_model(tmp_path):
    agent, driver, io, steps, result = make([DONE], "search YouTube for lofi", tmp_path=tmp_path)
    assert driver.actions[:2] == [("wake",), ("url", YT_SEARCH)]  # the very first action
    assert len(agent.client.requests) == 1  # the main model only confirmed done
    assert result.success and result.fast_steps == 1 and result.prompts == 0
    step = steps[0]
    assert step["tool"] == "open_link" and step["fast"] is True and step["source"] == "link" and step["model_ms"] == 0
    assert step["args"]["params"] == {"q": REDACTED} and step["outcome"] == "Opened a YouTube search in YouTube."
    assert step["variant"].endswith("+links")
    assert ("action", "open_link: Opened a YouTube search in YouTube.") in io.events and "lofi" not in str(io.events)
    told = first_user_text(agent.client)
    assert "quick helper" in told and "open_link" in told and "Opened a YouTube search in YouTube." in told


def test_the_fast_path_fills_a_value_with_the_small_text_model(tmp_path):
    task = "on youtube, could you look for some lofi beats"
    agent, driver, _, steps, _ = make([[text_block('{"q": "lofi beats"}')], DONE], task, tmp_path=tmp_path)
    assert opened(driver) == ["https://www.youtube.com/results?search_query=lofi%20beats"]
    asked = agent.client.requests[0]
    assert asked["model"] == agent.config.resolver_model and asked.get("tools") is None
    assert "a YouTube search" in asked["messages"][0]["content"] and "what to search for" in asked["messages"][0]["content"]
    assert steps[0]["source"] == "link" and steps[0]["model_ms"] >= 0 and len(agent.client.requests) == 2
    # No value from it: no link step; the main model decides as always.
    agent, driver, _, steps, _ = make([[text_block('{"q": null}')], DONE], task, tmp_path=tmp_path)
    assert opened(driver) == [] and len(agent.client.requests) == 2 and steps[0]["tool"] == "done"


def test_the_fast_path_steps_aside_when_the_link_wouldnt_open_in_its_app(tmp_path):
    for phone, why in ((Phone(opens={}), "the browser would take it"), (Phone(packages=[CHROME]), "the app isn't installed"),
                       (Phone(chooser=("https://www.youtube.com",)), "a chooser would show")):
        agent, driver, io, steps, _ = make([DONE], "search YouTube for lofi", driver=phone, tmp_path=tmp_path)
        assert opened(driver) == [] and steps[0]["tool"] == "done", why
        assert any(k == "status" and m.startswith("Link not taken") for k, m in io.events), why
        assert "lofi" not in str(io.events), why
    # An older app can't say beforehand: the link opens, and where it landed is reported after.
    agent, driver, _, steps, _ = make([DONE], "search YouTube for lofi", driver=Phone(resolves=False), tmp_path=tmp_path)
    assert opened(driver) == [YT_SEARCH] and steps[0]["outcome"] == "Opened a YouTube search in YouTube."


def test_a_misrouted_fast_link_hands_to_the_model_with_what_happened(tmp_path):
    agent, driver, _, steps, _ = make([DONE], "search YouTube for lofi", driver=Phone(opens={}, resolves=False), tmp_path=tmp_path)
    assert opened(driver) == [YT_SEARCH] and steps[0]["source"] == "link" and not steps[0]["is_error"]
    assert "not the YouTube app" in first_user_text(agent.client)


def test_a_cue_that_doesnt_show_hands_to_the_model(tmp_path, monkeypatch):
    """An entry may name a screen cue; the next look must show it, else the main model takes over."""
    cued = al.Link("maps", "route", "a route", "https://www.google.com/maps/dir/?api=1", (al.Param("destination", "where to"),),
                   (("destination", "destination"),), cue="Directions|Start", patterns=(r"^route me to (?P<destination>.+?) in {app}$",))
    monkeypatch.setattr(al, "LINKS", (*al.LINKS, cued))
    monkeypatch.setitem(al.BY_ID, cued.id, cued)
    agent, driver, _, _, _ = make([DONE], "route me to the airport in maps", driver=Phone(elements=[el(0, "Explore")]), tmp_path=tmp_path)
    assert opened(driver) == ["https://www.google.com/maps/dir/?api=1&destination=the%20airport"]
    assert "doesn't show what was expected" in first_user_text(agent.client)
    agent, driver, _, _, _ = make([DONE], "route me to the airport in maps", driver=Phone(elements=[el(0, "Start")]), tmp_path=tmp_path)
    assert "doesn't show what was expected" not in first_user_text(agent.client)


def test_the_fast_path_never_opens_a_draft_or_action_entry(tmp_path, monkeypatch):
    draft = al.Link("messages", "text", "a text", "sms:{number}", (al.Param("number", "n", kind="number"),), side_effect=al.DRAFT,
                    patterns=(r"^text\s+(?P<number>\+?\d[\d\s().-]{5,20}\d)\s+(?:on|in)\s+{app}$",))
    monkeypatch.setattr(al, "LINKS", (*al.LINKS, draft))
    assert al.match("text 5551234567 on messages", DEFAULT_CONFIRM_KEYWORDS).link is draft
    agent, driver, _, steps, _ = make([DONE], "text 5551234567 on messages", tmp_path=tmp_path)
    assert opened(driver) == [] and steps[0]["tool"] == "done"


def test_the_fast_path_follows_the_modes_the_blocks_and_the_switches(tmp_path):
    agent, driver, io, _, _ = make([DONE], "search YouTube for lofi", io=RecordingIO(answers=["yes"]), tmp_path=tmp_path, mode="ask")
    assert io.questions == ["Next step: open a YouTube search. Should I go ahead?"] and opened(driver) == [YT_SEARCH]
    agent, driver, io, _, _ = make([DONE], "search YouTube for lofi", io=RecordingIO(answers=["no"]), tmp_path=tmp_path, mode="ask")
    assert opened(driver) == [] and "did not approve" in first_user_text(agent.client)
    agent, driver, _, _, _ = make([DONE], "search YouTube for lofi", tmp_path=tmp_path, mode="plan")
    assert opened(driver) == []  # nothing before the plan; the model's first turn is the plan
    agent, driver, _, steps, _ = make([DONE], "search YouTube for lofi", tmp_path=tmp_path, blocked=lambda p: p == YT)
    assert opened(driver) == [] and steps[0]["tool"] == "done"
    agent, driver, _, steps, _ = make([DONE], "search YouTube for lofi", tmp_path=tmp_path, app_links_fast=False)
    assert opened(driver) == [] and "open_link" in {t["name"] for t in agent.client.requests[0]["tools"]}
    assert not steps[0]["variant"].endswith("+links")
    agent, driver, _, _, _ = make([DONE], "search YouTube for lofi", tmp_path=tmp_path, app_links=False)
    assert opened(driver) == [] and "open_link" not in {t["name"] for t in agent.client.requests[0]["tools"]}


def test_the_switches_read_the_environment(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    cfg = load_config(path)
    assert cfg.app_links is True and cfg.app_links_fast is True
    monkeypatch.setenv("OPENMOB_APP_LINKS", "off")
    assert load_config(path).app_links is False
    monkeypatch.delenv("OPENMOB_APP_LINKS")
    monkeypatch.setenv("OPENMOB_APP_LINKS_FAST", "off")
    assert load_config(path).app_links_fast is False and load_config(path).app_links is True


def test_the_docs_describe_the_catalog_and_the_classes():
    design = (ROOT / "docs/design/app-links.md").read_text()
    for link in al.LINKS:
        assert f"`{link.id}`" in design, link.id
    safety = (ROOT / "docs/safety.md").read_text()
    assert "open_link" in safety and "`view`" in safety and "`draft`" in safety and "`action`" in safety
    assert "app_links" in (ROOT / "config.example.yaml").read_text()
