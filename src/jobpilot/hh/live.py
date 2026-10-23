"""Chats in (near) real time, and replies typed on the site.

The watcher asks the chat app for the unread count every couple of minutes, one small request. When the
count changes, it reads the chats (the list plus the changed chats, the same work as the hourly pass);
without changes it still reads them once an hour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from ..db import Database
from .apply import CaptchaError
from .chats import SyncSummary, send_reply, sync_chats

log = logging.getLogger(__name__)

LIVE_KEY = "hh_chats_live"  # "0" on the settings page: chats are read hourly again
LIVE_HOURS = range(8, 24)   # local hours the watcher runs (the hourly pass used 8-23)


def live_on(db: Database) -> bool:
    return db.get(LIVE_KEY, "1") != "0"


def wait_before_sync(now: float, recent: deque, *, min_gap: float, per_hour: int) -> float:
    """Seconds to wait before the next read: at least `min_gap` after the last one, at most `per_hour` an hour."""
    while recent and recent[0] <= now - 3600:
        recent.popleft()
    wait = 0.0
    if recent:
        wait = max(wait, recent[-1] + min_gap - now)
    if len(recent) >= per_hour:
        wait = max(wait, recent[0] + 3600 - now)
    return wait


class ChatWatcher:
    def __init__(self, db: Database, client, account: str, *,
                 may_run: Callable[[], bool], on_summary: Callable[[SyncSummary], Awaitable[None]],
                 on_captcha: Callable[[], Awaitable[None]], poll: float = 120, min_gap: float = 60,
                 per_hour: int = 20, fallback: float = 3600, idle_check: float = 60, error_pause: float = 300,
                 clock=time.monotonic, sleep=asyncio.sleep, pause=asyncio.sleep) -> None:
        self.db, self.client, self.account = db, client, account
        self.may_run, self.on_summary, self.on_captcha = may_run, on_summary, on_captcha
        self.poll, self.min_gap, self.per_hour, self.fallback = poll, min_gap, per_hour, fallback
        self.idle_check, self.error_pause, self.clock = idle_check, error_pause, clock
        self.sleep = sleep
        self.pause = pause  # between two chats of one read
        self._recent: deque = deque()
        self._unread: int | None = None
        self._last_sync: float | None = None
        self.syncs = 0

    async def run(self) -> None:
        """Forever: watch while allowed, otherwise look again in a minute."""
        while True:
            await self.step()

    async def step(self) -> None:
        """One round: idle, or check the unread count and read the chats when something changed."""
        if not self.may_run():
            self._unread = None  # after a break, read the chats again first
            await self.sleep(self.idle_check)
            return
        try:
            unread = await self.client.unread()
            now = self.clock()
            stale = self._last_sync is None or now - self._last_sync >= self.fallback
            if unread != self._unread or stale:
                wait = wait_before_sync(now, self._recent, min_gap=self.min_gap, per_hour=self.per_hour)
                if wait > 0:
                    await self.sleep(wait)
                summary = await sync_chats(self.db, self.client, self.account, sleep=self.pause)
                self._unread, self._last_sync = unread, self.clock()
                self._recent.append(self._last_sync)
                self.syncs += 1
                await self.on_summary(summary)
            await self.sleep(self.poll)
        except CaptchaError:
            await self.on_captcha()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hh/%s chat watcher failed, next try in %d s", self.account, self.error_pause)
            await self.sleep(self.error_pause)


async def send_pending(db: Database, clients, *, may_send: Callable[[], bool],
                       notify: Callable[[str], Awaitable[None]],
                       on_captcha: Callable[[str], Awaitable[None]]) -> int:
    """Send the replies typed on the site, oldest first; returns how many went out.

    While hh is on a captcha hold the replies wait. A reply that fails is marked as an error with the reason
    and is not tried again until it is retried on the site, so nothing goes out twice.
    """
    sent = 0
    for row in db.outbox_pending():
        if not may_send():
            break
        try:
            await send_reply(db, clients.get(row["account"]), row["account"], row["chat_id"], row["text"])
        except CaptchaError:
            await on_captcha(row["account"])  # stays pending: goes out after the cooldown
            break
        except Exception as error:
            log.exception("hh/%s reply %d failed", row["account"], row["id"])
            db.outbox_mark(row["id"], "error", str(error)[:300])
            await notify(f"hh/{row['account']}: ответ в чат «{row['title']}» не ушёл: {error}")
            continue
        db.outbox_mark(row["id"], "sent")
        sent += 1
    return sent
