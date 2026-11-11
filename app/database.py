"""SQLite storage: recent chat history, saved memories and scheduled jobs."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,              -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'reminder' or 'briefing'
    text TEXT NOT NULL,              -- reminder message or briefing topic
    next_run_utc TEXT NOT NULL,      -- ISO time in UTC
    daily_time TEXT,                 -- 'HH:MM' local time for daily briefings, else NULL
    active INTEGER NOT NULL DEFAULT 1
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, used only from the bot's event loop thread.
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- Conversation history (short-term) ---

    def add_message(self, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, utc_now()),
        )
        self.conn.commit()

    def recent_messages(self, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # --- Saved memories (long-term) ---

    def add_memory(self, content: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO memories (content, created_at) VALUES (?, ?)", (content, utc_now())
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_memories(self) -> list[str]:
        return [r["content"] for r in self.conn.execute("SELECT content FROM memories ORDER BY id")]

    # --- Scheduled jobs ---

    def add_job(self, kind: str, text: str, next_run_utc: str, daily_time: str | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO jobs (kind, text, next_run_utc, daily_time) VALUES (?, ?, ?, ?)",
            (kind, text, next_run_utc, daily_time),
        )
        self.conn.commit()
        return cursor.lastrowid

    def active_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE active = 1 ORDER BY next_run_utc"
        ).fetchall()

    def due_jobs(self, now_utc: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE active = 1 AND next_run_utc <= ? ORDER BY next_run_utc",
            (now_utc,),
        ).fetchall()

    def reschedule_job(self, job_id: int, next_run_utc: str) -> None:
        self.conn.execute("UPDATE jobs SET next_run_utc = ? WHERE id = ?", (next_run_utc, job_id))
        self.conn.commit()

    def deactivate_job(self, job_id: int) -> bool:
        cursor = self.conn.execute("UPDATE jobs SET active = 0 WHERE id = ? AND active = 1", (job_id,))
        self.conn.commit()
        return cursor.rowcount == 1