from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Config
from ..db import Database
from ..hh.accounts import load_accounts
from ..tg.notifier import Notifier
from .client import HirifyClient
from .worker import FEED_NAME, PAUSED_KEY, PROFILE_KEY, SOURCE, Settings, hirify_pass, load_letter

log = logging.getLogger(__name__)


def _exclude_words(cfg: Config) -> tuple[str, ...]:
    """The go account's exclude list (junior, intern, ...) also skips Hirify vacancies."""
    if not cfg.accounts_path.exists():
        return ()
    account = load_accounts(cfg.accounts_path).get("go")
    return account.exclude if account else ()


def schedule_hirify(scheduler: AsyncIOScheduler, cfg: Config, db: Database, notifier: Notifier) -> None:
    key = os.environ.get("HIRIFY_API_KEY", "").strip()
    if not key:
        log.info("no HIRIFY_API_KEY, Hirify is off")
        return
    client = HirifyClient(key)

    async def hirify_job() -> None:
        if db.get("paused") == "1" or db.get(PAUSED_KEY) == "1":
            return
        try:
            if not db.get(PROFILE_KEY):
                profiles = await client.profiles()
                if profiles:
                    db.set(PROFILE_KEY, str(profiles[0]["profile_id"]))
            settings = Settings.from_db(db, _exclude_words(cfg))
            first = db.conn.execute("SELECT 1 FROM feed_jobs WHERE source = ? LIMIT 1", (SOURCE,)).fetchone() is None
            summary = await hirify_pass(db, client, settings, load_letter(cfg.data_dir / "letters"), cfg.tz)
        except Exception:
            log.exception("hirify pass failed")
            await notifier.say("Hirify: проход упал, подробности в логе сервера")
            return
        log.info("hirify pass: applied=%d listed=%d archived=%d revealed=%d skipped=%d failed=%d stopped=%s",
                 summary.applied, len(summary.listed), len(summary.archived), summary.revealed, summary.skipped,
                 summary.failed, summary.stopped)
        db.feed_mark_sent([job.id for job in summary.archived])
        if summary.applied or summary.failed:
            await notifier.say(f"Hirify: откликов {summary.applied}, ошибок {summary.failed}")
        if first:  # the whole current list is no news: it goes to the site, not to Telegram
            db.feed_mark_sent([job.id for job in summary.listed])
            await notifier.say(f"Hirify подключён: {len(summary.listed) + len(summary.archived)} вакансий на сайте,"
                               " вкладка Hirify")
            return
        await notifier.feed_jobs_found(summary.listed, FEED_NAME)

    # Hourly through the working day, between the hh passes (:05 and :35).
    scheduler.add_job(hirify_job, "cron", hour="9-20", minute=15, id="hirify", replace_existing=True,
                      max_instances=1, coalesce=True)
