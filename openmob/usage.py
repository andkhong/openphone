"""Token counts reported by the Claude and Jev APIs (exact numbers, not estimates)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


COUNTERS = ("claude_input", "claude_output", "claude_cache_read", "claude_calls", "jev_input", "jev_calls")


@dataclass
class Usage:
    claude_input: int = 0
    claude_output: int = 0
    claude_cache_read: int = 0
    claude_calls: int = 0
    jev_input: int = 0
    jev_calls: int = 0
    # Claude tokens per model, keyed by the model the API says served the call (not what we asked for).
    by_model: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_claude(self, usage, model: str | None = None) -> None:
        """`usage` is the `usage` object on an Anthropic Messages response (may be missing in tests);
        `model` is that response's `model` field."""
        if usage is None:
            return
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        with self._lock:
            self.claude_input += tokens_in
            self.claude_output += tokens_out
            self.claude_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.claude_calls += 1
            if model:
                entry = self.by_model.setdefault(model, {"input": 0, "output": 0, "calls": 0})
                entry["input"] += tokens_in
                entry["output"] += tokens_out
                entry["calls"] += 1

    def add_jev(self, input_tokens: int) -> None:
        with self._lock:
            self.jev_input += input_tokens or 0
            self.jev_calls += 1

    def add(self, other: Usage) -> None:
        data = other.as_dict()
        with self._lock:
            for name in COUNTERS:
                setattr(self, name, getattr(self, name) + data[name])
            for model, counts in data["by_model"].items():
                entry = self.by_model.setdefault(model, {"input": 0, "output": 0, "calls": 0})
                for key in entry:
                    entry[key] += counts[key]

    def as_dict(self) -> dict:
        # Not dataclasses.asdict(): that deep-copies fields, and a lock can't be copied.
        with self._lock:
            data = {f: getattr(self, f) for f in COUNTERS}
            data["by_model"] = {m: dict(c) for m, c in self.by_model.items()}
            return data

    def summary(self) -> str:
        """Claude tokens per model that served them. Jev usage is counted but not shown."""
        if not self.by_model:
            return f"{self.claude_input:,} in / {self.claude_output:,} out"
        return " · ".join(f"{m}: {c['input']:,} in / {c['output']:,} out" for m, c in self.by_model.items())
