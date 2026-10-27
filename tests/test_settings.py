import asyncio
import json
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from jobpilot.db import Database
from jobpilot.hh import accounts as hh_accounts
from jobpilot.hh.pipeline import captcha_hold, record_captcha
from jobpilot.tg import sources
from jobpilot.web.app import Site, build_app

PASSWORD = "correct horse"
TOML = """
[[account]]
name = "php"
queries = ["PHP backend", "Laravel"]
resumes = { ru = "PHP-разработчик", en = "PHP Developer" }
include = ["php"]
exclude = ["junior"]
start_limit = 20
max_limit = 40
per_pass = 8
"""


def _site(tmp_path):
    path = tmp_path / "accounts.toml"
    path.write_text(TOML)
    db = Database(tmp_path / "t.db")
    return Site(db=db, password=PASSWORD, secret=b"s" * 32, tz="Asia/Yerevan", accounts_path=path,
                tg_folder="Вакансии", tg_sources=("@from_env",))


def _run(tmp_path, scenario):
    site = _site(tmp_path)

    async def main():
        async with TestClient(TestServer(build_app(site))) as client:
            r = await client.post("/login", data={"password": PASSWORD, "next": "/"}, allow_redirects=False)
            assert r.status == 302
            await scenario(client, site.db, site)

    asyncio.run(main())


def test_overrides_keep_only_the_changes(tmp_path):
    site = _site(tmp_path)
    base = hh_accounts.load_accounts(site.accounts_path)["php"]
    stored = hh_accounts.set_overrides(site.db, base, {"max_limit": 25, "per_pass": 8, "include": ["php"],
                                                       "exclude_employers": ["Acme"]})
    assert stored == {"max_limit": 25, "exclude_employers": ["Acme"]}
    account = hh_accounts.load_accounts(site.accounts_path, site.db)["php"]
    assert account.max_limit == 25 and account.exclude_employers == ("Acme",) and account.per_pass == 8
    hh_accounts.set_overrides(site.db, base, {})
    assert hh_accounts.load_accounts(site.accounts_path, site.db)["php"] == base
    site.db.set("hh_account:php", "not json")
    assert hh_accounts.with_overrides(base, site.db) == base


def test_telegram_page_and_settings(tmp_path):
    async def scenario(client, db, site):
        db.add_post("h1", "Remote jobs", "https://t.me/remote/1", "Ищем PHP разработчика в команду " * 3)
        db.add_post("h2", "Go chat", "https://t.me/go/2", "Вакансия Go <script>" + "x" * 500)
        sources.save_seen(db, ["Remote jobs", "Go chat"], ["@gone"])
        text = await (await client.get("/jobs?src=tg")).text()
        assert "Remote jobs" in text and "&lt;script&gt;" in text and "Ищем PHP разработчика" in text
        text = await (await client.get("/settings/telegram")).text()
        assert "@gone" in text and "@from_env" in text and 'value="Вакансии"' in text
        assert "Ищем PHP" in await (await client.get("/jobs?st=new")).text()

        r = await client.post("/api/telegram", json={"mode": "realtime", "digest_time": "08:30",
                                                       "include": "PHP, Go\ngolang", "exclude": "", "paused": True})
        assert r.status == 200
        assert db.get("mode") == "realtime" and db.get("digest_time") == "08:30"
        assert db.filters().include == ["php", "go", "golang"] and db.filters().exclude == []
        assert sources.paused(db)
        assert (await client.post("/api/telegram", json={"digest_time": "25:00"})).status == 400
        assert (await client.post("/api/telegram", json={"mode": "often"})).status == 400

        r = await client.post("/api/telegram", json={"folder": "", "sources": "https://t.me/remote_jobs\n@more, -1001234567"})
        assert r.status == 200
        assert sources.extra(db) == ["@remote_jobs", "@more", "-1001234567"]
        assert sources.folder("Вакансии", db) is None
        r = await client.post("/api/telegram", json={"sources": "https://t.me/+secret"})
        assert r.status == 400 and "t.me/+secret" in await r.text()

        assert (await client.post("/api/telegram/posts/1/verdict", json={"verdict": 1})).status == 200
        liked = await (await client.get("/jobs?src=tg&st=liked")).text()
        assert "Remote jobs" in liked and "Вакансия Go" not in liked
        assert (await client.post("/api/telegram/posts/1/verdict", json={"verdict": None})).status == 200
        assert db.conn.execute("SELECT verdict FROM tg_posts WHERE id = 1").fetchone()[0] is None
        assert (await client.post("/api/telegram/posts/99/verdict", json={"verdict": 0})).status == 404
        by_source = await (await client.get("/jobs?src=tg&from=Go+chat")).text()
        assert "Вакансия Go" in by_source and "Ищем PHP" not in by_source

        assert (await client.post("/api/telegram/digest", json={})).status == 200
        assert db.get(sources.DIGEST_REQUEST) == "1"
        # a form post from another site is refused
        assert (await client.post("/api/telegram", data={"mode": "digest"})).status == 415

    _run(tmp_path, scenario)


