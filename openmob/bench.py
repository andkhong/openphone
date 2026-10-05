"""`openmob bench`: run the same tasks with and without the Jev classifier and compare.

Internal tool (docs/benchmarks.md). Each task runs once per mode from the home screen, on a fresh
agent, isolated from the chat's recent requests. Collected per run: success, steps, time per phase
(agent/timing.py), credits (steps × the model's tier price) and token counts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from .agent.timing import PHASES


@dataclass
class BenchTask:
    instruction: str
    name: str = ""
    max_steps: int | None = None


@dataclass
class BenchRun:
    task: BenchTask
    jev: bool
    success: bool
    steps: int
    summary: str
    timings: dict = field(default_factory=dict)  # ms per phase over the whole task
    credits: int = 0
    usage: dict = field(default_factory=dict)
    error: str | None = None


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
    make_agent: Callable[[bool], object],  # jev on? → a fresh Agent
    modes: tuple[bool, ...] = (True, False),
    credits_per_step: int = 1,
    go_home: bool = True,
    on_run: Callable[[BenchRun], None] | None = None,
) -> list[BenchRun]:
    runs = []
    for task in tasks:
        for jev in modes:
            agent = make_agent(jev)
            try:
                result = agent.run(task.instruction, go_home=go_home, max_steps=task.max_steps, isolated=True)
            except Exception as exc:  # one broken run must not lose the others
                run = BenchRun(task, jev, False, 0, "", error=f"{type(exc).__name__}: {exc}",
                               timings=agent.timer.task_totals(), usage=agent.usage.as_dict())
            else:
                run = BenchRun(task, jev, result.success, result.steps, result.summary, dict(result.timings),
                               result.steps * credits_per_step, result.usage.as_dict())
            runs.append(run)
            if on_run:
                on_run(run)
    return runs


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def agent_ms(timings: dict) -> int:
    return (timings.get("total_ms") or 0) - (timings.get("wait_ms") or 0)


def summarize(runs: list[BenchRun]) -> dict[bool, dict]:
    out = {}
    for jev in (True, False):
        mine = [r for r in runs if r.jev is jev]
        if not mine:
            continue
        out[jev] = {
            "runs": len(mine),
            "succeeded": sum(r.success for r in mine),
            "errors": sum(r.error is not None for r in mine),
            "steps": _mean([r.steps for r in mine]),
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
    modes = [m for m in (True, False) if m in s]
    label = {True: "jev on", False: "jev off"}
    lines = [f"{'per task (mean)':<20}" + "".join(f"{label[m]:>12}" for m in modes)
             + (f"{'Δ off−on':>12}" if len(modes) == 2 else "")]

    def row(name: str, key: str, fmt=lambda v: f"{v:,.0f}") -> None:
        values = [s[m][key] for m in modes]
        delta = f"{values[1] - values[0]:>+12,.0f}" if len(values) == 2 else ""
        lines.append(f"{name:<20}" + "".join(f"{fmt(v):>12}" for v in values) + delta)

    lines.append(f"{'success':<20}" + "".join(f"{s[m]['succeeded']}/{s[m]['runs']}".rjust(12) for m in modes))
    row("steps", "steps", lambda v: f"{v:.1f}")
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
    by_task: dict[int, dict[bool, BenchRun]] = {}
    for r in runs:
        by_task.setdefault(id(r.task), {})[r.jev] = r
    for pair in by_task.values():
        name = next(iter(pair.values())).task.name[:27]
        cells = []
        for m in modes:
            r = pair.get(m)
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
    modes = {"both": (True, False), "on": (True,), "off": (False,)}[args.only]
    if True in modes and not cfg.jev_api_key:
        sys.exit("TYPESAFE_API_KEY is not set, so there is no Jev to compare. Set it, or use --only off.")
    if args.serial:
        cfg.adb_serial = args.serial
    if args.model:
        cfg.model = args.model
    io = ConsoleIO(Speaker(False))

    def make_agent(jev: bool):
        return build_agent(dataclasses.replace(cfg, jev_enabled=jev), io)

    def progress(run: BenchRun) -> None:
        print(f"== {run.task.name} · {'jev on' if run.jev else 'jev off'}: "
              f"{'ok' if run.success else 'failed'} in {run.steps} steps, {agent_ms(run.timings) / 1000:.1f}s agent time"
              + (f" ({run.error})" if run.error else ""), flush=True)

    started = time.time()
    runs = run_bench(tasks, make_agent, modes, credits_per_step_for(cfg, cfg.model), go_home=not args.no_home,
                     on_run=progress)
    print(f"\n{len(runs)} runs of {len(tasks)} tasks on {cfg.model} in {time.time() - started:.0f}s\n")
    print(format_comparison(runs))
