"""Reading the feeds politely: one at a time, never more often than the feed's own ttl or MIN_INTERVAL,
conditional GET (ETag / Last-Modified), Retry-After and a growing pause after errors."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from ..core.db import Database, FeedJob
from ..filters import Filters, is_lead
from . import sources as feed_sources
from .parse import MAX_BYTES, FeedError, FeedItem, job_hash, parse
from .sources import Source

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "JobPilot/0.1 (personal job alerts; RSS reader)"
MAX_BACKOFF = 24 * 60          # minutes
WARN_AFTER = 3                 # errors in a row before Telegram hears about it
GAP_SECONDS = (3, 10)          # pause between two feeds in one round


@dataclass
class PollResult:
    source: str
    status: str                # ok, not_modified, error, skipped
    items: int = 0
    new: list[FeedJob] = field(default_factory=list)
    stored: int = 0
    error: str = ""
    first: bool = False        # the feed was never read before


def feed_filters(db: Database) -> Filters:
    return Filters(include=feed_sources.words(db, feed_sources.INCLUDE),
                   exclude=feed_sources.words(db, feed_sources.EXCLUDE), require_marker=False, min_length=0)


def matches(filters: Filters, item: FeedItem) -> bool:
    """Words are looked for in the title and the categories only: in English descriptions "go" is
    mostly the verb, and "junior" turns up in senior jobs ("mentor juniors")."""
    return not is_lead(item.title) and filters.match(" \n".join([item.title, *item.categories]))


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def is_due(state: dict, now: datetime) -> bool:
    due = _parse_time(state.get("next_at"))
    return due is None or due <= now


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    return -(-int(value) // 60) if value.isdigit() else None   # seconds -> minutes, rounded up


class FeedPoller:
    def __init__(self, db: Database, client: httpx.AsyncClient | None = None, *,
                 user_agent: str = DEFAULT_USER_AGENT, sleep=asyncio.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._db = db
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, max_redirects=3)
        self._ua = user_agent
        self._sleep = sleep
        self._now = clock

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, source: Source, state: dict) -> tuple[int, bytes, httpx.Headers, httpx.Response]:
        headers = {"User-Agent": self._ua, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("modified"):
            headers["If-Modified-Since"] = state["modified"]
        async with self._client.stream("GET", source.url, headers=headers) as response:
            body = b""
            if response.status_code == 200:
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FeedError("лента больше 2 МБ")
            return response.status_code, body, response.headers, response

    async def poll(self, source: Source, *, store: bool = True) -> PollResult:
        """Read one feed now. `store=False` is the dry check: nothing is written, the state is untouched."""
        state = feed_sources.state(self._db, source.key) if store else {}
        now = self._now()
        result = PollResult(source.key, "ok", first=not state.get("ok_at"))
        wait = None
        try:
            code, body, headers, response = await self._get(source, state)
            if code == 304:
                result.status = "not_modified"
            elif code != 200:
                wait = _retry_after(response) if code in (429, 503) else None
                raise FeedError(f"HTTP {code}")
            else:
                feed = parse(body)
                result.items = len(feed.items)
                if feed.ttl:
                    state["ttl"] = feed.ttl
                state["etag"] = headers.get("ETag") or None
                state["modified"] = headers.get("Last-Modified") or None
                state["title"] = feed.title
                if store:
                    filters = feed_filters(self._db)
                    for item in feed.items:
                        job = self._db.add_feed_job(source.key, source.country, item,
                                                    job_hash(item.title, item.company), matches(filters, item))
                        if job is None:
                            continue
                        result.stored += 1
                        if job.matched:
                            result.new.append(job)
                else:
                    filters = feed_filters(self._db)
                    result.new = [FeedJob(0, source.key, source.country, i.title, i.company, i.location, i.url,
                                          i.summary, matches(filters, i)) for i in feed.items]
        except (httpx.HTTPError, FeedError) as e:
            result.status, result.error = "error", (str(e) or type(e).__name__)[:300]
        if not store:
            return result
        state["last_at"] = now.isoformat()
        if result.status == "error":
            state["errors"] = int(state.get("errors", 0)) + 1
            state["error"] = result.error
            pause = min(source.interval * 2 ** (state["errors"] - 1), MAX_BACKOFF)
            pause = max(pause, wait or 0)
            log.warning("feed %s: %s, next try in %d min", source.key, result.error, pause)
        else:
            state.update(errors=0, error="", ok_at=now.isoformat())
            if result.status == "ok":
                state.update(count=result.items, stored=result.stored)
            pause = max(source.interval, int(state.get("ttl") or 0))
        # A little jitter so the reads do not fall on the same second every time.
        state["next_at"] = (now + timedelta(minutes=pause, seconds=random.randint(0, 59))).isoformat()
        feed_sources.save_state(self._db, source.key, state)
        return result

    async def poll_due(self, on_result: Callable[[Source, PollResult, dict], Awaitable[None]] | None = None
                       ) -> list[PollResult]:
        """One round: every enabled feed whose time has come, one after another."""
        if feed_sources.paused(self._db):
            return []
        results = []
        now = self._now()
        due = [s for s in feed_sources.all_sources(self._db)
               if s.enabled and is_due(feed_sources.state(self._db, s.key), now)]
        for number, source in enumerate(due):
            if number:
                await self._sleep(random.uniform(*GAP_SECONDS))
            result = await self.poll(source)
            results.append(result)
            if on_result is not None:
                await on_result(source, result, feed_sources.state(self._db, source.key))
        return results
