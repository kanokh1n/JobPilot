import sqlite3
from datetime import datetime, timedelta, timezone

from jobpilot.core import stages
from jobpilot.core.schema import V1, VERSION
from jobpilot.db import Database


def _v1_file(path):
    """A database as the bot left it before v2."""
    conn = sqlite3.connect(path)
    conn.executescript(V1)
    rows = [
        ("php", "1", "PHP dev", "A", "u1", "ru", "letter", "applied", "2026-10-19T08:00:00+00:00", 1),
        ("php", "2", "Bitrix", "B", "u2", "ru", "", "skipped", "2026-10-19T08:01:00+00:00", 0),
        ("go", "3", "Go dev", "C", "u3", "en", "letter", "answered:discard", "2026-10-18T08:00:00+00:00", 0),
        ("go", "4", "Go dev 2", "D", "u4", "ru", "letter", "failed", "2026-10-18T09:00:00+00:00", 0),
    ]
    conn.executemany(
        "INSERT INTO hh_applications (account, vacancy_id, name, employer, url, resume_lang, letter, status,"
        " applied_at, reminders) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.execute("INSERT INTO posts (hash, source, url, text, created_at) VALUES ('h', 's', 'u', 't', 'now')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('resume_lang', 'en')")
    conn.commit()
    conn.close()


def test_v1_file_is_moved_without_losses(tmp_path):
    path = tmp_path / "jobpilot.sqlite3"
    _v1_file(path)
    db = Database(path)
    conn = db.conn
    assert conn.execute("PRAGMA user_version").fetchone()[0] == VERSION
    apps = {r["vacancy_id"]: r for r in conn.execute("SELECT * FROM applications")}
    assert set(apps) == {"1", "2", "3", "4"}
    assert apps["1"]["stage"] == stages.SENT and apps["1"]["reminders"] == 1 and apps["1"]["letter"] == "letter"
    assert apps["2"]["stage"] is None
    assert apps["3"]["stage"] == stages.REJECTED and apps["3"]["hh_state"] == "discard"
    assert apps["4"]["stage"] is None
    assert db.stage_history(apps["1"]["id"])[0]["changed_by"] == "system"
    assert db.get("resume_lang") == "en"
    assert db.stats() == {"total": 1, "pending": 1}
    assert conn.execute("SELECT COUNT(*) FROM hh_applications_v1").fetchone()[0] == 4
    assert (tmp_path / "jobpilot.sqlite3.v0.bak").exists()
    assert db.hh_seen("php", "2") and db.hh_seen_any("3")
    Database(path)  # opening again changes nothing
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 4


def test_fresh_file_needs_no_backup(tmp_path):
    db = Database(tmp_path / "new.sqlite3")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == VERSION
    assert not list(tmp_path.glob("*.bak"))


def test_manual_stage_is_never_overwritten(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("php", "1", "PHP", "A", "u", "ru", "", "applied")
    app_id = db.conn.execute("SELECT id FROM applications").fetchone()["id"]
    db.set_stage(app_id, stages.GHOSTED, "молчат неделю")
    db.hh_sync_states("php", {"1": "viewed"})
    db.hh_set_status("php", "1", "answered:discard")
    row = db.application(app_id)
    assert row["stage"] == stages.GHOSTED and row["hh_state"] == "viewed"
    assert [h["changed_by"] for h in db.stage_history(app_id)] == ["system", "user"]


def test_system_moves_stage_only_forward(tmp_path):
    db = Database(tmp_path / "t.db")
    db.hh_record("go", "1", "Go", "A", "u", "ru", "", "applied")
    app_id = db.conn.execute("SELECT id FROM applications").fetchone()["id"]
    assert db.hh_sync_states("go", {"1": "viewed", "999": "viewed"}) == 1
    db.hh_sync_states("go", {"1": "invitation"})
    db.hh_sync_states("go", {"1": "viewed"})
    assert db.application(app_id)["stage"] == stages.INVITATION
    kinds = [e["kind"] for e in db.events_since(0)]
    assert kinds.count("stage") == 3 and kinds[0] == "application"


def test_reminders_stop_after_a_manual_rejection(tmp_path):
    db = Database(tmp_path / "t.db")
    now = datetime.now(timezone.utc)
    db.hh_record("php", "1", "a", "A", "u1", "ru", "", "applied", now - timedelta(days=3))
    db.hh_record("php", "2", "b", "B", "u2", "ru", "", "applied", now - timedelta(days=3))
    db.hh_sync_states("php", {"2": "viewed"})
    app_id = db.conn.execute("SELECT id FROM applications WHERE vacancy_id = '1'").fetchone()["id"]
    db.set_stage(app_id, stages.REJECTED)
    assert [r["vacancy_id"] for r in db.hh_due_reminders("php", now - timedelta(days=2))] == ["2"]
