"""Publish the dashboard over HTTPS with a Cloudflare quick tunnel (no account needed).

The tunnel runs as its own detached process and is reused across server restarts, so the
public URL (and every phone paired to it) stays the same. Stop it with `pkill cloudflared`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STATE_DIR = Path.home() / ".cache" / "openmob"


def _state_file(port: int) -> Path:
    return STATE_DIR / f"tunnel-{port}.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _reachable(url: str) -> bool:
    try:
        import certifi  # python.org builds on macOS ship without system CA certificates

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(f"{url}/pair", timeout=10, context=context) as response:
            return response.status == 200
    except Exception:
        return False


def open_tunnel(port: int, timeout: float = 30) -> str | None:
    """Return the public URL, reusing a running tunnel for this port when there is one."""
    state_file = _state_file(port)
    try:
        state = json.loads(state_file.read_text())
        if _alive(state["pid"]) and _reachable(state["url"]):
            return state["url"]
    except (OSError, ValueError, KeyError):
        pass

    binary = shutil.which("cloudflared")
    if not binary:
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / f"tunnel-{port}.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            # http2 (TCP) rather than the default QUIC: steadier on home networks that drop idle UDP.
            [binary, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://localhost:{port}"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # outlives this server, so restarts keep the same URL
        )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if match := URL_RE.search(log_path.read_text()):
            state_file.write_text(json.dumps({"pid": proc.pid, "url": match.group(0)}))
            return match.group(0)
        if proc.poll() is not None:
            return None
        time.sleep(0.3)
    return None
