"""`openmob bench` latency conditions (baseline / settle / fast) on the fake phone."""

import time

from conftest import RecordingIO, ScriptedClient, text_block, tool_use
from test_fast_path import DISPLAY, DONE, SETTINGS, Judge, Phone

from openmob.agent.loop import Agent
from openmob.config import Config
from openmob.phone.apps import AppResolver


def test_bench_compares_baseline_settle_and_fast_on_a_fake_phone(tmp_path):
    from openmob import bench

    class AppPhone(Phone):
        def wait_idle(self, quiet_ms, max_ms):
            time.sleep(min(quiet_ms, max_ms) / 1000)
            return {"waited_ms": min(quiet_ms, max_ms), "idle": True}

    task = bench.BenchTask("Open Settings and go to Display", "display")

    def make_agent(mode):
        driver = AppPhone(SETTINGS, {"Display": DISPLAY})
        tap = [tool_use("tap_element", "a", index=1, target="Display")]
        responses = [DONE] if mode == "fast" else [tap, DONE]
        judge = Judge([("TAP", 1, 0.95), ("DONE", None, 0.95)])
        config = Config(settle_delay=0.3, launch_settle_delay=1.0, settle_quiet_ms=30,
                        **bench.CONDITIONS[mode])
        resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
        return Agent(ScriptedClient(responses), driver, resolver, config, RecordingIO(), judge=judge, use_ocr=False)

    runs = bench.run_bench([task], make_agent, modes=("baseline", "settle", "fast"))
    base, settle, quick = runs
    assert [r.mode for r in runs] == ["baseline", "settle", "fast"] and all(r.success for r in runs)
    assert base.timings["settle_ms"] >= 550  # two fixed 0.3 s sleeps (after HOME, after the tap)
    assert settle.timings["settle_ms"] < base.timings["settle_ms"]
    assert quick.fast_steps == 1 and base.fast_steps == settle.fast_steps == 0
    assert quick.steps == base.steps == 2
    text = bench.format_comparison(runs)
    assert "baseline" in text and "Δ settle−baseline" in text and "Δ fast−baseline" in text and "fast steps" in text


def test_bench_round_c_against_round_d(tmp_path):
    from test_latency_round_d import DISPLAY_PAGE, LAUNCHER, FakeJev

    from openmob import bench
    from openmob.jev import JevJudge

    class AppPhone(Phone):
        def __init__(self):
            super().__init__(LAUNCHER, {"Display": DISPLAY_PAGE})
            self.shots = 0

        def launch_app(self, package):
            super().launch_app(package)
            self.elements = list(SETTINGS)

        def screenshot(self, max_age=0.0, attempts=2):
            self.shots += 1
            return super().screenshot(max_age, attempts)

    task = bench.BenchTask("Open Settings and go to Display", "display")
    phones, clients = {}, {}

    def make_agent(mode):
        driver = phones[mode] = AppPhone()
        # Round C: the text model names the app and the vision model confirms DONE.
        responses = [[text_block('{"app": "Settings"}')], DONE] if mode == "fast" else []
        jev = FakeJev([("LAUNCH_APP", None, 0.95), ("TAP", 1, 0.95), ("DONE", None, 0.95)], done=[0.1, 0.1, 0.95])
        config = Config(settle_delay=0, launch_settle_delay=0, **bench.CONDITIONS[mode])
        resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
        resolver.labels = {"com.android.settings": "Settings"}
        client = clients[mode] = ScriptedClient(responses)
        return Agent(client, driver, resolver, config, RecordingIO(), judge=JevJudge(jev),
                     use_ocr=False)

    runs = bench.run_bench([task], make_agent, modes=("fast", "fastd"), go_home=False)
    c, d = runs
    assert c.success and d.success and c.done_via == "vision" and d.done_via == "classifier"
    assert len(clients["fast"].requests) == 2 and clients["fastd"].requests == []  # no model at all in D
    assert phones["fast"].shots == 3 and phones["fastd"].shots == 0
    text = bench.format_comparison(runs)
    assert "done by jev (sum)" in text and "Δ fastd−fast" in text
    assert {("settle_quiet_ms", 200), ("fast_path_done", True)} <= set(bench.CONDITIONS["fastd-q200"].items())
