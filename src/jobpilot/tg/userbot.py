from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events, functions, utils

from ..config import Config
from ..db import Database
from ..filters import text_hash
from .notifier import Notifier

log = logging.getLogger(__name__)

REFRESH_SECONDS = 300


def make_client(cfg: Config) -> TelegramClient:
    return TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash)


async def resolve_source_ids(client: TelegramClient, cfg: Config) -> set[int]:
    """Ids of the chats to listen to: the named folder's chats plus explicit TG_SOURCES."""
    ids: set[int] = set()
    for source in cfg.sources:
        try:
            ids.add(await client.get_peer_id(source))
        except Exception:
            log.warning("Cannot resolve source %r (is the userbot account a member?)", source)
    if cfg.folder:
        result = await client(functions.messages.GetDialogFiltersRequest())
        for flt in getattr(result, "filters", result):
            title = getattr(flt, "title", None)
            title = getattr(title, "text", title)
            if title == cfg.folder:
                ids.update(utils.get_peer_id(peer) for peer in flt.include_peers)
                break
        else:
            log.warning("Telegram folder %r not found on the userbot account", cfg.folder)
    return ids


def post_url(event: events.NewMessage.Event) -> str:
    username = getattr(event.chat, "username", None)
    if username:
        return f"https://t.me/{username}/{event.message.id}"
    return f"https://t.me/c/{str(event.chat_id).removeprefix('-100')}/{event.message.id}"


async def start_listening(
    client: TelegramClient, cfg: Config, db: Database, notifier: Notifier
) -> asyncio.Task:
    """Listen to every incoming message and keep only the ones from the current source set.

    The set is re-read every REFRESH_SECONDS, so chats added to the folder (or joined by the
    userbot) start being watched without a restart. The returned task must be kept alive.
    """
    allowed = await resolve_source_ids(client, cfg)
    if not allowed:
        log.warning("No sources configured: set TG_FOLDER or TG_SOURCES")
    log.info("Listening to %d source(s)", len(allowed))

    async def refresh_forever() -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            try:
                fresh = await resolve_source_ids(client, cfg)
            except Exception:
                log.exception("Refreshing sources failed, keeping the previous list")
                continue
            if fresh != allowed:
                log.info("Sources changed: %d -> %d", len(allowed), len(fresh))
                allowed.clear()
                allowed.update(fresh)

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if event.chat_id not in allowed:
            return
        text = event.message.message or ""
        if not text or db.get("paused") == "1" or not db.filters().match(text):
            return
        source = getattr(event.chat, "title", None) or getattr(event.chat, "username", None) or "?"
        post = db.add_post(text_hash(text), source, post_url(event), text)
        if post is not None and db.get("mode") == "realtime":
            await notifier.send_post(post)

    return asyncio.create_task(refresh_forever())
