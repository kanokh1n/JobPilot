from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .filters import Filters

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    verdict INTEGER
);
CREATE TABLE IF NOT EXISTS hh_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    name TEXT NOT NULL,
    employer TEXT NOT NULL,
    url TEXT NOT NULL,
    resume_lang TEXT,
    letter TEXT,
    status TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    reminders INTEGER NOT NULL DEFAULT 0,
    last_reminder_at TEXT,
    UNIQUE (account, vacancy_id)
);
"""

DEFAULTS = {
    "include": json.dumps(["php", "go", "golang", "go lang"]),
    "exclude": json.dumps(["стажер", "стажёр", "джуниор", "junior", "intern"]),
    "mode": "digest",
    "digest_time": "09:00",
    "paused": "0",
}


@dataclass
class Post:
    id: int
    source: str
    url: str
    text: str


class Database:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        for key, value in DEFAULTS.items():
            self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def filters(self) -> Filters:
        return Filters(
            include=json.loads(self.get("include")),
            exclude=json.loads(self.get("exclude")),
        )

    def set_keywords(self, key: str, words: list[str]) -> None:
        self.set(key, json.dumps(words, ensure_ascii=False))

    def add_post(self, digest: str, source: str, url: str, text: str) -> Post | None:
        """Store a post; returns None when an identical one was already seen."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO posts(hash, source, url, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (digest, source, url, text, now),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return None
        return Post(cur.lastrowid, source, url, text)

    def mark_sent(self, ids: list[int]) -> None:
        self._conn.executemany("UPDATE posts SET sent = 1 WHERE id = ?", [(i,) for i in ids])
        self._conn.commit()

    def unsent(self) -> list[Post]:
        rows = self._conn.execute(
            "SELECT id, source, url, text FROM posts WHERE sent = 0 ORDER BY id"
        ).fetchall()
        return [Post(r["id"], r["source"], r["url"], r["text"]) for r in rows]

    def set_verdict(self, post_id: int, verdict: int) -> None:
        self._conn.execute("UPDATE posts SET verdict = ? WHERE id = ?", (verdict, post_id))
        self._conn.commit()

    def stats(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(sent = 0), 0) AS pending FROM posts"
        ).fetchone()
        return {"total": row["total"], "pending": row["pending"]}

    # --- hh.ru applications ---

    def hh_seen(self, account: str, vacancy_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM hh_applications WHERE account = ? AND vacancy_id = ?", (account, vacancy_id)
        ).fetchone()
        return row is not None

    def hh_seen_any(self, vacancy_id: str) -> bool:
        """True if any of our accounts already handled this vacancy."""
        row = self._conn.execute("SELECT 1 FROM hh_applications WHERE vacancy_id = ?", (vacancy_id,)).fetchone()
        return row is not None

    def hh_record(self, account: str, vacancy_id: str, name: str, employer: str, url: str,
                  resume_lang: str, letter: str, status: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT OR REPLACE INTO hh_applications"
            "(account, vacancy_id, name, employer, url, resume_lang, letter, status, applied_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account, vacancy_id, name, employer, url, resume_lang, letter, status, now.isoformat()),
        )
        self._conn.commit()

    def hh_applied_since(self, account: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM hh_applications"
            " WHERE account = ? AND status = 'applied' AND applied_at >= ?",
            (account, since.isoformat()),
        ).fetchone()
        return row["n"]

    def hh_set_status(self, account: str, vacancy_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE hh_applications SET status = ? WHERE account = ? AND vacancy_id = ?",
            (status, account, vacancy_id),
        )
        self._conn.commit()

    def hh_due_reminders(self, account: str, older_than: datetime, max_reminders: int = 1) -> list[sqlite3.Row]:
        """Applications still without an employer reaction that are old enough for a nudge."""
        return self._conn.execute(
            "SELECT * FROM hh_applications"
            " WHERE account = ? AND status = 'applied' AND reminders < ? AND applied_at <= ?"
            " ORDER BY applied_at",
            (account, max_reminders, older_than.isoformat()),
        ).fetchall()

    def hh_mark_reminded(self, account: str, vacancy_id: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._conn.execute(
            "UPDATE hh_applications SET reminders = reminders + 1, last_reminder_at = ?"
            " WHERE account = ? AND vacancy_id = ?",
            (now.isoformat(), account, vacancy_id),
        )
        self._conn.commit()

    def first_seen(self, key: str, now: datetime | None = None) -> datetime:
        """Timestamp stored under `key` the first time it is asked for (used for the limit ramp-up)."""
        stored = self.get(key)
        if stored is not None:
            return datetime.fromisoformat(stored)
        now = now or datetime.now(timezone.utc)
        self.set(key, now.isoformat())
        return now