def test_settings_page_and_account_api(tmp_path):
    async def scenario(client, db, site):
        assert "/settings/hh/php" in await (await client.get("/settings")).text()
        text = await (await client.get("/settings/hh/php")).text()
        assert "PHP backend\nLaravel" in text and "изменено на сайте" not in text
        assert (await client.get("/settings/hh/nobody")).status == 404

        r = await client.post("/api/settings/account/php", json={
            "paused": True, "start_limit": "10", "max_limit": "30", "per_pass": "5",
            "queries": "PHP backend\nSymfony, Laravel", "include": "php", "exclude": "Junior, стажёр",
            "exclude_employers": "Acme, Beta Group"})
        assert r.status == 200
        account = site.configured_accounts()["php"]
        assert account.max_limit == 30 and account.per_pass == 5 and account.start_limit == 10
        assert account.queries == ("PHP backend", "Symfony, Laravel")
        assert account.exclude == ("junior", "стажёр") and account.exclude_employers == ("Acme", "Beta Group")
        assert hh_accounts.paused(db, "php")
        assert "include" not in json.loads(db.get("hh_account:php"))  # same as the file
        text = await (await client.get("/settings/hh/php")).text()
        assert "изменено на сайте" in text
        assert "На паузе отклики аккаунтов: php" in await (await client.get("/")).text()

        assert (await client.post("/api/settings/account/php", json={"max_limit": 5})).status == 400  # < start
        assert (await client.post("/api/settings/account/php", json={"per_pass": 0})).status == 400
        assert (await client.post("/api/settings/account/php", json={"queries": ""})).status == 400
        assert (await client.post("/api/settings/account/nobody", json={})).status == 404
        # a partial save keeps the other changes
        assert (await client.post("/api/settings/account/php", json={"per_pass": 6})).status == 200
        assert site.configured_accounts()["php"].max_limit == 30

        assert (await client.post("/api/settings/account/php", json={"reset": True, "paused": False})).status == 200
        assert site.configured_accounts()["php"] == site.configured_accounts(file_only=True)["php"]
        assert not hh_accounts.paused(db, "php")

        assert (await client.post("/api/settings", json={"paused": True, "resume_lang": "en"})).status == 200
        assert db.get("paused") == "1" and db.get("resume_lang") == "en" and sources.paused(db)
        assert (await client.post("/api/settings", json={"resume_lang": "de"})).status == 400

        record_captcha(db, datetime.now(timezone.utc))
        assert "Снять паузу hh" in await (await client.get("/settings/captcha")).text()
        assert (await client.post("/api/hh/captcha/clear", json={})).status == 200
        assert captcha_hold(db, datetime.now(timezone.utc) + timedelta(minutes=1)) is None

    _run(tmp_path, scenario)


def test_scheduler_respects_account_pause(tmp_path):
    from jobpilot.hh.scheduler import _on_hold

    site = _site(tmp_path)

    class Cfg:
        tz = "Asia/Yerevan"

    assert not _on_hold(site.db, Cfg, "apply/php", "php")
    hh_accounts.set_paused(site.db, "php", True)
    assert _on_hold(site.db, Cfg, "apply/php", "php")
    assert not _on_hold(site.db, Cfg, "chats/php")  # chats keep being read


def test_scheduler_respects_hh_switches(tmp_path):
    from jobpilot.hh.scheduler import _on_hold

    site = _site(tmp_path)

    class Cfg:
        tz = "Asia/Yerevan"

    site.db.set(hh_accounts.APPLY_PAUSED, "1")
    assert _on_hold(site.db, Cfg, "apply/php", "php") and _on_hold(site.db, Cfg, "remind/php", "php")
    assert not _on_hold(site.db, Cfg, "chats/php") and not _on_hold(site.db, Cfg, "refresh/php")
    site.db.set(hh_accounts.APPLY_PAUSED, "0")
    site.db.set(hh_accounts.HH_OFF, "1")
    assert _on_hold(site.db, Cfg, "apply/php", "php") and _on_hold(site.db, Cfg, "chats/php")
    assert _on_hold(site.db, Cfg, "refresh/php")


def test_hh_switches_from_the_site(tmp_path):
    async def scenario(client, db, site):
        text = await (await client.get("/settings")).text()
        assert "Пауза откликов" in text and "Выключить hh" in text
        assert "Пауза откликов" in await (await client.get("/jobs?src=php")).text()

        assert (await client.post("/api/settings", json={"hh_apply_paused": True})).status == 200
        assert hh_accounts.apply_paused(db) and not hh_accounts.hh_off(db)
        assert "Отклики hh на паузе у всех аккаунтов" in await (await client.get("/")).text()

        assert (await client.post("/api/settings", json={"hh_off": True})).status == 200
        assert hh_accounts.hh_off(db)
        for page in ("/", "/jobs?src=php", "/chats", "/settings"):
            text = await (await client.get(page)).text()
            assert "hh выключен" in text and "Включить hh" in text, page

        assert (await client.post("/api/settings", json={"hh_off": False, "hh_apply_paused": False})).status == 200
        assert not hh_accounts.hh_off(db) and not hh_accounts.apply_paused(db)
        assert "hh выключен" not in await (await client.get("/")).text()

    _run(tmp_path, scenario)
