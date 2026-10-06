"""Adaptive settle: wait_idle on the phone, its fallbacks (older apps, adb, iOS), and what a step records."""

import threading
import time

import pytest
from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use
from test_remote import FakePhone, server, wait_for  # noqa: F401  (the server fixture)

from openmob.agent.loop import Agent
from openmob.config import Config, load_config
from openmob.phone.apps import AppResolver
from openmob.phone.base import DeviceError
from openmob.phone.remote import RemoteDriver


class IdlePhone(FakeDriver):
    """A phone whose app offers wait_idle: the screen goes quiet after `busy` seconds (None: never)."""

    def __init__(self, busy=0.02, **kw):
        super().__init__(**kw)
        self.busy = busy
        self.idle_calls = []

    def wait_idle(self, quiet_ms, max_ms):
        self.idle_calls.append((quiet_ms, max_ms))
        if self.busy is None or self.busy * 1000 + quiet_ms > max_ms:
            time.sleep(max_ms / 1000)
            return {"waited_ms": max_ms, "idle": False}
        time.sleep(self.busy + quiet_ms / 1000)
        return {"waited_ms": int(self.busy * 1000 + quiet_ms), "idle": True}


class AdbLike(FakeDriver):
    """adb: no wait_idle, but it can tell when a launched app is in front."""

    def __init__(self, front_after=0.05, **kw):
        super().__init__(**kw)
        self.front_after = front_after
        self.launched_at = None

    def launch_app(self, package):
        super().launch_app(package)
        self.launched_at = time.monotonic()
        self.package = "com.android.launcher3"  # not in front yet

    def wait_foreground(self, package, timeout, cancel=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if time.monotonic() - self.launched_at >= self.front_after:
                self.package = package
                return True
            time.sleep(0.005)
        return False


def run(driver, tmp_path, responses, **cfg):
    config = Config(**{"settle_delay": 1.5, "launch_settle_delay": 3.0, "settle_quiet_ms": 50, **cfg})
    io = RecordingIO()
    steps = []
    io.record_step = steps.append
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    agent = Agent(ScriptedClient(responses), driver, resolver, config, io, use_ocr=False)
    result = agent.run("open settings")
    return result, steps


LAUNCH_THEN_DONE = [
    [tool_use("launch_app", "a", app="settings")],
    [tool_use("press_key", "b", key="BACK")],
    [tool_use("done", "c", summary="ok", success=True)],
]


def test_settle_returns_once_the_screen_is_quiet(tmp_path):
    driver = IdlePhone(busy=0.02)
    result, steps = run(driver, tmp_path, LAUNCH_THEN_DONE)
    assert result.success
    # The launch's cap is 3 s and a tap's 1.5 s, but each settle ended when the screen went quiet.
    assert driver.idle_calls == [(50, 3000), (50, 1500)]
    back, done = steps[1], steps[2]
    assert back["settle_ms"] < 1000 and done["settle_ms"] < 1000  # well under either cap, even under load
    assert back["settle_idle"] is True and done["settle_idle"] is True
    assert steps[0]["settle_idle"] is None  # nothing settled before the first step
    assert {s["variant"] for s in steps} == {"adaptive+q50"}  # a non-default quiet time is named


def test_settle_stops_at_the_cap_on_a_screen_that_never_goes_quiet(tmp_path):
    driver = IdlePhone(busy=None)
    _, steps = run(driver, tmp_path, LAUNCH_THEN_DONE, settle_delay=0.2, launch_settle_delay=0.3)
    assert driver.idle_calls == [(50, 300), (50, 200)]
    assert steps[1]["settle_ms"] >= 280 and steps[1]["settle_idle"] is False
    assert steps[2]["settle_ms"] >= 180 and steps[2]["settle_idle"] is False


def test_phones_without_wait_idle_keep_the_fixed_sleep(tmp_path):
    driver = FakeDriver()  # an older app, or iOS: no wait_idle, no wait_foreground
    _, steps = run(driver, tmp_path, LAUNCH_THEN_DONE, settle_delay=0.2, launch_settle_delay=0.3)
    assert steps[1]["settle_ms"] >= 280 and steps[2]["settle_ms"] >= 180
    assert steps[1]["settle_idle"] is None


def test_adaptive_settle_can_be_switched_off(tmp_path, monkeypatch):
    driver = IdlePhone(busy=0.0)
    _, steps = run(driver, tmp_path, LAUNCH_THEN_DONE, settle_delay=0.2, launch_settle_delay=0.3, adaptive_settle=False)
    assert driver.idle_calls == [] and steps[1]["settle_ms"] >= 280
    assert {s["variant"] for s in steps} == {"fixed"}

    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("model: claude-sonnet-5-5\n")
    assert load_config(tmp_path / "c.yaml").adaptive_settle is True
    monkeypatch.setenv("OPENMOB_ADAPTIVE_SETTLE", "off")
    assert load_config(tmp_path / "c.yaml").adaptive_settle is False


def test_a_failing_wait_idle_falls_back_to_the_sleep(tmp_path):
    class Broken(IdlePhone):
        def wait_idle(self, quiet_ms, max_ms):
            raise DeviceError("Phone did not answer wait_idle")

    _, steps = run(Broken(), tmp_path, LAUNCH_THEN_DONE, settle_delay=0.2, launch_settle_delay=0.3)
    assert steps[1]["settle_ms"] >= 280 and steps[1]["settle_idle"] is None


def test_adb_launch_settles_once_the_app_is_in_front(tmp_path):
    driver = AdbLike(front_after=0.05)
    _, steps = run(driver, tmp_path, LAUNCH_THEN_DONE, settle_delay=0.1, launch_settle_delay=3.0)
    # In front after ~50 ms, then the short settle (0.1 s): far below the 3 s launch delay.
    assert 140 <= steps[1]["settle_ms"] < 2000
    assert driver.package == "com.android.settings"


# --- the protocol ---------------------------------------------------------------------------


class Link:
    def __init__(self, reply=None, error=None):
        self.closed, self.calls, self.reply, self.error = False, [], reply, error

    def call(self, method, params):
        self.calls.append((method, params))
        if self.error:
            raise DeviceError(self.error)
        return self.reply


def test_remote_wait_idle_only_on_apps_that_offer_it():
    old = RemoteDriver(Link(reply={"waited_ms": 1, "idle": True}))
    assert old.wait_idle(300, 800) is None and old.link.calls == []  # never asked

    new = RemoteDriver(Link(reply={"waited_ms": 260, "idle": True}))
    new.capabilities = {"wait_idle"}
    assert new.wait_idle(250, 3000) == {"waited_ms": 260, "idle": True}
    assert new.link.calls == [("wait_idle", {"quiet_ms": 250, "max_ms": 3000})]

    confused = RemoteDriver(Link(error="IllegalArgumentException: Unknown method wait_idle"))
    confused.capabilities = {"wait_idle"}
    assert confused.wait_idle(250, 800) is None and confused.capabilities == set()
    assert confused.wait_idle(250, 800) is None and len(confused.link.calls) == 1  # stopped asking


def test_server_uses_wait_idle_when_the_hello_offers_it(server):  # noqa: F811
    url, manager, _ = server
    manager.cfg.settle_delay, manager.cfg.launch_settle_delay = 0.5, 1.0
    driver = FakeDriver(packages=["com.android.settings"], labels={"com.android.settings": "Settings"})
    phone = FakePhone(url, driver, capabilities=["wait_idle"])
    assert phone.welcome.wait(5)
    session = manager.get("app-xyz")
    assert session.driver.capabilities == {"wait_idle"}
    session.coordinator.enqueue("open settings", "phone")
    assert wait_for(lambda: any(m == "wait_idle" for m, _ in phone.calls), timeout=10)
    assert ("wait_idle", {"quiet_ms": 300, "max_ms": 1000}) in phone.calls  # the launch's cap


def test_older_app_gets_no_wait_idle_calls(server):  # noqa: F811
    url, manager, clients = server
    driver = FakeDriver(packages=["com.android.settings"], labels={"com.android.settings": "Settings"})
    phone = FakePhone(url, driver)  # no capabilities in its hello
    assert phone.welcome.wait(5)
    session = manager.get("app-xyz")
    assert session.driver.capabilities == set()
    task = session.coordinator.enqueue("open settings", "phone")
    assert wait_for(lambda: (session.coordinator.task(task) or {}).get("status") in ("succeeded", "failed"), timeout=10)
    assert ("launch", "com.android.settings") in driver.actions
    assert all(m != "wait_idle" for m, _ in phone.calls)
