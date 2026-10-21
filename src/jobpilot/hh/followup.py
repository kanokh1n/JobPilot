from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone

from ..db import Database
from .apply import HHBrowser
from .selectors import NEGOTIATIONS_URL, RAISE_RESUME, RESUMES_URL

REMINDER = {
    "ru": "Здравствуйте! Хотел бы уточнить, актуальна ли вакансия «{name}» и есть ли новости по моему отклику. Буду рад обсудить детали.",
    "en": "Hello! I wanted to check whether the \"{name}\" position is still open and if there is any news on my application. Happy to discuss the details.",
}


def reminders_due(db: Database, account: str, now: datetime | None = None, after_days: int = 2):
    now = now or datetime.now(timezone.utc)
    return db.hh_due_reminders(account, now - timedelta(days=after_days))


def reminder_text(lang: str, vacancy_name: str) -> str:
    return REMINDER[lang].format(name=vacancy_name)


ITEM = '[data-qa="negotiations-item"]'
VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")
# Tags of a response nobody has answered yet: only these get a reminder.
WAITING_STATES = ("not-viewed", "viewed")


async def scan_negotiations(browser: HHBrowser) -> dict[str, str]:
    """Read https://hh.ru/applicant/negotiations and return {vacancy_id: state} for every listed response.

    State is the item's tag: "not-viewed" and "viewed" mean no answer yet, anything else
    (discard, invitation, ...) means the employer answered, so no reminder.
    """
    page = await browser.context.new_page()
    try:
        await page.goto(NEGOTIATIONS_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_selector(ITEM, timeout=20_000)
        states: dict[str, str] = {}
        for item in await page.locator(ITEM).all():
            href = await item.locator('a[href*="/vacancy/"]').first.get_attribute("href") or ""
            match = VACANCY_ID_RE.search(href)
            if not match:
                continue
            tag = item.locator('[data-qa^="negotiations-tag"]').first
            qa = (await tag.get_attribute("data-qa") or "") if await tag.count() else ""
            states[match.group(1)] = qa.split()[-1].removeprefix("negotiations-item-") if qa else "unknown"
        return states
    finally:
        await page.close()


CHAT_FRAME = "iframe.chatik-integration-iframe"
CHAT_INPUT = 'textarea[data-qa="text-input"]'
CHAT_BUBBLE = '[data-qa="chat-bubble-text"]'


async def send_reminder(browser: HHBrowser, vacancy_id: str, text: str) -> None:
    """Open the response chat of the vacancy from the negotiations page and send `text`."""
    page = await browser.context.new_page()
    try:
        await page.goto(NEGOTIATIONS_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_selector(ITEM, timeout=20_000)
        item = page.locator(ITEM).filter(has=page.locator(f'a[href*="/vacancy/{vacancy_id}"]')).first
        if not await item.count():
            raise LookupError(f"response to vacancy {vacancy_id} is not on the negotiations page")
        await item.locator('[data-qa="open_chat"]').click()
        chat = page.frame_locator(CHAT_FRAME)
        box = chat.locator(CHAT_INPUT)
        await box.wait_for(timeout=20_000)
        await box.fill(text)
        await box.press("Enter")
        await page.wait_for_timeout(2000)
        last = await chat.locator(CHAT_BUBBLE).last.inner_text()
        if text[:20] not in last:
            raise RuntimeError("the reminder did not appear in the chat")
    except Exception:
        await page.screenshot(path=str(browser.screenshots_dir / f"{vacancy_id}_remind.png"))
        raise
    finally:
        await page.close()


async def remind_pass(db: Database, browser: HHBrowser, account: str, live: bool, lang: str = "ru",
                      notify=print, sleep=asyncio.sleep, after_days: int = 2) -> int:
    """Nudge employers who have not reacted to an application older than `after_days`."""
    states = await scan_negotiations(browser)
    db.hh_sync_states(account, states)
    sent = 0
    for row in reminders_due(db, account, after_days=after_days):
        state = states.get(row["vacancy_id"])
        if state is None:
            continue  # not on the first page of the list
        if state not in WAITING_STATES:
            db.hh_set_status(account, row["vacancy_id"], f"answered:{state}")
            continue
        text = reminder_text(row["resume_lang"] or lang, row["name"])
        if not live:
            notify(f"[dry run] remind {row['name']} ({row['employer']}): {text}")
            sent += 1
            continue
        try:
            await send_reminder(browser, row["vacancy_id"], text)
        except Exception as error:
            notify(f"hh/{account}: reminder for {row['url']} failed: {error}")
            continue
        db.hh_mark_reminded(account, row["vacancy_id"])
        sent += 1
        await sleep(random.uniform(30, 90))
    return sent


RESUME_HREF_RE = re.compile(r"^/resume/[0-9a-f]{20,}")


async def refresh_resumes(browser: HHBrowser) -> int:
    """Press "raise in search" on the page of every resume of the account; returns how many were raised.

    The button lives on the resume's own page (hh.ru/resume/<id>), not on the list. hh refuses while the
    4-hour cooldown runs (the button is then disabled or absent), which is fine.
    """
    page = await browser.context.new_page()
    try:
        await page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3000)
        hrefs = []
        for link in await page.locator('a[href^="/resume/"]').all():
            href = (await link.get_attribute("href") or "").split("?")[0]
            if RESUME_HREF_RE.match(href) and href not in hrefs:
                hrefs.append(href)
        if not hrefs:  # keep the page so the links can be checked offline
            shots = browser.screenshots_dir
            (shots / "resumes.html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(shots / "resumes.png"), full_page=True)
        raised = 0
        for href in hrefs:
            await page.goto("https://hh.ru" + href, wait_until="domcontentloaded", timeout=90_000)
            button = page.get_by_role("button", name=RAISE_RESUME)
            try:
                await button.first.wait_for(timeout=8_000)
            except Exception:
                continue  # no button: still cooling down
            if await button.first.is_enabled():
                await button.first.click()
                await page.wait_for_timeout(1500)
                raised += 1
        return raised
    finally:
        await page.close()
