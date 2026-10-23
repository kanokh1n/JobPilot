from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import Config
from ..db import Database
from ..tg.notifier import Notifier
from .accounts import load_accounts, paused, with_overrides
from .apply import CaptchaError, prune_screenshots
from .chats import SyncSummary, sync_chats
from .client import HHClients
from .followup import remind_pass
from .live import LIVE_HOURS, ChatWatcher, live_on, send_pending
from .pipeline import apply_pass, captcha_hold, record_captcha

log = logging.getLogger(__name__)

# Apply, reminder and resume passes of the two accounts never run at the same time (one IP).
# Chats and replies do not wait for them.
_pass_lock = asyncio.Lock()
_watchers: list[asyncio.Task] = []  # keep references to the watcher tasks while they run


def _on_hold(db: Database, cfg: Config, job: str, account: str | None = None) -> bool:
    """Skip every hh job while the paused flag or a captcha cooldown is on.

    With `account`, the job also stops while that account is paused on the site (applications and
    reminders; chats and resume raises keep running).
    """
    if db.get("paused") == "1" or (account and paused(db, account)):
        return True
    until = captcha_hold(db, datetime.now(ZoneInfo(cfg.tz)))
    if until:
        log.info("hh %s skipped: captcha cooldown until %s", job, until)
        return True
    return False


def _captcha_now(db: Database, cfg: Config) -> datetime | None:
    return captcha_hold(db, datetime.now(ZoneInfo(cfg.tz)))


