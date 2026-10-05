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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_claude(self, usage) -> None:
        """`usage` is the `usage` object on an Anthropic Messages response (may be missing in tests)."""
        if usage is None:
            return
        with self._lock:
            self.claude_input += getattr(usage, "input_tokens", 0) or 0
            self.claude_output += getattr(usage, "output_tokens", 0) or 0
            self.claude_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.claude_calls += 1

    def add_jev(self, input_tokens: int) -> None:
        with self._lock:
            self.jev_input += input_tokens or 0
            self.jev_calls += 1

    def add(self, other: Usage) -> None:
        with self._lock:
            for name, value in other.as_dict().items():
                setattr(self, name, getattr(self, name) + value)

    def as_dict(self) -> dict:
        # Not dataclasses.asdict(): that deep-copies fields, and a lock can't be copied.
        return {f: getattr(self, f) for f in COUNTERS}

    def summary(self) -> str:
        claude = f"Claude {self.claude_input:,} in / {self.claude_output:,} out ({self.claude_calls} calls)"
        if self.claude_cache_read:
            claude += f", {self.claude_cache_read:,} cached"
        return f"{claude} · Jev {self.jev_input:,} in ({self.jev_calls} calls)"
