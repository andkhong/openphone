"""`openmob prompts`: how often the owner is asked, and how they answer (the prompt budget).

Budget (docs/safety.md#permission-modes): at most 1 prompt per typical task (the median), and fewer
than 20% of approvals given in under 2 s (faster than that, the owner is likely not reading).
"""

from __future__ import annotations

from collections import Counter
from statistics import median

BUDGET_PROMPTS_PER_TASK = 1
FAST_APPROVAL_MS = 2000
BUDGET_FAST_SHARE = 0.20


def summarize(prompts: list[dict], tasks: list[dict]) -> dict:
    counts = [t["prompts"] or 0 for t in tasks]
    latencies = [p["latency_ms"] for p in prompts if isinstance(p.get("latency_ms"), int)]
    approvals = [p for p in prompts if p.get("decision") in ("approved", "always")]
    fast = [p for p in approvals if isinstance(p.get("latency_ms"), int) and p["latency_ms"] < FAST_APPROVAL_MS]
    by_mode: dict[str, list[int]] = {}
    for t in tasks:
        by_mode.setdefault(t.get("mode") or "?", []).append(t["prompts"] or 0)
    return {
        "tasks": len(tasks),
        "prompts": len(prompts),
        "median_prompts_per_task": median(counts) if counts else None,
        "tasks_with_prompts": sum(1 for c in counts if c),
        "median_latency_ms": int(median(latencies)) if latencies else None,
        "decisions": dict(Counter(p.get("decision") or "?" for p in prompts)),
        "kinds": dict(Counter(p.get("action_kind") or p.get("kind") or "?" for p in prompts)),
        "fast_approval_share": round(len(fast) / len(approvals), 3) if approvals else None,
        "by_mode": {m: {"tasks": len(c), "median_prompts": median(c)} for m, c in sorted(by_mode.items())},
    }


def format_report(s: dict) -> str:
    if not s["tasks"] and not s["prompts"]:
        return "No finished tasks with prompt counts in this period."
    lines = [f"Tasks: {s['tasks']} ({s['tasks_with_prompts']} asked at least once); prompts: {s['prompts']}"]
    med = s["median_prompts_per_task"]
    ok = med is not None and med <= BUDGET_PROMPTS_PER_TASK
    lines.append(f"Median prompts per task: {med if med is not None else '-'}  "
                 f"(budget ≤ {BUDGET_PROMPTS_PER_TASK}: {'ok' if ok else 'OVER'})")
    if s["median_latency_ms"] is not None:
        lines.append(f"Median time to answer: {s['median_latency_ms'] / 1000:.1f} s")
    share = s["fast_approval_share"]
    if share is not None:
        lines.append(f"Approvals in under {FAST_APPROVAL_MS / 1000:g} s: {share:.0%}  "
                     f"(budget < {BUDGET_FAST_SHARE:.0%}: {'ok' if share < BUDGET_FAST_SHARE else 'OVER'})")
    if s["decisions"]:
        lines.append("Decisions: " + ", ".join(f"{k} {v}" for k, v in sorted(s["decisions"].items())))
    if s["kinds"]:
        lines.append("Asked about: " + ", ".join(f"{k} {v}" for k, v in sorted(s["kinds"].items())))
    for mode, row in s["by_mode"].items():
        lines.append(f"  {mode:8} {row['tasks']:4} tasks, median {row['median_prompts']} prompts")
    return "\n".join(lines)
