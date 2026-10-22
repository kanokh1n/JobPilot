import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from aiohttp.test_utils import TestClient, TestServer

from jobpilot.db import Database
from jobpilot.feeds import sources
from jobpilot.feeds.parse import FeedError, job_hash, parse
from jobpilot.feeds.poll import FeedPoller, is_due
from jobpilot.tg.notifier import Notifier
from jobpilot.web.app import Site, build_app

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HelloWorld.rs</title><ttl>15</ttl>
<item><title>Senior PHP Developer - Acme d.o.o.</title><link>https://www.helloworld.rs/posao/1</link>
<guid isPermaLink="false">hw-1</guid><pubDate>Wed, 22 Oct 2026 10:00:00 +0200</pubDate>
<description>&lt;p&gt;Laravel &amp;amp; MySQL&lt;/p&gt;&lt;p&gt;Beograd&lt;/p&gt;</description></item>
<item><title>Junior Go Engineer</title><link>https://www.helloworld.rs/posao/2</link><guid>hw-2</guid></item>
<item><title>Data Analyst</title><link>https://www.helloworld.rs/posao/3</link><guid>hw-3</guid>
<description>Ready to go? Python and SQL.</description></item>
</channel></rss>"""

WORDPRESS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Startit Poslovi</title>
<item><title>Backend developer</title><link>https://startit.rs/poslovi/backend/</link>
<dc:creator><![CDATA[Nordeus]]></dc:creator><category><![CDATA[Golang]]></category><category>Beograd</category>
<guid isPermaLink="false">https://startit.rs/?p=5</guid><pubDate>Tue, 14 Oct 2026 08:00:00 +0000</pubDate>
<description><![CDATA[Short]]></description><content:encoded><![CDATA[<p>Full <b>text</b></p>]]></content:encoded></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Jobs</title>
<entry><title>Go Developer</title><link rel="alternate" href="https://ex.com/j/1"/><id>urn:1</id>
<updated>2026-10-20T09:00:00Z</updated><author><name>Vega IT</name></author><summary>Novi Sad</summary></entry>
</feed>"""


def test_parse_rss_wordpress_and_atom():
    feed = parse(RSS)
    assert feed.ttl == 15 and len(feed.items) == 3
    first = feed.items[0]
    assert (first.title, first.company, first.external_id) == ("Senior PHP Developer", "Acme d.o.o.", "hw-1")
    assert first.summary == "Laravel & MySQL\nBeograd"
    assert first.published_at == "2026-10-22T08:00:00+00:00"

    wp = parse(WORDPRESS).items[0]
    assert wp.company == "Nordeus" and wp.categories == ["Golang", "Beograd"] and wp.summary == "Full text"

    atom = parse(ATOM).items[0]
    assert (atom.title, atom.url, atom.company, atom.published_at) == (
        "Go Developer", "https://ex.com/j/1", "Vega IT", "2026-10-20T09:00:00+00:00")


def test_parse_refuses_html_and_entities():
    for body in (b"<html><body>blocked</body></html>", b"not xml at all",
                 b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss/>'):
        try:
            parse(body)
        except FeedError:
            continue
        raise AssertionError(body)


def test_sources_settings_and_extra_lines(tmp_path):
    db = Database(tmp_path / "t.db")
    keys = [s.key for s in sources.all_sources(db)]
    assert keys == ["helloworld", "startit", "infostud"]
    assert {s.country for s in sources.all_sources(db)} == {"RS"}
    db.set(sources.OVERRIDES, json.dumps({"startit": {"enabled": False, "interval": 1}}))
    startit = sources.all_sources(db)[1]
    assert not startit.enabled and startit.interval == sources.MIN_INTERVAL
    good, bad = sources.parse_extra_lines("ME | Posao ME | https://ex.me/rss | 120\n# DE | Arbeit | https://de.ex/rss\nbroken line")
    assert [(s.key, s.country, s.interval, s.enabled) for s in good] == [
        ("posao-me", "ME", 120, True), ("arbeit", "DE", 60, False)]
    assert bad == ["broken line"]
    sources.save_extra(db, good)
    assert [s.key for s in sources.all_sources(db)][-2:] == ["posao-me", "arbeit"]
    assert sources.extra_lines(db).splitlines()[1].startswith("# DE | Arbeit")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def _poller(db, handler, clock):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_):
        return None

    return FeedPoller(db, client, sleep=no_sleep, clock=clock)


