"""Chat history, and the foundation for agents that learn from past interactions.

Every request is stored as a structured *turn*: what was asked, each step the agent took, how it
ended, which model ran it, its tokens, and the user's feedback (👍/👎). Later this lets an agent
look up what worked for similar requests, learn app-specific quirks, and measure success rates.

Text only: screenshots are never stored. Rows are scoped to the owning account, and deleting a
chat deletes its turns and events. SQLite in the data directory (a Fly volume when hosted).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".config" / "openmob") / "history.sqlite"
LOCAL_ACCOUNT = "local"  # owner of chats on servers without accounts (token mode)
TITLE_LENGTH = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_model TEXT,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_by_account ON chats (account_id, updated DESC);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,              -- the task id used in the live log
    chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    request TEXT NOT NULL,
    model TEXT,
    started REAL NOT NULL,
    finished REAL,
    success INTEGER,                  -- 1 / 0, NULL while running
    summary TEXT,
    steps INTEGER,
    usage TEXT,                       -- JSON token counts
    feedback INTEGER,                 -- 1 = 👍, -1 = 👎, NULL = none
    feedback_note TEXT
);
CREATE INDEX IF NOT EXISTS turns_by_chat ON turns (chat_id, started);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    turn_id TEXT,
    time REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT                         -- JSON details (final outcome, token counts)
);
CREATE INDEX IF NOT EXISTS events_by_chat ON events (chat_id, id);
"""


class History:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)  # private conversations: owner-only
        except OSError:
            pass

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- chats -------------------------------------------------------------

    def create_chat(self, account_id: str | None, device_id: str, device_model: str, first_request: str) -> str:
        chat_id = uuid.uuid4().hex[:16]
        now = time.time()
        title = " ".join(first_request.split())[:TITLE_LENGTH] or "New chat"
        self._q(
            "INSERT INTO chats (id, account_id, device_id, device_model, title, created, updated) VALUES (?,?,?,?,?,?,?)",
            (chat_id, account_id or LOCAL_ACCOUNT, device_id, device_model, title, now, now),
        )
        return chat_id

    def chat(self, account_id: str | None, chat_id: str) -> dict | None:
        """The chat if this account owns it, else None (callers answer 404)."""
        rows = self._q("SELECT * FROM chats WHERE id = ? AND account_id = ?", (chat_id, account_id or LOCAL_ACCOUNT))
        return dict(rows[0]) if rows else None

    def list_chats(self, account_id: str | None, limit: int = 100) -> list[dict]:
        rows = self._q(
            """SELECT c.*, (SELECT COUNT(*) FROM turns t WHERE t.chat_id = c.id) AS turns
               FROM chats c WHERE c.account_id = ? ORDER BY c.updated DESC LIMIT ?""",
            (account_id or LOCAL_ACCOUNT, limit),
        )
        return [dict(r) for r in rows]

    def rename_chat(self, account_id: str | None, chat_id: str, title: str) -> bool:
        title = " ".join(title.split())[:120]
        if not title or not self.chat(account_id, chat_id):
            return False
        self._q("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
        return True

    def delete_chat(self, account_id: str | None, chat_id: str) -> bool:
        """Deletes the chat with all its turns and events (not just hidden)."""
        if not self.chat(account_id, chat_id):
            return False
        self._q("DELETE FROM chats WHERE id = ?", (chat_id,))
        return True

    # --- recording (called by the Coordinator as events happen) ------------

    def record(self, chat_id: str, entry: dict) -> None:
        """Store one live-log entry and keep the turn summary up to date."""
        kind, task, data = entry["kind"], entry.get("task"), entry.get("data") or {}
        now = entry.get("time", time.time())
        with self._lock:
            db = self._db
            db.execute(
                "INSERT INTO events (chat_id, turn_id, time, kind, message, data) VALUES (?,?,?,?,?,?)",
                (chat_id, task, now, kind, entry["message"], json.dumps(data) if data else None),
            )
            db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            if task is None:
                return
            if kind == "heard" and not db.execute("SELECT 1 FROM turns WHERE id = ?", (task,)).fetchone():
                request = entry["message"].split("] ", 1)[-1] if entry["message"].startswith("[") else entry["message"]
                db.execute(
                    "INSERT INTO turns (id, chat_id, request, model, started) VALUES (?,?,?,?,?)",
                    (task, chat_id, request, data.get("model"), now),
                )
            elif data.get("final"):
                summary = entry["message"].rsplit(" (", 1)[0] if entry["message"].endswith(" steps)") else entry["message"]
                db.execute(
                    "UPDATE turns SET finished = ?, success = ?, summary = ?, steps = ? WHERE id = ?",
                    (now, 1 if data.get("success") else 0, summary, data.get("steps"), task),
                )
            elif kind == "usage":
                models = list((data.get("by_model") or {}).keys())
                db.execute(
                    "UPDATE turns SET usage = ?, model = COALESCE(?, model) WHERE id = ?",
                    (json.dumps(data), models[0] if len(models) == 1 else None, task),
                )

    # --- reading back ------------------------------------------------------

    def events(self, account_id: str | None, chat_id: str) -> list[dict] | None:
        """The chat's log, in the same shape as the live log (None if not this account's chat)."""
        if not self.chat(account_id, chat_id):
            return None
        rows = self._q("SELECT * FROM events WHERE chat_id = ? ORDER BY id", (chat_id,))
        out = []
        for r in rows:
            entry = {"time": r["time"], "kind": r["kind"], "message": r["message"], "task": r["turn_id"]}
            if r["data"]:
                entry["data"] = json.loads(r["data"])
            out.append(entry)
        return out

    def turns(self, account_id: str | None, chat_id: str) -> list[dict]:
        if not self.chat(account_id, chat_id):
            return []
        return [dict(r) for r in self._q("SELECT * FROM turns WHERE chat_id = ? ORDER BY started", (chat_id,))]

    def recent_requests(self, account_id: str | None, chat_id: str, limit: int = 5) -> list[tuple[str, str]]:
        """(request, outcome) pairs to restore the agent's memory when a chat is reopened."""
        done = [t for t in self.turns(account_id, chat_id) if t["finished"]]
        return [(t["request"], t["summary"] or "") for t in done[-limit:]]

    # --- feedback (learning signal) -----------------------------------------

    def set_feedback(self, account_id: str | None, turn_id: str, value: int, note: str = "") -> bool:
        if value not in (1, -1, 0):
            raise ValueError("feedback must be 1, -1 or 0")
        rows = self._q(
            "SELECT t.id FROM turns t JOIN chats c ON c.id = t.chat_id WHERE t.id = ? AND c.account_id = ?",
            (turn_id, account_id or LOCAL_ACCOUNT),
        )
        if not rows:
            return False
        self._q(
            "UPDATE turns SET feedback = ?, feedback_note = ? WHERE id = ?",
            (value or None, (note or "")[:1000] or None, turn_id),
        )
        return True

    def feedback_map(self, account_id: str | None, chat_id: str) -> dict[str, int]:
        return {t["id"]: t["feedback"] for t in self.turns(account_id, chat_id) if t["feedback"]}
