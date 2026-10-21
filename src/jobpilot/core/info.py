from __future__ import annotations

from ..config import Config
from .db import Database
from .schema import VERSION

TABLES = ("applications", "stage_history", "chats", "messages", "interviews", "outbox", "tg_posts", "events")


def main() -> None:
    """`jobpilot db-info`: migrate the file if needed and print what is in it."""
    cfg = Config.from_env(need_telegram=False)
    db = Database(cfg.db_path)
    conn = db.conn
    print(f"{cfg.db_path}: schema v{conn.execute('PRAGMA user_version').fetchone()[0]} (code v{VERSION})")
    for table in TABLES:
        print(f"  {table}: {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    print("applications by account, status, stage:")
    for row in conn.execute(
        "SELECT account, status, COALESCE(stage, '-') AS stage, COUNT(*) AS n FROM applications"
        " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ):
        print(f"  {row['account']:<6} {row['status']:<20} {row['stage']:<12} {row['n']}")
    old = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'hh_applications_v1'").fetchone()
    if old:
        print(f"  hh_applications_v1 (old table, kept): {conn.execute('SELECT COUNT(*) FROM hh_applications_v1').fetchone()[0]}")
