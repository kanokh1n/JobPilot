from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..db import Database
from ..filters import Filters
from .apply import ApplyResult
from .letter import load_template, render_letter
from .limits import can_apply, daily_limit, pause_seconds
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


def matches(account: Account, vacancy: Vacancy) -> bool:
    """Keyword check on the title and the snippet hh returns with the search result."""
    return account_filters(account).match(f"{vacancy.name}\n{vacancy.snippet}")


async def collect_candidates(search, account: Account, db: Database) -> list[Vacancy]:
    unique: dict[str, Vacancy] = {}
    for query in account.queries:
        for vacancy in await search.search(query, params=account.search_params):
            unique.setdefault(vacancy.id, vacancy)
    return [v for v in unique.values() if not v.has_test and not db.hh_seen_any(v.id)]


async def apply_pass(
    *, db: Database, account: Account, search, browser, letters_dir: Path, tz: str,
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

    for vacancy in await collect_candidates(search, account, db):
        if not matches(account, vacancy):
            db.hh_record(account.name, vacancy.id, vacancy.name, vacancy.employer, vacancy.url, lang, "", "skipped")
            summary.skipped += 1
            continue
        if not can_apply(local_now, done_today + summary.applied, limit):
            summary.stopped = "daily limit or outside working hours"
            break
        letter = render_letter(template, vacancy)
        if not live:
            summary.lines.append(f"[dry run] {vacancy.name} ({vacancy.employer}) {vacancy.url}")
            summary.applied += 1
            continue
        result = await browser.apply(vacancy, account.resumes[lang], letter)
        db.hh_record(account.name, vacancy.id, vacancy.name, vacancy.employer, vacancy.url, lang, letter, result.value)
        if result is ApplyResult.CAPTCHA:
            summary.stopped = "captcha"
            notify(f"hh/{account.name}: captcha, pausing this account")
            break
        if result is ApplyResult.APPLIED:
            summary.applied += 1
            streak = 0
            if done_today + summary.applied < limit:
                await sleep(pause_seconds())
        elif result is ApplyResult.FAILED:
            summary.failed += 1
            streak += 1
            if streak >= 3:
                summary.stopped = "3 failures in a row"
                notify(f"hh/{account.name}: 3 failed applications, stopping, check data/screenshots")
                break
        else:
            summary.skipped += 1
    return summary
