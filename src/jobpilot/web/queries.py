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


# hh negotiation state of a chat (chats.state) as shown on the site, with the stage colour of its pill.
CHAT_STATES = {
    "RESPONSE": ("отклик", "sent"),
    "INVITATION": ("приглашение", "invitation"),
    "PHONE_INTERVIEW": ("звонок", "invitation"),
    "INTERVIEW": ("собеседование", "interview"),
    "ASSESSMENT": ("оценка", "invitation"),
    "DISCARD": ("отказ", "rejected"),
    "HIRED": ("оффер", "offer"),
}
_INVITED = "('INVITATION', 'PHONE_INTERVIEW', 'INTERVIEW', 'ASSESSMENT')"
_LAST_DIRECTION = ("(SELECT m.direction FROM messages m WHERE m.chat_id = c.id"
                   " ORDER BY m.sent_at DESC, m.id DESC LIMIT 1)")
# Status filter of the chat list. Chats read before v8 have no state until hh lists them again: for them
# the stage of the linked application stands in.
CHAT_FILTERS = {
    "unread": ("непрочитанные", "c.unread > 0"),
    "my_turn": ("ждут моего ответа", f"{_LAST_DIRECTION} = 'in'"),
    "invite": ("приглашение", f"(c.state IN {_INVITED} OR (c.state IS NULL AND a.stage IN ('invitation', 'interview')))"),
    "reject": ("отказ", "(c.state = 'DISCARD' OR (c.state IS NULL AND a.stage = 'rejected'))"),
    "offer": ("оффер", "(c.state = 'HIRED' OR (c.state IS NULL AND a.stage = 'offer'))"),
    "waiting": ("без ответа", "(c.state = 'RESPONSE' OR (c.state IS NULL AND COALESCE(a.stage, 'sent') IN ('sent', 'viewed')))"),
}


def _chat_where(account: str = "", q: str = "", status: str = "") -> tuple[str, list]:
    clauses, args = [], []
    if account:
        clauses.append("c.account = ?")
        args.append(account)
    if q:
        clauses.append("(c.title LIKE ? OR c.employer LIKE ? OR c.last_text LIKE ?"
                       " OR c.id IN (SELECT chat_id FROM messages WHERE text LIKE ?))")
        args += [f"%{q}%"] * 4
    if status in CHAT_FILTERS:
        clauses.append(CHAT_FILTERS[status][1])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


def chats(conn: sqlite3.Connection, account: str = "", q: str = "", status: str = "",
          limit: int = 300) -> list[sqlite3.Row]:
    """Chat list, newest first, with the stage of the linked application."""
    where, args = _chat_where(account, q, status)
    return conn.execute(
        "SELECT c.*, a.name AS app_name, a.employer AS app_employer, a.stage, a.stage_manual,"
        f" {_LAST_DIRECTION} AS last_direction"
        " FROM chats c LEFT JOIN applications a ON a.id = c.application_id"
        f" {where} ORDER BY c.last_message_at IS NULL, c.last_message_at DESC, c.id DESC LIMIT ?",
        [*args, limit],
    ).fetchall()


def chat_filter_counts(conn: sqlite3.Connection, account: str = "") -> dict[str, int]:
    """How many chats each status filter would show (for the account picked, all of them otherwise)."""
    counts = {}
    for key in ("", *CHAT_FILTERS):
        where, args = _chat_where(account, "", key)
        counts[key] = conn.execute(
            f"SELECT COUNT(*) FROM chats c LEFT JOIN applications a ON a.id = c.application_id {where}", args
        ).fetchone()[0]
    return counts


def unread_total(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(unread), 0) FROM chats").fetchone()[0]


def chat_for_application(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM chats WHERE application_id = ? ORDER BY last_message_at DESC LIMIT 1", (app_id,)
    ).fetchone()


# --- calendar ---

