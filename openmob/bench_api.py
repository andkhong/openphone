"""`openmob bench-api`: compare models (API tiers) and permission modes through a running server.

Unlike `openmob bench` (which drives an adb phone from this process), every run here is an ordinary
developer-API task on a real server and phone: POST /v1/devices/{id}/tasks, then one SSE request that
follows the task to its end. So what is measured is what an API caller gets, and switching tiers needs
no server restart. Requests per run: 2 (create + events), plus 2 for the reset task, plus 1 to cancel
a task that asked a question (the bench never answers). The client keeps itself under the per-key
limit (60 requests a minute by default) without polling.

The plan for each task: an optional warm-up round (every condition once), then `runs` rounds with the
conditions interleaved (A, B, A, B, …). A task's `reset` instruction runs before every run, and is
neither measured nor reported. Results: one JSON line per run (resets and warm-ups included, marked)
and a markdown summary of the measured runs (docs/benchmarks.md, "Model comparison").
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import yaml

PHASES = ("observe_ms", "classifier_ms", "model_ms", "action_ms", "settle_ms", "wait_ms")
RATE_PER_MINUTE = 55  # stay under the server's default 60 per key per minute


@dataclass
class ApiTask:
    name: str
    instruction: str
    reset: str | None = None  # run before each run, not measured
    max_steps: int | None = None


@dataclass
class Condition:
    tier: str | None = None
    permission_mode: str | None = None

    @property
    def label(self) -> str:
        return "/".join(x for x in (self.tier, self.permission_mode and f"mode={self.permission_mode}") if x) or "default"

    def body(self) -> dict:
        out = {}
        if self.tier:
            out["tier"] = self.tier
        if self.permission_mode:
            out["permission_mode"] = self.permission_mode
        return out


@dataclass
class ApiRun:
    task: str
    condition: str
    kind: str  # measured | warmup | reset
    round: int
    task_id: str | None = None
    status: str | None = None
    success: bool = False
    steps: int | None = None
    credits: int | None = None
    wall_ms: int | None = None  # created -> finished, on the server's clock
    client_ms: int | None = None  # POST sent -> done event received, on this machine
    first_action_ms: int | None = None  # created -> first action event
    phases: dict = field(default_factory=dict)  # summed over the run's steps, ms
    step_timings: list = field(default_factory=list)  # each action event's public timings
    reason: str | None = None
    error: str | None = None


def load_api_tasks(path: str | Path) -> list[ApiTask]:
    """{tasks: [{name, instruction, reset?, max_steps?}, …]} (or a bare list; a string is an instruction)."""
    data = yaml.safe_load(Path(path).read_text()) or []
    items = data.get("tasks", []) if isinstance(data, dict) else data
    tasks = []
    for i, item in enumerate(items or [], 1):
        if isinstance(item, str):
            item = {"instruction": item}
        if not isinstance(item, dict) or not str(item.get("instruction", "")).strip():
            raise ValueError(f"task {i} in {path} needs an instruction")
        tasks.append(ApiTask(str(item.get("name") or f"task {i}"), str(item["instruction"]).strip(),
                             str(item["reset"]).strip() if item.get("reset") else None,
                             int(item["max_steps"]) if item.get("max_steps") else None))
    if not tasks:
        raise ValueError(f"no tasks in {path}")
    return tasks


def conditions_from(tiers: str | None, modes: str | None) -> list[Condition]:
    """Every tier × every permission mode (either list may be empty: the server's default)."""
    tier_list = [t.strip() for t in (tiers or "").split(",") if t.strip()] or [None]
    mode_list = [m.strip() for m in (modes or "").split(",") if m.strip()] or [None]
    return [Condition(t, m) for t in tier_list for m in mode_list]


def read_key(key_file: str | None = None) -> str:
    """OPENMOB_API_KEY, else the first line of key_file. Never printed or saved."""
    if key_file:
        key = Path(key_file).expanduser().read_text().strip().splitlines()[0].strip()
    else:
        key = os.environ.get("OPENMOB_API_KEY", "").strip()
    if not key:
        raise ValueError("Set OPENMOB_API_KEY or pass --key-file.")
    return key


class ApiClient:
    """The developer API with a client-side rate limit (a sliding minute) and Retry-After on 429."""

    def __init__(self, base_url: str, key: str, rate_per_minute: int = RATE_PER_MINUTE,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"},
                                 timeout=timeout)
        self.rate = rate_per_minute
        self.sleep = sleep
        self.sent: deque[float] = deque()
        self.requests = 0

    def _throttle(self) -> None:
        now = time.monotonic()
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        if len(self.sent) >= self.rate:
            self.sleep(60 - (now - self.sent[0]) + 0.1)
        self.sent.append(time.monotonic())
        self.requests += 1

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        for _ in range(5):
            self._throttle()
            response = self.http.request(method, path, **kw)
            if response.status_code != 429:
                return response
            self.sleep(float(response.headers.get("retry-after") or 5))
        return response

    def devices(self) -> list[dict]:
        response = self.request("GET", "/v1/devices")
        response.raise_for_status()
        data = response.json()
        return data.get("devices", data) if isinstance(data, dict) else data

    def create_task(self, device: str, body: dict) -> dict:
        response = self.request("POST", f"/v1/devices/{device}/tasks", json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"task not started ({response.status_code}): {response.text[:300]}")
        return response.json()

    def cancel(self, task_id: str) -> None:
        self.request("POST", f"/v1/tasks/{task_id}/cancel")

    def follow(self, task_id: str, timeout: float, on_question: Callable[[dict], None] | None = None):
        """One SSE request: (events, final task). Ends at the server's `done` event or after `timeout`."""
        self._throttle()
        events: list[dict] = []
        deadline = time.monotonic() + timeout
        with self.http.stream("GET", f"/v1/tasks/{task_id}/events",
                              timeout=httpx.Timeout(timeout, read=60.0)) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"event stream refused ({response.status_code})")
            kind, data = None, []
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    return events, None
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif not line and data:
                    payload = json.loads("\n".join(data))
                    data = []
                    if kind == "done":
                        return events, payload
                    events.append(payload)
                    if kind == "question" and on_question:
                        on_question(payload)
        return events, None


