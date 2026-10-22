"""v8: shared browser per account, the live chat watcher, replies from the site and the status filter."""
import asyncio
import json
from collections import deque
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.db import Database
from jobpilot.hh.apply import CaptchaError
from jobpilot.hh.browsers import SharedBrowsers
from jobpilot.hh.live import ChatWatcher, is_news, live_on, send_pending, wait_before_sync
from jobpilot.web import queries
from jobpilot.web.app import Site, build_app

from test_chats import LETTER, ME, REPLY, FakePage, _chat_data, _item, _listing

PASSWORD = "correct horse"


class Opened:
    """Stands in for HHBrowser in SharedBrowsers."""
    log: list = []

    def __init__(self, profile, shots):
        self.profile, self.closed = profile, False

    async def __aenter__(self):
        Opened.log.append(("open", self.profile.name))
        return self

    async def __aexit__(self, *exc):
        Opened.log.append(("close", self.profile.name))


def test_one_browser_per_account_while_anyone_uses_it(tmp_path):
    Opened.log = []
    browsers = SharedBrowsers(tmp_path, factory=Opened)

    async def main():
        async with browsers.use("php") as a:
            async with browsers.use("php") as b:  # the apply pass while the watcher holds it
                assert a is b
            assert browsers.is_open("php")
            a.closed = True  # Chromium died
            async with browsers.use("php") as c:
                assert c is not a
        assert not browsers.is_open("php")

    asyncio.run(main())
    assert Opened.log == [("open", "php"), ("open", "php"), ("close", "php"), ("close", "php")]


def test_frames_and_rate_limit():
    assert not is_news("ping") and not is_news('{"type":"pong"}')
    assert is_news(json.dumps({"type": "chat_message", "chatId": 5, "message": {"text": "Добрый день"}}))
    recent = deque([0.0])
    assert wait_before_sync(10, recent, min_gap=60, per_hour=20) == 50
    recent = deque(float(i) for i in range(3))
    assert wait_before_sync(100, recent, min_gap=60, per_hour=3) == 3500
    assert wait_before_sync(4000, recent, min_gap=60, per_hour=3) == 0 and not recent


class LivePage(FakePage):
    """The chat page of the watcher: hands its websocket to the watcher, counts reloads."""

    def __init__(self, state, answers):
        super().__init__(state, answers)
        self.handlers, self.loads, self.closed = {}, 0, False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, **kw):
        self.loads += 1

    async def close(self):
        self.closed = True


class Socket:
    url = "wss://websocket.hh.ru/chatik"

    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class Holder:
    def __init__(self, page):
        self.context = self
        self.page = page

    async def new_page(self):
        return self.page

    async def has_captcha(self, page, label):
        return False


class OneBrowser:
    def __init__(self, browser):
        self.browser = browser

    def use(self, account):
        browser = self.browser

        class Ctx:
            async def __aenter__(self):
                return browser

            async def __aexit__(self, *exc):
                pass

        return Ctx()


def test_watcher_reads_on_chat_frames_only(tmp_path):
    db = Database(tmp_path / "t.db")
    page = LivePage({"userId": ME, "chats": _listing([_item(1, "137", LETTER)])},
                    {"chatId=1&": _chat_data(1, [LETTER, REPLY])})
    summaries, allowed = [], {"on": True}

    async def on_summary(summary):
        summaries.append(summary)

    async def no_captcha():
        raise AssertionError

    watcher = ChatWatcher(db, OneBrowser(Holder(page)), "go", may_run=lambda: allowed["on"],
                          on_summary=on_summary, on_captcha=no_captcha, frames_log=tmp_path / "ws.txt",
                          debounce=0, min_gap=0, quiet=0, idle_check=0.05, fallback=3600,
                          pause=lambda _: asyncio.sleep(0))

    async def main():
        task = asyncio.create_task(watcher._session())
        await asyncio.sleep(0.05)
        assert page.loads == 1 and watcher.syncs == 1
        ws = Socket()
        page.handlers["websocket"](ws)
        ws.handlers["framereceived"]("ping")
        await asyncio.sleep(0.1)
        assert watcher.syncs == 1  # a ping is not news
        ws.handlers["framereceived"](json.dumps({"chatId": 1, "message": {"text": "Приглашаем на интервью"}}, ensure_ascii=False))
        await asyncio.sleep(0.1)
        assert watcher.syncs == 2 and page.loads == 2
        allowed["on"] = False  # captcha elsewhere, pause or night: the watcher lets the page go
        await asyncio.wait_for(task, 1)

    asyncio.run(main())
    assert page.closed and len(summaries) == 2
    assert "Приглашаем" in (tmp_path / "ws.txt").read_text()
    assert db.get("hh_chat_me:go") == ME


