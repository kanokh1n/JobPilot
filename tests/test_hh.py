import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jobpilot.db import Database
from jobpilot.hh.accounts import load_accounts
from jobpilot.hh.apply import ApplyResult, prune_screenshots
from jobpilot.hh.apply import CaptchaError
from jobpilot.hh.limits import can_apply, captcha_cooldown_hours, daily_limit, pause_seconds
from jobpilot.hh.models import Account, Vacancy
from jobpilot.hh.letter import load_template, render_letter
from jobpilot.hh.pipeline import apply_pass, captcha_hold, clear_captcha, matches
from jobpilot.hh.search import parse_vacancy

ITEM = {
    "id": "123",
    "name": "PHP Developer",
    "alternate_url": "https://hh.ru/vacancy/123",
    "employer": {"name": "Acme"},
    "snippet": {"requirement": "<highlighttext>PHP</highlighttext> 3+ years", "responsibility": None},
    "published_at": "2026-10-19T10:00:00+0300",
    "has_test": False,
}

TZ = "Asia/Yerevan"
NOON = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo(TZ))
ACCOUNT = Account(name="go", queries=("Go",), resumes={"ru": "Go-разработчик", "en": "Go Developer"},
                  include=("go", "golang", "go lang"), exclude=("стажер", "джуниор", "junior"),
                  start_limit=2, max_limit=4, ramp_step=1)


def test_parse_vacancy_strips_markup():
    vacancy = parse_vacancy(ITEM)
    assert vacancy.id == "123" and vacancy.employer == "Acme"
    assert vacancy.snippet == "PHP 3+ years"


def test_example_accounts_file_loads():
    accounts = load_accounts(Path(__file__).parent.parent / "accounts.example.toml")
    assert set(accounts) == {"php", "go"}
    assert accounts["go"].resumes["en"] == "Go Developer"
    assert accounts["php"].search_params == {} and accounts["php"].recommended
    assert "golang" in accounts["go"].include and "junior" in accounts["go"].exclude


def test_limit_ramps_up_and_caps():
    first = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert daily_limit(ACCOUNT, first, first) == 2
    assert daily_limit(ACCOUNT, first, first + timedelta(days=1)) == 3
    assert daily_limit(ACCOUNT, first, first + timedelta(days=30)) == 4


def test_can_apply_respects_hours_and_limit():
    assert can_apply(NOON, 0, 2)
    assert not can_apply(NOON, 2, 2)
    assert not can_apply(NOON.replace(hour=3), 0, 2)


def test_pause_is_human_like():
    assert all(60 <= pause_seconds(random.Random(i)) <= 180 for i in range(20))


def test_reminders_only_for_old_unanswered(tmp_path):
    db = Database(tmp_path / "t.db")
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    db.hh_record("php", "1", "old", "A", "u1", "ru", "", "applied", now - timedelta(days=3))
    db.hh_record("php", "2", "fresh", "B", "u2", "ru", "", "applied", now - timedelta(hours=5))
    db.hh_record("php", "3", "answered", "C", "u3", "ru", "", "applied", now - timedelta(days=3))
    db.hh_set_status("php", "3", "replied")
    due = db.hh_due_reminders("php", now - timedelta(days=2))
    assert [row["vacancy_id"] for row in due] == ["1"]
    db.hh_mark_reminded("php", "1", now)
    assert db.hh_due_reminders("php", now - timedelta(days=2)) == []


class FakeSearch:
    def __init__(self, vacancies, captcha=False):
        self._vacancies = vacancies
        self._captcha = captcha

    async def search(self, text, params=None, period_days=1):
        if self._captcha:
            raise CaptchaError("search")
        return self._vacancies

    async def recommended(self, resume_title):
        return []


class FakeBrowser:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def apply(self, vacancy, resume_title, letter):
        self.calls.append((vacancy.id, resume_title, letter))
        return self._results.pop(0)


def vacancy(id, name, snippet=""):
    return Vacancy(str(id), name, "E", f"https://hh.ru/vacancy/{id}", snippet, "")


def _vacancies(n):
    return [vacancy(i, f"Go developer {i}") for i in range(n)]


def test_keyword_rules_from_the_user():
    assert matches(ACCOUNT, vacancy(1, "Senior Go Developer"))
    assert matches(ACCOUNT, vacancy(2, "Backend (Golang)"))
    assert matches(ACCOUNT, vacancy(3, "Разработчик Go Lang"))
    assert not matches(ACCOUNT, vacancy(4, "Junior Go Developer"))
    assert not matches(ACCOUNT, vacancy(5, "Стажёр Go"))  # ё equals е
    assert not matches(ACCOUNT, vacancy(6, "Джуниор Golang разработчик"))
    assert not matches(ACCOUNT, vacancy(7, "Java Developer", "Google Cloud"))  # "go" is not inside "Google"
    assert not matches(ACCOUNT, vacancy(8, "Team Lead Go"))
    assert matches(ACCOUNT, vacancy(9, "Go developer", "Работа в команде с тимлидом"))  # lead only counts in the title


