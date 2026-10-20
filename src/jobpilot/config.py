from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required (see .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    tz: str
    folder: str | None
    sources: tuple[str, ...]
    data_dir: Path
    hh_user_agent: str
    hh_token: str | None

    @property
    def session_path(self) -> Path:
        return self.data_dir / "userbot"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobpilot.sqlite3"

    @property
    def accounts_path(self) -> Path:
        return Path(os.environ.get("ACCOUNTS_FILE", "accounts.toml"))

    @classmethod
    def from_env(cls, *, need_bot: bool = True, need_telegram: bool = True) -> "Config":
        """`need_telegram=False` is for the hh commands, which run without any Telegram settings."""
        load_dotenv()
        data_dir = Path(os.environ.get("DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        sources = tuple(s.strip() for s in os.environ.get("TG_SOURCES", "").split(",") if s.strip())
        need_bot = need_bot and need_telegram
        return cls(
            api_id=int(_required("TG_API_ID")) if need_telegram else 0,
            api_hash=_required("TG_API_HASH") if need_telegram else "",
            bot_token=_required("BOT_TOKEN") if need_bot else os.environ.get("BOT_TOKEN", ""),
            owner_id=int(_required("OWNER_ID")) if need_bot else int(os.environ.get("OWNER_ID") or 0),
            tz=os.environ.get("TZ", "Asia/Yerevan"),
            folder=os.environ.get("TG_FOLDER", "").strip() or None,
            sources=sources,
            data_dir=data_dir,
            hh_user_agent=os.environ.get("HH_USER_AGENT", "JobPilot/0.1 (set-your-email@example.com)"),
            hh_token=os.environ.get("HH_APP_TOKEN", "").strip() or None,
        )
