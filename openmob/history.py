"""Chat history, and the foundation for agents that learn from past interactions.

Every request is stored as a structured *turn*: what was asked, each step the agent took, how it
ended, which model ran it, its tokens, and the user's feedback (👍/👎). Each step is also a
structured *steps* row (tool, redacted args, normalized coordinates, app, element anchor, outcome,
latency), the raw material for learning skills, app notes and per-model success rates.

Text only: screenshots are never stored, and typed text is redacted. Rows are scoped to the owning
account, and deleting a chat deletes its turns, events and steps. SQLite in the data directory (a
Fly volume when hosted).
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
    feedback_note TEXT,
    package TEXT                      -- the last foreground app
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

-- One row per tool call the agent made (the trajectory). Never a screenshot; typed text redacted.
CREATE TABLE IF NOT EXISTS steps (
    turn_id TEXT NOT NULL,
    idx INTEGER NOT NULL,             -- 1, 2, … within the turn
    chat_id TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    device_id TEXT,
    t REAL NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,               -- JSON, redacted
    norm_xy TEXT,                     -- JSON [x, y, …]: each point as 0-1 fractions of the device screen
    package TEXT,                     -- foreground app when the step was taken
    screen_w INTEGER,
    screen_h INTEGER,
    anchor TEXT,                      -- JSON {text, desc, resource_id, class, bounds, index} for tap_element
    outcome TEXT,
    is_error INTEGER NOT NULL,
    latency_ms INTEGER,               -- the tool call itself: risk check, approval/answer waits, phone action (no settle, no model)
    source TEXT NOT NULL DEFAULT 'agent',  -- agent | replay (a stored path, no main model) | user_demo | macro | routine
    -- Where the step's time went, in ms (see agent/timing.py; NULL on rows from before timings).
    -- A step runs from the end of the previous step's action to the end of its own.
    observe_ms INTEGER,
    jev_ms INTEGER,                   -- the screen classifier (0 when it was off)
    wait_ms INTEGER,                  -- human / loading-screen time, kept out of agent time
    model_ms INTEGER,
    action_ms INTEGER,
    settle_ms INTEGER,
    total_ms INTEGER,
    jev INTEGER,                      -- 1 / 0: whether the classifier was on for this step
    model TEXT,                       -- the model that decided this step
    settle_idle INTEGER,              -- 1 / 0: the adaptive settle before it saw the screen go quiet / hit its cap (NULL: fixed sleep)
    fast INTEGER,                     -- 1: decided by the fast path (classifier + small text model), 0: by the main model
    variant TEXT,                     -- the latency features the agent ran with: fixed | adaptive | adaptive+fast …
    done_via TEXT,                    -- on a done step: vision (the main model) | classifier (fast_path_done)
    handled_by TEXT,                  -- a step no model decided: permission_rule (agent/permission_dialogs.py)
    -- The main model call(s) that decided this step (NULL on fast steps without one, and older rows).
    input_tokens INTEGER,             -- uncached input
    output_tokens INTEGER,
    cache_read_tokens INTEGER,        -- input read from the provider's prompt cache
    cache_write_tokens INTEGER,       -- input written to it (Anthropic)
    vision_image_side INTEGER,        -- the long side in px of the screenshot the model saw (NULL: none)
    PRIMARY KEY (turn_id, idx)
);
CREATE INDEX IF NOT EXISTS steps_by_chat ON steps (chat_id);

-- Per-model outcomes of finished tasks (no content), for success rates by model. Kept when a chat
-- is deleted, since it holds only the model, the outcome and the step count.
CREATE TABLE IF NOT EXISTS model_runs (
    turn_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    model TEXT NOT NULL,
    success INTEGER NOT NULL,
    steps INTEGER,
    finished REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS model_runs_by_model ON model_runs (model, account_id);

-- One row per approval prompt the owner saw (permission telemetry, `openmob prompts`). No text: the
-- prompt's kind, the mode, the decision and how long the owner took from shown to answered.
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    device_id TEXT,
    t REAL NOT NULL,
    kind TEXT,                        -- action | step (ask mode) | plan
    action_kind TEXT,                 -- send | delete | post | pay | fill | settings | NULL
    mode TEXT,                        -- the permission mode then
    decision TEXT,                    -- approved | always | denied | changes
    latency_ms INTEGER,
    package TEXT
);
CREATE INDEX IF NOT EXISTS prompts_by_time ON prompts (t);
"""

