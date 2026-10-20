import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jobpilot.db import Database
from jobpilot.hh.accounts import load_accounts
from jobpilot.hh.apply import ApplyResult
from jobpilot.hh.limits import can_apply, daily_limit, pause_seconds
from jobpilot.hh.models import Account, Vacancy
from jobpilot.hh.letter import load_template, render_letter
from jobpilot.hh.pipeline import apply_pass, matches
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
    assert accounts["php"].search_params == {"work_format": "REMOTE"}
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
    assert all(60 <= pause_seconds(random.Random(i)) <= 240 for i in range(20))


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
    def __init__(self, vacancies):
        self._vacancies = vacancies

    async def search(self, text, params=None):
        return self._vacancies


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


def test_letter_template_per_account_overrides_shared(tmp_path):
    (tmp_path / "ru.txt").write_text("shared {vacancy}", encoding="utf-8")
    assert load_template(tmp_path, ACCOUNT, "ru") == "shared {vacancy}"
    (tmp_path / "go_ru.txt").write_text("go {vacancy} at {employer}\n", encoding="utf-8")
    template = load_template(tmp_path, ACCOUNT, "ru")
    assert render_letter(template, vacancy(1, "Go dev")) == "go Go dev at E"


def _run(tmp_path, db, vacancies, browser, live=True, notify=print):
    (tmp_path / "ru.txt").write_text("Здравствуйте, {vacancy}", encoding="utf-8")
    (tmp_path / "en.txt").write_text("Hello, {vacancy}", encoding="utf-8")

    async def no_sleep(_):
        return None

    return asyncio.run(apply_pass(
        db=db, account=ACCOUNT, search=FakeSearch(vacancies), browser=browser,
        letters_dir=tmp_path, tz=TZ, live=live, sleep=no_sleep, now=NOON, notify=notify,
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