def schedule_hh(scheduler: AsyncIOScheduler, cfg: Config, db: Database, notifier: Notifier) -> None:
    """Apply passes every hour of the working day, one reminder pass a day, chats live or hourly, per account."""
    if not cfg.accounts_path.exists():
        log.info("no %s, hh part is off", cfg.accounts_path)
        return
    clients = HHClients(cfg.data_dir)

    def signed_in(account: str) -> bool:
        """Without `jobpilot hh-auth` there is no token: the account's jobs wait quietly."""
        if clients.get(account).signed_in:
            return True
        log.warning("hh/%s is not signed in: run jobpilot hh-auth %s <phone>", account, account)
        return False

    def watching() -> bool:
        """Whether the chat watchers may poll the chats now."""
        return (live_on(db) and db.get("paused") != "1" and _captcha_now(db, cfg) is None
                and datetime.now(ZoneInfo(cfg.tz)).hour in LIVE_HOURS)

    async def on_captcha(account: str, where: str) -> None:
        strikes, until = record_captcha(db, datetime.now(ZoneInfo(cfg.tz)))
        await notifier.say(f"hh/{account}: капча {where}, hh на паузе до {until:%d.%m %H:%M}")

    async def report_chats(account: str, summary: SyncSummary) -> None:
        log.info("hh/%s chats: %d listed, %d updated, %d new messages", account, summary.chats,
                 summary.updated, summary.new_messages)
        if summary.incoming:
            await notifier.say(f"hh/{account}: новые сообщения от работодателей:\n"
                               + "\n".join(f"• {name}" for name in summary.incoming))

    async def outbox_job() -> None:
        # Replies are typed by hand, so the paused flag does not hold them; a captcha cooldown does.
        if not db.outbox_pending() or _captcha_now(db, cfg):
            return
        sent = await send_pending(
            db, clients, may_send=lambda: _captcha_now(db, cfg) is None, notify=notifier.say,
            on_captcha=lambda account: on_captcha(account, "при отправке ответа"),
        )
        if sent:
            log.info("hh replies sent: %d", sent)

    def prune_job() -> None:
        removed = prune_screenshots(cfg.data_dir / "screenshots")
        log.info("hh screenshots removed: %d", removed)

    scheduler.add_job(prune_job, "cron", hour=4, minute=0, id="hh-prune-screenshots", replace_existing=True)
    for number, account in enumerate(load_accounts(cfg.accounts_path).values()):
        async def apply_job(base=account) -> None:
            if _on_hold(db, cfg, f"apply/{base.name}", base.name) or not signed_in(base.name):
                return
            account = with_overrides(base, db)  # limits and words changed on the site apply from the next pass
            client = clients.get(account.name)
            async with _pass_lock:
                try:
                    client.last_captcha_shot = None
                    summary = await apply_pass(
                        db=db, account=account, search=client, client=client,
                        letters_dir=cfg.data_dir / "letters", tz=cfg.tz, live=True,
                        notify=lambda text: asyncio.get_running_loop().create_task(notifier.say(text)),
                    )
                    captcha_shot = client.last_captcha_shot
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

        async def remind_job(account=account) -> None:
            if _on_hold(db, cfg, f"remind/{account.name}", account.name) or not signed_in(account.name):
                return
            async with _pass_lock:
                try:
                    sent = await remind_pass(
                        db, clients.get(account.name), account.name, live=True,
                        notify=lambda text: asyncio.get_running_loop().create_task(notifier.say(text)),
                    )
                except CaptchaError:
                    await on_captcha(account.name, "при напоминаниях")
                    return
                except Exception:
                    log.exception("hh reminder pass failed for %s", account.name)
                    return
            log.info("hh/%s reminders sent: %d", account.name, sent)
            if sent:
                await notifier.say(f"hh/{account.name}: напоминаний отправлено {sent}")

        async def refresh_job(account=account) -> None:
            if _on_hold(db, cfg, f"refresh/{account.name}") or not signed_in(account.name):
                return
            async with _pass_lock:
                try:
                    raised = await clients.get(account.name).raise_resumes()
                    log.info("hh/%s resumes raised: %d", account.name, raised)
                except CaptchaError:
                    await on_captcha(account.name, "при поднятии резюме")
                except Exception:
                    log.exception("hh resume refresh failed for %s", account.name)

        async def chats_job(account=account) -> None:
            """The hourly read, used only while the live watcher is switched off on the site."""
            if live_on(db) or _on_hold(db, cfg, f"chats/{account.name}") or not signed_in(account.name):
                return
            try:
                summary = await sync_chats(db, clients.get(account.name), account.name)
            except CaptchaError:
                await on_captcha(account.name, "на странице чатов")
                return
            except Exception:
                log.exception("hh chats pass failed for %s", account.name)
                return
            await report_chats(account.name, summary)

        watcher = ChatWatcher(
            db, clients.get(account.name), account.name,
            may_run=lambda name=account.name: watching() and clients.get(name).signed_in,
            on_summary=lambda summary, name=account.name: report_chats(name, summary),
            on_captcha=lambda name=account.name: on_captcha(name, "на странице чатов"),
        )
        _watchers.append(asyncio.get_running_loop().create_task(watcher.run(), name=f"hh-chats-live-{account.name}"))

        # Passes only run inside working hours; the per-day limit is enforced inside apply_pass.
        scheduler.add_job(apply_job, "cron", hour="9-20", minute=5 + 30 * number,
                          id=f"hh-apply-{account.name}", replace_existing=True)
        scheduler.add_job(remind_job, "cron", hour=13, minute=10 + 20 * number,
                          id=f"hh-remind-{account.name}", replace_existing=True)
        # Without the live watcher, chats are read every hour of the day, between the apply passes.
        scheduler.add_job(chats_job, "cron", hour="8-23", minute=20 + 30 * number,
                          id=f"hh-chats-{account.name}", replace_existing=True)
        # hh lets a resume be raised once per 4 hours.
        scheduler.add_job(refresh_job, "cron", hour="9,13,17,21", minute=50 + 5 * number,
                          id=f"hh-refresh-{account.name}", replace_existing=True)
    scheduler.add_job(outbox_job, "interval", seconds=15, id="hh-outbox", replace_existing=True,
                      max_instances=1, coalesce=True)
