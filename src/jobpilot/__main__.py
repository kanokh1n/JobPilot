from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Config
from .db import Database
from .hh.scheduler import schedule_hh
from .tg.bot import build_dispatcher, create_bot
from .tg.notifier import Notifier
from .tg.userbot import make_client, source_titles, start_listening


def login() -> None:
    """One-off interactive sign-in of the userbot account (asks for phone and code)."""
    cfg = Config.from_env(need_bot=False)
    client = make_client(cfg)
    client.start()
    print("Session saved to", cfg.session_path)
    client.disconnect()


async def run() -> None:
    cfg = Config.from_env()
    db = Database(cfg.db_path)
    bot = create_bot(cfg.bot_token)
    notifier = Notifier(bot, cfg.owner_id, db)

    scheduler = AsyncIOScheduler(timezone=cfg.tz)

    def schedule_digest(hhmm: str) -> None:
        hour, minute = hhmm.split(":")
        scheduler.add_job(
            notifier.send_digest, "cron", hour=int(hour), minute=int(minute),
            id="digest", replace_existing=True,
        )

    schedule_digest(db.get("digest_time"))
    scheduler.start()

    client = make_client(cfg)
    dp = build_dispatcher(db, notifier, cfg.owner_id, schedule_digest, lambda: source_titles(client, cfg))
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Userbot is not signed in. Run: jobpilot login")
    refresh_task = await start_listening(client, cfg, db, notifier)  # keep a reference while running
    schedule_hh(scheduler, cfg, db, notifier)

    await asyncio.gather(dp.start_polling(bot), client.run_until_disconnected())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "login":
        login()
    elif command == "run":
        asyncio.run(run())
    elif command == "web":
        from .web.app import main as web

        web()
    elif command == "interviews-import":
        from .core.db import Database
        from .core.interviews import import_from_notes

        cfg = Config.from_env(need_telegram=False)
        dry = "--dry-run" in sys.argv[2:]
        for app_id, found in import_from_notes(Database(cfg.db_path), cfg.tz, dry_run=dry):
            print(app_id, found.starts_at.strftime("%d.%m.%Y %H:%M"), "" if found.has_time else "(без времени)",
                  found.link)
        if dry:
            print("--dry-run: nothing written")
    elif command == "db-info":
        from .core.info import main as db_info

        db_info()
    elif command in ("hh-login", "hh-search", "hh-run", "hh-dump", "hh-remind", "hh-export", "hh-refresh", "hh-explain", "hh-chats-dump", "hh-chats"):
        from .hh import cli

        if len(sys.argv) < 3:
            raise SystemExit(f"Usage: jobpilot {command} <account>" + (" [--live] [--any-time]" if command == "hh-run" else " [--live] [--days N]" if command == "hh-remind" else ""))
        account = sys.argv[2]
        if command == "hh-login":
            cli.login(account)
        elif command == "hh-dump":
            if len(sys.argv) < 4:
                raise SystemExit("Usage: jobpilot hh-dump <account> <vacancy-url>")
            cli.dump(account, sys.argv[3])
        elif command == "hh-chats":
            pages = int(sys.argv[sys.argv.index("--pages") + 1]) if "--pages" in sys.argv else 3
            cli.chats(account, pages)
        elif command == "hh-chats-dump":
            cli.chats_dump(account)
        elif command == "hh-explain":
            cli.explain(account)
        elif command == "hh-refresh":
            cli.refresh(account)
        elif command == "hh-export":
            cli.export(account)
        elif command == "hh-remind":
            days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 2
            cli.remind(account, live="--live" in sys.argv[3:], after_days=days)
        elif command == "hh-search":
            cli.search_only(account)
        else:
            cli.run(account, live="--live" in sys.argv[3:], any_time="--any-time" in sys.argv[3:])
    else:
        raise SystemExit("Usage: jobpilot [run|login|web|db-info|interviews-import|hh-login|hh-search|hh-run|hh-remind|hh-export|hh-refresh|hh-explain|hh-dump|hh-chats|hh-chats-dump] [account]")


if __name__ == "__main__":
    main()