# Columns added after the first release, for databases created before them.
TIMING_COLUMNS = ("observe_ms", "jev_ms", "wait_ms", "model_ms", "action_ms", "settle_ms", "total_ms")
TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "vision_image_side")
MIGRATIONS = [("turns", "package", "TEXT")] + [
    ("steps", column, "INTEGER") for column in TIMING_COLUMNS + ("jev",)
] + [("steps", "model", "TEXT"), ("steps", "settle_idle", "INTEGER"), ("steps", "fast", "INTEGER"),
      ("steps", "variant", "TEXT"), ("steps", "done_via", "TEXT"), ("steps", "handled_by", "TEXT"),
      ("turns", "prompts", "INTEGER"), ("turns", "mode", "TEXT")] + [
    ("steps", column, "INTEGER") for column in TOKEN_COLUMNS
] + [("steps", column, kind) for column, kind in (
    # A wait (agent/waiting.py) on this step: the wait_until tool's, the `wait` tool's sleep, or the loop's own
    # ("auto:…", after a launch or an Install tap). wait_cond: e.g. text_appears:Open|Play, sleep, auto:loading_done.
    ("wait_cond", "TEXT"), ("wait_met", "INTEGER"), ("wait_via", "TEXT"), ("waited_ms", "INTEGER"))]
