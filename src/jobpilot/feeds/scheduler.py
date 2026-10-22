from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Config
from ..db import Database
from ..tg.notifier import Notifier
from . import sources as feed_sources
from .poll import WARN_AFTER, FeedPoller, PollResult
from .sources import Source

log = logging.getLogger(__name__)

TICK_MINUTES = 5   # how often the worker looks which feeds are due; each feed keeps its own interval


def schedule_feeds(scheduler: AsyncIOScheduler, cfg: Config, db: Database, notifier: Notifier) -> None:
    poller = FeedPoller(db, user_agent=cfg.feeds_user_agent)
    lock = asyncio.Lock()

    async def on_result(source: Source, result: PollResult, state: dict) -> None:
        if result.status == "error":
            log.warning("feed %s failed (%d in a row): %s", source.key, state.get("errors", 0), result.error)
            if state.get("errors") == WARN_AFTER:
                await notifier.say(f"Лента «{source.name}» не читается {WARN_AFTER} раза подряд: {result.error}. "
                                   "Буду пробовать реже; адрес и состояние на странице «Ленты».")
            return
        log.info("feed %s: %s, %d items, %d stored, %d matching", source.key, result.status, result.items,
                 result.stored, len(result.new))
        await notifier.feed_jobs_found(result.new, source.name)

    async def feeds_job() -> None:
        if lock.locked():
            return
        async with lock:
            try:
                await poller.poll_due(on_result)
            except Exception:
                log.exception("feeds round failed")

    # The first round a minute after the start; each feed's next read is stored, so a restart loop does not
    # make the sites read more often.
    scheduler.add_job(feeds_job, "interval", minutes=TICK_MINUTES, id="feeds", replace_existing=True,
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))


async def check(cfg: Config, db: Database, key: str | None = None) -> None:
    """`jobpilot feeds-check`: read the feeds once and print what they hold; writes nothing."""
    poller = FeedPoller(db, user_agent=cfg.feeds_user_agent)
    try:
        for source in feed_sources.all_sources(db):
            if key and source.key != key:
                continue
            result = await poller.poll(source, store=False)
            print(f"== {source.key} ({source.name}, {source.country}) {source.url}"
                  f"{'' if source.enabled else ' [выключена]'}")
            if result.status == "error":
                print("   ошибка:", result.error)
                continue
            print(f"   записей: {result.items}, подходят под слова: {sum(j.matched for j in result.new)}")
            for job in result.new:
                print(f"   {'+' if job.matched else ' '} {job.title} | {job.company} | {job.location} | {job.url}")
    finally:
        await poller.aclose()
