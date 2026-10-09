"""Settings deep links (docs/design/deep-links.md): the open_settings_page tool and the phone's open_settings method.

The phone holds a fixed key-to-intent table; the server sends only a key. The tool is offered only to phones that
advertise the capability, everyday pages open without asking, an ask-tier page asks the owner (refused under Don't
ask), an app's own pages are refused for blocked apps, and a phone without the page tells the agent to navigate.
"""

import re
import time
from pathlib import Path

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, system_text, tool_use
from test_adb import RecordingDriver
from test_fast_path import Judge, el
from test_remote import FakePhone, server  # noqa: F401  (server is a fixture)

from openmob import permissions as perm
from openmob.agent import fast
from openmob.agent.loop import BLOCKED_APP_MESSAGE, CHANGING, DEEP_LINK_SOURCE, Agent
from openmob.agent.tools import OPEN_SETTINGS_TOOL, OPEN_SETTINGS_TOOL_SPEC, SETTINGS_PAGE_LINE, system_prompt
from openmob.config import Config
from openmob.phone import settings_pages as sp
from openmob.phone.adb import AdbError
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError
from openmob.phone.protocol import METHODS

ROOT = Path(__file__).resolve().parent.parent
KOTLIN_TABLE = ROOT / "android/app/src/main/java/dev/openmob/companion/SettingsPages.kt"
CHROME, CHASE, SETTINGS, OWN = "com.android.chrome", "com.chase.sig.android", "com.android.settings", sp.OWN_PACKAGE
LABELS = {CHROME: "Chrome", CHASE: "Chase", SETTINGS: "Settings", OWN: "OpenMob"}
DONE = [tool_use("done", "z", summary="ok", success=True)]


class Phone(FakeDriver):
    """A phone whose app opens Settings pages directly (hello capability open_settings)."""

    can_open_settings = True

    def __init__(self, fail=None, **kw):
        super().__init__(packages=list(LABELS), labels=LABELS, **kw)
        self.fail = fail  # the phone's error for every open_settings call (an OEM without the page)

    def open_settings(self, page, package=None):
        if self.fail:
            raise DeviceError(self.fail)
        return super().open_settings(page, package)


