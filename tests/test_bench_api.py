"""`openmob bench-api`: model comparison through a real server's developer API (test server + fake phone)."""

import argparse
import json
from pathlib import Path

from test_api_v1 import api  # noqa: F401  (the server with two accounts and their fake phones)

from openmob import bench_api
from openmob.bench_api import ApiClient, ApiTask, Condition, conditions_from, load_api_tasks, run_bench_api

TASKS = Path(__file__).parent.parent / "bench" / "vision_tasks.yaml"


def key_of(user) -> str:
    return user.v1.headers["authorization"].split(" ", 1)[1]


def test_the_shipped_task_file_loads():
    tasks = load_api_tasks(TASKS)
    assert [t.name for t in tasks] == ["settings-display", "clock-stopwatch"]  # the game task is a commented placeholder
    assert all(t.reset and t.max_steps for t in tasks)
    assert tasks[0].instruction == "Open Settings and go to Display"


def test_conditions_cross_tiers_and_permission_modes():
    assert [c.label for c in conditions_from("fast,balanced", None)] == ["fast", "balanced"]
    crossed = conditions_from("fast", "ask,auto")
    assert [c.body() for c in crossed] == [{"tier": "fast", "permission_mode": "ask"},
                                           {"tier": "fast", "permission_mode": "auto"}]
    assert [c.label for c in conditions_from(None, None)] == ["default"]


def test_runs_interleave_conditions_after_a_warmup_and_exclude_resets(api, tmp_path):  # noqa: F811
    client = ApiClient(api.base, key_of(api.alice))
    tasks = [ApiTask("open settings", "Open Settings", reset="Go to the home screen", max_steps=5)]
    conditions = [Condition("fast"), Condition("balanced")]
    results = run_bench_api(tasks, conditions, client, api.alice.device, runs=2, warmup=1, timeout=20)

    order = [(r.kind, r.condition) for r in results if r.kind != "reset"]
    assert order == [("warmup", "fast"), ("warmup", "balanced"), ("measured", "fast"), ("measured", "balanced"),
                     ("measured", "fast"), ("measured", "balanced")]
    assert [r.kind for r in results[::2]] == ["reset"] * 6  # a reset before every run
    assert client.requests == 2 * len(results)  # create + one event stream per run: no polling
    measured = [r for r in results if r.kind == "measured"]
    assert all(r.success and r.steps == 1 and r.error is None for r in measured)
    assert {r.condition: r.credits for r in measured} == {"fast": 1, "balanced": 3}
    assert all(r.wall_ms is not None and r.first_action_ms is not None and r.step_timings for r in measured)
    assert all(r.phases["total_ms"] == sum(t["total_ms"] for t in r.step_timings) for r in measured)

    summary = bench_api.summarize_runs(results)["open settings"]
    assert summary["fast"]["runs"] == 2 and summary["balanced"]["succeeded"] == 2
    text = bench_api.format_markdown(results, {"conditions": ["fast", "balanced"], "runs": 2, "warmup": 1})
    assert "| fast | 2/2 |" in text and "| balanced | 2/2 |" in text and "Per phase" in text


def test_a_task_that_asks_is_cancelled_not_answered(api):  # noqa: F811
    client = ApiClient(api.base, key_of(api.alice))
    [run] = run_bench_api([ApiTask("asks", "ask me which account")], [Condition("fast")], client,
                          api.alice.device, runs=1, warmup=0, timeout=20)
    assert not run.success and run.reason.startswith("asked a question") and run.status in ("cancelled", "failed")


def test_the_client_stays_under_the_rate_limit():
    slept = []
    client = ApiClient("http://127.0.0.1:9", "k", rate_per_minute=2, sleep=slept.append)
    for _ in range(3):
        client._throttle()
    assert len(slept) == 1 and 59 < slept[0] <= 60.2


def test_cli_saves_results_and_never_prints_the_key(api, tmp_path, capsys, monkeypatch):  # noqa: F811
    key = key_of(api.alice)
    monkeypatch.setenv("OPENMOB_API_KEY", key)
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("tasks:\n  - name: settings\n    instruction: Open Settings\n")
    args = argparse.Namespace(tasks=str(tasks), url=api.base, device=None, tiers="fast", permission_modes=None,
                              runs=1, warmup=0, timeout=20, out=str(tmp_path / "out"), key_file=None)
    bench_api.main(args)  # one online phone: picked without --device
    printed = capsys.readouterr().out
    files = sorted((tmp_path / "out").iterdir())
    assert [f.suffix for f in files] == [".jsonl", ".md"]
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["success"] and rows[0]["condition"] == "fast"
    assert key not in printed and all(key not in f.read_text() for f in files)
    assert "## settings" in files[1].read_text()


def test_key_comes_from_a_file_or_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENMOB_API_KEY", raising=False)
    path = tmp_path / "key"
    path.write_text("om_live_x_secret\n")
    assert bench_api.read_key(str(path)) == "om_live_x_secret"
    monkeypatch.setenv("OPENMOB_API_KEY", "om_live_y_secret")
    assert bench_api.read_key(None) == "om_live_y_secret"