WAIT_COLUMNS = ("wait_cond", "wait_met", "wait_via", "waited_ms")


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
        for table, column, kind in MIGRATIONS:
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
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
                    "UPDATE turns SET finished = ?, success = ?, summary = ?, steps = ?, prompts = ?, mode = ? "
                    "WHERE id = ?",
                    (now, 1 if data.get("success") else 0, summary, data.get("steps"), data.get("prompts"),
                     data.get("mode"), task),
                )
            elif kind == "usage":
                models = list((data.get("by_model") or {}).keys())
                db.execute(
                    "UPDATE turns SET usage = ?, model = COALESCE(?, model) WHERE id = ?",
                    (json.dumps(data), models[0] if len(models) == 1 else None, task),
                )

    def record_step(self, chat_id: str, turn_id: str, step: dict, account_id: str | None = None, device_id: str = "") -> None:
        """Store one structured step (from Agent._record_step) and the turn's latest foreground app."""
        def js(value):
            return json.dumps(value) if value is not None else None

        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO steps (turn_id, idx, chat_id, account_id, device_id, t, tool, args, norm_xy,
                   package, screen_w, screen_h, anchor, outcome, is_error, latency_ms, source,
                   observe_ms, jev_ms, wait_ms, model_ms, action_ms, settle_ms, total_ms, jev, model,
                   settle_idle, fast, variant, done_via, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, vision_image_side, handled_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (turn_id, step["idx"], chat_id, account_id or LOCAL_ACCOUNT, device_id, step.get("t", time.time()),
                 step["tool"], json.dumps(step.get("args") or {}, default=str), js(step.get("norm_xy")),
                 step.get("package"), step.get("screen_w"), step.get("screen_h"), js(step.get("anchor")),
                 step.get("outcome"), 1 if step.get("is_error") else 0, step.get("latency_ms"),
                 step.get("source", "agent"),
                 *(step.get(c) for c in TIMING_COLUMNS),
                 None if step.get("jev") is None else int(bool(step["jev"])), step.get("model"),
                 None if step.get("settle_idle") is None else int(bool(step["settle_idle"])),
                 None if step.get("fast") is None else int(bool(step["fast"])), step.get("variant"),
                 step.get("done_via"), *(step.get(c) for c in TOKEN_COLUMNS), step.get("handled_by")),
            )
            if step.get("wait_cond"):
                met = step.get("wait_met")
                self._db.execute("UPDATE steps SET wait_cond = ?, wait_met = ?, wait_via = ?, waited_ms = ? "
                                 "WHERE turn_id = ? AND idx = ?",
                                 (step["wait_cond"], None if met is None else int(bool(met)), step.get("wait_via"),
                                  step.get("waited_ms"), turn_id, step["idx"]))
            if step.get("package"):
                self._db.execute("UPDATE turns SET package = ? WHERE id = ?", (step["package"], turn_id))

    def record_prompt(self, turn_id: str, prompt: dict, account_id: str | None = None, device_id: str = "") -> None:
        """One approval prompt (Agent._ask_owner): its kind, mode, decision and latency. No text."""
        self._q(
            "INSERT INTO prompts (turn_id, account_id, device_id, t, kind, action_kind, mode, decision, latency_ms, "
            "package) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (turn_id, account_id or LOCAL_ACCOUNT, device_id, prompt.get("t", time.time()), prompt.get("kind"),
             prompt.get("action_kind"), prompt.get("mode"), prompt.get("decision"), prompt.get("latency_ms"),
             prompt.get("package")),
        )

    def prompt_rows(self, since: float = 0.0) -> tuple[list[dict], list[dict]]:
        """(prompts since then, finished tasks since then with their prompt count and mode)."""
        prompts = [dict(r) for r in self._q("SELECT * FROM prompts WHERE t >= ? ORDER BY t", (since,))]
        tasks = [dict(r) for r in self._q(
            "SELECT id, prompts, mode, success FROM turns WHERE finished IS NOT NULL AND started >= ? "
            "AND prompts IS NOT NULL ORDER BY started", (since,))]
        return prompts, tasks

    def record_model_run(self, account_id: str | None, turn_id: str, model: str, success: bool, steps: int | None) -> None:
        self._q(
            "INSERT OR REPLACE INTO model_runs (turn_id, account_id, model, success, steps, finished) VALUES (?,?,?,?,?,?)",
            (turn_id, account_id or LOCAL_ACCOUNT, model, 1 if success else 0, steps, time.time()),
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

    def turn(self, account_id: str | None, turn_id: str) -> dict | None:
        """One turn (a task) by its id, if this account owns it, with its chat's device."""
        rows = self._q(
            """SELECT t.*, c.device_id FROM turns t JOIN chats c ON c.id = t.chat_id
               WHERE t.id = ? AND c.account_id = ?""",
            (turn_id, account_id or LOCAL_ACCOUNT),
        )
        return dict(rows[0]) if rows else None

    def steps(self, account_id: str | None, turn_id: str) -> list[dict]:
        """A turn's structured steps, in order (empty if it isn't this account's)."""
        rows = self._q("SELECT * FROM steps WHERE turn_id = ? AND account_id = ? ORDER BY idx",
                       (turn_id, account_id or LOCAL_ACCOUNT))
        out = []
        for r in rows:
            step = dict(r)
            for key in ("args", "norm_xy", "anchor"):
                step[key] = json.loads(step[key]) if step[key] else None
            step["is_error"] = bool(step["is_error"])
            for key in ("jev", "settle_idle", "fast"):
                step[key] = None if step[key] is None else bool(step[key])
            out.append(step)
        return out

    def step_timings(self, since: float = 0.0, chat_id: str | None = None, model: str | None = None) -> list[dict]:
        """Every account's agent steps that have timings (for `openmob timings`): the timing
        columns plus turn_id, jev and model (the step's, else its turn's)."""
        where, args = ["s.total_ms IS NOT NULL", "s.t >= ?", "s.source IN ('agent', 'replay')"], [since]
        if chat_id:
            where.append("s.chat_id = ?")
            args.append(chat_id)
        if model:
            where.append("COALESCE(s.model, t.model) = ?")
            args.append(model)
        rows = self._q(
            f"""SELECT s.turn_id, s.idx, s.jev, s.fast, s.variant, s.settle_idle, s.done_via, COALESCE(s.model, t.model) AS model,
                       {", ".join("s." + c for c in TIMING_COLUMNS + TOKEN_COLUMNS + WAIT_COLUMNS)}
                FROM steps s LEFT JOIN turns t ON t.id = s.turn_id WHERE {" AND ".join(where)} ORDER BY s.t""",
            tuple(args),
        )
        return [dict(r) for r in rows]

    def model_stats(self, account_id: str | None = None, since: float = 0.0) -> list[dict]:
        """Success rate per model over finished tasks (stopped tasks don't count): one account's,
        or every account's when account_id is None. Most-used model first."""
        where, args = "finished >= ?", [since]
        if account_id is not None:
            where += " AND account_id = ?"
            args.append(account_id)
        rows = self._q(
            f"""SELECT model, COUNT(*) AS tasks, SUM(success) AS succeeded, AVG(steps) AS avg_steps
                FROM model_runs WHERE {where} GROUP BY model ORDER BY tasks DESC, model""",
            tuple(args),
        )
        return [{"model": r["model"], "tasks": r["tasks"], "succeeded": r["succeeded"],
                 "success_rate": round(r["succeeded"] / r["tasks"], 3),
                 "avg_steps": round(r["avg_steps"], 1) if r["avg_steps"] is not None else None} for r in rows]

    def recent_turns(self, account_id: str | None, limit: int = 50) -> list[dict]:
        """This account's latest turns across all chats, newest first."""
        rows = self._q(
            """SELECT t.id, t.request, t.started, t.finished, t.success, t.summary, t.steps, c.device_id
               FROM turns t JOIN chats c ON c.id = t.chat_id WHERE c.account_id = ?
               ORDER BY t.started DESC LIMIT ?""",
            (account_id or LOCAL_ACCOUNT, limit),
        )
        return [dict(r) for r in rows]

    def purge(self, older_than_days: float) -> int:
        """Retention: delete chats not updated for this many days (turns and events go with them)."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            return self._db.execute("DELETE FROM chats WHERE updated < ?", (cutoff,)).rowcount

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
