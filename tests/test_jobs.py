import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.core import stages
from jobpilot.db import Database
from jobpilot.web import queries
from jobpilot.web.app import Site, build_app

PASSWORD = "correct horse"


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("php", "1", "PHP dev", "Acme", "https://hh.ru/vacancy/1", "ru", "", "applied",
                 datetime.now(timezone.utc))
    db.add_post("h1", "@golang_jobs", "https://t.me/golang_jobs/7", "Ищем Go-разработчика\nУдалённо, до 400k")
    item = SimpleNamespace(external_id="g1", title="Senior Go Developer", company="Nordeus", location="Beograd",
                           url="https://www.helloworld.rs/1", summary="", categories=[], published_at=None)
    db.add_feed_job("helloworld", "RS", item, "x1", matched=True)
    return db


def _run(tmp_path, scenario):
    db = _db(tmp_path)
    site = Site(db=db, password=PASSWORD, secret=b"s" * 32, tz="Asia/Yerevan", accounts_path=tmp_path / "none.toml")

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            await client.post("/login", data={"password": PASSWORD, "next": "/"}, allow_redirects=False)
            await scenario(client, db)

    asyncio.run(main())


def test_applied_by_hand_from_telegram_and_a_feed(tmp_path):
    async def scenario(client, db):
        assert "Откликнулся" in await (await client.get("/jobs?src=tg")).text()
        r = await client.post("/api/jobs/tg/1/applied", json={})
        app_id = (await r.json())["id"]
        assert (await (await client.post("/api/jobs/tg/1/applied", json={})).json())["id"] == app_id  # same one
        app = db.application(app_id)
        assert (app["source"], app["account"], app["name"], app["employer"]) == \
            ("tg", "tg", "Ищем Go-разработчика", "@golang_jobs")
        assert app["stage"] == stages.SENT and app["stage_manual"] == 1
        assert db.conn.execute("SELECT verdict FROM tg_posts WHERE id = 1").fetchone()[0] == 1

        tg = await (await client.get("/jobs?src=tg")).text()
        assert f"/application/{app_id}" in tg and "Откликнулся" not in tg and "откликнулись вручную" in tg
        assert "data-by-hand" in tg and "вернуть автомат" not in tg
        assert f"/application/{app_id}" in await (await client.get("/jobs?src=tg&st=applied")).text()
        assert f"/application/{app_id}" in await (await client.get("/jobs?src=tg&from=%40golang_jobs")).text()
        assert "hh · tg" not in tg  # not an hh account tab

        await client.post(f"/api/applications/{app_id}/stage", json={"stage": stages.INTERVIEW})
        await client.post(f"/api/applications/{app_id}/stage", json={"auto": True})  # nothing to hand back to
        assert db.application(app_id)["stage"] == stages.INTERVIEW
        assert f"/application/{app_id}" in await (await client.get("/jobs?st=process")).text()
        page = await (await client.get(f"/application/{app_id}")).text()
        assert "Вакансии · Телеграм" in page and "открыть пост" in page and "убрать отклик" in page

        r = await client.post("/api/jobs/feed/1/applied", json={})
        feed_app = db.application((await r.json())["id"])
        assert (feed_app["source"], feed_app["account"], feed_app["employer"]) == ("feed", "rs", "Nordeus")
        rs = await (await client.get("/jobs?src=rs")).text()
        assert f"/application/{feed_app['id']}" in rs and "Сербия" in rs
        assert (await client.post("/api/jobs/feed/99/applied", json={})).status == 404

        s = queries.summary(db.conn, "Asia/Yerevan")
        assert s["total"] == 3 and s["by_stage"][stages.INTERVIEW] == 1

        assert (await client.post("/api/applications/1/delete", json={})).status == 404  # hh stays
        assert (await client.post(f"/api/applications/{app_id}/delete", json={})).status == 200
        assert db.application(app_id) is None
        assert "Ищем Go-разработчика" in await (await client.get("/jobs?src=tg&st=liked")).text()

    _run(tmp_path, scenario)
