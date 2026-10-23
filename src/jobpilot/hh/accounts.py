from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

from .models import Account

# What the site may change per account; the rest (resumes, search params) stays in accounts.toml.
LIST_FIELDS = ("queries", "include", "exclude", "exclude_employers")
INT_FIELDS = {"start_limit": (1, 200), "max_limit": (1, 200), "per_pass": (1, 50)}  # name -> (min, max)


def load_accounts(path: Path, db=None) -> dict[str, Account]:
    """Accounts from the file; with `db`, the values changed on the site win over the file."""
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    accounts: dict[str, Account] = {}
    for item in raw.get("account", []):
        account = Account(
            name=item["name"],
            queries=tuple(item["queries"]),
            resumes=dict(item["resumes"]),
            include=tuple(item.get("include", ())),
            exclude=tuple(item.get("exclude", ())),
            exclude_employers=tuple(item.get("exclude_employers", ())),
            search_period=item.get("search_period", 1),
            start_limit=item.get("start_limit", 20),
            max_limit=item.get("max_limit", 40),
            ramp_step=item.get("ramp_step", 5),
            per_pass=item.get("per_pass", 8),
            search_every_hours=item.get("search_every_hours", 3),
            search_params={k: str(v) for k, v in item.get("search_params", {}).items()},
            recommended=item.get("recommended", True),
        )
        accounts[account.name] = with_overrides(account, db) if db is not None else account
    return accounts


def _key(name: str) -> str:
    return f"hh_account:{name}"


def overrides(db, name: str) -> dict:
    """Fields set on the site for this account (only the ones that differ from accounts.toml are kept)."""
    try:
        value = json.loads(db.get(_key(name)) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def with_overrides(account: Account, db) -> Account:
    changes = {}
    for field, value in overrides(db, account.name).items():
        if field in LIST_FIELDS and isinstance(value, list):
            changes[field] = tuple(str(v) for v in value)
        elif field in INT_FIELDS and isinstance(value, int):
            changes[field] = value
    return replace(account, **changes)


def set_overrides(db, base: Account, values: dict) -> dict:
    """Store what differs from accounts.toml; an empty dict brings the file's values back."""
    stored = {}
    for field in (*LIST_FIELDS, *INT_FIELDS):
        if field in values:
            value = values[field]
            same = tuple(value) == getattr(base, field) if field in LIST_FIELDS else value == getattr(base, field)
            if not same:
                stored[field] = list(value) if field in LIST_FIELDS else value
    db.set(_key(base.name), json.dumps(stored, ensure_ascii=False))
    return stored


def paused(db, name: str) -> bool:
    """Applications and reminders of this one account are stopped from the site."""
    return db.get(f"hh_paused:{name}") == "1"


def set_paused(db, name: str, value: bool) -> None:
    db.set(f"hh_paused:{name}", "1" if value else "0")
