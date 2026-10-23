"""v8 over the hh app API: the live chat watcher, replies from the site and the status filter."""
import asyncio
from collections import deque
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.db import Database
from jobpilot.hh.apply import CaptchaError
from jobpilot.hh.live import ChatWatcher, live_on, send_pending, wait_before_sync
from jobpilot.web import queries
from jobpilot.web.app import Site, build_app

from test_chats import LETTER, REPLY, FakeClient, _chat_data, _item, _listing

PASSWORD = "correct horse"


def test_rate_limit():
    recent = deque([0.0])
    assert wait_before_sync(10, recent, min_gap=60, per_hour=20) == 50
    recent = deque(float(i) for i in range(3))
    assert wait_before_sync(100, recent, min_gap=60, per_hour=3) == 3500
    assert wait_before_sync(4000, recent, min_gap=60, per_hour=3) == 0 and not recent


class Unread(FakeClient):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.count, self.captcha = 0, False

    async def unread(self):
        if self.captcha:
            raise CaptchaError("chats")
        return self.count


def test_watcher_reads_when_the_unread_count_changes(tmp_path):
    db = Database(tmp_path / "t.db")
    client = Unread({None: _listing([_item(1, "137", LETTER)])}, {"1": _chat_data(1, [LETTER, REPLY])})
    summaries, captchas, clock = [], [], {"t": 0.0}
    allowed = {"on": True}

    async def on_summary(summary):
        summaries.append(summary)

    async def on_captcha():
        captchas.append(1)

    async def tick(seconds):
        clock["t"] += seconds

    watcher = ChatWatcher(db, client, "go", may_run=lambda: allowed["on"], on_summary=on_summary,
                          on_captcha=on_captcha, poll=120, min_gap=0, fallback=3600, idle_check=60,
                          clock=lambda: clock["t"], sleep=tick, pause=lambda _: asyncio.sleep(0))

    async def main():
        await watcher.step()
        assert watcher.syncs == 1  # the first round always reads
        await watcher.step()
        assert watcher.syncs == 1  # same count: only the small unread request
        client.count = 1
        await watcher.step()
        assert watcher.syncs == 2
        clock["t"] += 3600
        await watcher.step()
        assert watcher.syncs == 3  # nothing new for an hour: read anyway
        allowed["on"] = False
        await watcher.step()
        assert watcher.syncs == 3
        allowed["on"] = True
        await watcher.step()
        assert watcher.syncs == 4  # after a break it reads again first
        client.captcha = True
        await watcher.step()
        assert captchas == [1]

    asyncio.run(main())
    assert len(summaries) == 4


class Clients:
    def __init__(self, client):
        self.client = client

    def get(self, account):
        return self.client


def _chat(db):
    db.chat_upsert("go", "1", vacancy_id="137", title="Go dev", employer="Acme", last_message_at=None)
    return db.conn.execute("SELECT id FROM chats").fetchone()[0]


def test_reply_goes_out_and_comes_back_as_an_hh_message(tmp_path):
    db = Database(tmp_path / "t.db")
    chat_id = _chat(db)
    mine = {**LETTER, "id": 13, "text": "Да, удобно в четверг", "creationTime": "2026-10-22T12:00:00+03:00"}
    client = FakeClient({}, {"1": _chat_data(1, [LETTER, REPLY, mine])})
    outbox_id = db.outbox_add(chat_id, "Да, удобно в четверг")
    assert [r["id"] for r in db.outbox_for_chat(chat_id)] == [outbox_id]
    notes = []

    async def notify(text):
        notes.append(text)

    async def captcha(account):
        raise AssertionError

    sent = asyncio.run(send_pending(db, Clients(client), may_send=lambda: True, notify=notify,
                                    on_captcha=captcha))
    assert sent == 1 and client.sent == [("1", "Да, удобно в четверг")] and not notes
    assert db.outbox_item(outbox_id)["status"] == "sent"
    assert db.outbox_for_chat(chat_id) == []  # the copy from hh replaced it
    assert [m["text"] for m in db.chat_messages(chat_id)][-1] == "Да, удобно в четверг"
    assert db.chat(chat_id)["last_message_at"] == "2026-10-22T09:00:00+00:00"


class Failing(FakeClient):
    error: Exception = RuntimeError("HTTP 500")

    async def send_message(self, chat_id, text):
        raise self.error