def test_letter_template_per_account_overrides_shared(tmp_path):
    (tmp_path / "ru.txt").write_text("shared {vacancy}", encoding="utf-8")
    assert load_template(tmp_path, ACCOUNT, "ru") == "shared {vacancy}"
    (tmp_path / "go_ru.txt").write_text("go {vacancy} at {employer}\n", encoding="utf-8")
    template = load_template(tmp_path, ACCOUNT, "ru")
    assert render_letter(template, vacancy(1, "Go dev")) == "go Go dev at E"


def _run(tmp_path, db, vacancies, browser, live=True, notify=print, account=ACCOUNT, search=None, now=NOON):
    (tmp_path / "ru.txt").write_text("Здравствуйте, {vacancy}", encoding="utf-8")
    (tmp_path / "en.txt").write_text("Hello, {vacancy}", encoding="utf-8")

    async def no_sleep(_):
        return None

    return asyncio.run(apply_pass(
        db=db, account=account, search=search or FakeSearch(vacancies), client=browser,
        letters_dir=tmp_path, tz=TZ, live=live, sleep=no_sleep, now=now, notify=notify,
    ))


def test_apply_pass_stops_at_daily_limit_and_uses_selected_resume(tmp_path):
    db = Database(tmp_path / "t.db")
    db.set("resume_lang", "en")
    browser = FakeBrowser([ApplyResult.APPLIED] * 5)
    summary = _run(tmp_path, db, _vacancies(5), browser)
    assert summary.applied == 2 and summary.stopped
    assert browser.calls[0] == ("0", "Go Developer", "Hello, Go developer 0")


