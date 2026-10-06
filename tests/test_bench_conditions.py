"""`openmob bench` latency conditions (baseline / settle / fast) on the fake phone."""

import time

from conftest import RecordingIO, ScriptedClient, tool_use
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
