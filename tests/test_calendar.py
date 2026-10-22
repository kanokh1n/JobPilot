import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.core import interviews, stages
from jobpilot.db import Database
from jobpilot.web import charts, queries
from jobpilot.web.app import Site, build_app

TZ = "Asia/Yerevan"
REF = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
PASSWORD = "correct horse"


def test_find_when_reads_common_notes():
    f = interviews.find_when("собес 24.10 в 15:00, zoom https://zoom.us/j/1.", TZ, REF)
    assert f.starts_at == datetime(2026, 10, 24, 15, 0, tzinfo=ZoneInfo(TZ)) and f.has_time
    assert f.link == "https://zoom.us/j/1"
    f = interviews.find_when("техническое 28 октября, 11:30", TZ, REF)
    assert (f.starts_at.month, f.starts_at.day, f.starts_at.hour, f.starts_at.minute) == (10, 28, 11, 30)
    f = interviews.find_when("в 16:30 созвон 3.11", TZ, REF)
    assert (f.starts_at.day, f.starts_at.month, f.starts_at.hour) == (3, 11, 16)
    f = interviews.find_when("HR 5 января", TZ, REF)
    assert f.starts_at.year == 2027 and not f.has_time and f.starts_at.hour == interviews.DEFAULT_HOUR
    f = interviews.find_when("25.10.2026 10:00", TZ, REF)
    assert f.starts_at.year == 2026 and f.starts_at.hour == 10
    assert interviews.find_when("без даты", TZ, REF) is None
    assert interviews.find_when("31.02", TZ, REF) is None


def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    now = datetime.now(timezone.utc)
    db.hh_record("go", "1", "Go dev", "Acme", "https://hh.ru/vacancy/1", "ru", "", "applied", now - timedelta(days=3))
    db.set_stage(1, stages.INTERVIEW)
    db.set_note(1, "собес 24.10 в 15:00 https://meet.google.com/abc-defg-hij")
    db.hh_record("php", "2", "PHP dev", "Beta", "https://hh.ru/vacancy/2", "ru", "", "applied", now - timedelta(days=1))
    db.set_stage(2, stages.INTERVIEW)  # no date anywhere
    db.hh_record("php", "3", "PHP lead", "Gamma", "https://hh.ru/vacancy/3", "ru", "", "applied", now)
    db.set_note(3, "написать HR 24.10")  # a date, but nothing says interview
    db.hh_sync_states("php", {"3": "discard"})
    return db


def test_import_moves_dates_from_notes_once(tmp_path):
    db = _db(tmp_path)
    assert interviews.import_once(db, TZ) == 1
    rows = db.conn.execute("SELECT * FROM interviews").fetchall()
    assert len(rows) == 1 and rows[0]["application_id"] == 1
    assert rows[0]["link"] == "https://meet.google.com/abc-defg-hij"
    assert db.application(1)["note"].startswith("собес")  # the note stays
    assert interviews.import_once(db, TZ) == 0
    # the one without a date waits in the "no date" list
    assert [app_id for app_id, found in interviews.candidates(db, TZ)] == [2]


def test_interview_moves_stage_but_not_past_it(tmp_path):
    db = _db(tmp_path)
    db.hh_record("go", "4", "Go 2", "Delta", "https://hh.ru/vacancy/4", "ru", "", "applied")
    db.set_stage(4, stages.INVITATION)  # manual
    db.add_interview(datetime.now(timezone.utc), application_id=4)
    assert db.application(4)["stage"] == stages.INTERVIEW
    db.add_interview(datetime.now(timezone.utc), application_id=3)  # rejected stays rejected
    assert db.application(3)["stage"] == stages.REJECTED


def test_charts_and_funnel(tmp_path):
    db = _db(tmp_path)
    applied = queries.applied_by_day(db.conn, TZ, 7, ["php", "go"])
    assert sum(applied["series"]["php"]) == 2 and sum(applied["series"]["go"]) == 1
    answers = queries.reactions_by_day(db.conn, TZ, 7)
    assert sum(answers["series"]["rejected"]) == 1 and sum(answers["series"]["invited"]) == 2
    f = queries.funnel(db.conn, TZ, 30)
    assert f["total"]["sent"] == 3 and f["total"]["interview"] == 2 and f["total"]["answered"] == 3
    assert f["accounts"]["php"]["rejected"] == 1
    c = charts.stacked(applied["days"], applied["series"], {})
    assert not c.empty and len(c.columns) == 7 and c.ticks[0][1] == 0
    assert charts.stacked(applied["days"], {"x": [0] * 7}, {}).empty
    assert charts.bar_path(0, 0, 10, 20, True).startswith("M")


def _run(tmp_path, scenario):
    db = _db(tmp_path)
    site = Site(db=db, password=PASSWORD, secret=b"s" * 32, tz=TZ, accounts_path=tmp_path / "none.toml")

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            r = await client.post("/login", data={"password": PASSWORD, "next": "/"}, allow_redirects=False)
            assert r.status == 302
            await scenario(client, db)

    asyncio.run(main())


def test_calendar_pages_and_api(tmp_path):
    async def scenario(client, db):
        page = await (await client.get("/calendar")).text()
        assert "Собеседование без даты" in page and "PHP dev" in page and "Go dev" in page
        assert "проверить и добавить 24.10 15:00" in page
        form = await (await client.get("/calendar/new?app=1")).text()
        assert 'value="2026-10-24T15:00"' in form
        r = await client.post("/api/interviews", data="x")
        assert r.status == 415
        r = await client.post("/api/interviews", json={"starts_at": "2026-10-24T15:00", "application_id": "1",
                                                        "link": "javascript:alert(1)"})
        assert r.status == 400
        r = await client.post("/api/interviews", json={"starts_at": "2026-10-24T15:00", "application_id": "1",
                                                        "link": "https://meet.google.com/x", "title": "техническое"})
        iv = (await r.json())["id"]
        row = db.interview(iv)
        assert row["starts_at"] == "2026-10-24T11:00:00+00:00"  # Yerevan is UTC+4
        month = await (await client.get("/calendar?month=2026-10")).text()
        assert "15:00" in month and "Acme" in month and "Собеседование без даты" in month  # PHP dev still waits
        r = await client.post(f"/api/interviews/{iv}", json={"starts_at": "2026-10-25T10:30", "application_id": ""})
        assert r.status == 200 and db.interview(iv)["application_id"] is None
        assert 'value="2026-10-25T10:30"' in await (await client.get(f"/calendar/{iv}")).text()
        assert (await client.get("/calendar/999")).status == 404
        r = await client.post(f"/api/interviews/{iv}/delete", json={})
        assert r.status == 200 and db.interview(iv) is None
        assert (await client.post(f"/api/interviews/{iv}/delete", json={})).status == 404
        assert (await client.get("/calendar?month=bad")).status == 200

    _run(tmp_path, scenario)


def test_stats_page(tmp_path):
    async def scenario(client, db):
        for q in ("", "?days=7", "?days=90&account=php", "?days=5&account=nobody"):
            r = await client.get("/stats" + q)
            text = await r.text()
            assert r.status == 200 and "Отклики по дням" in text and "<svg" in text
        db.add_interview(datetime.now(timezone.utc) + timedelta(days=1), application_id=2, link="https://x.test/m")
        overview = await (await client.get("/")).text()
        assert "Требует внимания" in overview and "собеседование, Beta" in overview
        app = await (await client.get("/application/2")).text()
        assert "https://x.test/m" in app

    _run(tmp_path, scenario)
