from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from ..db import Database

REMINDER = {
    "ru": "Здравствуйте! Хотел бы уточнить, актуальна ли вакансия «{name}» и есть ли новости по моему отклику. Буду рад обсудить детали.",
    "en": "Hello! I wanted to check whether the \"{name}\" position is still open and if there is any news on my application. Happy to discuss the details.",
}


def reminders_due(db: Database, account: str, now: datetime | None = None, after_days: int = 2):
    now = now or datetime.now(timezone.utc)
    return db.hh_due_reminders(account, now - timedelta(days=after_days))


def reminder_text(lang: str, vacancy_name: str) -> str:
    return REMINDER[lang].format(name=vacancy_name)


# States of a response nobody has answered yet: only these get a reminder.
WAITING_STATES = ("not-viewed", "viewed")


async def remind_pass(db: Database, client, account: str, live: bool, lang: str = "ru",
                      notify=print, sleep=asyncio.sleep, after_days: int = 2) -> int:
    """Nudge employers who have not reacted to an application older than `after_days`."""
    negotiations = await client.negotiations()
    db.hh_sync_states(account, {vacancy: state for vacancy, (state, _) in negotiations.items()})
    sent = 0
    for row in reminders_due(db, account, after_days=after_days):
        found = negotiations.get(row["vacancy_id"])
        if found is None:
            continue  # not among the newest responses
        state, chat = found
        if state not in WAITING_STATES:
            db.hh_set_status(account, row["vacancy_id"], f"answered:{state}")
            continue
        text = reminder_text(row["resume_lang"] or lang, row["name"])
        if not live:
            notify(f"[dry run] remind {row['name']} ({row['employer']}): {text}")
            sent += 1
            continue
        if not chat:
            continue  # hh opened no chat for this response
        try:
            await client.send_message(chat, text)
        except Exception as error:
            notify(f"hh/{account}: reminder for {row['url']} failed: {error}")
            continue
        db.hh_mark_reminded(account, row["vacancy_id"])
        sent += 1
        await sleep(random.uniform(30, 90))
    return sent