def interviews_between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[sqlite3.Row]:
    """Interviews in [start, end), with what they belong to."""
    return conn.execute(
        "SELECT i.*, a.name, a.employer, a.account, a.stage, a.url FROM interviews i"
        " LEFT JOIN applications a ON a.id = i.application_id"
        " WHERE i.starts_at >= ? AND i.starts_at < ? ORDER BY i.starts_at, i.id",
        (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
    ).fetchall()


def upcoming_interviews(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    """From an hour ago on: an interview that is going on right now still shows."""
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    return interviews_between(conn, start, start + timedelta(days=3650))[:limit]


def interviews_for_application(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM interviews WHERE application_id = ? ORDER BY starts_at", (app_id,)
    ).fetchall()


def linkable_applications(conn: sqlite3.Connection, keep: int | None = None) -> list[sqlite3.Row]:
    """Applications an interview can be tied to: the employer already reacted (plus the one tied now)."""
    return conn.execute(
        "SELECT id, account, name, employer, stage FROM applications"
        " WHERE (stage IS NOT NULL AND stage NOT IN (?, ?, ?)) OR id = ?"
        " ORDER BY updated_at DESC LIMIT 300",
        (stages.SENT, stages.VIEWED, stages.GHOSTED, keep or 0),
    ).fetchall()


# --- charts ---

# How far an application got, from the stages it ever had: each level includes the ones after it.
REACHED = (
    ("sent", "отправлено", None),
    ("viewed", "просмотрено", {stages.VIEWED, stages.INVITATION, stages.INTERVIEW, stages.OFFER, stages.REJECTED}),
    ("answered", "ответили", {stages.INVITATION, stages.INTERVIEW, stages.OFFER, stages.REJECTED}),
    ("invited", "пригласили", {stages.INVITATION, stages.INTERVIEW, stages.OFFER}),
    ("interview", "собеседование", {stages.INTERVIEW, stages.OFFER}),
    ("offer", "оффер", {stages.OFFER}),
)
# Employer answers per day: the first day an application got each of them (by hh or set by hand).
REACTIONS = (
    ("invited", "приглашение", {stages.INVITATION, stages.INTERVIEW, stages.OFFER}),
    ("rejected", "отказ", {stages.REJECTED}),
)


def _days(tz: str, days: int) -> list:
    today = datetime.now(ZoneInfo(tz)).date()
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]


def applied_by_day(conn: sqlite3.Connection, tz: str, days: int, accounts_: list[str]) -> dict:
    """{"days": [date...], "series": {account: [n per day]}} for applications sent."""
    zone, axis = ZoneInfo(tz), _days(tz, days)
    index = {d: i for i, d in enumerate(axis)}
    series = {a: [0] * days for a in accounts_}
    for row in conn.execute(
        "SELECT account, applied_at FROM applications WHERE status = 'applied' AND applied_at >= ?",
        (_day_start(tz, days - 1),),
    ):
        i = index.get(datetime.fromisoformat(row["applied_at"]).astimezone(zone).date())
        if i is not None:
            series.setdefault(row["account"], [0] * days)[i] += 1
    return {"days": axis, "series": series}


def reactions_by_day(conn: sqlite3.Connection, tz: str, days: int, account: str = "") -> dict:
    """{"days": [...], "series": {reaction: [n per day]}}: first invitation, rejection."""
    zone, axis = ZoneInfo(tz), _days(tz, days)
    index = {d: i for i, d in enumerate(axis)}
    series = {key: [0] * days for key, _, _ in REACTIONS}
    where, args = ("AND a.account = ?", [account]) if account else ("", [])
    first: dict[tuple[int, str], str] = {}
    for row in conn.execute(
        "SELECT h.application_id, h.stage, h.at FROM stage_history h JOIN applications a ON a.id = h.application_id"
        f" WHERE 1 = 1 {where} ORDER BY h.at", args
    ):
        for key, _, members in REACTIONS:
            if row["stage"] in members:
                first.setdefault((row["application_id"], key), row["at"])
    for (_, key), at in first.items():
        i = index.get(datetime.fromisoformat(at).astimezone(zone).date())
        if i is not None:
            series[key][i] += 1
    return {"days": axis, "series": series}


def funnel(conn: sqlite3.Connection, tz: str, days: int, account: str = "") -> dict:
    """How far the applications sent in the period got; by account and in total."""
    where, args = ("AND account = ?", [account]) if account else ("", [])
    apps = conn.execute(
        f"SELECT id, account, stage FROM applications WHERE status = 'applied' AND applied_at >= ? {where}",
        [_day_start(tz, days - 1), *args],
    ).fetchall()
    seen: dict[int, set[str]] = {r["id"]: {r["stage"]} for r in apps}
    if seen:
        marks = ",".join("?" * len(seen))
        for row in conn.execute(
            f"SELECT application_id, stage FROM stage_history WHERE application_id IN ({marks})", list(seen)
        ):
            seen[row["application_id"]].add(row["stage"])
        for row in conn.execute(
            f"SELECT DISTINCT application_id FROM interviews WHERE application_id IN ({marks})", list(seen)
        ):
            seen[row["application_id"]].add(stages.INTERVIEW)

    def count(rows) -> dict:
        out = {}
        for key, _, members in REACHED:
            out[key] = sum(1 for r in rows if members is None or seen[r["id"]] & members)
        out["rejected"] = sum(1 for r in rows if r["stage"] == stages.REJECTED)
        out["ghosted"] = sum(1 for r in rows if r["stage"] == stages.GHOSTED)
        return out

    by_account: dict[str, list] = {}
    for r in apps:
        by_account.setdefault(r["account"], []).append(r)
    return {"total": count(apps), "accounts": {a: count(rows) for a, rows in sorted(by_account.items())}}


# --- Telegram (v6) ---

VERDICTS = {"": "все", "new": "без оценки", "1": "интересно", "0": "мимо"}
TG_PAGE = 50


def tg_posts(conn: sqlite3.Connection, verdict: str = "", source: str = "", q: str = "",
             page: int = 1) -> tuple[list[sqlite3.Row], int]:
    where, args = [], []
    if verdict == "new":
        where.append("verdict IS NULL")
    elif verdict in ("0", "1"):
        where.append("verdict = ?")
        args.append(int(verdict))
    if source:
        where.append("source = ?")
        args.append(source)
    if q:
        where.append("text LIKE ?")
        args.append(f"%{q}%")
    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM tg_posts {sql_where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM tg_posts {sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*args, TG_PAGE, (max(page, 1) - 1) * TG_PAGE],
    ).fetchall()
    return rows, total


def tg_by_source(conn: sqlite3.Connection, tz: str) -> list[sqlite3.Row]:
    """Per source: posts found in all and in the last 7 days, and the last one's time."""
    return conn.execute(
        "SELECT source, COUNT(*) AS n, SUM(created_at >= ?) AS week, MAX(created_at) AS last"
        " FROM tg_posts GROUP BY source ORDER BY last DESC",
        (_day_start(tz, 6),),
    ).fetchall()


def tg_summary(conn: sqlite3.Connection, tz: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(sent = 0), 0) AS pending, COALESCE(SUM(created_at >= ?), 0) AS today,"
        " COALESCE(SUM(created_at >= ?), 0) AS week, COALESCE(SUM(verdict = 1), 0) AS liked FROM tg_posts",
        (_day_start(tz), _day_start(tz, 6)),
    ).fetchone()
    return dict(row)
