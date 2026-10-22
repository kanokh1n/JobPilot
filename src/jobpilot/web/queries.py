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

def accounts(conn: sqlite3.Connection, configured: list[str]) -> list[str]:
    """hh accounts: the configured ones and any that has applications (applications by hand are not one)."""
    seen = [r[0] for r in conn.execute("SELECT DISTINCT account FROM applications WHERE source = 'hh' ORDER BY account")]
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


# --- job-site feeds (v9) ---

def feed_by_source(conn: sqlite3.Connection, tz: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n, COALESCE(SUM(matched), 0) AS matched, COALESCE(SUM(created_at >= ?), 0) AS week,"
        " MAX(created_at) AS last FROM feed_jobs GROUP BY source",
        (_day_start(tz, 6),),
    ).fetchall()
    return {r["source"]: r for r in rows}


def feed_countries(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT country FROM feed_jobs ORDER BY country")]


def feed_summary(conn: sqlite3.Connection, tz: str, country: str = "") -> dict:
    where, args = ("WHERE lower(country) = ?", [country.lower()]) if country else ("", [])
    row = conn.execute(
        "SELECT COALESCE(SUM(matched = 1 AND created_at >= ?), 0) AS today,"
        " COALESCE(SUM(matched = 1 AND created_at >= ?), 0) AS week,"
        " COALESCE(SUM(matched = 1 AND sent = 0), 0) AS pending, COALESCE(SUM(verdict = 1), 0) AS liked,"
        f" COUNT(*) AS total FROM feed_jobs {where}",
        [_day_start(tz), _day_start(tz, 6), *args],
    ).fetchone()
    return dict(row)


# --- every vacancy in one list (v10): hh applications, Telegram posts and job-site feed items ---

# Status groups shared by all sources; "" (all) leaves out "skip".
BUCKETS = {"": "Все", "new": "Новые", "liked": "Интересные", "applied": "Отклик", "process": "В процессе",
           "offer": "Оффер", "rejected": "Отказ", "skip": "Мимо"}
JOBS_PAGE = 100
TG_TAB = "tg"

# kind: app (an application, from hh or sent by hand), tg or feed (found, not applied to yet).
# src: the tab a row belongs to (hh account, "tg", feed country in lower case); origin: account, channel or feed key.
# A post or feed item that got an application by hand shows only as that application.
_JOBS = f"""
SELECT 'app' AS kind, id, applied_at AS at, account AS src,
       CASE source WHEN 'tg' THEN (SELECT p.source FROM tg_posts p WHERE CAST(p.id AS TEXT) = vacancy_id)
                   WHEN 'feed' THEN (SELECT j.source FROM feed_jobs j WHERE CAST(j.id AS TEXT) = vacancy_id)
                   ELSE account END AS origin,
       name AS title, employer AS sub,
       '' AS place, url, stage, stage_manual, NULL AS verdict, note, '' AS body, status, source,
       CASE WHEN stage IS NULL THEN 'skip' WHEN stage IN ('{stages.SENT}', '{stages.VIEWED}') THEN 'applied'
            WHEN stage = '{stages.OFFER}' THEN 'offer'
            WHEN stage IN ('{stages.REJECTED}', '{stages.GHOSTED}') THEN 'rejected' ELSE 'process' END AS bucket,
       name || ' ' || employer || ' ' || note AS search
FROM applications
UNION ALL
SELECT 'tg', id, created_at, '{TG_TAB}', source, '', source, '', url, NULL, 0, verdict, '', text,
       CASE WHEN sent = 0 THEN 'pending' ELSE '' END, 'tg',
       CASE WHEN verdict IS NULL THEN 'new' WHEN verdict = 1 THEN 'liked' ELSE 'skip' END,
       text
FROM tg_posts WHERE CAST(id AS TEXT) NOT IN (SELECT vacancy_id FROM applications WHERE source = 'tg')
UNION ALL
SELECT 'feed', id, created_at, lower(country), source, title, company, location, url, NULL, 0, verdict, '', summary,
       CASE WHEN matched = 1 AND sent = 0 THEN 'pending' WHEN matched = 0 THEN 'other' ELSE '' END, 'feed',
       CASE WHEN matched = 0 OR verdict = 0 THEN 'skip' WHEN verdict = 1 THEN 'liked' ELSE 'new' END,
       title || ' ' || company || ' ' || summary || ' ' || categories
FROM feed_jobs WHERE CAST(id AS TEXT) NOT IN (SELECT vacancy_id FROM applications WHERE source = 'feed')
"""


@dataclass
class JobsFilter:
    src: str = ""          # "" = every source, an hh account, TG_TAB or a feed country ("rs")
    bucket: str = ""       # a BUCKETS key
    stage: str = ""        # exact stage, hh tabs only
    origin: str = ""       # channel or feed key, Telegram and feed tabs only
    q: str = ""
    page: int = 1


def _jobs_where(f: JobsFilter) -> tuple[str, list]:
    clauses, args = [], []
    if f.src:
        clauses.append("src = ?")
        args.append(f.src)
    if f.bucket in BUCKETS and f.bucket:
        clauses.append("bucket = ?")
        args.append(f.bucket)
    elif not f.stage:
        clauses.append("bucket != 'skip'")
    if f.stage:
        clauses.append("stage = ?")
        args.append(f.stage)
    if f.origin:
        clauses.append("origin = ?")
        args.append(f.origin)
    if f.q:
        clauses.append("search LIKE ?")
        args.append(f"%{f.q}%")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", args


def _title_of_post(text: str) -> tuple[str, str]:
    """A Telegram post has no title: its first line is one, the rest is the body."""
    lines = [line.strip() for line in (text or "").strip().splitlines()]
    first = next((line for line in lines if line), "")
    if len(first) > 140:
        cut = first[:140].rsplit(" ", 1)[0]
        return cut + "…", text.strip()
    rest = "\n".join(lines[lines.index(first) + 1:]).strip() if first else ""
    return first or "без текста", rest


def jobs(conn: sqlite3.Connection, f: JobsFilter) -> tuple[list[dict], int]:
    """One page of the merged list, newest first, and how many rows match."""
    where, args = _jobs_where(f)
    total = conn.execute(f"SELECT COUNT(*) FROM ({_JOBS}) {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM ({_JOBS}) {where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
        [*args, JOBS_PAGE, (max(f.page, 1) - 1) * JOBS_PAGE],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item.pop("search")
        if item["kind"] == "tg":
            item["title"], item["body"] = _title_of_post(item["body"])
        out.append(item)
    return out, total


def jobs_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """{src: {bucket: n}} over everything, for the tab and chip numbers ("" = all sources, all buckets but skip)."""
    counts: dict[str, dict[str, int]] = {"": {}}
    for row in conn.execute(f"SELECT src, bucket, COUNT(*) AS n FROM ({_JOBS}) GROUP BY src, bucket"):
        for key in (row["src"], ""):
            per = counts.setdefault(key, {})
            per[row["bucket"]] = per.get(row["bucket"], 0) + row["n"]
    for per in counts.values():
        per[""] = sum(n for b, n in per.items() if b not in ("", "skip"))
    return counts


def jobs_origins(conn: sqlite3.Connection, src: str) -> list[str]:
    """Channels of the Telegram tab or feed keys of a country tab, for the drop-down."""
    if src == TG_TAB:
        return [r[0] for r in conn.execute("SELECT DISTINCT source FROM tg_posts ORDER BY source")]
    return [r[0] for r in conn.execute("SELECT DISTINCT source FROM feed_jobs WHERE lower(country) = ? ORDER BY source",
                                       (src,))]


def new_since(conn: sqlite3.Connection, tz: str) -> dict:
    """New (not yet rated) Telegram posts and matched feed items found today, for the overview."""
    start = _day_start(tz)
    tg = conn.execute("SELECT COUNT(*) FROM tg_posts WHERE verdict IS NULL AND created_at >= ?", (start,)).fetchone()[0]
    feeds = conn.execute("SELECT COUNT(*) FROM feed_jobs WHERE verdict IS NULL AND matched = 1 AND created_at >= ?",
                         (start,)).fetchone()[0]
    return {"tg": tg, "feeds": feeds, "total": tg + feeds}


def my_turn_chats(conn: sqlite3.Connection) -> int:
    where, args = _chat_where(status="my_turn")
    return conn.execute(
        f"SELECT COUNT(*) FROM chats c LEFT JOIN applications a ON a.id = c.application_id {where}", args
    ).fetchone()[0]
