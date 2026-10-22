"""Read-only queries behind the site's pages."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..core import stages

# Order of the stage columns and filters on the site.
STAGE_ORDER = (stages.SENT, stages.VIEWED, stages.INVITATION, stages.INTERVIEW, stages.OFFER,
               stages.REJECTED, stages.GHOSTED)
# Stages that mean the employer reacted to the application.
ANSWERED = (stages.INVITATION, stages.INTERVIEW, stages.OFFER, stages.REJECTED)

NOT_APPLIED = "none"  # filter value for vacancies we skipped or failed to apply to
PAGE = 100


@dataclass
class Filter:
    account: str = ""
    stage: str = ""        # "" = every application we sent, NOT_APPLIED, or a stage
    q: str = ""
    page: int = 1


def accounts(conn: sqlite3.Connection, configured: list[str]) -> list[str]:
    seen = [r[0] for r in conn.execute("SELECT DISTINCT account FROM applications ORDER BY account")]
    return list(dict.fromkeys(configured + seen))


def _day_start(tz: str, days_back: int = 0) -> str:
    local = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_back)).astimezone(timezone.utc).isoformat()


def summary(conn: sqlite3.Connection, tz: str, account: str | None = None) -> dict:
    """Counts for the cards on top of a page: by stage, today, last 7 days."""
    where, args = ("AND account = ?", [account]) if account else ("", [])
    by_stage = {s: 0 for s in STAGE_ORDER}
    for row in conn.execute(
        f"SELECT stage, COUNT(*) AS n FROM applications WHERE stage IS NOT NULL {where} GROUP BY stage", args
    ):
        by_stage[row["stage"]] = row["n"]
    total = sum(by_stage.values())

    def applied_since(start: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM applications WHERE status = 'applied' AND applied_at >= ? {where}",
            [start, *args],
        ).fetchone()[0]

    answered = sum(by_stage.get(s, 0) for s in ANSWERED)
    return {
        "total": total,
        "by_stage": by_stage,
        "today": applied_since(_day_start(tz)),
        "week": applied_since(_day_start(tz, 6)),
        "answered": answered,
        "answer_rate": round(100 * answered / total) if total else 0,
        "skipped": conn.execute(
            f"SELECT COUNT(*) FROM applications WHERE stage IS NULL {where}", args
        ).fetchone()[0],
    }


def daily(conn: sqlite3.Connection, tz: str, days: int = 14, account: str | None = None) -> list[dict]:
    """Applications sent per local day, oldest first, for the small bar chart."""
    where, args = ("AND account = ?", [account]) if account else ("", [])
    rows = conn.execute(
        f"SELECT applied_at FROM applications WHERE status = 'applied' AND applied_at >= ? {where}",
        [_day_start(tz, days - 1), *args],
    ).fetchall()
    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    counts = {today - timedelta(days=i): 0 for i in range(days)}
    for row in rows:
        day = datetime.fromisoformat(row["applied_at"]).astimezone(zone).date()
        if day in counts:
            counts[day] += 1
    return [{"day": d, "n": counts[d]} for d in sorted(counts)]


def applications(conn: sqlite3.Connection, f: Filter) -> tuple[list[sqlite3.Row], int]:
    """One page of the stage table and how many rows match in total."""
    clauses, args = [], []
    if f.account:
        clauses.append("account = ?")
        args.append(f.account)
    if f.stage == NOT_APPLIED:
        clauses.append("stage IS NULL")
    elif f.stage:
        clauses.append("stage = ?")
        args.append(f.stage)
    else:
        clauses.append("stage IS NOT NULL")
    if f.q:
        clauses.append("(name LIKE ? OR employer LIKE ? OR note LIKE ?)")
        args += [f"%{f.q}%"] * 3
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM applications WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM applications WHERE {where} ORDER BY applied_at DESC, id DESC LIMIT ? OFFSET ?",
        [*args, PAGE, (max(f.page, 1) - 1) * PAGE],
    ).fetchall()
    return rows, total


def recent_changes(conn: sqlite3.Connection, limit: int = 12, account: str | None = None) -> list[sqlite3.Row]:
    """Latest stage moves past "sent": what is worth a look."""
    where, args = ("AND a.account = ?", [account]) if account else ("", [])
    return conn.execute(
        "SELECT h.*, a.name, a.employer, a.account FROM stage_history h"
        " JOIN applications a ON a.id = h.application_id"
        f" WHERE h.stage != ? {where} ORDER BY h.at DESC, h.id DESC LIMIT ?",
        [stages.SENT, *args, limit],
    ).fetchall()


def custom_stages(conn: sqlite3.Connection) -> list[str]:
    """Stages typed by hand that are not in the standard list, for the drop-down."""
    known = set(STAGE_ORDER)
    rows = conn.execute("SELECT DISTINCT stage FROM applications WHERE stage IS NOT NULL ORDER BY stage")
    return [r[0] for r in rows if r[0] not in known]


def last_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