def test_poll_stores_matches_and_uses_conditional_get(tmp_path):
    db = Database(tmp_path / "t.db")
    clock, seen = Clock(), []

    def handler(request):
        seen.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=RSS, headers={"ETag": '"v1"'})

    poller = _poller(db, handler, clock)
    hw = sources.all_sources(db)[0]

    async def main():
        first = await poller.poll(hw)
        assert first.status == "ok" and first.first and first.items == 3 and first.stored == 3
        # PHP matches; "Junior Go" is excluded; "go" in the analyst's description does not count
        assert [j.title for j in first.new] == ["Senior PHP Developer"]
        state = sources.state(db, hw.key)
        assert state["etag"] == '"v1"' and state["ttl"] == 15
        assert not is_due(state, clock.now + timedelta(minutes=29))
        assert is_due(state, clock.now + timedelta(minutes=31))
        clock.now += timedelta(minutes=31)
        second = await poller.poll(hw)
        assert second.status == "not_modified" and not second.first
        assert seen[-1].headers["User-Agent"].startswith("JobPilot")

    asyncio.run(main())
    assert db.conn.execute("SELECT COUNT(*) FROM feed_jobs").fetchone()[0] == 3
    assert [j.title for j in db.feed_unsent()] == ["Senior PHP Developer"]


def test_errors_back_off_and_retry_after(tmp_path):
    db = Database(tmp_path / "t.db")
    clock = Clock()
    responses = [httpx.Response(500), httpx.Response(200, content=b"<html>captcha</html>"),
                 httpx.Response(429, headers={"Retry-After": "7200"})]
    poller = _poller(db, lambda request: responses.pop(0), clock)
    hw = sources.all_sources(db)[0]   # every 30 minutes

    def wait_minutes():
        return (datetime.fromisoformat(sources.state(db, hw.key)["next_at"]) - clock.now).total_seconds() // 60

    async def main():
        assert (await poller.poll(hw)).status == "error"
        assert wait_minutes() == 30
        assert (await poller.poll(hw)).error.startswith("это не RSS")
        assert wait_minutes() == 60
        result = await poller.poll(hw)
        assert result.error == "HTTP 429" and wait_minutes() == 120
        assert sources.state(db, hw.key)["errors"] == 3

    asyncio.run(main())


def test_round_skips_paused_disabled_and_dedupes_across_feeds(tmp_path):
    db = Database(tmp_path / "t.db")
    clock, asked = Clock(), []

    def handler(request):
        asked.append(request.url.host)
        return httpx.Response(200, content=RSS)

    poller = _poller(db, handler, clock)
    db.set(sources.OVERRIDES, json.dumps({"startit": {"enabled": False}, "infostud": {"url": "https://rss.infostud.com/x"}}))

    async def main():
        db.set(sources.PAUSED, "1")
        assert await poller.poll_due() == []
        db.set(sources.PAUSED, "0")
        results = await poller.poll_due()
        assert [r.source for r in results] == ["helloworld", "infostud"]
        assert results[1].stored == 0       # same titles and companies as HelloWorld
        assert await poller.poll_due() == []  # nothing due yet

    asyncio.run(main())
    assert asked == ["www.helloworld.rs", "rss.infostud.com"]


def test_dry_check_writes_nothing(tmp_path):
    db = Database(tmp_path / "t.db")
    poller = _poller(db, lambda r: httpx.Response(200, content=RSS), Clock())
    result = asyncio.run(poller.poll(sources.all_sources(db)[0], store=False))
    assert result.items == 3 and [j.matched for j in result.new] == [True, False, False]
    assert db.conn.execute("SELECT COUNT(*) FROM feed_jobs").fetchone()[0] == 0
    assert sources.state(db, "helloworld") == {}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


def _store(db, n, source="helloworld"):
    from jobpilot.feeds.parse import FeedItem

    jobs = []
    for i in range(n):
        item = FeedItem(f"{source}-{i}", f"PHP dev {i}", f"https://ex.rs/{source}/{i}", company="Acme", summary="text")
        jobs.append(db.add_feed_job(source, "RS", item, job_hash(item.title, source), True))
    return jobs


