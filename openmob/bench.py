"""`openmob bench`: run the same tasks under several conditions and compare.

Internal tool (docs/benchmarks.md). A mode is either Jev on / off (True / False), or a latency
condition (CONDITIONS: baseline, settle, fast). Each task runs once per mode from the home screen,
on a fresh agent, isolated from the chat's recent requests. Collected per run: success, steps, fast
steps, time per phase (agent/timing.py), credits (steps × the model's tier price) and token counts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from .agent.timing import PHASES

# Latency conditions: the config each one runs with.
CONDITIONS = {
    "baseline": {"adaptive_settle": False, "fast_path": "off"},  # fixed settle sleeps, every step to the model
    "settle": {"adaptive_settle": True, "fast_path": "off"},  # adaptive settle only
    "fast": {"adaptive_settle": True, "fast_path": "on"},  # adaptive settle + the fast path (needs Jev)
}
Mode = bool | str


def mode_label(mode: Mode) -> str:
    return {True: "jev on", False: "jev off"}.get(mode, mode) if isinstance(mode, bool) else str(mode)


@dataclass
class BenchTask:
    instruction: str
    name: str = ""
    max_steps: int | None = None


@dataclass
class BenchRun:
    task: BenchTask
    mode: Mode  # Jev on / off (bool), or a condition name
    success: bool
    steps: int
    summary: str
    timings: dict = field(default_factory=dict)  # ms per phase over the whole task
    credits: int = 0
    usage: dict = field(default_factory=dict)
    error: str | None = None
    fast_steps: int = 0  # steps the fast path took

    @property
    def jev(self) -> Mode:
        return self.mode


def load_tasks(path: str | Path) -> list[BenchTask]:
    """A YAML list of instructions, or {tasks: [...]}; an item is a string or
    {instruction, name?, max_steps?}."""
    data = yaml.safe_load(Path(path).read_text()) or []
    items = data.get("tasks", []) if isinstance(data, dict) else data
    tasks = []
    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            item = {"instruction": item}
        if not isinstance(item, dict) or not str(item.get("instruction", "")).strip():
            raise ValueError(f"task {i} in {path} needs an instruction")
        tasks.append(BenchTask(str(item["instruction"]).strip(), str(item.get("name") or f"task {i}"),
                               int(item["max_steps"]) if item.get("max_steps") else None))
    if not tasks:
        raise ValueError(f"no tasks in {path}")
    return tasks


def run_bench(
    tasks: list[BenchTask],
    make_agent: Callable[[Mode], object],  # a mode → a fresh Agent
    modes: tuple[Mode, ...] = (True, False),
    credits_per_step: int = 1,
    go_home: bool = True,
    on_run: Callable[[BenchRun], None] | None = None,
) -> list[BenchRun]:
    runs = []
    for task in tasks:
        for mode in modes:
            agent = make_agent(mode)
            try:
                result = agent.run(task.instruction, go_home=go_home, max_steps=task.max_steps, isolated=True)
            except Exception as exc:  # one broken run must not lose the others
                run = BenchRun(task, mode, False, 0, "", error=f"{type(exc).__name__}: {exc}",
                               timings=agent.timer.task_totals(), usage=agent.usage.as_dict())
            else:
                # Credits count the steps the model decided; fast steps are priced the same here.
                run = BenchRun(task, mode, result.success, result.steps, result.summary, dict(result.timings),
                               result.steps * credits_per_step, result.usage.as_dict(), fast_steps=result.fast_steps)
            runs.append(run)
            if on_run:
                on_run(run)
    return runs


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def agent_ms(timings: dict) -> int:
    return (timings.get("total_ms") or 0) - (timings.get("wait_ms") or 0)


def _modes(runs: list[BenchRun]) -> list[Mode]:
    seen: list[Mode] = []
    for r in runs:
        if not any(m is r.mode or (type(m) is type(r.mode) and m == r.mode) for m in seen):
            seen.append(r.mode)
    return seen


def summarize(runs: list[BenchRun]) -> dict[Mode, dict]:
    out = {}
    for mode in _modes(runs):
        mine = [r for r in runs if type(r.mode) is type(mode) and r.mode == mode]
        out[mode] = {
            "runs": len(mine),
            "succeeded": sum(r.success for r in mine),
            "errors": sum(r.error is not None for r in mine),
            "steps": _mean([r.steps for r in mine]),
            "fast_steps": _mean([r.fast_steps for r in mine]),
            "credits": sum(r.credits for r in mine),
            "jev_calls": sum(r.usage.get("jev_calls", 0) for r in mine),
            "model_calls": sum(r.usage.get("model_calls", 0) for r in mine),
            "agent_ms": _mean([agent_ms(r.timings) for r in mine]),
            **{f"{p}_ms": _mean([r.timings.get(f"{p}_ms", 0) for r in mine]) for p in PHASES},
            "total_ms": _mean([r.timings.get("total_ms", 0) for r in mine]),
        }
    return out


def format_comparison(runs: list[BenchRun]) -> str:
    s = summarize(runs)
    modes = list(s)
    label = {m: mode_label(m) for m in modes}
    # Deltas against the first mode: "off−on" for the Jev comparison, else "<mode>−<first>".
    deltas = [f"Δ {label[m].replace('jev ', '')}−{label[modes[0]].replace('jev ', '')}" for m in modes[1:]]
    lines = [f"{'per task (mean)':<20}" + "".join(f"{label[m]:>12}" for m in modes) + "".join(f"{d:>16}" for d in deltas)]

    def row(name: str, key: str, fmt=lambda v: f"{v:,.0f}") -> None:
        values = [s[m][key] for m in modes]
        delta = "".join(f"{v - values[0]:>+16,.0f}" for v in values[1:])
        lines.append(f"{name:<20}" + "".join(f"{fmt(v):>12}" for v in values) + delta)

    lines.append(f"{'success':<20}" + "".join(f"{s[m]['succeeded']}/{s[m]['runs']}".rjust(12) for m in modes))
    row("steps", "steps", lambda v: f"{v:.1f}")
    if any(s[m]["fast_steps"] for m in modes):
        row("fast steps", "fast_steps", lambda v: f"{v:.1f}")
    for p in PHASES:
        row(f"{p} ms", f"{p}_ms")
    row("agent ms", "agent_ms")
    row("total ms", "total_ms")
    row("credits (sum)", "credits")
    row("model calls (sum)", "model_calls")
    row("jev calls (sum)", "jev_calls")
    if any(s[m]["errors"] for m in modes):
        row("errors", "errors")
    lines.append("")
    lines.append(f"{'task':<28}" + "".join(f"{label[m] + ' ok/steps/agent s':>26}" for m in modes))
    by_task: dict[int, dict] = {}
    for r in runs:
        by_task.setdefault(id(r.task), {})[(type(r.mode), r.mode)] = r
    for pair in by_task.values():
        name = next(iter(pair.values())).task.name[:27]
        cells = []
        for m in modes:
            r = pair.get((type(m), m))
            cells.append("-" if r is None else
                         f"{'ok' if r.success else ('ERR' if r.error else 'fail')}/{r.steps}/{agent_ms(r.timings) / 1000:.1f}")
        lines.append(f"{name:<28}" + "".join(f"{c:>26}" for c in cells))
    lines.append("")
    lines.append("agent ms = total − wait (approvals, answers, loading screens).")
    return "\n".join(lines)


def credits_per_step_for(cfg, model: str) -> int:
    """The price of a step of `model` in the API tiers (1 when no tier uses it)."""
    for tier in cfg.api_tiers.values():
        if tier.get("model") == model:
            return int(tier.get("credits_per_step", 1))
    return 1


def main(cfg, args) -> None:
    """The CLI: build a real agent per run on the chosen adb phone."""
    import dataclasses
    import sys

    from .main import build_agent
    from .runner import ConsoleIO
    from .voice.tts import Speaker

    tasks = load_tasks(args.tasks)
    if args.conditions:
        modes = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
        unknown = [c for c in modes if c not in CONDITIONS]
        if unknown or not modes:
            sys.exit(f"--conditions takes a comma-separated list of: {', '.join(CONDITIONS)}")
        if "fast" in modes and not (cfg.jev_api_key and cfg.jev_enabled):
            sys.exit("The fast condition needs Jev: set TYPESAFE_API_KEY (and don't set OPENMOB_JEV=off).")
    else:
        modes = {"both": (True, False), "on": (True,), "off": (False,)}[args.only]
        if True in modes and not cfg.jev_api_key:
            sys.exit("TYPESAFE_API_KEY is not set, so there is no Jev to compare. Set it, or use --only off.")
    if args.serial:
        cfg.adb_serial = args.serial
    if args.model:
        cfg.model = args.model
    io = ConsoleIO(Speaker(False))

    def make_agent(mode: Mode):
        if isinstance(mode, bool):
            return build_agent(dataclasses.replace(cfg, jev_enabled=mode), io)
        return build_agent(dataclasses.replace(cfg, **CONDITIONS[mode]), io)

    def progress(run: BenchRun) -> None:
        print(f"== {run.task.name} · {mode_label(run.mode)}: "
              f"{'ok' if run.success else 'failed'} in {run.steps} steps"
              + (f" ({run.fast_steps} fast)" if run.fast_steps else "")
              + f", {agent_ms(run.timings) / 1000:.1f}s agent time"
              + (f" ({run.error})" if run.error else ""), flush=True)

    started = time.time()
    runs = run_bench(tasks, make_agent, modes, credits_per_step_for(cfg, cfg.model), go_home=not args.no_home,
                     on_run=progress)
    print(f"\n{len(runs)} runs of {len(tasks)} tasks on {cfg.model} in {time.time() - started:.0f}s\n")
    print(format_comparison(runs))
