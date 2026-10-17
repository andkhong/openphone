"""Load model keys into this process's environment as data (the way scripts/om's cmd_run does).

Only allowlisted names are read; values are never printed or logged. Refuses a keys file other users can read.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

KEYS_FILE = Path.home() / ".config" / "openmob-gatekeeper" / "provider-keys.env"
ALLOWED = {"ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_WORKSPACE_ID"}
REQUIRED = {"ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"}


def load(path: Path = KEYS_FILE) -> None:
    if not path.is_file():
        sys.exit("keys file missing; nothing loaded")
    if path.stat().st_mode & 0o077:
        sys.exit("keys file can be read by other users; keys were not loaded")
    for line in path.read_text().splitlines():
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            sys.exit("keys file has invalid quoting; keys were not loaded")
        if parts and parts[0] == "export":
            parts = parts[1:]
        if len(parts) != 1 or "=" not in parts[0]:
            continue
        name, value = parts[0].split("=", 1)
        if name in ALLOWED and value and not os.environ.get(name):
            os.environ[name] = value
    missing = [n for n in sorted(REQUIRED) if not os.environ.get(n)]
    if missing:
        sys.exit(f"missing keys: {', '.join(missing)}")