def test_notifier_realtime_burst_and_digest(tmp_path):
    db = Database(tmp_path / "t.db")
    bot = FakeBot()
    notifier = Notifier(bot, 1, db)

    async def main():
        db.set("mode", "digest")
        await notifier.feed_jobs_found(_store(db, 2), "HelloWorld.rs")
        assert bot.sent == []
        db.add_post("h", "Chat", "https://t.me/c/1", "Вакансия PHP")
        assert await notifier.send_digest() == 3
        texts = [t for t, _ in bot.sent]
        assert texts[0] == "Дайджест вакансий: 3 шт." and "С сайтов вакансий, Сербия: 2" in texts
        assert db.feed_unsent() == []

        bot.sent.clear()
        db.set("mode", "realtime")
        await notifier.feed_jobs_found(_store(db, 1, "startit"), "Startit")
        text, keyboard = bot.sent[0]
        assert "PHP dev 0 · Acme" in text and "Startit (Сербия)" in text
        assert keyboard.inline_keyboard[0][0].callback_data.startswith("fv:")
        bot.sent.clear()
        await notifier.feed_jobs_found(_store(db, 7, "infostud"), "Infostud")
        assert len(bot.sent) == 2 and bot.sent[0][0] == "Infostud: новых вакансий 7"
        assert db.feed_unsent() == []

    asyncio.run(main())


PASSWORD = "correct horse"


def test_feeds_page_and_settings(tmp_path):
    db = Database(tmp_path / "t.db")
    jobs = _store(db, 2)
    db.conn.execute("UPDATE feed_jobs SET matched = 0, title = 'Designer <b>' WHERE id = ?", (jobs[1].id,))
    site = Site(db=db, password=PASSWORD, secret=b"s" * 32, tz="Asia/Yerevan", accounts_path=tmp_path / "none.toml")

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            assert (await client.get("/feeds", allow_redirects=False)).status == 302
            await client.post("/login", data={"password": PASSWORD, "next": "/"}, allow_redirects=False)
            text = await (await client.get("/feeds")).text()
            assert "PHP dev 0" in text and "Designer" not in text and "HelloWorld.rs" in text
            text = await (await client.get("/feeds?show=all&country=RS")).text()
            assert "Designer &lt;b&gt;" in text

            r = await client.post("/api/feeds", json={"include": "PHP, Go", "exclude": "", "paused": True,
                                                       "enabled:startit": False, "interval:helloworld": "45",
                                                       "url:infostud": "https://rss.infostud.com/poslovi/?k=it",
                                                       "extra": "ME | Posao | https://posao.me/rss | 90"})
            assert r.status == 200
            assert sources.words(db, sources.INCLUDE) == ["php", "go"] and sources.paused(db)
            by_key = {s.key: s for s in sources.all_sources(db)}
            assert not by_key["startit"].enabled and by_key["helloworld"].interval == 45
            assert by_key["infostud"].url.endswith("?k=it") and by_key["posao"].country == "ME"
            assert json.loads(db.get(sources.OVERRIDES)) == {
                "helloworld": {"interval": 45}, "startit": {"enabled": False},
                "infostud": {"url": "https://rss.infostud.com/poslovi/?k=it"}}
            assert (await client.post("/api/feeds", json={"interval:helloworld": "5"})).status == 400
            assert (await client.post("/api/feeds", json={"url:infostud": "ftp://x"})).status == 400
            assert (await client.post("/api/feeds", json={"extra": "nonsense"})).status == 400
            assert (await client.post("/api/feeds", data={"paused": "0"})).status == 415

            r = await client.post(f"/api/feeds/jobs/{jobs[0].id}/verdict", json={"verdict": 1})
            assert r.status == 200
            assert db.conn.execute("SELECT verdict FROM feed_jobs WHERE id = ?", (jobs[0].id,)).fetchone()[0] == 1
            assert (await client.post("/api/feeds/jobs/999/verdict", json={"verdict": 1})).status == 404

            sources.save_state(db, "helloworld", {"last_at": datetime.now(timezone.utc).isoformat(),
                                                  "next_at": "2030-01-01T00:00:00+00:00"})
            r = await client.post("/api/feeds/helloworld/now", json={})
            assert r.status == 200
            next_at = datetime.fromisoformat((await r.json())["next_at"])
            assert timedelta(minutes=14) < next_at - datetime.now(timezone.utc) <= timedelta(minutes=15)
            assert (await client.post("/api/feeds/nope/now", json={})).status == 404

    asyncio.run(main())


def test_v4_database_gets_the_feed_table(tmp_path):
    from jobpilot.core.schema import STEPS, VERSION

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for step in STEPS[:4]:
        step(conn)
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == VERSION == 5
    assert db.conn.execute("SELECT COUNT(*) FROM feed_jobs").fetchone()[0] == 0
    assert (tmp_path / "old.db.v4.bak").exists()
