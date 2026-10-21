from __future__ import annotations

from aiogram import Bot
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from ..db import Database, Post

MESSAGE_LIMIT = 3800
PREVIEW_CHARS = 700
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def verdict_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="👍 интересно", callback_data=f"v:{post_id}:1"),
            InlineKeyboardButton(text="👎 мимо", callback_data=f"v:{post_id}:0"),
        ]]
    )


def format_post(post: Post) -> str:
    text = post.text.strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS].rstrip() + "…"
    return f"{text}\n\n{post.source}\n{post.url}"


def digest_chunks(posts: list[Post]) -> list[str]:
    """Pack one-line entries into messages under Telegram's length limit."""
    chunks: list[str] = []
    current = ""
    for number, post in enumerate(posts, 1):
        first_line = post.text.strip().splitlines()[0][:110]
        entry = f"{number}. {first_line}\n{post.url}\n\n"
        if current and len(current) + len(entry) > MESSAGE_LIMIT:
            chunks.append(current.rstrip())
            current = ""
        current += entry
    if current:
        chunks.append(current.rstrip())
    return chunks


class Notifier:
    def __init__(self, bot: Bot, owner_id: int, db: Database) -> None:
        self._bot = bot
        self._owner_id = owner_id
        self._db = db

    async def say(self, text: str) -> None:
        await self._bot.send_message(self._owner_id, text)

    async def say_photo(self, path: Path, caption: str) -> None:
        await self._bot.send_photo(self._owner_id, FSInputFile(path), caption=caption[:1000])

    async def send_post(self, post: Post) -> None:
        await self._bot.send_message(
            self._owner_id,
            format_post(post),
            reply_markup=verdict_keyboard(post.id),
            link_preview_options=NO_PREVIEW,
        )
        self._db.mark_sent([post.id])

    async def send_digest(self) -> int:
        posts = self._db.unsent()
        if not posts:
            return 0
        await self._bot.send_message(self._owner_id, f"Дайджест вакансий: {len(posts)} шт.")
        for chunk in digest_chunks(posts):
            await self._bot.send_message(self._owner_id, chunk, link_preview_options=NO_PREVIEW)
        self._db.mark_sent([p.id for p in posts])
        return len(posts)
