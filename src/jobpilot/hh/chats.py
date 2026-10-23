"""Employer chats of an hh account, copied into the shared database for the site, and replies sent from it.

Everything goes through the chat app's own API (chatik.hh.ru: chats, chat_data, send), called by HHClient
with the site cookies saved at sign-in. Reading posts nothing to hh, so chats stay unread there.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core import stages
from ..db import Database

log = logging.getLogger(__name__)

# Applicant state of the negotiation, as the chat app reports it, to our stage.
CHAT_STAGES = {
    "DISCARD": stages.REJECTED,
    "INVITATION": stages.INVITATION,
    "PHONE_INTERVIEW": stages.INVITATION,
    "INTERVIEW": stages.INVITATION,
    "ASSESSMENT": stages.INVITATION,
    "HIRED": stages.OFFER,
}


@dataclass
class ChatInfo:
    id: str
    vacancy_id: str | None
    title: str
    employer: str
    last_message_at: str | None
    last_text: str
    unread: int
    state: str | None              # applicant state of the negotiation: RESPONSE, DISCARD, INTERVIEW, ...


@dataclass
class Message:
    id: str
    direction: str                 # in (employer) or out (us)
    author: str
    text: str
    sent_at: str


@dataclass
class ChatPage:
    chats: list[ChatInfo]
    next_cursor: str | None
    me: str | None                 # our participant id, needed by chat_data


@dataclass
class SyncSummary:
    chats: int = 0
    updated: int = 0
    new_messages: int = 0
    incoming: list[str] = field(default_factory=list)  # "title (employer)" of chats with new employer messages
    stages_moved: int = 0


def _utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def _message_text(message: dict) -> str:
    text = (message.get("text") or "").strip()
    if text:
        return text
    kind = message.get("type") or ""
    state = (message.get("workflowTransition") or {}).get("applicantState")
    if state:
        return f"[{state.lower()}]"
    return f"[{kind.lower() or 'без текста'}]"


def parse_chat_list(block: dict) -> ChatPage:
    """`block` is {"chats": {"items", "nextFrom", ...}, "chatsDisplayInfo", "resources"}."""
    listing = block.get("chats") or {}
    display = block.get("chatsDisplayInfo") or {}
    topics = (block.get("resources") or {}).get("negotiation_topics") or {}
    chats, me = [], None
    for item in listing.get("items") or []:
        if item.get("type") != "NEGOTIATION":
            continue  # support and system chats
        chat_id = str(item["id"])
        resources = item.get("resources") or {}
        vacancy = (resources.get("VACANCY") or [None])[0]
        topic = topics.get(str((resources.get("NEGOTIATION_TOPIC") or [""])[0])) or {}
        last = item.get("lastMessage") or {}
        state = topic.get("currentApplicantState") or (last.get("workflowTransition") or {}).get("applicantState")
        info = display.get(chat_id) or {}
        me = me or item.get("currentParticipantId")
        chats.append(ChatInfo(
            id=chat_id,
            vacancy_id=str(vacancy) if vacancy else None,
            title=info.get("title") or "",
            employer=info.get("subtitle") or "",
            last_message_at=_utc(last.get("creationTime") or item.get("lastActivityTime")),
            last_text=_message_text(last) if last else "",
            unread=int(item.get("unreadCount") or 0),
            state=state,
        ))
    return ChatPage(chats, listing.get("nextFrom"), me)


def parse_messages(data: dict) -> list[Message]:
    """Messages of one chat from a chat_data answer, oldest first."""
    chat = data.get("chat") or {}
    me = str(chat.get("currentParticipantId") or "")
    result = []
    for message in (chat.get("messages") or {}).get("items") or []:
        if message.get("hidden"):
            continue
        result.append(Message(
            id=str(message["id"]),
            direction="out" if str(message.get("participantId") or "") == me else "in",
            author=(message.get("participantDisplay") or {}).get("name") or "",
            text=_message_text(message),
            sent_at=_utc(message.get("creationTime")) or "",
        ))
    return sorted(result, key=lambda m: (m.sent_at, m.id))


async def sync_chats(db: Database, client, account: str, max_pages: int = 3,
                     sleep=asyncio.sleep) -> SyncSummary:
    """Copy new chats and messages of the account into the database.

    Only chats whose last message changed since the previous pass are opened. Older pages of the list
    are read only while they still hold changed chats (on the first run: up to `max_pages` pages).
    """
    summary = SyncSummary()
    listing = parse_chat_list(await client.chat_list())
    chats = list(listing.chats)
    cursor, pages = listing.next_cursor, 1
    while cursor and pages < max_pages and listing.chats and any(
        db.chat_last_message_at(account, c.id) != c.last_message_at for c in listing.chats
    ):
        try:
            listing = parse_chat_list(await client.chat_list(cursor))
        except Exception as error:
            log.warning("hh/%s chats: next page not read: %s", account, error)
            break
        chats += listing.chats
        cursor, pages = listing.next_cursor, pages + 1

    for chat in chats:
        stored = db.chat_last_message_at(account, chat.id)
        chat_id = db.chat_upsert(account, chat.id, vacancy_id=chat.vacancy_id, title=chat.title,
                                 employer=chat.employer, last_message_at=chat.last_message_at,
                                 unread=chat.unread, last_text=chat.last_text, state=chat.state)
        summary.chats += 1
        row = db.chat(chat_id)
        stage = CHAT_STAGES.get(chat.state or "")
        if row["application_id"] and stage and db.suggest_stage(row["application_id"], stage,
                                                                f"чат hh: {chat.state}"):
            summary.stages_moved += 1
        if stored == chat.last_message_at and stored is not None:
            continue
        messages = parse_messages(await client.chat_data(chat.id))
        new, incoming = db.chat_add_messages(chat_id, [(m.id, m.direction, m.author, m.text, m.sent_at)
                                                       for m in messages])
        summary.updated += 1
        summary.new_messages += new
        if incoming and stored is not None:  # not on the very first copy of a chat
            summary.incoming.append(f"{chat.title} ({chat.employer})")
        await sleep(random.uniform(1.5, 4))
    return summary


async def send_reply(db: Database, client, account: str, chat_id: int, text: str) -> int:
    """Send `text` to the hh chat, then copy the chat's messages, so the reply shows up as an hh message on
    the site. Returns how many new messages were stored."""
    chat = db.chat(chat_id)
    await client.send_message(chat["external_id"], text)
    try:
        messages = parse_messages(await client.chat_data(chat["external_id"]))
    except Exception as error:
        log.warning("hh/%s reply sent, chat not re-read: %s", account, error)
        return 0
    new, _ = db.chat_add_messages(chat_id, [(m.id, m.direction, m.author, m.text, m.sent_at) for m in messages])
    if messages:
        last = messages[-1]
        db.chat_upsert(account, chat["external_id"], last_message_at=last.sent_at, last_text=last.text, unread=0)
    return new