def measure(task: ApiTask, instruction: str, condition: Condition, kind: str, round_no: int, client: ApiClient,
            device: str, timeout: float) -> ApiRun:
    run = ApiRun(task.name, condition.label, kind, round_no)
    body = {"instruction": instruction, **condition.body()}
    if task.max_steps and kind != "reset":
        body["max_steps"] = task.max_steps
    started = time.monotonic()
    try:
        created = client.create_task(device, body)
        run.task_id = created["id"]

        def question(event: dict) -> None:  # nobody answers during a bench: stop the run
            run.reason = "asked a question" + (" (confirmation)" if event.get("confirmation") else "")
            client.cancel(run.task_id)

        events, final = client.follow(run.task_id, timeout, question)
        run.client_ms = int((time.monotonic() - started) * 1000)
        if final is None:
            client.cancel(run.task_id)
            run.error = f"no result within {timeout:.0f}s (cancelled)"
            return run
        run.status = final.get("status")
        run.success = bool((final.get("outcome") or {}).get("success"))
        run.steps = final.get("steps")
        run.credits = final.get("credits")
        run.reason = run.reason or (final.get("outcome") or {}).get("reason")
        if final.get("created") and final.get("finished"):
            run.wall_ms = int((final["finished"] - final["created"]) * 1000)
        actions = [e for e in events if e.get("type") in ("action", "warning") and e.get("timings")]
        run.step_timings = [e["timings"] for e in actions]
        run.phases = {p: sum(t.get(p) or 0 for t in run.step_timings) for p in PHASES + ("total_ms",)}
        first = next((e for e in events if e.get("type") == "action"), None)
        if first and final.get("created"):
            run.first_action_ms = int((first["time"] - final["created"]) * 1000)
    except Exception as exc:  # one broken run must not lose the others
        run.error = f"{type(exc).__name__}: {exc}"
    return run


def plan(tasks: list[ApiTask], conditions: list[Condition], runs: int, warmup: int):
    """(task, condition, kind, round) in run order: warm-up rounds, then interleaved measured rounds."""
    for task in tasks:
        for r in range(warmup):
            for condition in conditions:
                yield task, condition, "warmup", r + 1
        for r in range(runs):
            for condition in conditions:
                yield task, condition, "measured", r + 1


def run_bench_api(tasks: list[ApiTask], conditions: list[Condition], client: ApiClient, device: str,
                  runs: int = 3, warmup: int = 1, timeout: float = 300.0,
                  on_run: Callable[[ApiRun], None] | None = None) -> list[ApiRun]:
    results = []
    for task, condition, kind, round_no in plan(tasks, conditions, runs, warmup):
        if task.reset:
            reset = measure(task, task.reset, condition, "reset", round_no, client, device, timeout)
            results.append(reset)
            if on_run:
                on_run(reset)
        run = measure(task, task.instruction, condition, kind, round_no, client, device, timeout)
        results.append(run)
        if on_run:
            on_run(run)
    return results


# --- the summary -------------------------------------------------------------------------


def _spread(values: list[float], scale: float = 1.0, fmt: str = "{:.1f}") -> str:
    values = [v / scale for v in values if v is not None]
    if not values:
        return "-"
    med = statistics.median(values)
    if len(values) == 1 or min(values) == max(values):
        return fmt.format(med)
    return f"{fmt.format(med)} ({fmt.format(min(values))}–{fmt.format(max(values))})"


