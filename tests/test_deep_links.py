"""Settings deep links (docs/design/deep-links.md): the open_settings_page tool and the phone's open_settings method.

The phone holds a fixed key-to-intent table; the server sends only a key. The tool is offered only to phones that
advertise the capability, everyday pages open without asking, an ask-tier page asks the owner (refused under Don't
ask), an app's own pages are refused for blocked apps, and a phone without the page tells the agent to navigate.
"""

import re
from pathlib import Path

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, system_text, tool_use
from test_adb import RecordingDriver
from test_fast_path import Judge, el
from test_remote import FakePhone, server  # noqa: F401  (server is a fixture)

from openmob import permissions as perm
from openmob.agent import fast
from openmob.agent.loop import BLOCKED_APP_MESSAGE, CHANGING, Agent
from openmob.agent.tools import OPEN_SETTINGS_TOOL_SPEC, SETTINGS_PAGE_LINE, system_prompt
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
