import asyncio
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.core import stages
from jobpilot.db import Database
from jobpilot.web import queries
from jobpilot.web.app import Site, build_app

PASSWORD = "correct horse"


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    now = datetime.now(timezone.utc)
    db.hh_record("php", "1", "PHP dev", "Acme", "https://hh.ru/vacancy/1", "ru", "Здравствуйте", "applied", now)
    db.hh_record("php", "2", "Bitrix", "Beta", "https://hh.ru/vacancy/2", "ru", "", "skipped", now)
    db.hh_record("go", "3", "Go dev", "Gamma <b>", "https://hh.ru/vacancy/3", "en", "", "applied",
                 now - timedelta(days=2))
    db.hh_sync_states("go", {"3": "discard"})
    return db


def _run(tmp_path, scenario):
    db = _db(tmp_path)
    site = Site(db=db, password=PASSWORD, secret=b"s" * 32, tz="Asia/Yerevan", accounts_path=tmp_path / "none.toml")

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            await scenario(client, db)

    asyncio.run(main())


async def _login(client):
    r = await client.post("/login", data={"password": PASSWORD, "next": "/stages"}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/stages"


def test_pages_need_the_password(tmp_path):
    async def scenario(client, db):
        r = await client.get("/stages", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].startswith("/login")
        r = await client.post("/api/applications/1/stage", json={"stage": "offer"})
        assert r.status == 401
        r = await client.post("/login", data={"password": "nope"})
        assert "Неверный пароль" in await r.text()
        assert db.application(1)["stage"] == stages.SENT

    _run(tmp_path, scenario)


def test_login_is_throttled(tmp_path):
    async def scenario(client, db):
        for _ in range(5):
            await client.post("/login", data={"password": "nope"})
        r = await client.post("/login", data={"password": PASSWORD}, allow_redirects=False)
        assert r.status == 200 and "Слишком много попыток" in await r.text()

    _run(tmp_path, scenario)


def test_pages_show_both_accounts(tmp_path):
    async def scenario(client, db):
        await _login(client)
        overview = await client.get("/")
        text = await overview.text()
        assert overview.status == 200 and "/account/php" in text and "/account/go" in text
        stages_page = await (await client.get("/stages")).text()
        assert "PHP dev" in stages_page and "Go dev" in stages_page and "Bitrix" not in stages_page
        assert "Gamma &lt;b&gt;" in stages_page  # escaped
        skipped = await (await client.get(f"/stages?stage={queries.NOT_APPLIED}")).text()
        assert "Bitrix" in skipped and "PHP dev" not in skipped
        php = await (await client.get("/account/php")).text()
        assert "PHP dev" in php and "Go dev" not in php
        assert (await client.get("/account/nobody")).status == 404
        detail = await (await client.get("/application/1")).text()
        assert "Здравствуйте" in detail

    _run(tmp_path, scenario)


def test_manual_stage_sticks_and_can_be_released(tmp_path):
    async def scenario(client, db):
        await _login(client)
        r = await client.post("/api/applications/1/stage", json={"stage": stages.GHOSTED})
        assert r.status == 200 and (await r.json())["manual"] is True
        db.hh_sync_states("php", {"1": "viewed"})
        assert db.application(1)["stage"] == stages.GHOSTED
        r = await client.post("/api/applications/1/stage", json={"auto": True})
        body = await r.json()
        assert body["manual"] is False and body["stage"] == stages.VIEWED
        r = await client.post("/api/applications/1/stage", json={"stage": "тестовое"})
        assert (await r.json())["stage"] == "тестовое"
        assert "тестовое" in await (await client.get("/stages")).text()

    _run(tmp_path, scenario)


def test_note_and_csrf_guard(tmp_path):
    async def scenario(client, db):
        await _login(client)
        r = await client.post("/api/applications/1/note", data={"note": "x"})  # a plain form: refused
        assert r.status == 415
        r = await client.post("/api/applications/1/note", json={"note": "созвон во вторник"})
        assert r.status == 200 and db.application(1)["note"] == "созвон во вторник"
        assert (await client.post("/api/applications/999/note", json={"note": "x"})).status == 404
        assert "созвон" in await (await client.get("/stages?q=созвон")).text()

    _run(tmp_path, scenario)


def test_summary_counts(tmp_path):
    db = _db(tmp_path)
    s = queries.summary(db.conn, "Asia/Yerevan")
    assert s["total"] == 2 and s["today"] == 1 and s["week"] == 2 and s["skipped"] == 1
    assert s["by_stage"][stages.REJECTED] == 1 and s["answered"] == 1 and s["answer_rate"] == 50
    assert sum(d["n"] for d in queries.daily(db.conn, "Asia/Yerevan")) == 2


def test_chats_pages(tmp_path):
    async def scenario(client, db):
        chat = db.chat_upsert("php", "900", vacancy_id="1", title="PHP dev", employer="Acme",
                              last_message_at="2026-10-22T07:00:00+00:00", unread=2, last_text="Приглашаем <b>")
        db.chat_add_messages(chat, [("1", "out", "Я", "Здравствуйте", "2026-10-22T06:00:00+00:00"),
                                    ("2", "in", "HR", "Приглашаем <b>", "2026-10-22T07:00:00+00:00")])
        db.chat_upsert("go", "901", vacancy_id="777", title="Чужой отклик", employer="X")
        await _login(client)
        listing = await (await client.get("/chats")).text()
        assert "PHP dev" in listing and "Чужой отклик" in listing and 'class="badge">2<' in listing
        opened = await (await client.get(f"/chats/{chat}")).text()
        assert "Приглашаем &lt;b&gt;" in opened and "/application/1" in opened and 'data-id="1"' in opened
        assert "Здравствуйте" in await (await client.get(f"/chats/{chat}?account=go")).text()
        assert "PHP dev" not in await (await client.get("/chats?account=go")).text()
        assert "Чужой" not in await (await client.get("/chats?q=Приглашаем")).text()
        assert (await client.get("/chats/999")).status == 404
        assert "Приглашаем" in await (await client.get("/application/1")).text()

    _run(tmp_path, scenario)
