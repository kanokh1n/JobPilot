from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events, functions, utils

from ..config import Config
from ..db import Database
from ..filters import text_hash
from . import sources
from .notifier import Notifier

log = logging.getLogger(__name__)

REFRESH_SECONDS = 300


def make_client(cfg: Config) -> TelegramClient:
    return TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash)


async def resolve_sources(client: TelegramClient, cfg: Config, db: Database) -> tuple[set[int], list[str]]:
    """Ids of the chats to listen to (the folder's chats plus the explicit sources from .env and the site),
    and the sources that could not be resolved."""
    ids: set[int] = set()
    unresolved: list[str] = []
    for source in sources.all_sources(cfg.sources, db):
        try:
            ids.add(await client.get_peer_id(int(source) if source.lstrip("-").isdigit() else source))
        except Exception:
            log.warning("Cannot resolve source %r (is the userbot account a member?)", source)
            unresolved.append(source)
    folder = sources.folder(cfg.folder, db)
    if folder:
        result = await client(functions.messages.GetDialogFiltersRequest())
        for flt in getattr(result, "filters", result):
            title = getattr(flt, "title", None)
            title = getattr(title, "text", title)
            if title == folder:
                ids.update(utils.get_peer_id(peer) for peer in flt.include_peers)
                break
        else:
            log.warning("Telegram folder %r not found on the userbot account", folder)
            unresolved.append(f"папка «{folder}»")
    return ids, unresolved


async def titles(client: TelegramClient, ids: set[int]) -> list[str]:
    """Names of the chats; an id is shown when the chat is not among the dialogs."""
    names: dict[int, str] = {}
    async for dialog in client.iter_dialogs():
        if dialog.id in ids:
            names[dialog.id] = dialog.name or str(dialog.id)
    return sorted(names.values(), key=str.lower) + [str(i) for i in sorted(ids - names.keys())]


async def source_titles(client: TelegramClient, cfg: Config, db: Database) -> list[str]:
    ids, unresolved = await resolve_sources(client, cfg, db)
    return await titles(client, ids) + [f"не найден: {u}" for u in unresolved]


async def _publish(client: TelegramClient, db: Database, ids: set[int], unresolved: list[str]) -> None:
    """Tell the site what is being listened to."""
    try:
        sources.save_seen(db, await titles(client, ids), unresolved)
    except Exception:
        log.exception("Could not list source names for the site")


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
    userbot, or added on the site) start being watched without a restart. The returned task must be kept alive.
    """
    allowed, unresolved = await resolve_sources(client, cfg, db)
    await _publish(client, db, allowed, unresolved)
    if not allowed:
        log.warning("No sources configured: set TG_FOLDER or TG_SOURCES, or add them on the site")
    log.info("Listening to %d source(s)", len(allowed))

    async def refresh_forever() -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            try:
                fresh, missing = await resolve_sources(client, cfg, db)
            except Exception:
                log.exception("Refreshing sources failed, keeping the previous list")
                continue
            if fresh != allowed or missing != unresolved:
                log.info("Sources changed: %d -> %d", len(allowed), len(fresh))
                allowed.clear()
                allowed.update(fresh)
                unresolved[:] = missing
                await _publish(client, db, allowed, unresolved)

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if event.chat_id not in allowed:
            return
        text = event.message.message or ""
        if not text or sources.paused(db) or not db.filters().match(text):
            return
        source = getattr(event.chat, "title", None) or getattr(event.chat, "username", None) or "?"
        post = db.add_post(text_hash(text), source, post_url(event), text)
        if post is not None and db.get("mode") == "realtime":
            await notifier.send_post(post)

    return asyncio.create_task(refresh_forever())
