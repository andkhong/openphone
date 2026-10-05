"""`openmob timings`: where agent steps spend their time, from the steps table.

Per group (model × Jev on/off, plus all models together): median / p90 / mean of each phase per
step, and steps per task. "Agent time" is total minus wait: waits are human or loading-screen time
(approvals, ask_user answers, loading screens), shown but kept out of the agent's own time.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

PHASE_ROWS = (  # (label, row key)
    ("observe", "observe_ms"),
    ("jev", "jev_ms"),
    ("model", "model_ms"),
    ("action", "action_ms"),
    ("settle", "settle_ms"),
    ("other", "other_ms"),  # total − every phase: bookkeeping between phases
    ("agent time", "agent_ms"),
    ("wait (human)", "wait_ms"),
    ("total", "total_ms"),
)
PHASES = ("observe_ms", "jev_ms", "wait_ms", "model_ms", "action_ms", "settle_ms")
ALL_MODELS = "(all models)"


def parse_since(text: str | None, now: float | None = None) -> float:
    """'7d', '24h', '30m', '90s' or a plain number of days → a unix time (0 for None/'all')."""
    if not text or text == "all":
        return 0.0
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([dhms]?)\s*", text.lower())
    if not match:
        raise ValueError(f"--since takes e.g. 7d, 24h, 30m or all, not {text!r}")
    amount, unit = float(match[1]), match[2] or "d"
    return (now if now is not None else time.time()) - amount * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default). None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def stats(values: list[float]) -> dict:
    return {"median": percentile(values, 0.5), "p90": percentile(values, 0.9),
            "mean": sum(values) / len(values) if values else None, "n": len(values)}


def _derived(row: dict) -> dict:
    out = dict(row)
    total, wait = row.get("total_ms"), row.get("wait_ms") or 0
    if total is not None:
        out["agent_ms"] = total - wait
        out["other_ms"] = max(total - sum(row.get(p) or 0 for p in PHASES), 0)
    if out.get("jev_ms") is None and row.get("jev") is not None:
        out["jev_ms"] = 0  # classifier off: it took no time
    return out


def summarize(rows: list[dict]) -> list[dict]:
    """Groups of steps by (model, jev), then by jev over all models. Each group: its label,
    tasks, steps, steps-per-task stats and per-phase stats (ms per step)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        row = _derived(row)
        jev = None if row.get("jev") is None else bool(row["jev"])
        groups[(row.get("model") or "?", jev)].append(row)
        groups[(ALL_MODELS, jev)].append(row)
    models = {m for m, _ in groups if m != ALL_MODELS}
    out = []
    for (model, jev), members in sorted(groups.items(), key=lambda kv: (kv[0][0] == ALL_MODELS, kv[0][0], kv[0][1] is not True)):
        if model == ALL_MODELS and len(models) < 2:
            continue  # the same as the one model's group
        per_task: dict[str, int] = defaultdict(int)
        for row in members:
            per_task[row["turn_id"]] += 1
        out.append({
            "model": model, "jev": jev, "steps": len(members), "tasks": len(per_task),
            "steps_per_task": stats([float(n) for n in per_task.values()]),
            "phases": {key: stats([float(r[key]) for r in members if r.get(key) is not None]) for _, key in PHASE_ROWS},
        })
    return out


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def format_report(groups: list[dict]) -> str:
    if not groups:
        return "No timed steps yet. Steps get timings from this version on."
    lines = []
    for g in groups:
        jev = {True: "jev on", False: "jev off", None: "jev unknown"}[g["jev"]]
        spt = g["steps_per_task"]
        lines.append(f"{g['model']} · {jev} · {g['tasks']} tasks · {g['steps']} steps · steps/task "
                     f"median {spt['median']:.1f}, p90 {spt['p90']:.1f}, mean {spt['mean']:.1f}")
        agent_mean = g["phases"]["agent_ms"]["mean"] or 0
        lines.append(f"  {'ms per step':<14}{'median':>9}{'p90':>9}{'mean':>9}{'% agent':>9}")
        for label, key in PHASE_ROWS:
            s = g["phases"][key]
            share = "" if key in ("agent_ms", "wait_ms", "total_ms") or not agent_mean or s["mean"] is None \
                else f"{100 * s['mean'] / agent_mean:.0f}%"
            lines.append(f"  {label:<14}{_ms(s['median']):>9}{_ms(s['p90']):>9}{_ms(s['mean']):>9}{share:>9}")
        lines.append("")
    lines.append("agent time = total − wait (approvals, answers, loading screens). "
                 "settle is counted on the step after the action that caused it.")
    return "\n".join(lines)
