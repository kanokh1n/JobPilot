from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from hh_applicant_tool.tool import HHApplicantTool

from ..config import Config
from ..db import Database
from .accounts import load_accounts
from .chats import sync_chats
from .client import HHClient
from .followup import remind_pass
from .pipeline import apply_pass, reject_reason


def _setup(name: str):
    cfg = Config.from_env(need_telegram=False)
    accounts = load_accounts(cfg.accounts_path, Database(cfg.db_path))  # with the site's changes
    if name not in accounts:
        raise SystemExit(f"Unknown account {name!r}; known: {', '.join(accounts)}")
    return cfg, accounts[name]


def _client(cfg: Config, name: str) -> HHClient:
    client = HHClient(cfg.data_dir / "hh", name, cfg.data_dir / "screenshots")
    if not client.signed_in:
        raise SystemExit(f"hh/{name} is not signed in: run `jobpilot hh-auth {name} <phone>` first")
    return client


def auth(name: str, login: str, password: str | None = None) -> None:
    """Sign in once with an SMS (or e-mail) code, or a password; hh-applicant-tool asks for the code and a
    captcha, if hh shows one. The token and cookies go to data/hh/<name>/."""
    cfg, account = _setup(name)
    tool = HHApplicantTool(config_dir=cfg.data_dir / "hh", profile_id=account.name)
    code = tool.run(["authorize", login, *([password] if password else [])])
    if code:
        raise SystemExit(code)
    print("Signed in as", tool.api_client.get("/me").get("first_name", "?"),
          "- wait 10-15 minutes before the first applications, hh often shows a captcha right after a sign-in")


def whoami(name: str) -> None:
    cfg, _ = _setup(name)
    client = _client(cfg, name)

    async def go() -> None:
        me = await client.me()
        resumes = await client.get("/resumes/mine")
        print(me.get("first_name"), me.get("last_name"), "| id", me.get("id"))
        for resume in resumes.get("items") or []:
            print("  resume:", resume.get("title"), "|", (resume.get("status") or {}).get("id"))

    asyncio.run(go())


def chats(name: str, pages: int = 3) -> None:
    """Copy the account's hh chats into the database once and say what changed."""
    cfg, _ = _setup(name)
    db = Database(cfg.db_path)
    s = asyncio.run(sync_chats(db, _client(cfg, name), name, max_pages=pages))
    print(f"chats listed={s.chats} updated={s.updated} new messages={s.new_messages} stages moved={s.stages_moved}")
    for title in s.incoming:
        print("  new from employer:", title)


def search_only(name: str) -> None:
    """Print what the account's queries find, without scoring or applying."""
    cfg, account = _setup(name)
    client = _client(cfg, name)

    async def go() -> None:
        for query in account.queries:
            found = await client.search(query, params=account.search_params, period_days=account.search_period)
            print(f"{query!r}: {len(found)} vacancies")
            for vacancy in found[:5]:
                print("  ", vacancy.name, "|", vacancy.employer, "|", vacancy.url)

    asyncio.run(go())


def run(name: str, live: bool, any_time: bool = False) -> None:
    cfg, account = _setup(name)
    db = Database(cfg.db_path)
    client = _client(cfg, name)
    summary = asyncio.run(apply_pass(
        db=db, account=account, search=client, client=client,
        letters_dir=cfg.data_dir / "letters", tz=cfg.tz, live=live,
        now=datetime.now(ZoneInfo(cfg.tz)).replace(hour=12) if any_time else None,
    ))
    print("\n".join(summary.lines))
    print(f"applied={summary.applied} skipped={summary.skipped} failed={summary.failed} stopped={summary.stopped}")


def remind(name: str, live: bool, after_days: int = 2) -> None:
    cfg, _ = _setup(name)
    db = Database(cfg.db_path)
    sent = asyncio.run(remind_pass(db, _client(cfg, name), name, live, after_days=after_days))
    print(f"reminders={sent}")


def refresh(name: str) -> None:
    cfg, _ = _setup(name)
    print("resumes raised:", asyncio.run(_client(cfg, name).raise_resumes()))


def explain(name: str) -> None:
    """Search like a real pass (ignoring what the database has seen) and say why each vacancy is taken or not."""
    cfg, account = _setup(name)
    client = _client(cfg, name)

    async def go() -> dict:
        unique = {}
        for query in account.queries:
            found = await client.search(query, params=account.search_params, period_days=account.search_period)
            print(f"query {query!r}: {len(found)} found")
            for vacancy in found:
                unique.setdefault(vacancy.id, vacancy)
        return unique

    unique = asyncio.run(go())
    taken, reasons = [], {}
    for vacancy in unique.values():
        reason = "has a test" if vacancy.has_test else reject_reason(account, vacancy)
        if reason is None:
            taken.append(vacancy)
        else:
            reasons.setdefault(reason, []).append(vacancy)
    print(f"\nunique vacancies: {len(unique)}, taken: {len(taken)}")
    for vacancy in taken:
        print("  TAKE", vacancy.name, "|", vacancy.employer)
    for reason, items in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"\nskipped, {reason}: {len(items)}")
        for vacancy in items[:8]:
            print("  ", vacancy.name, "|", vacancy.employer)