def test_apply_pass_skips_seen_and_non_matching(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("go", "0", "v0", "E", "u", "ru", "", "applied")
    vacancies = [vacancy(0, "Go dev"), vacancy(1, "Junior Go"), vacancy(2, "Go dev 2")]
    browser = FakeBrowser([ApplyResult.APPLIED])
    summary = _run(tmp_path, db, vacancies, browser)
    assert [c[0] for c in browser.calls] == ["2"]
    assert summary.skipped == 1 and summary.applied == 1
    assert db.hh_seen("go", "1")  # remembered, so it is not re-checked tomorrow


def test_captcha_stops_the_account(tmp_path):
    db = Database(tmp_path / "t.db")
    messages = []
    browser = FakeBrowser([ApplyResult.CAPTCHA, ApplyResult.APPLIED])
    summary = _run(tmp_path, db, _vacancies(3), browser, notify=messages.append)
    assert summary.stopped == "captcha" and len(browser.calls) == 1 and messages
    assert not db.hh_seen("go", "0")  # the vacancy is tried again after the cooldown
    until = captcha_hold(db, NOON)
    assert until == NOON + timedelta(hours=3)
    assert captcha_hold(db, NOON + timedelta(hours=4)) is None


def test_captcha_cooldown_grows_and_a_success_resets_it(tmp_path):
    db = Database(tmp_path / "t.db")
    assert [captcha_cooldown_hours(n) for n in (1, 2, 3, 9)] == [3, 12, 24, 24]
    _run(tmp_path, db, [], None, search=FakeSearch([], captcha=True), notify=lambda m: None)
    later = NOON + timedelta(hours=4)
    _run(tmp_path, db, [], None, search=FakeSearch([], captcha=True), notify=lambda m: None, now=later)
    assert captcha_hold(db, later) == later + timedelta(hours=12)
    clear_captcha(db)
    assert captcha_hold(db, later) is None
    _run(tmp_path, db, _vacancies(1), FakeBrowser([ApplyResult.APPLIED]), now=later)
    assert db.get("hh_captcha_strikes") == "0"


def test_captcha_on_search_page_stops_before_any_application(tmp_path):
    db = Database(tmp_path / "t.db")
    browser = FakeBrowser([])
    summary = _run(tmp_path, db, _vacancies(3), browser, search=FakeSearch([], captcha=True), notify=lambda m: None)
    assert summary.stopped == "captcha" and browser.calls == [] and captcha_hold(db, NOON)


def test_per_pass_limit_spreads_the_day(tmp_path):
    from dataclasses import replace

    db = Database(tmp_path / "t.db")
    account = replace(ACCOUNT, start_limit=10, max_limit=10, per_pass=3)
    browser = FakeBrowser([ApplyResult.APPLIED] * 5)
    summary = _run(tmp_path, db, _vacancies(5), browser, account=account)
    assert summary.applied == 3 and summary.stopped == "per-pass limit"


def test_three_failures_in_a_row_stop_but_a_success_resets(tmp_path):
    db = Database(tmp_path / "t.db")
    browser = FakeBrowser([ApplyResult.FAILED, ApplyResult.FAILED, ApplyResult.APPLIED, ApplyResult.FAILED])
    summary = _run(tmp_path, db, _vacancies(4), browser, notify=lambda m: None)
    assert summary.stopped is None and summary.failed == 3 and summary.applied == 1
    browser = FakeBrowser([ApplyResult.FAILED] * 3)
    db2 = Database(tmp_path / "t2.db")
    summary = _run(tmp_path, db2, _vacancies(4), browser, notify=lambda m: None)
    assert summary.stopped == "3 failures in a row" and len(browser.calls) == 3


def test_dry_run_never_touches_the_browser(tmp_path):
    db = Database(tmp_path / "t.db")
    summary = _run(tmp_path, db, _vacancies(2), None, live=False)
    assert len(summary.lines) == 2 and summary.applied == 2


def test_other_account_applying_blocks_the_same_vacancy(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("php", "0", "v0", "E", "u", "ru", "", "applied")
    browser = FakeBrowser([ApplyResult.APPLIED])
    summary = _run(tmp_path, db, [vacancy(0, "Go dev"), vacancy(1, "Go dev 2")], browser)
    assert [c[0] for c in browser.calls] == ["1"] and summary.applied == 1


def test_blocked_employer_is_skipped_whatever_the_vacancy():
    from dataclasses import replace
    from jobpilot.hh.pipeline import matches

    account = replace(ACCOUNT, include=("go",), exclude_employers=("озон", "Evrone", "vk"))
    vacancy = lambda employer: Vacancy(id="1", name="Go developer", employer=employer, url="u", snippet="", published_at="")
    assert matches(account, vacancy("Ozon Tech")) is True
    assert matches(account, vacancy("Озон Банк")) is False
    assert matches(account, vacancy("evrone.ru")) is False
    assert matches(account, vacancy("VK Play")) is False
    assert matches(account, vacancy("Vkusno Tech")) is True


def test_prune_screenshots_keeps_only_the_last_day(tmp_path):
    old, fresh = tmp_path / "1_noletter.png", tmp_path / "captcha_2.html"
    old.write_text("x")
    fresh.write_text("x")
    day_ago = old.stat().st_mtime - 25 * 3600
    os.utime(old, (day_ago, day_ago))
    assert prune_screenshots(tmp_path) == 1
    assert not old.exists() and fresh.exists()
    assert prune_screenshots(tmp_path / "missing") == 0


def test_search_pages_are_loaded_once_per_search_every_hours(tmp_path):
    from dataclasses import replace

    from jobpilot.hh.pipeline import collect_candidates

    class CountingSearch(FakeSearch):
        calls = 0

        async def search(self, text, params=None, period_days=1):
            CountingSearch.calls += 1
            return await super().search(text, params, period_days)

    db = Database(tmp_path / "t.db")
    account = replace(ACCOUNT, search_every_hours=3)
    search = CountingSearch(_vacancies(3))
    first = asyncio.run(collect_candidates(search, account, db, NOON))
    db.hh_record("go", "0", "v0", "E", "u", "ru", "", "applied")
    again = asyncio.run(collect_candidates(search, account, db, NOON + timedelta(hours=2)))
    assert CountingSearch.calls == 1 and len(first) == 3 and [v.id for v in again] == ["1", "2"]
    asyncio.run(collect_candidates(search, account, db, NOON + timedelta(hours=3)))
    assert CountingSearch.calls == 2


def test_hh_daily_limit_stops_without_recording(tmp_path):
    db = Database(tmp_path / "t.db")
    browser = FakeBrowser([ApplyResult.APPLIED, ApplyResult.LIMIT, ApplyResult.APPLIED])
    summary = _run(tmp_path, db, _vacancies(3), browser)
    assert summary.applied == 1 and summary.stopped == "hh daily limit" and len(browser.calls) == 2
    assert not db.hh_seen("go", "1")


def test_recommended_vacancies_join_the_search(tmp_path):
    from jobpilot.hh.pipeline import collect_candidates

    class WithRecommended(FakeSearch):
        titles = []

        async def recommended(self, resume_title):
            self.titles.append(resume_title)
            return [vacancy(9, "Go developer 9"), vacancy(0, "Go developer 0")]

    db = Database(tmp_path / "t.db")
    search = WithRecommended(_vacancies(2))
    found = asyncio.run(collect_candidates(search, ACCOUNT, db, NOON))
    assert [v.id for v in found] == ["0", "1", "9"] and search.titles == ["Go-разработчик", "Go Developer"]
