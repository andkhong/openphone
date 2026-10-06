"""Token counts reported by the model providers and Jev (exact numbers, not estimates)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


COUNTERS = ("model_input", "model_output", "model_cache_read", "model_calls", "jev_input", "jev_calls")


@dataclass
class TokenUsage:
    """One model call's tokens, the same for every provider. `input` excludes cached input."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @classmethod
    def from_anthropic(cls, usage) -> TokenUsage | None:
        """From the `usage` object on an Anthropic Messages response (None when missing, as in fakes)."""
        if usage is None:
            return None
        return cls(
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


@dataclass
class Usage:
    model_input: int = 0
    model_output: int = 0
    model_cache_read: int = 0
    model_calls: int = 0
    jev_input: int = 0
    jev_calls: int = 0
    # Model tokens per model, keyed by the model the API says served the call (not what we asked for).
    by_model: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_model(self, tokens: TokenUsage | None, model: str | None = None) -> None:
        """One call to any provider's model; `model` is the id its response reports."""
        if tokens is None:
            return
        with self._lock:
            self.model_input += tokens.input
            self.model_output += tokens.output
            self.model_cache_read += tokens.cache_read
            self.model_calls += 1
            if model:
                entry = self.by_model.setdefault(model, {"input": 0, "output": 0, "calls": 0})
                entry["input"] += tokens.input
                entry["output"] += tokens.output
                entry["calls"] += 1

    def add_claude(self, usage, model: str | None = None) -> None:
        """An Anthropic Messages response's `usage` object (may be missing in tests)."""
        self.add_model(TokenUsage.from_anthropic(usage), model)

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
        """Model tokens per model that served them. Jev usage is counted but not shown."""
        if not self.by_model:
            return f"{self.model_input:,} in / {self.model_output:,} out"
        return " · ".join(f"{m}: {c['input']:,} in / {c['output']:,} out" for m, c in self.by_model.items())
