"""One Hirify pass: read the vacancies of the filter, apply where Hirify takes the application itself, and put
the rest into the vacancy list (feed_jobs) like a job-site feed, for the site and Telegram.

Applications are rows of `applications` with source "hirify"; the other vacancies are feed items of the source
"hirify" in the pseudo-country HIRIFY, so they share the site's Hirify tab with the applications.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.db import Database, FeedJob
from ..feeds.parse import FeedItem, job_hash
from ..filters import _has_word
from ..hh.limits import in_working_hours
from .client import SITE_URL, HirifyClient, HirifyError

log = logging.getLogger(__name__)

SOURCE = "hirify"
ACCOUNT = "hirify"
COUNTRY = "HIRIFY"
FEED_NAME = "Hirify"
DEFAULT_FILTER = "specializations=backend_dev&skills=go"

FILTER_KEY = "hirify_filter"
PROFILE_KEY = "hirify_profile_id"
DAILY_KEY = "hirify_daily_limit"
PER_PASS_KEY = "hirify_per_pass"
PAUSED_KEY = "hirify_paused"


@dataclass
class Settings:
    filter: str = DEFAULT_FILTER
    profile_id: int | None = None
    daily_limit: int = 100
    per_pass: int = 10
    reveals_per_pass: int = 10      # contacts revealed for the vacancies we do not apply to (100 a day at Hirify)
    exclude: tuple[str, ...] = ()   # words in the title that skip a vacancy (the go account's exclude list)
    pages: int = 10

    @classmethod
    def from_db(cls, db: Database, exclude: tuple[str, ...] = ()) -> "Settings":
        profile = db.get(PROFILE_KEY)
        return cls(filter=db.get(FILTER_KEY) or DEFAULT_FILTER,
                   profile_id=int(profile) if profile and profile.isdigit() else None,
                   daily_limit=int(db.get(DAILY_KEY) or 100), per_pass=int(db.get(PER_PASS_KEY) or 10),
                   exclude=exclude)


@dataclass
class Summary:
    applied: int = 0
    listed: list[FeedJob] = field(default_factory=list)  # new vacancies to apply to by hand
    revealed: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: str | None = None


def applied(db: Database, vacancy_id: str) -> bool:
    """We applied through Hirify, or the user marked the listed vacancy as applied by hand on the site."""
    return db.conn.execute(
        "SELECT 1 FROM applications WHERE (source = ? AND vacancy_id = ?) OR (source = 'feed' AND vacancy_id IN"
        " (SELECT CAST(id AS TEXT) FROM feed_jobs WHERE source = ? AND external_id = ?))",
        (SOURCE, vacancy_id, SOURCE, vacancy_id)).fetchone() is not None


def known(db: Database, vacancy_id: str) -> bool:
    return db.feed_job_known(SOURCE, vacancy_id) or applied(db, vacancy_id)


def excluded(settings: Settings, card: dict) -> str | None:
    for word in settings.exclude:
        if _has_word(card.get("title") or "", word):
            return word
    return None


def contact_link(contact: dict) -> str | None:
    """A revealed contact as something to click: Telegram, e-mail, a form or the company site."""
    kind, value = (contact.get("type") or "").lower(), (contact.get("value") or "").strip()
    if not value:
        return None
    if kind == "telegram":
        return "https://t.me/" + value.lstrip("@").removeprefix("https://t.me/")
    if kind == "email":
        return "mailto:" + value
    if value.startswith(("http://", "https://")):
        return value
    return None


def _item(card: dict, company: str = "", contacts: list[dict] | None = None) -> FeedItem:
    """A Hirify vacancy as a feed item. With revealed contacts the link leads to the first of them and the
    summary lists them all; without, it leads to the vacancy on Hirify."""
    formats = ", ".join(card.get("work_format") or [])
    remote = card.get("remote_type") or ""
    page = SITE_URL + card["slug"]
    links = [link for link in (contact_link(c) for c in contacts or []) if link]
    lines = [f"Откликнуться: {', '.join(links)}"] if links else []
    lines += [", ".join(card.get("skills") or []), f"Hirify: {page}"]
    return FeedItem(
        external_id=str(card["vacancy_id"]), title=card.get("title") or "", url=links[0] if links else page,
        company=company, location=" · ".join(x for x in (formats, remote) if x),
        summary="\n".join(lines), categories=list(card.get("grades") or []),
    )


async def hirify_pass(db: Database, client: HirifyClient, settings: Settings, letter: str, tz: str, *,
                      live: bool = True, sleep=asyncio.sleep, now: datetime | None = None) -> Summary:
    summary = Summary()
    local_now = now or datetime.now(ZoneInfo(tz))
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    done_today = db.app_applied_since(SOURCE, ACCOUNT, start_of_day)
    may_apply = live and settings.profile_id is not None and in_working_hours(local_now)
    if may_apply:
        quota = ((await client.me()).get("quota") or {}).get("apply") or {}
        if not quota.get("remaining"):
            may_apply, summary.stopped = False, "hirify quota"

    reveals = settings.reveals_per_pass if live else 0
    cards: list[dict] = []
    for page in range(1, settings.pages + 1):
        found, meta = await client.search(settings.filter, page=page)
        cards += found
        if page >= int(meta.get("last_page") or 1):
            break

    for card in cards:
        vacancy_id = str(card["vacancy_id"])
        direct = bool(card.get("can_apply_directly"))
        # A vacancy Hirify takes applications for stays a candidate until we applied: when the limits of a pass
        # stop us, it is only listed, and a later pass applies to it.
        if card.get("has_applied") or (applied(db, vacancy_id) if direct else known(db, vacancy_id)):
            continue
        if direct and db.conn.execute("SELECT 1 FROM feed_jobs WHERE source = ? AND external_id = ? AND matched = 0",
                                      (SOURCE, vacancy_id)).fetchone():
            continue  # skipped for an exclude word before
        word = excluded(settings, card)
        if word:
            db.add_feed_job(SOURCE, COUNTRY, _item(card), job_hash(card.get("title") or "", vacancy_id), False)
            summary.skipped += 1
            continue
        apply_now = (may_apply and card.get("can_apply_directly")
                     and done_today + summary.applied < settings.daily_limit and summary.applied < settings.per_pass)
        if not apply_now:
            if direct and db.feed_job_known(SOURCE, vacancy_id):
                continue  # listed already, waits for the next pass with room
            company, contacts = "", []
            if reveals > 0 and not card.get("can_apply_directly"):
                try:
                    found = await client.reveal(vacancy_id)
                    company, contacts = found.get("company") or "", found.get("contacts") or []
                    summary.revealed += 1
                    reveals -= 1
                except HirifyError as error:
                    log.warning("hirify reveal of %s: %s", vacancy_id, error)
                    if error.code in ("quota_exhausted", "rate_limited", "reasonable_rate_limit_exceeded"):
                        reveals = 0
            job = db.add_feed_job(SOURCE, COUNTRY, _item(card, company, contacts),
                                  job_hash(card.get("title") or "", vacancy_id), True)
            if job is not None:
                summary.listed.append(job)
            continue
        try:
            detail = await client.vacancy(vacancy_id)
            company = detail.get("company") or ""
            text = letter.replace("{vacancy}", card.get("title") or "").replace("{employer}", company)
            await client.apply(card["slug"], settings.profile_id, text)
        except HirifyError as error:
            if error.code in ("quota_exhausted", "rate_limited", "reasonable_rate_limit_exceeded"):
                summary.stopped = error.code
                may_apply = False
                continue
            if error.code == "conflict":  # applied already, on the site or from another client
                db.app_record(SOURCE, ACCOUNT, vacancy_id, card.get("title") or "", "", SITE_URL + card["slug"],
                              "en", "", "already")
                continue
            log.error("hirify apply to %s failed: %s", card.get("slug"), error)
            db.app_record(SOURCE, ACCOUNT, vacancy_id, card.get("title") or "", "", SITE_URL + card["slug"],
                          "en", "", "failed")
            summary.failed += 1
            continue
        db.app_record(SOURCE, ACCOUNT, vacancy_id, card.get("title") or "", company, SITE_URL + card["slug"],
                      "en", text, "applied")
        db.conn.execute("DELETE FROM feed_jobs WHERE source = ? AND external_id = ?", (SOURCE, vacancy_id))
        summary.applied += 1
        await sleep(random.uniform(20, 60))
    return summary


def load_letter(letters_dir: Path) -> str:
    """hirify_en.txt if present, else the go account's English letter."""
    for name in ("hirify_en.txt", "go_en.txt", "en.txt"):
        path = letters_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"no cover letter for Hirify in {letters_dir}")
