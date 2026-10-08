"""parallel_observe: the screenshot is taken while the app and the elements are read."""

import time

from conftest import FakeDriver, RecordingIO, ScriptedClient, tool_use

from openmob.agent.loop import Agent, _TaskState
from openmob.config import Config
from openmob.phone.apps import AppResolver


class SlowDriver(FakeDriver):
    def screenshot(self, max_age=0.0, attempts=2):
        time.sleep(0.2)
        return super().screenshot(max_age, attempts)

    def ui_elements(self):
        time.sleep(0.2)
        return super().ui_elements()


def observe(tmp_path, parallel):
    import threading

    driver = SlowDriver()
    agent = Agent(ScriptedClient([]), driver, AppResolver(driver, {}, label_cache=tmp_path / "l.json"),
                  Config(parallel_observe=parallel, ui_tree="always"), RecordingIO(), use_ocr=False)
    state = _TaskState(task="t", cancel=threading.Event())
    start = time.monotonic()
    obs = agent._observe(state)
    return obs, time.monotonic() - start


def test_the_screenshot_overlaps_the_other_reads(tmp_path):
    serial, serial_s = observe(tmp_path, parallel=False)
    parallel, parallel_s = observe(tmp_path, parallel=True)
    assert serial_s >= 0.4 and parallel_s < 0.35  # about the slowest read, not the sum
    assert parallel._shot is not None and parallel.package == serial.package
    assert (parallel._shot.width, parallel._shot.height) == (serial._shot.width, serial._shot.height)  # taken now, not deferred


def test_a_task_runs_the_same_with_parallel_observe(tmp_path):
    driver = FakeDriver()
    client = ScriptedClient([[tool_use("tap", "a", x=10, y=10)], [tool_use("done", "d", summary="ok", success=True)]])
    agent = Agent(client, driver, AppResolver(driver, {}, label_cache=tmp_path / "l.json"),
                  Config(settle_delay=0, launch_settle_delay=0, parallel_observe=True), RecordingIO(), use_ocr=False)
    assert agent.run("tap it").success and ("tap", 20, 20) in driver.actions
