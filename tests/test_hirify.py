import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from jobpilot.db import Database
from jobpilot.hirify.client import HirifyError
from jobpilot.hirify.worker import Settings, hirify_pass

TZ = "Asia/Yerevan"
NOON = datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo(TZ))


def card(i, title="Senior Go Developer", direct=True, applied=False):
    return {"vacancy_id": i, "title": title, "slug": f"{i}-go", "can_apply_directly": direct, "has_applied": applied,
            "skills": ["go", "kafka"], "grades": ["senior"], "work_format": ["remote"], "remote_type": "global"}


class FakeHirify:
    def __init__(self, cards, remaining=100, errors=None):
        self.cards, self.remaining, self.errors, self.applied = cards, remaining, errors or {}, []

    async def me(self):
        return {"quota": {"apply": {"remaining": self.remaining}}}

    async def search(self, filter_query, page=1, per_page=50):
        return (self.cards if page == 1 else []), {"last_page": 1}

    async def vacancy(self, vacancy_id):
        return {"company": f"Company {vacancy_id}"}

    async def reveal(self, vacancy_id):
        self.revealed = getattr(self, "revealed", 0) + 1
        return {"company": "Acme", "contacts": [{"type": "telegram", "value": "@hr_acme"},
                                                {"type": "email", "value": "hr@acme.io"}]}

    async def apply(self, slug, profile_id, cover_letter):
        if slug in self.errors:
            raise self.errors[slug]
        self.applied.append((slug, profile_id, cover_letter))
        return {}


def run(db, client, settings=None, now=NOON, live=True):
    async def no_sleep(_):
        pass

    settings = settings or Settings(profile_id=1, exclude=("junior",))
    return asyncio.run(hirify_pass(db, client, settings, "Hello {employer}, about {vacancy}", TZ, live=live,
                                   sleep=no_sleep, now=now))


def test_applies_to_direct_vacancies_and_lists_the_rest(tmp_path):
    db = Database(tmp_path / "t.db")
    client = FakeHirify([card(1), card(2, direct=False), card(3, "Junior Go Developer"), card(4, applied=True)])
    s = run(db, client)
    assert s.applied == 1 and [j.title for j in s.listed] == ["Senior Go Developer"] and s.skipped == 1
    assert client.applied == [("1-go", 1, "Hello Company 1, about Senior Go Developer")]
    app = db.conn.execute("SELECT * FROM applications WHERE source = 'hirify'").fetchone()
    assert app["status"] == "applied" and app["employer"] == "Company 1" and app["stage"] == "sent"
    assert app["url"] == "https://hirify.me/jobs/1-go"
    again = run(db, FakeHirify([card(1), card(2, direct=False)]))
    assert again.applied == 0 and again.listed == []  # nothing twice


def test_limits_hours_and_quota(tmp_path):
    db = Database(tmp_path / "t.db")
    s = run(db, FakeHirify([card(i) for i in range(1, 9)]), Settings(profile_id=1, per_pass=3))
    assert s.applied == 3 and len(s.listed) == 5  # the rest waits as listed vacancies
    db2 = Database(tmp_path / "t2.db")
    night = NOON.replace(hour=23)
    assert run(db2, FakeHirify([card(1)]), now=night).applied == 0
    db3 = Database(tmp_path / "t3.db")
    s = run(db3, FakeHirify([card(1)], remaining=0))
    assert s.applied == 0 and s.stopped == "hirify quota"
    db4 = Database(tmp_path / "t4.db")
    assert run(db4, FakeHirify([card(1)]), Settings(profile_id=None)).applied == 0  # no profile yet


def test_errors(tmp_path):
    db = Database(tmp_path / "t.db")
    client = FakeHirify([card(1), card(2), card(3)], errors={
        "1-go": HirifyError(409, "already", "conflict"), "2-go": HirifyError(422, "bad", "validation_failed"),
        "3-go": HirifyError(429, "slow down", "rate_limited")})
    s = run(db, client)
    statuses = dict(db.conn.execute("SELECT vacancy_id, status FROM applications WHERE source = 'hirify'").fetchall())
    assert statuses == {"1": "already", "2": "failed"} and s.failed == 1 and s.stopped == "rate_limited"


def test_contacts_of_the_rest_are_revealed(tmp_path):
    db = Database(tmp_path / "t.db")
    client = FakeHirify([card(1, direct=False), card(2, direct=False)])
    s = run(db, client, Settings(profile_id=1, reveals_per_pass=1))
    first, second = s.listed
    assert s.revealed == 1 and client.revealed == 1
    assert first.url == "https://t.me/hr_acme" and first.company == "Acme"
    assert "mailto:hr@acme.io" in first.summary and "https://hirify.me/jobs/1-go" in first.summary
    assert second.url == "https://hirify.me/jobs/2-go"  # no reveals left in this pass
    assert run(Database(tmp_path / "t2.db"), FakeHirify([card(3, direct=False)]), live=False).revealed == 0


def test_direct_vacancies_left_by_the_pass_limit_get_applied_later(tmp_path):
    db = Database(tmp_path / "t.db")
    cards = [card(1), card(2), card(3, "Junior Go Developer")]
    first = run(db, FakeHirify(cards), Settings(profile_id=1, per_pass=1, exclude=("junior",)))
    assert first.applied == 1 and [j.title for j in first.listed] == ["Senior Go Developer"]
    second = run(db, FakeHirify(cards), Settings(profile_id=1, per_pass=1, exclude=("junior",)))
    assert second.applied == 1 and second.listed == []
    apps = [r[0] for r in db.conn.execute("SELECT vacancy_id FROM applications WHERE source = 'hirify' ORDER BY 1")]
    assert apps == ["1", "2"]
    assert db.conn.execute("SELECT COUNT(*) FROM feed_jobs WHERE source = 'hirify' AND matched = 1").fetchone()[0] == 0
    assert run(db, FakeHirify(cards), Settings(profile_id=1, exclude=("junior",))).applied == 0  # junior stays out


def test_a_listed_vacancy_applied_to_by_hand_is_left_alone(tmp_path):
    db = Database(tmp_path / "t.db")
    run(db, FakeHirify([card(1)]), Settings(profile_id=1, per_pass=0))
    job_id = db.conn.execute("SELECT id FROM feed_jobs WHERE source = 'hirify'").fetchone()[0]
    db.add_manual_application("feed", "hirify", str(job_id), "Senior Go Developer", "", "u")
    assert run(db, FakeHirify([card(1)])).applied == 0
