from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from ..config import Config
from ..db import Database
from .accounts import load_accounts
from .apply import HHBrowser, login_interactive
from .dump import dump_pages
from .followup import refresh_resumes, remind_pass
from .pipeline import apply_pass
from .search import WebSearch


def _setup(name: str):
    cfg = Config.from_env(need_telegram=False)
    accounts = load_accounts(cfg.accounts_path)
    if name not in accounts:
        raise SystemExit(f"Unknown account {name!r}; known: {', '.join(accounts)}")
    return cfg, accounts[name]


def profile_dir(cfg: Config, name: str) -> Path:
    return cfg.data_dir / "browser" / name


def login(name: str) -> None:
    cfg, account = _setup(name)
    asyncio.run(login_interactive(profile_dir(cfg, account.name), cfg.data_dir / "screenshots"))


def dump(name: str, vacancy_url: str) -> None:
    cfg, account = _setup(name)
    asyncio.run(dump_pages(profile_dir(cfg, account.name), cfg.data_dir / "dumps" / account.name, vacancy_url))


def search_only(name: str) -> None:
    """Print what the account's queries find, without scoring or applying."""
    cfg, account = _setup(name)

    async def go() -> None:
        async with HHBrowser(profile_dir(cfg, name), cfg.data_dir / "screenshots") as browser:
            search = WebSearch(browser)
            for query in account.queries:
                found = await search.search(query, params=account.search_params)
                print(f"{query!r}: {len(found)} vacancies")
                for vacancy in found[:5]:
                    print("  ", vacancy.name, "|", vacancy.employer, "|", vacancy.url)

    asyncio.run(go())


def run(name: str, live: bool, any_time: bool = False) -> None:
    cfg, account = _setup(name)
    db = Database(cfg.db_path)

    async def go() -> None:
        async with HHBrowser(profile_dir(cfg, name), cfg.data_dir / "screenshots") as browser:
            summary = await apply_pass(
                db=db, account=account, search=WebSearch(browser),
                browser=browser if live else None,
                letters_dir=cfg.data_dir / "letters", tz=cfg.tz, live=live,
                now=datetime.now(ZoneInfo(cfg.tz)).replace(hour=12) if any_time else None,
            )
        print("\n".join(summary.lines))
        print(f"applied={summary.applied} skipped={summary.skipped} failed={summary.failed} stopped={summary.stopped}")

    asyncio.run(go())


def remind(name: str, live: bool, after_days: int = 2) -> None:
    cfg, account = _setup(name)
    db = Database(cfg.db_path)

    async def go() -> None:
        async with HHBrowser(profile_dir(cfg, name), cfg.data_dir / "screenshots") as browser:
            sent = await remind_pass(db, browser, name, live, after_days=after_days)
        print(f"reminders={sent}")

    asyncio.run(go())


def export(name: str) -> None:
    """Save the account's login as data/browser/<name>/state.json, to copy to the server."""
    cfg, account = _setup(name)

    async def go() -> None:
        async with HHBrowser(profile_dir(cfg, name), cfg.data_dir / "screenshots") as browser:
            page = await browser.context.new_page()
            if not await browser.is_logged_in(page):
                raise SystemExit("Not signed in: run `jobpilot hh-login` first")
            print("Saved", await browser.export_state())

    asyncio.run(go())


def refresh(name: str) -> None:
    cfg, account = _setup(name)

    async def go() -> None:
        async with HHBrowser(profile_dir(cfg, name), cfg.data_dir / "screenshots") as browser:
            print("resumes raised:", await refresh_resumes(browser))

    asyncio.run(go())
