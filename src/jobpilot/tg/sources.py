"""Which Telegram chats the userbot listens to: .env gives the start, the site can add more.

Kept free of Telethon so the site can use it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

EXTRA = "tg_sources"        # JSON list added on the site, on top of TG_SOURCES from .env
FOLDER = "tg_folder"        # folder title set on the site; missing = TG_FOLDER from .env, "" = no folder
SEEN = "tg_sources_seen"    # what the userbot resolved last time: {"at", "names", "unresolved"}
PAUSED = "tg_paused"        # Telegram only; the global "paused" stops everything
DIGEST_REQUEST = "tg_digest_request"  # "1" = the site asked for the digest now; the bot sends it and resets

_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})/?(?:\d+)?$")


def normalize(raw: str) -> str | None:
    """@name, t.me/name or a numeric id; None for anything the userbot could not resolve anyway.

    Invite links (t.me/+..., joinchat) are refused: the userbot has to join such a chat first,
    after which it is easier to put it into the folder.
    """
    value = raw.strip()
    if not value:
        return None
    if re.fullmatch(r"-?\d{5,}", value):
        return value
    match = _LINK.match(value)
    if match:
        value = match.group(1)
    value = value.removeprefix("@")
    if value.lower() in ("joinchat", "addlist") or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return None
    return "@" + value


def extra(db) -> list[str]:
    try:
        value = json.loads(db.get(EXTRA) or "[]")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def all_sources(env_sources: tuple[str, ...], db) -> list[str]:
    return list(dict.fromkeys([*env_sources, *extra(db)]))


def folder(env_folder: str | None, db) -> str | None:
    value = db.get(FOLDER)
    if value is None:
        return env_folder
    return value or None


def save_seen(db, names: list[str], unresolved: list[str]) -> None:
    db.set(SEEN, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "names": names,
                             "unresolved": unresolved}, ensure_ascii=False))


def seen(db) -> dict:
    try:
        value = json.loads(db.get(SEEN) or "{}")
    except ValueError:
        value = {}
    return {"at": value.get("at"), "names": value.get("names", []), "unresolved": value.get("unresolved", [])}


def paused(db) -> bool:
    return db.get("paused") == "1" or db.get(PAUSED) == "1"