def make(responses, driver=None, io=None, judge=None, tmp_path=None, mode=None, settings=None, blocked=None, **cfg):
    config = Config(settle_delay=0, launch_settle_delay=0, **cfg)
    driver = driver if driver is not None else Phone()
    resolver = AppResolver(driver, {"settings": SETTINGS}, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    agent = Agent(ScriptedClient(responses), driver, resolver, config, io, judge=judge, use_ocr=False)
    perms = perm.TaskPermissions(mode or "auto", lambda: perm.normalize(settings or perm.default_settings()))
    result = agent.run("open the settings page", permissions=perms, blocked=blocked)
    return agent, driver, io, steps, result


def tool_result(client, index=1):
    return client.requests[index]["messages"][-1]["content"][0]["content"][0]["text"]


def opened(driver):
    return [a for a in driver.actions if a[0] == "open_settings"]


# --- the table ---------------------------------------------------------------------------------


def test_the_servers_table_matches_the_phones():
    """Both apps hold the same fixed table (SettingsPages.kt): keys in order, actions, tiers, package rules."""
    rows = re.findall(r'^\s*page\("(\w+)", "([^"]+)", "([^"]+)"(.*?)\),', KOTLIN_TABLE.read_text(), re.M)
    assert [r[0] for r in rows] == sp.PAGE_KEYS
    for key, action, title, extra in rows:
        page = sp.PAGES[key]
        assert (action, title) == (page.action, page.title), key
        assert ("Tier.ASK" in extra) == (page.tier == sp.ASK), key
        assert ("needsPackage = true" in extra) == page.needs_package, key


def test_the_table_has_only_everyday_and_ask_pages():
    """No key reaches a page the gatekeeper hard-denies (phone-gatekeeper.md §2.3), and only app pages need a package."""
    denied = re.compile(r"ACCESSIBILITY|DEVELOPMENT|SECURITY|VPN|LOCALE|LANGUAGE|USER|ACCOUNT|DEVICE_ADMIN|"
                        r"NOTIFICATION_LISTENER|NOTIFICATION_POLICY|USAGE_ACCESS|MANAGE_UNKNOWN|OVERLAY|INPUT_METHOD|"
                        r"WIRELESS_DEBUG|RESET", re.I)
    for page in sp.PAGES.values():
        assert page.action.startswith(("android.settings.", "android.intent.action.")) and not denied.search(page.action)
        assert page.tier in (sp.EVERYDAY, sp.ASK)
    assert [p.key for p in sp.PAGES.values() if p.tier == sp.ASK] == ["date_time"]
    assert [p.key for p in sp.PAGES.values() if p.needs_package] == ["app_details", "app_notifications"]
    assert "open_settings" in METHODS and OPEN_SETTINGS_TOOL_SPEC["parameters"]["properties"]["page"]["enum"] == sp.PAGE_KEYS
    assert "open_settings_page" in CHANGING  # ask mode asks before it; plan mode needs the plan first


def test_requests_name_pages_exactly_or_not_at_all():
    assert sp.page_for("Display").key == "display" and sp.page_for("the Wi-Fi settings").key == "wifi"
    assert sp.page_for("wifi").key == "wifi" and sp.page_for("Date & time").key == "date_time"
    assert sp.page_for("airplane mode page").key == "airplane_mode" and sp.page_for("Battery").key == "battery"
    for text in ("Display size", "Chrome notifications", "App info", "settings", "", None):
        assert sp.page_for(text) is None, text
    assert fast.settings_page("Open Settings and go to Display") == "display"
    assert fast.settings_page("go to display in settings") == "display"
    assert fast.settings_page("show me the battery page") == "battery"
    assert fast.settings_page("open the wifi settings") == "wifi"
    # Another app, a side effect, or no page named: the usual path.
    for task in ("Open Chrome and go to Display", "turn on dark mode in display settings", "open settings",
                 "open Chrome's notification settings"):
        assert fast.settings_page(task) is None, task
    assert sp.valid_package("com.android.chrome") and not sp.valid_package("chrome") and not sp.valid_package("")
    assert not sp.valid_package("package:com.x") and not sp.valid_package("com.x; rm")


# --- the tool ----------------------------------------------------------------------------------


def test_the_tool_and_its_prompt_line_are_offered_only_when_the_phone_can(tmp_path):
    agent, _, _, _, _ = make([DONE], driver=FakeDriver(packages=[SETTINGS]), tmp_path=tmp_path)
    request = agent.client.requests[0]
    assert "open_settings_page" not in {t["name"] for t in request["tools"]}
    assert SETTINGS_PAGE_LINE.strip() not in system_text(request)

    agent, _, _, _, _ = make([DONE], tmp_path=tmp_path)
    request = agent.client.requests[0]
    assert "open_settings_page" in {t["name"] for t in request["tools"]}
    assert SETTINGS_PAGE_LINE.strip() in system_text(request)
    assert "open_settings_page" not in system_prompt() and "open_settings_page" in system_prompt(settings_pages=True)


def test_an_everyday_page_opens_directly_and_is_recorded(tmp_path):
    agent, driver, io, steps, result = make([[tool_use("open_settings_page", "a", page="display")], DONE],
                                            tmp_path=tmp_path)
    assert opened(driver) == [("open_settings", "display", None)]
    assert tool_result(agent.client).startswith("Opened the Display Settings page. Some phones show this page in their own app")
    assert io.questions == [] and result.success and result.prompts == 0
    assert steps[0]["tool"] == "open_settings_page" and steps[0]["args"] == {"page": "display"} and not steps[0]["is_error"]
    assert ("action", "open_settings_page: Opened the Display Settings page.") in io.events


def test_an_unknown_page_or_an_older_app_gets_a_clear_error_and_the_phone_is_not_asked(tmp_path):
    agent, driver, _, _, _ = make([[tool_use("open_settings_page", "a", page="developer_options")], DONE], tmp_path=tmp_path)
    assert opened(driver) == []
    assert tool_result(agent.client).startswith("No Settings page 'developer_options'. Pages: display, wifi, ")

    old = FakeDriver(packages=[SETTINGS])  # an app that didn't advertise the capability (the model may still try)
    agent, driver, _, _, _ = make([[tool_use("open_settings_page", "a", page="display")], DONE], driver=old, tmp_path=tmp_path)
    assert opened(driver) == []
    assert "can't open Settings pages directly" in tool_result(agent.client) and "launch_app" in tool_result(agent.client)


def test_a_phone_without_the_page_tells_the_agent_to_navigate(tmp_path):
    phone = Phone(fail="IllegalStateException: This phone has no Display Settings page. Navigate there instead.")
    agent, _, _, steps, _ = make([[tool_use("open_settings_page", "a", page="display")], DONE], driver=phone, tmp_path=tmp_path)
    text = tool_result(agent.client)
    assert text.startswith("Couldn't open the Display Settings page directly (") and "navigate there instead" in text
    assert steps[0]["is_error"]


def test_an_apps_own_pages_take_its_name_and_refuse_apps_the_task_may_not_touch(tmp_path):
    agent, driver, _, _, _ = make([[tool_use("open_settings_page", "a", page="app_notifications", package="Chrome")],
                                   [tool_use("open_settings_page", "b", page="app_details", package=CHROME)], DONE],
                                  tmp_path=tmp_path)
    assert opened(driver) == [("open_settings", "app_notifications", CHROME), ("open_settings", "app_details", CHROME)]
    assert tool_result(agent.client).startswith("Opened the App notifications Settings page of Chrome.")

    blocked = lambda package: package == CHASE  # noqa: E731
    agent, driver, _, _, _ = make([[tool_use("open_settings_page", "a", page="app_details", package="Chase")],
                                   [tool_use("open_settings_page", "b", page="app_notifications", package=OWN)],
                                   [tool_use("open_settings_page", "c", page="app_details")],
                                   [tool_use("open_settings_page", "d", page="app_details", package="Nope")], DONE],
                                  tmp_path=tmp_path, blocked=blocked)
    assert opened(driver) == []
    assert tool_result(agent.client, 1).startswith(BLOCKED_APP_MESSAGE)  # a blocked app's pages
    assert tool_result(agent.client, 2).startswith(BLOCKED_APP_MESSAGE)  # OpenMob's own pages
    assert tool_result(agent.client, 3).startswith("The App info page needs an installed app")
    assert tool_result(agent.client, 4).startswith("The App info page needs an installed app")


def test_the_server_never_opens_a_page_its_own_rules_refuse(tmp_path):
    """The server's Settings word lists judge deep links too: "date & time" is refused by default (docs/safety.md),
    whatever the table says, so a deep link reaches nothing a tap couldn't."""
    agent, driver, io, _, _ = make([[tool_use("open_settings_page", "a", page="date_time")], DONE], tmp_path=tmp_path)
    assert opened(driver) == [] and io.questions == []
    assert tool_result(agent.client).startswith("Not allowed on this phone: This Settings page is security-critical")


def test_an_ask_page_asks_the_owner_and_is_refused_under_dont_ask(tmp_path):
    call = [tool_use("open_settings_page", "a", page="date_time")]
    agent, driver, io, _, result = make([call, DONE], io=RecordingIO(answers=["yes"]), tmp_path=tmp_path,
                                        settings_refuse_terms=[])
    assert io.questions == ["I'm about to open the Date & time Settings page. Should I go ahead?"]
    assert opened(driver) == [("open_settings", "date_time", None)] and result.prompts == 1

    agent, driver, io, _, _ = make([call, DONE], io=RecordingIO(answers=["no"]), tmp_path=tmp_path, settings_refuse_terms=[])
    assert opened(driver) == [] and "did not approve" in tool_result(agent.client)

    never = perm.apply_patch(perm.default_settings(), {"never": "1h"}, {})[0]
    agent, driver, io, _, _ = make([call, DONE], tmp_path=tmp_path, settings_refuse_terms=[], mode="never", settings=never)
    assert opened(driver) == [] and io.questions == []
    assert tool_result(agent.client).startswith("Not allowed on this phone: This Settings page asks the owner first")

    # Everyday pages still don't ask under Don't ask.
    agent, driver, io, _, _ = make([[tool_use("open_settings_page", "a", page="wifi")], DONE], tmp_path=tmp_path,
                                   mode="never", settings=never)
    assert opened(driver) == [("open_settings", "wifi", None)] and io.questions == []


def test_ask_every_step_asks_before_opening_a_page(tmp_path):
    call = [tool_use("open_settings_page", "a", page="display")]
    agent, driver, io, _, _ = make([call, DONE], io=RecordingIO(answers=["yes"]), tmp_path=tmp_path, mode="ask")
    assert io.questions == ["Next step: open the Display Settings page. Should I go ahead?"]
    assert opened(driver) == [("open_settings", "display", None)]
    agent, driver, io, _, _ = make([call, DONE], io=RecordingIO(answers=["no"]), tmp_path=tmp_path, mode="ask")
    assert opened(driver) == [] and "did not approve" in tool_result(agent.client)


def test_plan_first_needs_the_plan_before_a_deep_link(tmp_path):
    agent, driver, _, _, _ = make([[tool_use("open_settings_page", "a", page="display")], DONE], tmp_path=tmp_path, mode="plan")
    assert opened(driver) == [] and tool_result(agent.client).startswith("Plan first")


# --- the fast path -----------------------------------------------------------------------------


HOME = [el(0, "Chrome"), el(1, "Settings"), el(2, "Chase")]


def test_a_plain_settings_page_request_takes_the_deep_link_without_the_main_model(tmp_path):
    driver = Phone(elements=HOME)
    config = Config(settle_delay=0, launch_settle_delay=0, fast_path="on", fast_path_done=False)
    resolver = AppResolver(driver, {"settings": SETTINGS}, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    io = RecordingIO()
    steps = []
    io.record_step = steps.append
    client = ScriptedClient([DONE])
    agent = Agent(client, driver, resolver, config, io, judge=Judge([("LAUNCH_APP", None, 0.95)]), use_ocr=False)
    result = agent.run("Open Settings and go to Display")
    assert result.success and result.fast_steps >= 1
    assert opened(driver) == [("open_settings", "display", None)] and not [a for a in driver.actions if a[0] == "launch"]
    assert steps[0]["tool"] == "open_settings_page" and steps[0]["fast"] is True
    assert len(client.requests) == 1  # the main model only confirmed done


def test_the_fast_path_launches_as_usual_when_another_app_or_an_old_app_is_involved(tmp_path):
    for driver, task, expect in ((Phone(elements=HOME), "Open Chrome and go to Display", ("launch", CHROME)),
                                 (FakeDriver(packages=list(LABELS), labels=LABELS, elements=HOME),
                                  "Open Settings and go to Display", ("launch", SETTINGS))):
        config = Config(settle_delay=0, launch_settle_delay=0, fast_path="on", fast_path_done=False)
        resolver = AppResolver(driver, {"settings": SETTINGS}, label_cache=tmp_path / "l.json")
        resolver.ensure_labels()
        agent = Agent(ScriptedClient([DONE]), driver, resolver, config, RecordingIO(),
                      judge=Judge([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.95)]), use_ocr=False)
        agent.run(task)
        assert opened(driver) == [] and expect in driver.actions, task


# --- the direct Settings step (deep_links_fast): the page from the request, before any classifier or model ---


NOTIFY = ("Open Settings, go to Apps, open Chrome, open its Notifications settings, and tell me whether "
          "notifications are allowed")
KW = Config().confirm_keywords


class CountingJudge(Judge):
    """Counts the classifier's looks at screens (assess_screen), besides the next_step answers it records."""

    def __init__(self, script, screen_delay=0.0):
        super().__init__(script)
        self.screens, self.screen_delay = 0, screen_delay

    def assess_screen(self, request, screen):
        self.screens += 1
        time.sleep(self.screen_delay)
        return super().assess_screen(request, screen)


def run_direct(task, responses=(DONE,), driver=None, judge=None, tmp_path=None, mode=None, io=None, blocked=None,
               go_home=False, **cfg):
    config = Config(**{"settle_delay": 0, "launch_settle_delay": 0, "fast_path": "on", "fast_path_done": False, **cfg})
    driver = driver if driver is not None else Phone(elements=HOME)
    resolver = AppResolver(driver, {"settings": SETTINGS}, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    io = io or RecordingIO()
    steps = []
    io.record_step = steps.append
    agent = Agent(ScriptedClient(list(responses)), driver, resolver, config, io, judge=judge, use_ocr=False)
    perms = perm.TaskPermissions(mode or "auto", lambda: perm.normalize(perm.default_settings()))
    result = agent.run(task, permissions=perms, blocked=blocked, go_home=go_home)
    return agent, driver, io, steps, result


def helper_note(client) -> str:
    """What the main model was told before its first decision (the quick helper's note, the screen)."""
    return " ".join(b.get("text", "") for b in client.requests[0]["messages"][0]["content"] if isinstance(b, dict))


@pytest.mark.parametrize("task, want", [
    (NOTIFY, ("app_notifications", "Chrome")),
    (NOTIFY + ".", ("app_notifications", "Chrome")),
    ("open Chrome's notification settings", ("app_notifications", "Chrome")),
    ("open chrome notification settings", ("app_notifications", "chrome")),
    ("go to Chrome's notifications in Settings", ("app_notifications", "Chrome")),
    ("open the notification settings for Chrome", ("app_notifications", "Chrome")),
    ("show me the notifications settings of the Chrome app", ("app_notifications", "Chrome")),
    ("open chrome's notification settings, are they on?", ("app_notifications", "chrome")),
    ("open Chrome's notification settings and check if notifications are allowed", ("app_notifications", "Chrome")),
    ("please open Chrome's app info", ("app_details", "Chrome")),
    ("open the app info page for Chrome", ("app_details", "Chrome")),
    ("Open Settings, go to Apps, open Chrome", ("app_details", "Chrome")),
    ("Open Settings and go to Display", ("display", None)),
    ("Open Settings and go to Display and tell me the brightness", ("display", None)),
    ("show me the battery page", ("battery", None)),
    ("open the notification settings", ("notifications", None)),
    # Not a plain page request: a side effect, an unclear or missing app, another app on the way, navigation
    # after the read-back, a correction: the main model's call.
    ("open Chrome's notification settings and turn them off", None),
    ("open Chrome's notification settings and allow notifications", None),
    ("open Chrome's settings", None),
    ("open Chrome", None),
    ("open Chrome and go to Display", None),
    ("open Chrome, open Maps' notification settings", None),
    ("open settings and see the toggle", None),
    ("open display settings and look at the brightness", None),
    ("is bluetooth on", None),
    ("open Chrome's notification settings, then open YouTube", None),
    ("tell me whether notifications are allowed, then open Chrome's notification settings", None),
    ("Open Settings and go to Display\nUser correction: go to Sound instead", None),
    ("turn on dark mode in display settings", None),
    ("open Chrome's notification settings and send a message", None),
])
def test_requests_that_name_a_page_or_an_apps_page_with_a_read_back(task, want):
    assert fast.direct_settings_request(task, KW) == want


def test_an_apps_page_request_opens_the_page_first_without_the_classifier_or_the_model(tmp_path):
    judge = CountingJudge([("ESCALATE", None, 0.95)])
    agent, driver, io, steps, result = run_direct(NOTIFY, judge=judge, tmp_path=tmp_path)
    assert driver.actions[:2] == [("wake",), ("open_settings", "app_notifications", CHROME)]  # the very first action
    first = steps[0]
    assert first["tool"] == "open_settings_page" and first["args"] == {"page": "app_notifications", "package": CHROME}
    assert first["fast"] is True and first["source"] == DEEP_LINK_SOURCE and first["model_ms"] == 0 and first["jev_ms"] == 0
    assert "+pages" in first["variant"]
    # The classifier's first look was at the page that opened, never at the home screen the step left.
    assert judge.screens == 1 and [a["action"] for a in judge.asked[0][1]] == ["open_settings_page app_notifications"]
    # The read-back is the main model's: one call, which answered from the page.
    assert len(agent.client.requests) == 1 and result.success and result.done_via == "vision" and result.fast_steps == 1
    told = helper_note(agent.client)
    assert "quick helper" in told and "open_settings_page" in told and "Opened the App notifications Settings page of Chrome." in told
    assert ("action", "open_settings_page: Opened the App notifications Settings page of Chrome.") in io.events


def test_a_plain_page_request_takes_the_deep_link_without_jev(tmp_path):
    agent, driver, _, steps, result = run_direct("Open Settings and go to Display", tmp_path=tmp_path)  # no judge at all
    assert opened(driver) == [("open_settings", "display", None)] and driver.actions[1] == ("open_settings", "display", None)
    assert steps[0]["source"] == DEEP_LINK_SOURCE and steps[0]["fast"] is True and steps[0]["jev_ms"] == 0
    assert len(agent.client.requests) == 1 and result.success


def test_phrasings_of_an_apps_pages_open_the_right_page(tmp_path):
    for task, page in (("open Chrome's notification settings", "app_notifications"),
                       ("open the notification settings for Chrome and tell me if they're on", "app_notifications"),
                       ("show me Chrome's app info", "app_details"),
                       ("Open Settings, go to Apps, open Chrome", "app_details")):
        _, driver, _, steps, _ = run_direct(task, tmp_path=tmp_path)
        assert opened(driver) == [("open_settings", page, CHROME)] and steps[0]["args"]["package"] == CHROME, task


def test_requests_that_arent_plain_or_name_an_app_the_task_may_not_touch_leave_it_to_the_model(tmp_path):
    blocked = lambda package: package == CHASE  # noqa: E731
    for task in ("open Chrome's notification settings and turn them off",  # a side effect
                 "open Chrom's notification settings",  # not exactly an installed app's name: no guessing
                 "open Chase's notification settings",  # a blocked app
                 "open OpenMob's notification settings",  # OpenMob itself
                 "open Chrome's settings"):  # the app's own settings or its App info page? unclear
        agent, driver, _, steps, _ = run_direct(task, tmp_path=tmp_path, blocked=blocked)
        assert opened(driver) == [] and steps[0]["tool"] == "done" and len(agent.client.requests) == 1, task
    # The model's own call for a blocked app is refused as before (the tool's check, not the fast path's).
    agent, driver, _, _, _ = run_direct("open Chase's notification settings", tmp_path=tmp_path, blocked=blocked, responses=[
        [tool_use("open_settings_page", "a", page="app_notifications", package="Chase")], DONE])
    assert opened(driver) == [] and tool_result(agent.client).startswith(BLOCKED_APP_MESSAGE)


def test_the_direct_step_can_be_switched_off_and_the_classifiers_deep_link_stays(monkeypatch, tmp_path):
    judge = CountingJudge([("LAUNCH_APP", None, 0.95), ("ESCALATE", None, 0.95)])
    agent, driver, _, steps, _ = run_direct("Open Settings and go to Display", judge=judge, tmp_path=tmp_path,
                                            deep_links_fast=False)
    # The classifier looked at the home screen first and chose to launch; the fast step opened the page instead.
    assert opened(driver) == [("open_settings", "display", None)] and judge.screens == 2
    assert steps[0]["fast"] is True and steps[0].get("source") is None and "+pages" not in steps[0]["variant"]
    agent, driver, _, steps, _ = run_direct("Open Settings and go to Display", tmp_path=tmp_path, deep_links_fast=False)
    assert opened(driver) == [] and steps[0]["tool"] == "done"  # no judge, no direct step: the model's call

    from openmob.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    assert load_config(path).deep_links_fast is True
    monkeypatch.setenv("OPENMOB_DEEP_LINKS_FAST", "off")
    assert load_config(path).deep_links_fast is False


def test_the_classifier_is_asked_when_the_direct_step_steps_aside_after_all(tmp_path):
    """A plain link request whose link the phone would hand to a browser: the first look skipped the classifier
    (a direct step was expected), so it is asked before the usual path goes on."""
    from test_app_links import Phone as LinkPhone

    judge = CountingJudge([("ESCALATE", None, 0.95)], screen_delay=0.02)
    agent, driver, io, steps, _ = run_direct("search YouTube for lofi", driver=LinkPhone(opens={}, elements=HOME),
                                             judge=judge, tmp_path=tmp_path)
    assert [a for a in driver.actions if a[0] == "url"] == [] and opened(driver) == []
    assert any(k == "status" and m.startswith("Link not taken") for k, m in io.events)
    assert judge.screens == 1 and len(judge.asked) == 1  # asked about the home screen, once, before the model
    assert steps[0]["tool"] == "done" and steps[0]["jev_ms"] >= 15 and len(agent.client.requests) == 1


def test_ask_every_step_asks_before_the_direct_step_and_plan_first_leaves_it_to_the_plan(tmp_path):
    agent, driver, io, steps, _ = run_direct("open Chrome's notification settings", io=RecordingIO(answers=["yes"]),
                                             tmp_path=tmp_path, mode="ask")
    assert io.questions == ["Next step: open the App notifications Settings page of Chrome. Should I go ahead?"]
    assert opened(driver) == [("open_settings", "app_notifications", CHROME)] and steps[0]["source"] == DEEP_LINK_SOURCE
    agent, driver, _, steps, _ = run_direct("open Chrome's notification settings", io=RecordingIO(answers=["no"]),
                                            tmp_path=tmp_path, mode="ask")
    assert opened(driver) == [] and steps[0]["is_error"] and "did not approve" in helper_note(agent.client)
    agent, driver, _, _, _ = run_direct("open Chrome's notification settings", tmp_path=tmp_path, mode="plan")
    assert opened(driver) == [] and len(agent.client.requests) == 1


def test_home_is_pressed_only_when_the_launcher_isnt_in_front(tmp_path):
    _, driver, _, _, _ = run_direct("Open Settings and go to Display", tmp_path=tmp_path, go_home=True)
    assert driver.actions == [("wake",), ("open_settings", "display", None)]  # the launcher was in front: no HOME
    phone = Phone(elements=HOME)
    phone.package = CHROME
    _, driver, _, _, _ = run_direct("Open Settings and go to Display", driver=phone, tmp_path=tmp_path, go_home=True)
    assert driver.actions[:3] == [("wake",), ("key", "HOME"), ("open_settings", "display", None)]


def test_the_wake_and_home_count_as_the_first_steps_action_time(tmp_path):
    class SleepyPhone(Phone):
        def wake_unlock(self):
            time.sleep(0.05)
            super().wake_unlock()

    _, _, _, steps, _ = run_direct("Open Settings and go to Display", driver=SleepyPhone(elements=HOME), tmp_path=tmp_path)
    assert steps[0]["action_ms"] >= 40 and steps[1]["action_ms"] < 40


def test_the_prompt_nudges_the_model_toward_an_apps_page_even_when_the_taps_are_spelled_out():
    assert "app_notifications with package Chrome" in SETTINGS_PAGE_LINE and "tell me whether" in SETTINGS_PAGE_LINE
    assert "Settings > Apps > Chrome > Notifications is app_notifications" in OPEN_SETTINGS_TOOL["description"]


# --- the drivers -------------------------------------------------------------------------------


def test_the_remote_driver_calls_the_phone_only_with_the_capability(server):  # noqa: F811
    url, manager, _ = server
    phone = FakePhone(url, FakeDriver(), capabilities=["wait_idle", "open_settings"])
    assert phone.welcome.wait(5)
    remote = manager.get("app-xyz").driver
    assert remote.can_open_settings
    assert remote.open_settings("display") == {"package": SETTINGS}
    assert remote.open_settings("app_details", CHROME) == {"package": SETTINGS}
    assert [c for c in phone.calls if c[0] == "open_settings"] == [
        ("open_settings", {"page": "display"}), ("open_settings", {"page": "app_details", "package": CHROME})]
    phone.disconnect()

    old = FakePhone(url, FakeDriver(), device_id="old", capabilities=["wait_idle"])
    assert old.welcome.wait(5)
    remote = manager.get("app-old").driver
    assert not remote.can_open_settings
    with pytest.raises(DeviceError, match="can't open Settings pages directly"):
        remote.open_settings("display")
    assert not [c for c in old.calls if c[0] == "open_settings"]
    old.disconnect()


def test_the_adb_driver_starts_the_pages_action():
    driver = RecordingDriver()
    assert driver.can_open_settings
    assert driver.open_settings("display") == {"package": None}
    assert driver.open_settings("app_details", CHROME) == {"package": None}
    driver.open_settings("app_notifications", CHROME)
    commands = [c[1] for c in driver.calls if c[0] == "shell"]
    assert commands == ["am start -a android.settings.DISPLAY_SETTINGS",
                        f"am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:{CHROME}",
                        f"am start -a android.settings.APP_NOTIFICATION_SETTINGS --es android.provider.extra.APP_PACKAGE {CHROME}"]
    with pytest.raises(AdbError, match="Unknown Settings page"):
        driver.open_settings("accessibility")
    with pytest.raises(AdbError, match="needs an app's package"):
        driver.open_settings("app_details", "chrome")
    failing = RecordingDriver({"am start": b"Error: Activity not started, unable to resolve Intent"})
    with pytest.raises(AdbError, match="no Display Settings page .* Navigate there instead"):
        failing.open_settings("display")


def test_the_docs_describe_the_method_and_the_tool():
    protocol = (ROOT / "docs/phone-protocol.md").read_text()
    assert "`open_settings`" in protocol and '"open_settings"' in protocol
    safety = (ROOT / "docs/safety.md").read_text()
    assert "open_settings_page" in safety
    assert (ROOT / "docs/design/deep-links.md").exists()



def test_deep_links_can_be_switched_off_for_comparisons(monkeypatch, tmp_path):
    from openmob.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 10\n")
    assert load_config(path).deep_links is True
    monkeypatch.setenv("OPENMOB_DEEP_LINKS", "off")
    assert load_config(path).deep_links is False


def test_a_link_waits_for_the_screen_to_leave_the_one_it_was_opened_from(monkeypatch):
    import threading
    import time as _time

    from conftest import FakeDriver, RecordingIO, ScriptedClient

    from openmob.agent import loop as loop_mod
    from openmob.agent.loop import Agent, Observation, _TaskState
    from openmob.config import Config
    from openmob.phone.adb import UIElement

    home = [UIElement(0, "Chrome", "", "", "android.widget.TextView", True, None, (0, 0, 100, 50))]
    wifi = [UIElement(0, "Wi-Fi", "", "", "android.widget.TextView", False, None, (0, 0, 100, 50))]

    class SlowPhone(FakeDriver):
        fast_ui_tree = True

        def __init__(self, draws_after):
            super().__init__()
            self.reads, self.draws_after = 0, draws_after

        def current_app(self):
            return "com.android.settings" if self.reads >= self.draws_after else "com.google.android.apps.nexuslauncher"

        def ui_elements(self):
            self.reads += 1
            return wifi if self.reads > self.draws_after else home

    monkeypatch.setattr(loop_mod, "LINK_LAND_S", 0.6)
    before = Observation(None, home, "com.google.android.apps.nexuslauncher")
    state = _TaskState(task="t", cancel=threading.Event())
    agent = Agent(ScriptedClient([]), SlowPhone(3), None, Config(), RecordingIO(), use_ocr=False)
    start = _time.monotonic()
    agent._await_new_screen(state, before)
    assert _time.monotonic() - start < 0.55 and agent.driver.reads == 4  # left as soon as the new page drew
    agent = Agent(ScriptedClient([]), SlowPhone(999), None, Config(), RecordingIO(), use_ocr=False)
    start = _time.monotonic()
    agent._await_new_screen(state, before)
    assert 0.55 <= _time.monotonic() - start < 1.0  # never changed: gives up at the limit, the usual settle follows
