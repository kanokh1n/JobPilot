"""One open browser per hh account, shared by every job of that account.

A persistent Chromium profile can be opened by one browser at a time. Before v8 every job launched its own
browser on the profile and the jobs waited for each other; now the chat watcher keeps the account's browser
open and the apply, reminder, resume and reply jobs borrow it, each in its own tab. The browser closes when
the last job lets go of it, so outside the watcher's hours nothing stays in memory.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from .apply import HHBrowser

log = logging.getLogger(__name__)


class SharedBrowsers:
    def __init__(self, data_dir: Path, factory=HHBrowser) -> None:
        self._data_dir = data_dir
        self._factory = factory
        self._open: dict[str, HHBrowser] = {}
        self._users: dict[int, int] = defaultdict(int)  # id(browser) -> jobs using it
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def is_open(self, account: str) -> bool:
        return account in self._open

    @asynccontextmanager
    async def use(self, account: str):
        """The account's browser, opened if needed (or again, when it crashed)."""
        async with self._locks[account]:
            browser = self._open.get(account)
            if browser is not None and getattr(browser, "closed", False):
                log.warning("hh/%s browser was closed under us, opening it again", account)
                self._open.pop(account)
                browser = None
            if browser is None:
                browser = self._factory(self._data_dir / "browser" / account, self._data_dir / "screenshots")
                await browser.__aenter__()
                self._open[account] = browser
            self._users[id(browser)] += 1
        try:
            yield browser
        finally:
            async with self._locks[account]:
                self._users[id(browser)] -= 1
                if self._users[id(browser)] == 0:  # the last job of this browser (current or a crashed one)
                    self._users.pop(id(browser))
                    if self._open.get(account) is browser:
                        self._open.pop(account)
                    try:
                        await browser.__aexit__(None, None, None)
                    except Exception:
                        log.exception("hh/%s browser did not close cleanly", account)
