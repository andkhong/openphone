"""Developer API records: one row per API task (the usage ledger credits are billed from) and an
append-only audit log of every API action.

Kept apart from the account store because both only grow: SQLite appends cheaply, while the
JSON account store rewrites the whole file on each change. Instructions are stored as a hash,
never as text (the request itself lives in the owner's chat history, under its retention rules).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".config" / "openmob") / "ledger.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tasks (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    instruction_hash TEXT NOT NULL,
    created REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    credits INTEGER NOT NULL DEFAULT 0,
    internal_usage TEXT               -- JSON token counts; admin/billing only, never in API responses
);
CREATE INDEX IF NOT EXISTS api_tasks_by_key ON api_tasks (key_id, created);
CREATE INDEX IF NOT EXISTS api_tasks_by_account ON api_tasks (account_id, created DESC);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    account_id TEXT,
    key_id TEXT,
    device_id TEXT,
    task_id TEXT,
    action TEXT NOT NULL,
    instruction_hash TEXT,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit (time DESC);
"""


def instruction_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def day_start(now: float | None = None) -> float:
    """Start of the current UTC day (daily quotas reset then)."""
    now = time.time() if now is None else now
    return now - now % 86400


class Ledger:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- audit (append-only: there is no update or delete) ----------------------

    def audit(self, action: str, account_id: str | None = None, key_id: str | None = None,
              device_id: str | None = None, task_id: str | None = None,
              instruction: str | None = None, outcome: str | None = None) -> None:
        self._q(
            "INSERT INTO audit (time, account_id, key_id, device_id, task_id, action, instruction_hash, outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), account_id, key_id, device_id, task_id, action,
             instruction_hash(instruction) if instruction is not None else None, outcome),
        )

    def audit_log(self, limit: int = 200, account_id: str | None = None) -> list[dict]:
        if account_id:
            rows = self._q("SELECT * FROM audit WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit))
        else:
            rows = self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- tasks / usage -------------------------------------------------------------

    def start_task(self, task_id: str, account_id: str, key_id: str, device_id: str, tier: str, instruction: str) -> None:
        self._q(
            "INSERT INTO api_tasks (id, account_id, key_id, device_id, tier, instruction_hash, created, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, account_id, key_id, device_id, tier, instruction_hash(instruction), time.time(), "queued"),
        )

    def finish_task(self, task_id: str, status: str, steps: int, credits: int) -> None:
        self._q("UPDATE api_tasks SET status = ?, steps = ?, credits = ?, finished = ? WHERE id = ?",
                (status, steps, credits, time.time(), task_id))

    def set_internal_usage(self, task_id: str, usage: dict) -> None:
        self._q("UPDATE api_tasks SET internal_usage = ? WHERE id = ?", (json.dumps(usage), task_id))

    def task(self, account_id: str, task_id: str) -> dict | None:
        """An API task, only if it belongs to this account (else None → 404)."""
        rows = self._q("SELECT * FROM api_tasks WHERE id = ? AND account_id = ?", (task_id, account_id))
        return dict(rows[0]) if rows else None

    def recent_tasks(self, account_id: str, limit: int = 50) -> list[dict]:
        rows = self._q(
            "SELECT id, key_id, device_id, tier, created, finished, status, steps, credits "
            "FROM api_tasks WHERE account_id = ? ORDER BY created DESC LIMIT ?",
            (account_id, limit),
        )
        return [dict(r) for r in rows]

    def tasks_today(self, key_id: str) -> int:
        return self._q("SELECT COUNT(*) FROM api_tasks WHERE key_id = ? AND created >= ?", (key_id, day_start()))[0][0]

    def usage(self, account_id: str, since: float) -> dict:
        row = self._q(
            "SELECT COUNT(*) AS tasks, COALESCE(SUM(steps), 0) AS steps, COALESCE(SUM(credits), 0) AS credits "
            "FROM api_tasks WHERE account_id = ? AND created >= ?",
            (account_id, since),
        )[0]
        return dict(row)
