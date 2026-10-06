"""What the agent remembers about its user: short pinned notes, model-neutral by design.

Two kinds of note:
- about the user ("Use my second game account", "I prefer dark mode"): read before every task;
- about one app, keyed by its package ("In Where Winds Meet the quests are under the scroll icon"):
  read when that app is in front.

Notes are plain text stored here, never in a model provider's own memory feature, so switching
models or providers keeps everything; any agent can also read them through the API.

A note is `active` (used in tasks) or `proposed` (the agent inferred it; the user approves or
rejects it). Only the user's own words become active directly; anything read from a screen can at
most be proposed, so a web page or message can't plant instructions for future tasks.

Rows are scoped to the owning account (other accounts get None/404). SQLite in the data dir.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .history import LOCAL_ACCOUNT

DEFAULT_PATH = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".config" / "openmob") / "memory.sqlite"
MAX_TEXT = 300  # characters per note: a note, not a document
MAX_NOTES = 500  # per account
MAX_USER_NOTES_IN_TASK = 40  # pinned notes about the user, sent with every task
MAX_APP_NOTES_IN_TASK = 25  # notes about the app in front
STATUSES = ("active", "proposed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    package TEXT,                     -- NULL: about the user; else about this app
    text TEXT NOT NULL,
    status TEXT NOT NULL,             -- active | proposed
    source TEXT NOT NULL,             -- user (said or typed it) | agent (inferred) | api
    task_id TEXT,                     -- the task it came from, if any
    created REAL NOT NULL,
    updated REAL NOT NULL,
    last_used REAL,
    uses INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notes_by_account ON notes (account_id, package, status);
"""

# The user's own request asks to remember something: the agent may save it as active right away.
REMEMBER_INTENT = re.compile(
    r"\b(remember|don'?t forget|keep in mind|note that|from now on|always|never|learn that|my preference)\b",
    re.IGNORECASE,
)


class NoteError(ValueError):
    """A note was empty, too long, or over the account's limit."""


def clean_text(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise NoteError("A note can't be empty.")
    if len(text) > MAX_TEXT:
        raise NoteError(f"Keep a note under {MAX_TEXT} characters.")
    return text


class Memory:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)  # personal facts: owner-only
        except OSError:
            pass

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- writing ---------------------------------------------------------------------

    def add(self, account_id: str | None, text: str, package: str | None = None, status: str = "active",
            source: str = "user", task_id: str | None = None) -> dict:
        if status not in STATUSES:
            raise NoteError(f"status must be one of: {', '.join(STATUSES)}")
        account = account_id or LOCAL_ACCOUNT
        text = clean_text(text)
        package = (package or "").strip() or None
        # Saying the same thing twice doesn't make two notes; a repeat of a proposal confirms it.
        same = self._q("SELECT id, status FROM notes WHERE account_id = ? AND package IS ? AND lower(text) = lower(?)",
                       (account, package, text))
        if same:
            if status == "active" and same[0]["status"] != "active":
                self._q("UPDATE notes SET status = 'active', updated = ? WHERE id = ?", (time.time(), same[0]["id"]))
            return self.get(account, same[0]["id"])
        if self._q("SELECT COUNT(*) FROM notes WHERE account_id = ?", (account,))[0][0] >= MAX_NOTES:
            raise NoteError(f"You have {MAX_NOTES} notes, the most allowed. Delete some first.")
        note_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._q(
            "INSERT INTO notes (id, account_id, package, text, status, source, task_id, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (note_id, account, package, text, status, source, task_id, now, now),
        )
        return self.get(account, note_id)

    def update(self, account_id: str | None, note_id: str, text: str | None = None,
               status: str | None = None) -> dict | None:
        """Edit a note's text and/or approve a proposal (status="active"). None if not this account's."""
        note = self.get(account_id, note_id)
        if note is None:
            return None
        if status is not None and status not in STATUSES:
            raise NoteError(f"status must be one of: {', '.join(STATUSES)}")
        self._q("UPDATE notes SET text = ?, status = ?, updated = ? WHERE id = ?",
                (clean_text(text) if text is not None else note["text"], status or note["status"], time.time(), note_id))
        return self.get(account_id, note_id)

    def delete(self, account_id: str | None, note_id: str) -> bool:
        """Delete (or reject, for a proposal) one note. False if not this account's."""
        if self.get(account_id, note_id) is None:
            return False
        self._q("DELETE FROM notes WHERE id = ?", (note_id,))
        return True

    def delete_all(self, account_id: str | None) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM notes WHERE account_id = ?", (account_id or LOCAL_ACCOUNT,)).rowcount

    # --- reading ---------------------------------------------------------------------

    def get(self, account_id: str | None, note_id: str) -> dict | None:
        rows = self._q("SELECT * FROM notes WHERE id = ? AND account_id = ?", (note_id, account_id or LOCAL_ACCOUNT))
        return self._public(rows[0]) if rows else None

    def list(self, account_id: str | None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM notes WHERE account_id = ?", [account_id or LOCAL_ACCOUNT]
        if status:
            sql += " AND status = ?"
            args.append(status)
        rows = self._q(sql + " ORDER BY package IS NOT NULL, package, created", tuple(args))
        return [self._public(r) for r in rows]

    def user_notes(self, account_id: str | None) -> list[dict]:
        """Active notes about the user, for every task (oldest first, capped)."""
        rows = self._q(
            "SELECT * FROM notes WHERE account_id = ? AND package IS NULL AND status = 'active' ORDER BY created LIMIT ?",
            (account_id or LOCAL_ACCOUNT, MAX_USER_NOTES_IN_TASK),
        )
        return [self._public(r) for r in rows]

    def app_notes(self, account_id: str | None, package: str) -> list[dict]:
        """Active notes about one app, for when it's in front."""
        rows = self._q(
            "SELECT * FROM notes WHERE account_id = ? AND package = ? AND status = 'active' ORDER BY created LIMIT ?",
            (account_id or LOCAL_ACCOUNT, package, MAX_APP_NOTES_IN_TASK),
        )
        return [self._public(r) for r in rows]

    def mark_used(self, note_ids: list[str]) -> None:
        if note_ids:
            now = time.time()
            with self._lock:
                self._db.executemany("UPDATE notes SET uses = uses + 1, last_used = ? WHERE id = ?",
                                     [(now, i) for i in note_ids])

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        note = dict(row)
        note.pop("account_id", None)
        return note


def render_user_notes(notes: list[dict]) -> str:
    """Pinned notes as plain text for any model's instructions ("" when there are none)."""
    if not notes:
        return ""
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return ("\n\nWhat you know about this user (notes they asked you to remember; follow them unless the "
            "current request says otherwise):\n" + lines)


def render_app_notes(package: str, notes: list[dict]) -> str:
    if not notes:
        return ""
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return f"\nYour notes about {package} (from this user):\n{lines}"
