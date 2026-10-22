"""Interview dates written by hand before the calendar existed: found in notes and moved into `interviews`."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import stages

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_TIME = r"(?:\s*(?:в|во|,|-|—)?\s*(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)(?!\d))?"
_NUMERIC = re.compile(r"(?<![\d.])(?P<d>[0-2]?\d|3[01])[./](?P<mo>0?[1-9]|1[0-2])(?:[./](?P<y>\d{4}|\d{2}))?(?![\d])" + _TIME)
_WORDS = re.compile(r"(?<!\d)(?P<d>[0-2]?\d|3[01])\s+(?P<mo>" + "|".join(MONTHS) + r")[а-я]*\.?(?:\s+(?P<y>\d{4}))?" + _TIME,
                    re.IGNORECASE)
_TIME_ONLY = re.compile(r"(?<![\d.:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_URL = re.compile(r"https?://[^\s<>\"']+")
_WORDS_INTERVIEW = ("собес", "интервью", "interview", "созвон", "встреч", "техническ", "скрининг")

DEFAULT_HOUR = 12  # when the note has a date but no time


@dataclass
class Found:
    starts_at: datetime     # aware, in the site time zone
    has_time: bool
    link: str


def _year(day: int, month: int, ref: datetime) -> int:
    """A date without a year is the one nearest to when it was written (a note in December may mean January)."""
    candidate = datetime(ref.year, month, min(day, 28) if month == 2 and day > 28 else day)
    if (ref.replace(tzinfo=None) - candidate).days > 60:
        return ref.year + 1
    if (candidate - ref.replace(tzinfo=None)).days > 300:
        return ref.year - 1
    return ref.year


def find_when(text: str, tz: str, ref: datetime) -> Found | None:
    """First date (and time, if any) in free text such as "собес 24.10 в 15:00" or "28 октября, 11:30"."""
    zone = ZoneInfo(tz)
    ref = ref.astimezone(zone)
    for pattern in (_NUMERIC, _WORDS):
        match = pattern.search(text)
        if not match:
            continue
        day = int(match["d"])
        mo = match["mo"].lower()
        month = int(mo) if mo.isdigit() else next(v for k, v in MONTHS.items() if mo.startswith(k))
        year = match["y"]
        year = (int(year) + 2000 if len(year) == 2 else int(year)) if year else _year(day, month, ref)
        hour, minute, has_time = DEFAULT_HOUR, 0, False
        if match["h"] is not None:
            hour, minute, has_time = int(match["h"]), int(match["m"]), True
        else:
            # "15:00, 24.10": a time somewhere else in the same text
            other = _TIME_ONLY.search(text[:match.start()] + " " + text[match.end():])
            if other:
                hour, minute, has_time = int(other[1]), int(other[2]), True
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=zone)
        except ValueError:
            continue
        url = _URL.search(text)
        return Found(when, has_time, url[0].rstrip(".,;)") if url else "")
    return None


def _looks_like_interview(stage: str | None) -> bool:
    return stage == stages.INTERVIEW or bool(stage and any(w in stage.lower() for w in _WORDS_INTERVIEW))


def candidates(db, tz: str) -> list[tuple[int, Found | None]]:
    """Applications at the interview stage that have no interview yet, with the date found in their notes."""
    rows = db.conn.execute(
        "SELECT a.* FROM applications a WHERE NOT EXISTS (SELECT 1 FROM interviews i WHERE i.application_id = a.id)"
        " AND (a.stage IS NOT NULL OR a.note != '') ORDER BY a.id"
    ).fetchall()
    out = []
    for row in rows:
        comments = [h["comment"] for h in db.stage_history(row["id"]) if h["changed_by"] == "user" and h["comment"]]
        text = "\n".join([row["note"], *comments])
        keyword = any(w in text.lower() for w in _WORDS_INTERVIEW)
        if not (_looks_like_interview(row["stage"]) or keyword):
            continue
        ref = datetime.fromisoformat(row["updated_at"] or row["applied_at"])
        found = find_when(text, tz, ref) if text.strip() else None
        if found is None and not _looks_like_interview(row["stage"]):
            continue  # a word like "созвон" without a date and without the stage says too little
        out.append((row["id"], found))
    return out


def import_from_notes(db, tz: str, dry_run: bool = False) -> list[tuple[int, Found]]:
    """Create calendar entries for interview dates found in notes; the notes stay as they are.

    Safe to run again: an application that already has an interview is skipped.
    """
    made = []
    for app_id, found in candidates(db, tz):
        if found is None:
            continue
        if not dry_run:
            note = "перенесено из заметки" + ("" if found.has_time else "; время в заметке не найдено, стоит 12:00")
            db.add_interview(found.starts_at.astimezone(timezone.utc), application_id=app_id, link=found.link,
                             note=note)
        made.append((app_id, found))
    return made


def import_once(db, tz: str) -> int:
    """The one automatic import, on the first start of the site with the calendar."""
    if db.get("interviews_imported") == "1":
        return 0
    made = import_from_notes(db, tz)
    db.set("interviews_imported", "1")
    return len(made)


def local_input(value: datetime, tz: str) -> str:
    """Value for an <input type=datetime-local>."""
    return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def parse_local(value: str, tz: str) -> datetime:
    """"2026-10-24T15:00" typed in the site time zone -> aware UTC datetime."""
    when = datetime.fromisoformat(value.strip())
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz))
    return when.astimezone(timezone.utc)

