"""The shared database: the bot and the hh worker write it, the site reads it and queues tasks."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..filters import Filters
from . import stages
from .schema import migrate

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        # Autocommit: every write lands at once, so the site sees it; schema steps open their own transaction.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")  # the site reads while the workers write
        self._conn.execute("PRAGMA foreign_keys = ON")
        migrate(self._conn, Path(path))
        for key, value in DEFAULTS.items():
            self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))

    def filters(self) -> Filters:
        return Filters(
            include=json.loads(self.get("include")),
            exclude=json.loads(self.get("exclude")),
        )

    def set_keywords(self, key: str, words: list[str]) -> None:
        self.set(key, json.dumps(words, ensure_ascii=False))

    # --- events, for the site ---

    def event(self, kind: str, account: str | None = None, ref_id: int | None = None, **payload) -> None:
        self._conn.execute(
            "INSERT INTO events (kind, account, ref_id, payload, at) VALUES (?, ?, ?, ?, ?)",
            (kind, account, ref_id, json.dumps(payload, ensure_ascii=False), _now()),
        )

    def events_since(self, last_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)
        ).fetchall()

    # --- Telegram posts ---

    def add_post(self, digest: str, source: str, url: str, text: str) -> Post | None:
        """Store a post; returns None when an identical one was already seen."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO tg_posts(hash, source, url, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (digest, source, url, text, _now()),
        )
        if cur.rowcount == 0:
            return None
        self.event("tg_post", ref_id=cur.lastrowid, source=source)
        return Post(cur.lastrowid, source, url, text)

    def mark_sent(self, ids: list[int]) -> None:
        self._conn.executemany("UPDATE tg_posts SET sent = 1 WHERE id = ?", [(i,) for i in ids])

    def unsent(self) -> list[Post]:
        rows = self._conn.execute(
            "SELECT id, source, url, text FROM tg_posts WHERE sent = 0 ORDER BY id"
        ).fetchall()
        return [Post(r["id"], r["source"], r["url"], r["text"]) for r in rows]

    def set_verdict(self, post_id: int, verdict: int) -> None:
        self._conn.execute("UPDATE tg_posts SET verdict = ? WHERE id = ?", (verdict, post_id))

    def stats(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(sent = 0), 0) AS pending FROM tg_posts"
        ).fetchone()
        return {"total": row["total"], "pending": row["pending"]}

    # --- applications and their stages ---

    def application(self, app_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()

    def stage_history(self, app_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM stage_history WHERE application_id = ? ORDER BY at, id", (app_id,)
        ).fetchall()

    def _log_stage(self, app_id: int, account: str, stage: str, by: str, comment: str) -> None:
        self._conn.execute(
            "INSERT INTO stage_history (application_id, stage, changed_by, comment, at) VALUES (?, ?, ?, ?, ?)",
            (app_id, stage, by, comment, _now()),
        )
        self.event("stage", account, app_id, stage=stage, by=by)

    def suggest_stage(self, app_id: int, stage: str | None, comment: str = "") -> bool:
        """Automatic stage update: never over a stage set by hand, and only forward."""
        if stage is None:
            return False
        row = self.application(app_id)
        if row is None or row["stage_manual"] or not stages.system_may_move(row["stage"], stage):
            return False
        self._conn.execute(
            "UPDATE applications SET stage = ?, updated_at = ? WHERE id = ?", (stage, _now(), app_id)
        )
        self._log_stage(app_id, row["account"], stage, "system", comment)
        return True

    def set_stage(self, app_id: int, stage: str, comment: str = "") -> None:
        """A stage set by hand; from now on the system does not change it."""
        row = self.application(app_id)
        if row is None:
            raise LookupError(f"no application {app_id}")
        self._conn.execute(
            "UPDATE applications SET stage = ?, stage_manual = 1, updated_at = ? WHERE id = ?",
            (stage, _now(), app_id),
        )
        self._log_stage(app_id, row["account"], stage, "user", comment)

    # --- hh.ru applications ---

    def _hh_id(self, account: str, vacancy_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM applications WHERE source = 'hh' AND account = ? AND vacancy_id = ?",
            (account, vacancy_id),
        ).fetchone()
        return row["id"] if row else None

    def hh_seen(self, account: str, vacancy_id: str) -> bool:
        return self._hh_id(account, vacancy_id) is not None

    def hh_seen_any(self, vacancy_id: str) -> bool:
        """True if any of our accounts already handled this vacancy."""
        row = self._conn.execute(
            "SELECT 1 FROM applications WHERE source = 'hh' AND vacancy_id = ?", (vacancy_id,)
        ).fetchone()
        return row is not None

    def hh_record(self, account: str, vacancy_id: str, name: str, employer: str, url: str,
                  resume_lang: str, letter: str, status: str, now: datetime | None = None) -> None:
        at = (now or datetime.now(timezone.utc)).isoformat()
        self._conn.execute(
            "INSERT INTO applications"
            " (source, account, vacancy_id, name, employer, url, resume_lang, letter, status, applied_at, updated_at)"
            " VALUES ('hh', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source, account, vacancy_id) DO UPDATE SET"
            " name = excluded.name, employer = excluded.employer, url = excluded.url,"
            " resume_lang = excluded.resume_lang, letter = excluded.letter, status = excluded.status,"
            " applied_at = excluded.applied_at, updated_at = excluded.updated_at",
            (account, vacancy_id, name, employer, url, resume_lang, letter, status, at, _now()),
        )
        app_id = self._hh_id(account, vacancy_id)
        self.event("application", account, app_id, status=status)
        self.suggest_stage(app_id, stages.stage_for_status(status))

    def hh_applied_since(self, account: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM applications"
            " WHERE source = 'hh' AND account = ? AND status = 'applied' AND applied_at >= ?",
            (account, since.isoformat()),
        ).fetchone()
        return row["n"]

    def hh_set_status(self, account: str, vacancy_id: str, status: str) -> None:
        app_id = self._hh_id(account, vacancy_id)
        if app_id is None:
            return
        self._conn.execute(
            "UPDATE applications SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), app_id)
        )
        self.suggest_stage(app_id, stages.stage_for_status(status), "статус hh")

    def hh_sync_states(self, account: str, states: dict[str, str]) -> int:
        """Store the tags read from the negotiations page; returns how many stages moved."""
        moved = 0
        for vacancy_id, state in states.items():
            app_id = self._hh_id(account, vacancy_id)
            if app_id is None:
                continue  # a response sent by hand, not by us
            cur = self._conn.execute(
                "UPDATE applications SET hh_state = ?, updated_at = ? WHERE id = ? AND hh_state IS NOT ?",
                (state, _now(), app_id, state),
            )
            if cur.rowcount and self.suggest_stage(app_id, stages.stage_for_hh_state(state), f"hh: {state}"):
                moved += 1
        return moved

    def hh_due_reminders(self, account: str, older_than: datetime, max_reminders: int = 1) -> list[sqlite3.Row]:
        """Applications still without an employer reaction that are old enough for a nudge.

        A stage set to anything past "viewed" (by hh or by hand, e.g. "rejected") stops the reminders.
        """
        return self._conn.execute(
            "SELECT * FROM applications"
            " WHERE source = 'hh' AND account = ? AND status = 'applied' AND stage IN (?, ?)"
            " AND reminders < ? AND applied_at <= ?"
            " ORDER BY applied_at",
            (account, stages.SENT, stages.VIEWED, max_reminders, older_than.isoformat()),
        ).fetchall()

    def hh_mark_reminded(self, account: str, vacancy_id: str, now: datetime | None = None) -> None:
        at = (now or datetime.now(timezone.utc)).isoformat()
        self._conn.execute(
            "UPDATE applications SET reminders = reminders + 1, last_reminder_at = ?, updated_at = ?"
            " WHERE source = 'hh' AND account = ? AND vacancy_id = ?",
            (at, _now(), account, vacancy_id),
        )
        self.event("reminder", account, self._hh_id(account, vacancy_id))

    def first_seen(self, key: str, now: datetime | None = None) -> datetime:
        """Timestamp stored under `key` the first time it is asked for (used for the limit ramp-up)."""
        stored = self.get(key)
        if stored is not None:
            return datetime.fromisoformat(stored)
        now = now or datetime.now(timezone.utc)
        self.set(key, now.isoformat())
        return now
