from __future__ import annotations

import re
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..filters import split_keywords
from ..hh.pipeline import captcha_hold, clear_captcha
from .notifier import Notifier

HELP = (
    "/filters: текущие фильтры\n"
    "/include php, go: слова, из которых нужно хотя бы одно\n"
    "/exclude junior, стажёр: слова, которые отбрасывают вакансию\n"
    "/mode realtime|digest: сразу или пачкой\n"
    "/digest_time 09:00: время дайджеста\n"
    "/digest: прислать накопленное сейчас\n"
    "/resume ru|en: какое резюме использовать для откликов на hh\n"
    "/sources: какие чаты и каналы слушает юзербот\n"
    "/pause и /unpause: остановить и возобновить\n"
    "/hh_resume: снять паузу hh после капчи раньше срока\n"
    "/status: сколько вакансий накоплено"
)


def build_dispatcher(
    db: Database,
    notifier: Notifier,
    owner_id: int,
    reschedule: Callable[[str], None],
    list_sources: Callable[[], Awaitable[list[str]]] | None = None,
) -> Dispatcher:
    router = Router()
    router.message.filter(F.from_user.id == owner_id)
    router.callback_query.filter(F.from_user.id == owner_id)

    @router.message(Command("start", "help"))
    async def help_(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("filters"))
    async def filters(message: Message) -> None:
        f = db.filters()
        await message.answer(
            f"Режим: {db.get('mode')}, дайджест в {db.get('digest_time')}\n"
            f"Нужны (любое из): {', '.join(f.include) or 'любые'}\n"
            f"Исключить: {', '.join(f.exclude) or 'ничего'}\n"
            f"Пауза: {'да' if db.get('paused') == '1' else 'нет'}"
        )

    @router.message(Command("include"))
    async def include(message: Message, command: CommandObject) -> None:
        db.set_keywords("include", split_keywords(command.args or ""))
        await message.answer("Готово. /filters покажет итог.")

    @router.message(Command("exclude"))
    async def exclude(message: Message, command: CommandObject) -> None:
        db.set_keywords("exclude", split_keywords(command.args or ""))
        await message.answer("Готово. /filters покажет итог.")

    @router.message(Command("mode"))
    async def mode(message: Message, command: CommandObject) -> None:
        value = (command.args or "").strip().lower()
        if value not in ("realtime", "digest"):
            await message.answer("Напишите /mode realtime или /mode digest")
            return
        db.set("mode", value)
        await message.answer(f"Режим: {value}")

    @router.message(Command("digest_time"))
    async def digest_time(message: Message, command: CommandObject) -> None:
        value = (command.args or "").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            await message.answer("Формат: /digest_time 09:00")
            return
        db.set("digest_time", value)
        reschedule(value)
        await message.answer(f"Дайджест будет приходить в {value}")

    @router.message(Command("digest"))
    async def digest_now(message: Message) -> None:
        if await notifier.send_digest() == 0:
            await message.answer("Новых вакансий нет.")

    @router.message(Command("resume"))
    async def resume(message: Message, command: CommandObject) -> None:
        value = (command.args or "").strip().lower()
        if value not in ("ru", "en"):
            await message.answer("Напишите /resume ru или /resume en")
            return
        db.set("resume_lang", value)
        await message.answer(f"Для откликов теперь резюме: {value}")

    @router.message(Command("sources"))
    async def sources(message: Message) -> None:
        if list_sources is None:
            await message.answer("Список источников недоступен.")
            return
        names = await list_sources()
        lines = [f"Слушаю источников: {len(names)}"] + [f"- {name}" for name in names]
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3800:  # Telegram limits a message to 4096 characters
                await message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        await message.answer(chunk)

    @router.message(Command("pause"))
    async def pause(message: Message) -> None:
        db.set("paused", "1")
        await message.answer("Пауза включена.")

    @router.message(Command("unpause"))
    async def unpause(message: Message) -> None:
        db.set("paused", "0")
        await message.answer("Снова работаю.")

    @router.message(Command("hh_resume"))
    async def hh_resume(message: Message) -> None:
        if not captcha_hold(db, datetime.now(timezone.utc)):
            await message.answer("hh не на паузе из-за капчи.")
            return
        clear_captcha(db)
        await message.answer("Пауза hh снята, следующий проход по расписанию. Если капча повторится, пауза снова включится.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        s = db.stats()
        feeds = db.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sent = 0 AND matched = 1), 0) AS pending FROM feed_jobs"
        ).fetchone()
        await message.answer(f"Телеграм: найдено {s['total']}, ждут отправки {s['pending']}\n"
                             f"Сайты вакансий: найдено {feeds['n']}, ждут отправки {feeds['pending']}")

    @router.callback_query(F.data.startswith("v:"))
    async def verdict(query: CallbackQuery) -> None:
        _, post_id, value = query.data.split(":")
        db.set_verdict(int(post_id), int(value))
        await query.answer("Запомнил")

    @router.callback_query(F.data.startswith("fv:"))
    async def feed_verdict(query: CallbackQuery) -> None:
        _, job_id, value = query.data.split(":")
        db.feed_set_verdict(int(job_id), int(value))
        await query.answer("Запомнил")

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def create_bot(token: str) -> Bot:
    return Bot(token)