class ReplyPage(LivePage):
    """hh.ru/chat/<id>: a text box that shows what was typed as the last bubble."""

    def __init__(self, answers, echo=True):
        super().__init__({}, answers)
        self.typed, self.echo, self.url = "", echo, "https://hh.ru/chat/1"

    def locator(self, selector):
        page = self

        class Loc:
            first = last = property(lambda self: self)

            async def wait_for(self, timeout=None):
                pass

            async def fill(self, text):
                page.typed = text

            async def press(self, key):
                assert key == "Enter"

            async def count(self):
                return 1

            async def inner_text(self):
                return page.typed if page.echo else "старое"

        return Loc()

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self, path):
        pass


class ReplyBrowser(Holder):
    screenshots_dir = None

    async def has_captcha(self, page, label):
        return self.captcha


def _chat(db):
    db.chat_upsert("go", "1", vacancy_id="137", title="Go dev", employer="Acme", last_message_at=None)
    return db.conn.execute("SELECT id FROM chats").fetchone()[0]


def test_reply_goes_out_and_comes_back_as_an_hh_message(tmp_path):
    db = Database(tmp_path / "t.db")
    chat_id = _chat(db)
    db.set("hh_chat_me:go", ME)
    mine = {**LETTER, "id": 13, "text": "Да, удобно в четверг", "creationTime": "2026-10-22T12:00:00+03:00"}
    page = ReplyPage({"chatId=1&": _chat_data(1, [LETTER, REPLY, mine])})
    browser = ReplyBrowser(page)
    browser.captcha = False
    outbox_id = db.outbox_add(chat_id, "Да, удобно в четверг")
    assert [r["id"] for r in db.outbox_for_chat(chat_id)] == [outbox_id]
    notes = []

    async def notify(text):
        notes.append(text)

    async def captcha(account):
        raise AssertionError

    sent = asyncio.run(send_pending(db, OneBrowser(browser), may_send=lambda: True, notify=notify,
                                    on_captcha=captcha))
    assert sent == 1 and page.typed == "Да, удобно в четверг" and not notes
    assert db.outbox_item(outbox_id)["status"] == "sent"
    assert db.outbox_for_chat(chat_id) == []  # the copy from hh replaced it
    assert [m["text"] for m in db.chat_messages(chat_id)][-1] == "Да, удобно в четверг"
    assert db.chat(chat_id)["last_message_at"] == "2026-10-22T09:00:00+00:00"


def test_failed_and_captcha_replies(tmp_path):
    db = Database(tmp_path / "t.db")
    chat_id = _chat(db)
    page = ReplyPage({}, echo=False)
    browser = ReplyBrowser(page)
    browser.captcha = False
    bad = db.outbox_add(chat_id, "Привет")
    notes, captchas = [], []

    async def notify(text):
        notes.append(text)

    async def captcha(account):
        captchas.append(account)

    run = lambda: asyncio.run(send_pending(db, OneBrowser(browser), may_send=lambda: True, notify=notify,
                                           on_captcha=captcha))
    assert run() == 0
    row = db.outbox_item(bad)
    assert row["status"] == "error" and "did not appear" in row["error"] and "Go dev" in notes[0]
    assert db.outbox_pending() == []  # not tried again by itself

    db.outbox_mark(bad, "pending")  # "повторить" on the site
    browser.captcha = True
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
