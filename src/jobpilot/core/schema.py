"""Schema of the shared database and the steps that bring an older file up to date.

The version lives in `PRAGMA user_version`. Each step runs once, in its own transaction,
and a copy of the file is saved next to it before the first step that changes existing data.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .stages import stage_for_status

log = logging.getLogger(__name__)

# v1: what the bot and the hh worker used before the site (tables settings, posts, hh_applications).
V1 = """
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

# v2: the shared base the workers write and the site reads.
V2 = """
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'hh',
    account TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    name TEXT NOT NULL,
    employer TEXT NOT NULL,
    url TEXT NOT NULL,
    resume_lang TEXT,
    letter TEXT,
    status TEXT NOT NULL,                       -- what the worker did: applied, skipped, failed, answered:<hh tag>, ...
    hh_state TEXT,                              -- last tag seen on hh.ru/applicant/negotiations
    stage TEXT,                                 -- see core/stages.py, NULL when we did not apply
    stage_manual INTEGER NOT NULL DEFAULT 0,    -- 1 once the stage was set by hand: the system leaves it alone
    note TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reminders INTEGER NOT NULL DEFAULT 0,
    last_reminder_at TEXT,
    UNIQUE (source, account, vacancy_id)
);
CREATE INDEX applications_vacancy ON applications (source, vacancy_id);
CREATE INDEX applications_stage ON applications (stage, applied_at);

CREATE TABLE stage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    changed_by TEXT NOT NULL,                   -- system or user
    comment TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX stage_history_application ON stage_history (application_id, at);

CREATE TABLE chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'hh',
    account TEXT NOT NULL,
    external_id TEXT NOT NULL,
    application_id INTEGER REFERENCES applications (id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    last_message_at TEXT,
    unread INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, account, external_id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    direction TEXT NOT NULL,                    -- in (employer) or out (us)
    author TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE (chat_id, external_id)
);

CREATE TABLE interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications (id) ON DELETE SET NULL,
    starts_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX interviews_starts ON interviews (starts_at);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    chat_id INTEGER NOT NULL REFERENCES chats (id),
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending, sent, error
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

-- What happened, in order: the site follows it to refresh pages.
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    account TEXT,
    ref_id INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL
);

ALTER TABLE posts RENAME TO tg_posts;
"""


# v3: what the chat reader needs to show a chat without an application of ours (a response sent by hand,
# an invitation from the employer) and to skip chats that did not change.
V3 = """
ALTER TABLE chats ADD COLUMN vacancy_id TEXT;
ALTER TABLE chats ADD COLUMN employer TEXT NOT NULL DEFAULT '';
ALTER TABLE chats ADD COLUMN last_text TEXT NOT NULL DEFAULT '';
ALTER TABLE chats ADD COLUMN synced_at TEXT;
CREATE INDEX chats_last ON chats (last_message_at);
CREATE INDEX chats_application ON chats (application_id);
CREATE INDEX messages_order ON messages (chat_id, sent_at);
"""


# v4: the negotiation state hh reports for a chat (RESPONSE, INVITATION, DISCARD, ...), for the status
# filter of the chat list, and where a reply typed on the site waits for the worker.
V4 = """
ALTER TABLE chats ADD COLUMN state TEXT;
CREATE INDEX outbox_status ON outbox (status, id);
"""


# v5: vacancies read from job-site feeds (RSS/Atom), per source and country; read-only, like tg_posts.
V5 = """
CREATE TABLE feed_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                       -- feed key, see feeds/sources.py
    country TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL,                  -- guid of the item
    hash TEXT NOT NULL,                         -- title + company, to drop the same job from a second site
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    created_at TEXT NOT NULL,
    matched INTEGER NOT NULL DEFAULT 0,         -- 1 = passed the feed words: goes to Telegram
    sent INTEGER NOT NULL DEFAULT 0,
    verdict INTEGER,
    UNIQUE (source, external_id)
);
CREATE INDEX feed_jobs_hash ON feed_jobs (hash, created_at);
CREATE INDEX feed_jobs_order ON feed_jobs (created_at);
CREATE INDEX feed_jobs_unsent ON feed_jobs (sent, matched)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(conn: sqlite3.Connection, script: str) -> None:
    # Not executescript: it commits on its own and would break the step's transaction.
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def _v1(conn: sqlite3.Connection) -> None:
    _run(conn, V1)


def _v2(conn: sqlite3.Connection) -> None:
    _run(conn, V2)
    now = _now()
    old = conn.execute("SELECT * FROM hh_applications ORDER BY id").fetchall()
    for row in old:
        stage = stage_for_status(row["status"])
        hh_state = row["status"].removeprefix("answered:") if row["status"].startswith("answered:") else None
        cur = conn.execute(
            "INSERT INTO applications (source, account, vacancy_id, name, employer, url, resume_lang, letter,"
            " status, hh_state, stage, applied_at, updated_at, reminders, last_reminder_at)"
            " VALUES ('hh', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["account"], row["vacancy_id"], row["name"], row["employer"], row["url"], row["resume_lang"],
             row["letter"], row["status"], hh_state, stage, row["applied_at"], now, row["reminders"],
             row["last_reminder_at"]),
        )
        if stage:
            conn.execute(
                "INSERT INTO stage_history (application_id, stage, changed_by, comment, at)"
                " VALUES (?, ?, 'system', 'перенос из v1', ?)",
                (cur.lastrowid, stage, row["applied_at"]),
            )
    # Kept, not dropped, so nothing is lost if the copy above ever needs checking.
    conn.execute("ALTER TABLE hh_applications RENAME TO hh_applications_v1")
    log.info("database v2: moved %d hh applications", len(old))


def _v3(conn: sqlite3.Connection) -> None:
    _run(conn, V3)


def _v4(conn: sqlite3.Connection) -> None:
    _run(conn, V4)


def _v5(conn: sqlite3.Connection) -> None:
    _run(conn, V5)


# Index + 1 is the version a step brings the file to.
STEPS: list[Callable[[sqlite3.Connection], None]] = [_v1, _v2, _v3, _v4, _v5]
VERSION = len(STEPS)


def _has_data(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return bool(tables & {"posts", "hh_applications", "tg_posts", "applications", "feed_jobs"})


def migrate(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """Bring the file to VERSION; returns the version it had before."""
    start = conn.execute("PRAGMA user_version").fetchone()[0]
    if start > VERSION:
        raise RuntimeError(f"database {path} is version {start}, this code knows only up to {VERSION}")
    if start == VERSION:
        return start
    if path is not None and _has_data(conn):
        backup = path.with_name(f"{path.name}.v{start}.bak")
        if not backup.exists():
            with sqlite3.connect(backup) as copy:
                conn.backup(copy)
            log.info("database backup before migration: %s", backup)
    for version in range(start + 1, VERSION + 1):
        conn.execute("BEGIN")
        try:
            STEPS[version - 1](conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return start
