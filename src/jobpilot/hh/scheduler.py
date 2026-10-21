from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Config
from ..db import Database
from ..tg.notifier import Notifier
from .accounts import load_accounts
from .apply import HHBrowser
from .followup import refresh_resumes, remind_pass
from .pipeline import apply_pass, captcha_hold
from .search import WebSearch

log = logging.getLogger(__name__)

# Two accounts never drive a browser at the same time.
_browser_lock = asyncio.Lock()


def _on_hold(db: Database, cfg: Config, job: str) -> bool:
    """Skip every hh job while the paused flag or a captcha cooldown is on."""
    if db.get("paused") == "1":
        return True
    until = captcha_hold(db, datetime.now(ZoneInfo(cfg.tz)))
    if until:
        log.info("hh %s skipped: captcha cooldown until %s", job, until)
        return True
    return False


def schedule_hh(scheduler: AsyncIOScheduler, cfg: Config, db: Database, notifier: Notifier) -> None:
    """Apply passes every hour of the working day and one reminder pass a day, per account."""
    if not cfg.accounts_path.exists():
        log.info("no %s, hh part is off", cfg.accounts_path)
        return
    for number, account in enumerate(load_accounts(cfg.accounts_path).values()):
        profile = cfg.data_dir / "browser" / account.name
        shots = cfg.data_dir / "screenshots"

        async def apply_job(account=account, profile=profile, shots=shots) -> None:
            if _on_hold(db, cfg, f"apply/{account.name}"):
                return
            async with _browser_lock:
                try:
                    async with HHBrowser(profile, shots) as browser:
                        summary = await apply_pass(
                            db=db, account=account, search=WebSearch(browser), browser=browser,
                            letters_dir=cfg.data_dir / "letters", tz=cfg.tz, live=True,
                            notify=lambda text: asyncio.get_running_loop().create_task(notifier.say(text)),
                        )
                        captcha_shot = browser.last_captcha_shot
                except Exception:
                    log.exception("hh apply pass failed for %s", account.name)
                    await notifier.say(f"hh/{account.name}: проход упал, подробности в логе сервера")
                    return
            log.info("hh/%s pass: applied=%d skipped=%d failed=%d stopped=%s",
                     account.name, summary.applied, summary.skipped, summary.failed, summary.stopped)
            if summary.stopped == "captcha" and captcha_shot:
                try:
                    await notifier.say_photo(captcha_shot, f"hh/{account.name}: вот что показал hh")
                except Exception:
                    log.exception("could not send the captcha screenshot")
            if summary.applied or summary.failed:
                await notifier.say(f"hh/{account.name}: откликов {summary.applied}, ошибок {summary.failed}")

        async def remind_job(account=account, profile=profile, shots=shots) -> None:
            if _on_hold(db, cfg, f"remind/{account.name}"):
                return
            async with _browser_lock:
                try:
                    async with HHBrowser(profile, shots) as browser:
                        sent = await remind_pass(
                            db, browser, account.name, live=True,
                            notify=lambda text: asyncio.get_running_loop().create_task(notifier.say(text)),
                        )
                except Exception:
                    log.exception("hh reminder pass failed for %s", account.name)
                    return
            log.info("hh/%s reminders sent: %d", account.name, sent)
            if sent:
                await notifier.say(f"hh/{account.name}: напоминаний отправлено {sent}")

        async def refresh_job(account=account, profile=profile, shots=shots) -> None:
            if _on_hold(db, cfg, f"refresh/{account.name}"):
                return
            async with _browser_lock:
                try:
                    async with HHBrowser(profile, shots) as browser:
                        await refresh_resumes(browser)
                except Exception:
                    log.exception("hh resume refresh failed for %s", account.name)

        # Passes only run inside working hours; the per-day limit is enforced inside apply_pass.
        scheduler.add_job(apply_job, "cron", hour="9-20", minute=5 + 30 * number,
                          id=f"hh-apply-{account.name}", replace_existing=True)
        scheduler.add_job(remind_job, "cron", hour=13, minute=10 + 20 * number,
                          id=f"hh-remind-{account.name}", replace_existing=True)
        # hh lets a resume be raised once per 4 hours.
        scheduler.add_job(refresh_job, "cron", hour="9,13,17,21", minute=50 + 5 * number,
                          id=f"hh-refresh-{account.name}", replace_existing=True)
