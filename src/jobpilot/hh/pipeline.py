from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..db import Database
from ..filters import Filters, _has_word
from .apply import ApplyResult, CaptchaError
from .letter import load_template, render_letter
from .limits import can_apply, captcha_cooldown_hours, daily_limit, pause_seconds, short_pause_seconds
from .models import Account, Vacancy

log = logging.getLogger(__name__)


@dataclass
class Summary:
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: str | None = None
    lines: list[str] = field(default_factory=list)


def account_filters(account: Account) -> Filters:
    return Filters(include=list(account.include), exclude=list(account.exclude), require_marker=False, min_length=0)


def reject_reason(account: Account, vacancy: Vacancy) -> str | None:
    """Why the vacancy is not taken (None when it is): blocked employer, excluded word or no include word.

    Words are checked on the title and the snippet hh returns with the search result; employer names
    match as whole words, so "vk" blocks "VK Play" but not "Vkusno".
    """
    for name in account.exclude_employers:
        if _has_word(vacancy.employer, name):
            return f"employer {name!r}"
    text = f"{vacancy.name}\n{vacancy.snippet}"
    for word in account.exclude:
        if _has_word(text, word):
            return f"excluded word {word!r}"
    if account.include and not any(_has_word(text, word) for word in account.include):
        return "no include word in title/snippet"
    return None


def matches(account: Account, vacancy: Vacancy) -> bool:
    return reject_reason(account, vacancy) is None


# One server IP serves both accounts, so a captcha on either one puts all of hh on hold.
CAPTCHA_UNTIL = "hh_captcha_until"
CAPTCHA_STRIKES = "hh_captcha_strikes"


def captcha_hold(db: Database, now: datetime) -> datetime | None:
    """End of the current captcha cooldown, or None when hh may be used."""
    until = db.get(CAPTCHA_UNTIL)
    if not until:
        return None
    until_at = datetime.fromisoformat(until)
    return until_at if now < until_at else None


def record_captcha(db: Database, now: datetime) -> tuple[int, datetime]:
    """Start a cooldown that grows with every captcha in a row; returns (strikes, cooldown end)."""
    strikes = int(db.get(CAPTCHA_STRIKES, "0")) + 1
    until = now + timedelta(hours=captcha_cooldown_hours(strikes))
    db.set(CAPTCHA_STRIKES, str(strikes))
    db.set(CAPTCHA_UNTIL, until.isoformat())
    return strikes, until


def clear_captcha(db: Database) -> None:
    db.set(CAPTCHA_STRIKES, "0")
    db.set(CAPTCHA_UNTIL, "")


def _cached_search(db: Database, account: Account, now: datetime) -> list[Vacancy] | None:
    """The last search result while it is younger than `search_every_hours`, else None."""
    raw = db.get(f"hh_search_cache:{account.name}")
    if not raw:
        return None
    try:
        cache = json.loads(raw)
        if now - datetime.fromisoformat(cache["at"]) >= timedelta(hours=account.search_every_hours):
            return None
        return [Vacancy(**item) for item in cache["vacancies"]]
    except (ValueError, KeyError, TypeError):
        return None


async def collect_candidates(search, account: Account, db: Database, now: datetime | None = None) -> list[Vacancy]:
    """New vacancies of the account's queries. Search pages are the bulk of the hh traffic, so the list is
    loaded once per `search_every_hours` and the hourly passes in between take what is still not applied."""
    now = now or datetime.now(timezone.utc)
    found = _cached_search(db, account, now)
    if found is None:
        unique: dict[str, Vacancy] = {}
        for query in account.queries:
            for vacancy in await search.search(query, params=account.search_params, period_days=account.search_period):
                unique.setdefault(vacancy.id, vacancy)
        if account.recommended:
            for title in dict.fromkeys(account.resumes.values()):
                for vacancy in await search.recommended(title):
                    unique.setdefault(vacancy.id, vacancy)
        found = list(unique.values())
        db.set(f"hh_search_cache:{account.name}",
               json.dumps({"at": now.isoformat(), "vacancies": [asdict(v) for v in found]}, ensure_ascii=False))
    return [v for v in found if not v.has_test and not db.hh_seen_any(v.id)]


async def apply_pass(
    *, db: Database, account: Account, search, client, letters_dir: Path, tz: str,
    live: bool, notify: Callable[[str], None] = print, sleep=asyncio.sleep,
    now: datetime | None = None,
) -> Summary:
    """One run: find new vacancies, keep those matching the keywords, apply with the template letter."""
    local_now = now or datetime.now(ZoneInfo(tz))
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    limit = daily_limit(account, db.first_seen(f"hh_start:{account.name}"), local_now)
    done_today = db.hh_applied_since(account.name, start_of_day)
    lang = db.get("resume_lang", "ru")
    template = load_template(letters_dir, account, lang)
    summary = Summary()
    streak = 0  # consecutive failures

    def on_captcha() -> Summary:
        strikes, until = record_captcha(db, local_now)
        summary.stopped = "captcha"
        notify(f"hh/{account.name}: hh показал капчу. Оба аккаунта стоят до "
               f"{until.astimezone(ZoneInfo(tz)):%d.%m %H:%M} (капча {strikes} раз подряд). "
               f"Снять паузу раньше: /hh_resume")
        return summary

    try:
        candidates = await collect_candidates(search, account, db, local_now)
    except CaptchaError:
        return on_captcha()

    for vacancy in candidates:
        if not matches(account, vacancy):
            db.hh_record(account.name, vacancy.id, vacancy.name, vacancy.employer, vacancy.url, lang, "", "skipped")
            summary.skipped += 1
            continue
        if not can_apply(local_now, done_today + summary.applied, limit):
            summary.stopped = "daily limit or outside working hours"
            break
        if summary.applied >= account.per_pass:
            summary.stopped = "per-pass limit"
            break
        letter = render_letter(template, vacancy)
        if not live:
            summary.lines.append(f"[dry run] {vacancy.name} ({vacancy.employer}) {vacancy.url}")
            summary.applied += 1
            continue
        result = await client.apply(vacancy, account.resumes[lang], letter)
        if result is ApplyResult.CAPTCHA:
            return on_captcha()  # not recorded: the vacancy is tried again after the cooldown
        if result is ApplyResult.LIMIT:
            summary.stopped = "hh daily limit"  # not recorded either: tried again tomorrow
            break
        db.hh_record(account.name, vacancy.id, vacancy.name, vacancy.employer, vacancy.url, lang, letter, result.value)
        if result is ApplyResult.APPLIED:
            summary.applied += 1
            streak = 0
            clear_captcha(db)
            if done_today + summary.applied < limit and summary.applied < account.per_pass:
                await sleep(pause_seconds())
            continue
        await sleep(short_pause_seconds())  # requests back to back also look like a bot
        if result is ApplyResult.FAILED:
            summary.failed += 1
            streak += 1
            if streak >= 3:
                summary.stopped = "3 failures in a row"
                notify(f"hh/{account.name}: 3 failed applications, stopping, check data/screenshots")
                break
        else:
            summary.skipped += 1
    return summary
