"""Employer chats of an hh account, copied into the shared database for the site, and replies sent from it.

The chat list comes from the page https://hh.ru/chat (its first 20 chats are in a JSON <template>), the
messages from the chat app's own API (chatik.hh.ru/chatik/api/chat_data), fetched from inside that page
the way the app does it. Reading posts nothing to hh, so chats stay unread there.
Checked against answers saved with `jobpilot hh-chats-dump` on 2026-10-22, except the next-page call
(CHATS_PAGE_URL), which is a guess: when it fails, only the newest 20 chats are read.

A reply is typed into the chat page like a person would (send_reply); the input is the one the reminders
already use in the response iframe, assumed to be the same on hh.ru/chat/<id> (unverified).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core import stages
from ..db import Database
from .apply import CaptchaError, HHBrowser

log = logging.getLogger(__name__)

CHAT_LIST_URL = "https://hh.ru/chat"
API = "https://chatik.hh.ru/chatik/api"
CHAT_DATA_URL = API + "/chat_data?chatId={chat}&applicantId={me}&reason=connect&do_not_track_session_events=true"
CHATS_PAGE_URL = API + "/chats?from={cursor}&filterUnread=false&filterHasTextMessage=false" \
                       "&do_not_track_session_events=true"
CHAT_URL = "https://hh.ru/chat/{chat}"
CHAT_INPUT = 'textarea[data-qa="text-input"]'
CHAT_BUBBLE = '[data-qa="chat-bubble-text"]'
ME_KEY = "hh_chat_me:{account}"  # our participant id in the chat app, kept for single-chat reads
INITIAL_STATE_RE = re.compile(r'<template[^>]*class="Chatik-InitialState"[^>]*>(.*?)</template>', re.S)

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


def initial_state(page_html: str) -> dict:
    """The JSON the chat page ships with; raises when hh changed the page."""
    match = INITIAL_STATE_RE.search(page_html)
    if not match:
        raise LookupError("no Chatik-InitialState on the chat page")
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(html.unescape(raw))


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


async def _fetch_json(page, url: str) -> dict:
    """GET from inside the chat page, with its cookies, like the chat app itself."""
    result = await page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: "include", headers: {Accept: "application/json"}});
            return {status: r.status, type: r.headers.get("content-type") || "", body: await r.text()};
        }""",
        url,
    )
    if result["status"] != 200 or "json" not in result["type"]:
        raise RuntimeError(f"{url.split('?')[0]}: HTTP {result['status']} {result['type']}")
    return json.loads(result["body"])


async def sync_chats(db: Database, browser: HHBrowser, account: str, max_pages: int = 3,
                     sleep=asyncio.sleep, page=None) -> SyncSummary:
    """Copy new chats and messages of the account into the database.

    Only chats whose last message changed since the previous pass are opened. Older pages of the list
    are read only while they still hold changed chats (on the first run: up to `max_pages` pages).
    With `page` (the chat watcher's open tab) that tab is reloaded and left open.
    """
    summary = SyncSummary()
    own_page = page is None
    page = page or await browser.context.new_page()
    try:
        await page.goto(CHAT_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        if await browser.has_captcha(page, f"chats_{account}"):
            raise CaptchaError("captcha on the chat page")
        if "/login" in page.url:
            raise RuntimeError("not signed in to hh")
        state = initial_state(await page.content())
        listing = parse_chat_list(state.get("chats") or {})
        me = listing.me or str(state.get("userId") or "")
        if me:
            db.set(ME_KEY.format(account=account), me)
        chats = list(listing.chats)
        cursor, pages = listing.next_cursor, 1
        while cursor and pages < max_pages and listing.chats and any(
            db.chat_last_message_at(account, c.id) != c.last_message_at for c in listing.chats
        ):
            try:
                listing = parse_chat_list(await _fetch_json(page, CHATS_PAGE_URL.format(cursor=cursor)))
            except Exception as error:  # the next-page call is unverified: keep what we have
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
            data = await _fetch_json(page, CHAT_DATA_URL.format(chat=chat.id, me=me))
            messages = parse_messages(data)
            new, incoming = db.chat_add_messages(chat_id, [(m.id, m.direction, m.author, m.text, m.sent_at)
                                                           for m in messages])
            summary.updated += 1
            summary.new_messages += new
            if incoming and stored is not None:  # not on the very first copy of a chat
                summary.incoming.append(f"{chat.title} ({chat.employer})")
            await sleep(random.uniform(1.5, 4))
        return summary
    finally:
        if own_page:
            await page.close()


async def _chat_input(page):
    """The message box of an open chat: on the page itself or inside the chat iframe."""
    box = page.locator(CHAT_INPUT)
    try:
        await box.first.wait_for(timeout=20_000)
        return box.first, page
    except Exception:
        frame = page.frame_locator("iframe[src*='chatik'], iframe.chatik-integration-iframe")
        box = frame.locator(CHAT_INPUT)
        await box.first.wait_for(timeout=10_000)
        return box.first, frame


async def send_reply(db: Database, browser: HHBrowser, account: str, chat_id: int, text: str) -> int:
    """Type `text` into the hh chat and send it; then copy the chat's messages, so the reply shows up as
    an hh message on the site. Returns how many new messages were stored."""
    chat = db.chat(chat_id)
    page = await browser.context.new_page()
    try:
        await page.goto(CHAT_URL.format(chat=chat["external_id"]), wait_until="domcontentloaded", timeout=90_000)
        if await browser.has_captcha(page, f"reply_{account}"):
            raise CaptchaError("captcha on the chat page")
        if "/login" in page.url:
            raise RuntimeError("not signed in to hh")
        box, scope = await _chat_input(page)
        await box.fill(text)
        await box.press("Enter")
        await page.wait_for_timeout(2500)
        bubbles = scope.locator(CHAT_BUBBLE)
        if not await bubbles.count() or text.strip()[:20] not in await bubbles.last.inner_text():
            raise RuntimeError("the reply did not appear in the chat")
        me = db.get(ME_KEY.format(account=account))
        if not me:
            try:
                me = parse_chat_list(initial_state(await page.content()).get("chats") or {}).me
            except LookupError:
                me = None
        if not me:
            return 0  # sent; the next chat pass copies it
        try:
            messages = parse_messages(await _fetch_json(page, CHAT_DATA_URL.format(chat=chat["external_id"], me=me)))
        except Exception as error:
            log.warning("hh/%s reply sent, chat not re-read: %s", account, error)
            return 0
        new, _ = db.chat_add_messages(chat_id, [(m.id, m.direction, m.author, m.text, m.sent_at) for m in messages])
        if messages:
            last = messages[-1]
            db.chat_upsert(account, chat["external_id"], last_message_at=last.sent_at, last_text=last.text,
                           unread=0)
        return new
    except Exception:
        try:
            await page.screenshot(path=str(browser.screenshots_dir / f"reply_{account}_{chat_id}.png"))
        except Exception:
            pass
        raise
    finally:
        await page.close()
