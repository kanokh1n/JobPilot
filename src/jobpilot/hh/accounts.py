from __future__ import annotations

import tomllib
from pathlib import Path

from .models import Account


def load_accounts(path: Path) -> dict[str, Account]:
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
            search_params={k: str(v) for k, v in item.get("search_params", {}).items()},
        )
        accounts[account.name] = account
    return accounts
