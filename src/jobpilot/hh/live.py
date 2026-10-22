"""Chats in (near) real time, and replies typed on the site.

The watcher keeps https://hh.ru/chat open in the account's shared browser. The chat app on that page holds a
websocket to hh and gets a frame when something happens in a chat; such a frame makes the watcher read the
chats again (one reload of the page plus the changed chats, the same work as the hourly pass did). Without
frames it still reads them once an hour, so hh sees no more requests than before, only earlier ones.

What hh sends over that websocket is not verified yet: `is_news` is a guess (a frame that mentions a chat
and a message). The first frames of every session are written to dumps/<account>/ws_frames.txt to check it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from ..db import Database
from .apply import CaptchaError
from .browsers import SharedBrowsers
from .chats import SyncSummary, send_reply, sync_chats

log = logging.getLogger(__name__)

LIVE_KEY = "hh_chats_live"  # "0" on the settings page: no open browser, chats are read hourly again
LIVE_HOURS = range(8, 24)   # local hours the watcher keeps the browser open (the hourly pass used 8-23)


def live_on(db: Database) -> bool:
    return db.get(LIVE_KEY, "1") != "0"


def is_news(payload: str | bytes) -> bool:
    """Whether a websocket frame looks like a chat event rather than a ping or a handshake."""
    text = payload if isinstance(payload, str) else payload.decode("utf-8", "ignore")
    if len(text) < 40:
        return False
    low = text.lower()
    return "chat" in low and ("message" in low or "negotiation" in low)


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
    def __init__(self, db: Database, browsers: SharedBrowsers, account: str, *,
                 may_run: Callable[[], bool], on_summary: Callable[[SyncSummary], Awaitable[None]],
                 on_captcha: Callable[[], Awaitable[None]], frames_log: Path | None = None,
                 debounce: float = 8, min_gap: float = 60, per_hour: int = 20, fallback: float = 3600,
                 quiet: float = 20, idle_check: float = 60, error_pause: float = 300, clock=time.monotonic,
                 pause=asyncio.sleep) -> None:
        self.db, self.browsers, self.account = db, browsers, account
        self.may_run, self.on_summary, self.on_captcha = may_run, on_summary, on_captcha
        self.frames_log = frames_log
        self.debounce, self.min_gap, self.per_hour, self.fallback = debounce, min_gap, per_hour, fallback
        self.quiet, self.idle_check, self.error_pause, self.clock = quiet, idle_check, error_pause, clock
        self.pause = pause  # between two chats of one read
        self._changed = asyncio.Event()
        self._quiet_until = 0.0
        self._frames_logged = 0
        self.syncs = 0

    def _frame(self, payload) -> None:
        if self.frames_log is not None and self._frames_logged < 40:
            self._frames_logged += 1
            try:
                text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
                with self.frames_log.open("a", encoding="utf-8") as out:
                    out.write(f"{time.strftime('%H:%M:%S')} {text[:400]!r}\n")
            except Exception:
                pass
        if self.clock() >= self._quiet_until and is_news(payload):
            self._changed.set()

    def _attach(self, ws) -> None:
        log.info("hh/%s chat websocket: %s", self.account, ws.url.split("?")[0])
        ws.on("framereceived", self._frame)

    async def run(self) -> None:
        """Forever: watch while allowed, otherwise let the browser go and look again in a minute."""
        while True:
            if not self.may_run():
                await asyncio.sleep(self.idle_check)
                continue
            try:
                await self._session()
            except CaptchaError:
                await self.on_captcha()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hh/%s chat watcher failed, next try in %d s", self.account, self.error_pause)
                await asyncio.sleep(self.error_pause)

    async def _session(self) -> None:
        if self.frames_log is not None:
            self.frames_log.parent.mkdir(parents=True, exist_ok=True)
            self.frames_log.write_text("", encoding="utf-8")
            self._frames_logged = 0
        recent: deque = deque()
        async with self.browsers.use(self.account) as browser:
            page = await browser.context.new_page()
            page.on("websocket", self._attach)
            try:
                while self.may_run():
                    self._quiet_until = self.clock() + self.quiet  # frames of a fresh connection are no news
                    self._changed.clear()
                    summary = await sync_chats(self.db, browser, self.account, page=page, sleep=self.pause)
                    recent.append(self.clock())
                    self.syncs += 1
                    await self.on_summary(summary)
                    if not await self._wait_for_news(recent):
                        return
            finally:
                await page.close()

    async def _wait_for_news(self, recent: deque) -> bool:
        """Until a chat event or the hourly fallback; False when the watcher has to stop."""
        deadline = self.clock() + self.fallback
        while self.clock() < deadline:
            if not self.may_run():
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(self.idle_check, deadline - self.clock()))
            except asyncio.TimeoutError:
                continue
            await asyncio.sleep(max(self.debounce, wait_before_sync(
                self.clock(), recent, min_gap=self.min_gap, per_hour=self.per_hour)))
            return self.may_run()
        return self.may_run()


async def send_pending(db: Database, browsers: SharedBrowsers, *, may_send: Callable[[], bool],
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
            async with browsers.use(row["account"]) as browser:
                await send_reply(db, browser, row["account"], row["chat_id"], row["text"])
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