def test_failed_and_captcha_replies(tmp_path):
    db = Database(tmp_path / "t.db")
    chat_id = _chat(db)
    client = Failing({})
    bad = db.outbox_add(chat_id, "Привет")
    notes, captchas = [], []

    async def notify(text):
        notes.append(text)

    async def captcha(account):
        captchas.append(account)

    run = lambda: asyncio.run(send_pending(db, Clients(client), may_send=lambda: True, notify=notify,
                                           on_captcha=captcha))
    assert run() == 0
    row = db.outbox_item(bad)
    assert row["status"] == "error" and "HTTP 500" in row["error"] and "Go dev" in notes[0]
    assert db.outbox_pending() == []  # not tried again by itself

    db.outbox_mark(bad, "pending")  # "повторить" on the site
    client.error = CaptchaError("send")
    assert run() == 0 and captchas == ["go"] and db.outbox_item(bad)["status"] == "pending"


def _site(tmp_path):
    db = Database(tmp_path / "t.db")
    now = datetime.now(timezone.utc)
    db.hh_record("go", "137", "Go dev", "Acme", "https://hh.ru/vacancy/137", "ru", "", "applied", now)
    db.hh_record("go", "138", "Old", "Beta", "https://hh.ru/vacancy/138", "ru", "", "applied", now)
    db.hh_sync_states("go", {"138": "discard"})
    a = db.chat_upsert("go", "1", vacancy_id="137", title="Go dev", last_message_at="2026-10-22T09:00:00+00:00",
                       state="INTERVIEW", unread=1)
    db.chat_add_messages(a, [("1", "out", "", "Письмо", "2026-10-22T08:00:00+00:00"),
                             ("2", "in", "HR", "Приглашаем", "2026-10-22T09:00:00+00:00")])
    db.chat_upsert("go", "2", vacancy_id="138", title="Old", last_message_at="2026-10-21T09:00:00+00:00")  # pre-v8
    db.chat_upsert("php", "3", title="Waiting", last_message_at="2026-10-20T09:00:00+00:00", state="RESPONSE")
    return Site(db=db, password=PASSWORD, secret=b"s" * 32, tz="Asia/Yerevan", accounts_path=tmp_path / "none.toml")


def test_status_filter(tmp_path):
    site = _site(tmp_path)
    titles = lambda **kw: [r["title"] for r in queries.chats(site.db.conn, **kw)]
    assert titles(status="invite") == ["Go dev"]
    assert titles(status="reject") == ["Old"]  # no hh state yet: the application's stage decides
    assert titles(status="waiting") == ["Waiting"]
    assert titles(status="my_turn") == titles(status="unread") == ["Go dev"]
    assert titles(status="reject", account="php") == []
    counts = queries.chat_filter_counts(site.db.conn)
    assert counts[""] == 3 and counts["invite"] == 1 and counts["offer"] == 0
    site.db.chat_upsert("go", "2", state="DISCARD")
    site.db.chat_upsert("go", "2")  # a later read without a state keeps it
    assert site.db.chat(2)["state"] == "DISCARD"


def test_reply_from_the_site(tmp_path):
    site = _site(tmp_path)

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            await client.post("/login", data={"password": PASSWORD, "next": "/"}, allow_redirects=False)
            page = await (await client.get("/chats?status=invite")).text()
            assert "собеседование" in page and "ваш ход" in page and "Waiting" not in page
            page = await (await client.get("/chats/1")).text()
            assert 'id="reply"' in page
            r = await client.post("/api/chats/1/reply", data="text=hi")  # a cross-site form is refused
            assert r.status == 415
            assert (await client.post("/api/chats/1/reply", json={"text": "  "})).status == 400
            assert (await client.post("/api/chats/1/reply", json={"text": "x" * 4001})).status == 400
            r = await client.post("/api/chats/1/reply", json={"text": "Подходит четверг 15:00"})
            outbox_id = (await r.json())["id"]
            page = await (await client.get("/chats/1")).text()
            assert "Подходит четверг 15:00" in page and "отправляется" in page
            site.db.outbox_mark(outbox_id, "error", "the reply did not appear in the chat")
            page = await (await client.get("/chats/1")).text()
            assert "не ушло" in page and "повторить" in page
            assert (await client.post(f"/api/outbox/{outbox_id}/retry", json={})).status == 200
            assert site.db.outbox_item(outbox_id)["status"] == "pending"
            assert (await client.post(f"/api/outbox/{outbox_id}/delete", json={})).status == 200
            assert site.db.outbox_item(outbox_id) is None
            assert (await client.post("/api/settings", json={"chats_live": False})).status == 200
            assert not live_on(site.db)
            assert "раз в час" in await (await client.get("/settings")).text() and \
                "checked" not in (await (await client.get("/settings")).text()).split("chats_live")[1][:40]

    asyncio.run(main())