def summarize_runs(results: list[ApiRun]) -> dict:
    """{task: {condition: stats}} over the measured runs."""
    out: dict = {}
    for run in results:
        if run.kind != "measured":
            continue
        out.setdefault(run.task, {}).setdefault(run.condition, []).append(run)
    summary: dict = {}
    for task, by_condition in out.items():
        summary[task] = {}
        for condition, mine in by_condition.items():
            summary[task][condition] = {
                "runs": len(mine),
                "succeeded": sum(r.success for r in mine),
                "errors": sum(r.error is not None for r in mine),
                "wall_ms": [r.wall_ms for r in mine if r.wall_ms is not None],
                "steps": [r.steps for r in mine if r.steps is not None],
                "credits": [r.credits for r in mine if r.credits is not None],
                "first_action_ms": [r.first_action_ms for r in mine if r.first_action_ms is not None],
                "phases": {p: [r.phases.get(p, 0) for r in mine if r.phases] for p in PHASES + ("total_ms",)},
            }
    return summary


def format_markdown(results: list[ApiRun], meta: dict) -> str:
    summary = summarize_runs(results)
    lines = [f"# Model comparison ({time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('started', time.time())))})",
             "",
             f"Server {meta.get('server')} · device {meta.get('device')} · conditions {', '.join(meta.get('conditions', []))} · "
             f"{meta.get('runs')} measured runs per condition after {meta.get('warmup')} warm-up round(s), interleaved. "
             "Median (min–max). Resets and warm-ups are excluded.", ""]
    for task, by_condition in summary.items():
        lines += [f"## {task}", "",
                  "| condition | success | wall s | steps | credits | first action s |",
                  "|---|---|---|---|---|---|"]
        for condition, s in by_condition.items():
            ok = f"{s['succeeded']}/{s['runs']}" + (f" ({s['errors']} errors)" if s["errors"] else "")
            lines.append(f"| {condition} | {ok} | {_spread(s['wall_ms'], 1000)} | {_spread(s['steps'], fmt='{:.0f}')} | "
                         f"{_spread(s['credits'], fmt='{:.0f}')} | {_spread(s['first_action_ms'], 1000)} |")
        lines += ["", "Per phase, ms per run (summed over its steps; median (min–max)):", "",
                  "| condition | " + " | ".join(p.removesuffix("_ms") for p in PHASES + ("total_ms",)) + " |",
                  "|---|" + "---|" * (len(PHASES) + 1)]
        for condition, s in by_condition.items():
            lines.append(f"| {condition} | " + " | ".join(_spread(s["phases"][p], fmt="{:,.0f}")
                                                         for p in PHASES + ("total_ms",)) + " |")
        lines.append("")
    failed = [r for r in results if r.kind == "measured" and (r.error or not r.success)]
    if failed:
        lines += ["## Unsuccessful runs", ""]
        lines += [f"- {r.task} · {r.condition} · round {r.round}: {r.error or r.reason or r.status}" for r in failed]
        lines.append("")
    return "\n".join(lines)


def save(results: list[ApiRun], meta: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.get("started", time.time())))
    jsonl = out / f"bench-api-{stamp}.jsonl"
    with jsonl.open("w") as f:
        for run in results:
            f.write(json.dumps(asdict(run)) + "\n")
    markdown = out / f"bench-api-{stamp}.md"
    markdown.write_text(format_markdown(results, meta))
    return jsonl, markdown


def pick_device(client: ApiClient, device: str | None) -> str:
    if device:
        return device
    devices = client.devices()
    online = [d for d in devices if d.get("online", d.get("status") == "online")]
    choices = online or devices
    if len(choices) != 1:
        raise ValueError(f"Say which device with --device (found: {', '.join(d['id'] for d in choices) or 'none'}).")
    return choices[0]["id"]


def main(args) -> None:
    try:
        key = read_key(args.key_file)
        tasks = load_api_tasks(args.tasks)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
    conditions = conditions_from(args.tiers, args.permission_modes)
    client = ApiClient(args.url, key)
    try:
        device = pick_device(client, args.device)
    except (ValueError, httpx.HTTPError) as exc:
        sys.exit(str(exc))
    meta = {"server": args.url, "device": device, "conditions": [c.label for c in conditions],
            "runs": args.runs, "warmup": args.warmup, "started": time.time()}
    total = len(tasks) * len(conditions) * (args.runs + args.warmup)
    print(f"{total} runs ({len(tasks)} tasks × {len(conditions)} conditions × {args.warmup} warm-up + {args.runs}), "
          "resets not counted.", flush=True)

    def progress(run: ApiRun) -> None:
        tag = {"reset": "  reset", "warmup": "warm-up"}.get(run.kind, f"run {run.round}")
        took = f"{run.wall_ms / 1000:.1f}s" if run.wall_ms is not None else "-"
        print(f"{tag:>8} · {run.task} · {run.condition}: "
              f"{'ok' if run.success else (run.error or run.reason or run.status or 'failed')} "
              f"in {run.steps if run.steps is not None else '-'} steps, {took}", flush=True)

    results = run_bench_api(tasks, conditions, client, device, args.runs, args.warmup, args.timeout, progress)
    jsonl, markdown = save(results, meta, args.out)
    print()
    print(markdown.read_text())
    print(f"Saved {jsonl} and {markdown} ({client.requests} API requests).")
