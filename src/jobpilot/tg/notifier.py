from __future__ import annotations

from aiogram import Bot
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from ..db import Database, FeedJob, Post
from ..feeds.sources import country_name

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


FEED_ONE_BY_ONE = 5   # more new feed jobs than this in one read go as one list (a first read, a burst)


def feed_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="👍 интересно", callback_data=f"fv:{job_id}:1"),
            InlineKeyboardButton(text="👎 мимо", callback_data=f"fv:{job_id}:0"),
        ]]
    )


def _feed_head(job: FeedJob) -> str:
    return " · ".join(p for p in (job.title, job.company, job.location) if p)


def format_feed_job(job: FeedJob, source_name: str) -> str:
    text = job.summary.strip()
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS].rstrip() + "…"
    parts = [_feed_head(job), text, f"{source_name} ({country_name(job.country)})\n{job.url}"]
    return "\n\n".join(p for p in parts if p)


def feed_chunks(jobs: list[FeedJob], start: int = 1) -> list[str]:
    chunks: list[str] = []
    current = ""
    for number, job in enumerate(jobs, start):
        entry = f"{number}. {_feed_head(job)[:150]}\n{job.url}\n\n"
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

    async def send_feed_job(self, job: FeedJob, source_name: str) -> None:
        await self._bot.send_message(
            self._owner_id, format_feed_job(job, source_name), reply_markup=feed_keyboard(job.id),
            link_preview_options=NO_PREVIEW,
        )
        self._db.feed_mark_sent([job.id])

    async def feed_jobs_found(self, jobs: list[FeedJob], source_name: str) -> None:
        """New matching jobs of one feed read: by one in realtime mode, otherwise they wait for the digest."""
        if not jobs or self._db.get("mode") != "realtime":
            return
        if len(jobs) <= FEED_ONE_BY_ONE:
            for job in jobs:
                await self.send_feed_job(job, source_name)
            return
        await self._bot.send_message(self._owner_id, f"{source_name}: новых вакансий {len(jobs)}")
        for chunk in feed_chunks(jobs):
            await self._bot.send_message(self._owner_id, chunk, link_preview_options=NO_PREVIEW)
        self._db.feed_mark_sent([j.id for j in jobs])

    async def send_digest(self) -> int:
        posts = self._db.unsent()
        jobs = self._db.feed_unsent()
        if not posts and not jobs:
            return 0
        await self._bot.send_message(self._owner_id, f"Дайджест вакансий: {len(posts) + len(jobs)} шт.")
        for chunk in digest_chunks(posts):
            await self._bot.send_message(self._owner_id, chunk, link_preview_options=NO_PREVIEW)
        self._db.mark_sent([p.id for p in posts])
        by_country: dict[str, list[FeedJob]] = {}
        for job in jobs:
            by_country.setdefault(job.country, []).append(job)
        for country, group in by_country.items():
            await self._bot.send_message(self._owner_id, f"С сайтов вакансий, {country_name(country)}: {len(group)}")
            for chunk in feed_chunks(group):
                await self._bot.send_message(self._owner_id, chunk, link_preview_options=NO_PREVIEW)
            self._db.feed_mark_sent([j.id for j in group])
        return len(posts) + len(jobs)
