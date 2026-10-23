import asyncio
import json
from datetime import datetime, timezone

from jobpilot.core import stages
from jobpilot.db import Database
from jobpilot.hh import chats as hh_chats
from jobpilot.hh.chats import parse_chat_list, parse_messages, sync_chats

ME = "100"


def _message(mid, chat, at, text, who=ME, name="Я", state=None):
    m = {"id": mid, "chatId": chat, "creationTime": at, "text": text, "type": "SIMPLE", "hidden": False,
         "participantId": who, "participantDisplay": {"name": name, "isBot": False}}
    if state:
        m["workflowTransition"] = {"applicantState": state}
    return m


def _listing(items, next_from=None):
    """Shaped like the "chats" block of the chat page (and like chats/by_ids)."""
    return {
        "chats": {"items": items, "found": len(items), "nextFrom": next_from},
        "chatsDisplayInfo": {str(i["id"]): {"title": f"Вакансия {i['id']}", "subtitle": "Acme"} for i in items},
        "resources": {"negotiation_topics": {
            f"t{i['id']}": {"currentApplicantState": i.pop("_state", "RESPONSE")} for i in items}},
    }


def _item(chat, vacancy, last, unread=0, state="RESPONSE", kind="NEGOTIATION"):
    return {"id": chat, "type": kind, "unreadCount": unread, "currentParticipantId": ME,
            "resources": {"VACANCY": [vacancy], "NEGOTIATION_TOPIC": [f"t{chat}"]},
            "lastMessage": last, "_state": state}


def _chat_data(chat, messages):
    return {"chat": {"id": chat, "currentParticipantId": ME, "messages": {"items": messages, "hasMore": False}}}


LETTER = _message(11, 1, "2026-10-22T09:47:32.049669+03:00", "Добрый день! Я Go-разработчик")
REPLY = _message(12, 1, "2026-10-22T10:47:41.650024+03:00", "Спасибо, но <нет>", who="555", name="Елена",
                 state="DISCARD")


def test_parse_list_and_messages():
    page = parse_chat_list(_listing([
        _item(1, "137", REPLY, unread=1, state="DISCARD"),
        _item(2, None, {}, kind="SUPPORT"),
    ], next_from="cursor1"))
    assert page.next_cursor == "cursor1" and page.me == ME and len(page.chats) == 1
    chat = page.chats[0]
    assert (chat.id, chat.vacancy_id, chat.title, chat.employer, chat.unread, chat.state) == \
        ("1", "137", "Вакансия 1", "Acme", 1, "DISCARD")
    assert chat.last_message_at == "2026-10-22T07:47:41.650024+00:00"
    messages = parse_messages(_chat_data(1, [REPLY, LETTER]))
    assert [m.direction for m in messages] == ["out", "in"]
    assert messages[1].author == "Елена" and messages[1].text == "Спасибо, но <нет>"


class FakeClient:
    """Answers like HHClient: chat list pages by cursor, chat_data by chat id."""

    def __init__(self, pages, chats=None, sent=None):
        self.pages, self.chats, self.fetched = pages, chats or {}, []
        self.sent = sent if sent is not None else []

    async def chat_list(self, cursor=None):
        self.fetched.append(("list", cursor))
        if cursor not in self.pages:
            raise RuntimeError(f"no page {cursor}")
        return json.loads(json.dumps(self.pages[cursor]))

    async def chat_data(self, chat_id):
        self.fetched.append(("chat", chat_id))
        return self.chats[chat_id]

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _sync(db, client):
    async def no_sleep(_):
        pass

    return asyncio.run(sync_chats(db, client, "go", sleep=no_sleep))


def test_sync_links_the_application_and_reads_only_changed_chats(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("go", "137", "Go dev", "Acme", "https://hh.ru/vacancy/137", "ru", "", "applied",
                 datetime.now(timezone.utc))
    app_id = db.conn.execute("SELECT id FROM applications").fetchone()[0]

    first = FakeClient({None: _listing([_item(1, "137", LETTER)])}, {"1": _chat_data(1, [LETTER])})
    s = _sync(db, first)
    assert (s.chats, s.updated, s.new_messages, s.incoming) == (1, 1, 1, [])
    chat = db.conn.execute("SELECT * FROM chats").fetchone()
    assert chat["application_id"] == app_id and chat["vacancy_id"] == "137" and chat["title"] == "Вакансия 1"

    same = FakeClient({None: _listing([_item(1, "137", LETTER)])})
    assert _sync(db, same).updated == 0 and not [f for f in same.fetched if f[0] == "chat"]

    answered = FakeClient({None: _listing([_item(1, "137", REPLY, unread=1, state="DISCARD")])},
                          {"1": _chat_data(1, [LETTER, REPLY])})
    s = _sync(db, answered)
    assert s.new_messages == 1 and s.incoming == [] and s.stages_moved == 1  # a rejection is not announced
    assert db.application(app_id)["stage"] == stages.REJECTED
    assert [m["direction"] for m in db.chat_messages(chat["id"])] == ["out", "in"]
    assert db.chat(chat["id"])["unread"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'chat'").fetchone()[0] == 2


def test_an_invitation_is_announced(tmp_path):
    db = Database(tmp_path / "t.db")
    _sync(db, FakeClient({None: _listing([_item(1, "137", LETTER)])}, {"1": _chat_data(1, [LETTER])}))
    invite = {**REPLY, "text": "Приглашаем на интервью", "workflowTransition": {"applicantState": "INVITATION"}}
    s = _sync(db, FakeClient({None: _listing([_item(1, "137", invite, unread=1, state="INVITATION")])},
                             {"1": _chat_data(1, [LETTER, invite])}))
    assert s.incoming == ["Вакансия 1 (Acme)"]


def test_sync_reads_older_pages_while_they_change(tmp_path):
    db = Database(tmp_path / "t.db")
    client = FakeClient(
        {None: _listing([_item(1, "137", LETTER)], next_from="c2"),
         "c2": _listing([_item(2, "138", LETTER)], next_from="c3"),
         "c3": _listing([_item(3, "139", LETTER)])},
        {str(i): _chat_data(i, [LETTER]) for i in (1, 2, 3)},
    )
    s = asyncio.run(sync_chats(db, client, "go", max_pages=2, sleep=lambda _: asyncio.sleep(0)))
    assert s.chats == 2 and ("list", "c3") not in client.fetched


def test_a_failing_next_page_keeps_the_first(tmp_path):
    db = Database(tmp_path / "t.db")
    client = FakeClient({None: _listing([_item(1, "137", LETTER)], next_from="c2")}, {"1": _chat_data(1, [LETTER])})
    assert _sync(db, client).chats == 1


def test_chat_stages_are_known():
    assert set(hh_chats.CHAT_STAGES.values()) <= set(stages.LABELS)


def test_a_v2_file_gets_the_chat_columns(tmp_path):
    import sqlite3

    from jobpilot.core.schema import STEPS, VERSION

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for step in STEPS[:2]:
        step(conn)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO chats (account, external_id, title) VALUES ('go', '5', 'old')")
    conn.close()
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == VERSION
    assert db.chat_upsert("go", "5", vacancy_id="9", employer="Acme") == 1
    assert dict(db.chat(1))["title"] == "old" and db.chat(1)["employer"] == "Acme"
    assert (tmp_path / "v2.db.v2.bak").exists()
