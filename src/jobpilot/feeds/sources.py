"""Which feeds are read and how often: built-in ones per country, changed and extended on the site.

Kept free of httpx so the site can use it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace

OVERRIDES = "feeds_overrides"   # {"<key>": {"enabled": bool, "interval": minutes, "url": str}} for the built-in feeds
EXTRA = "feeds_extra"           # JSON list of feeds added on the site, same fields as Source
PAUSED = "feeds_paused"         # feeds only; the global "paused" stops them too
INCLUDE = "feeds_include"
EXCLUDE = "feeds_exclude"
STATE = "feed_state:"           # + key: what the poller last saw, see poll.py

MIN_INTERVAL = 15               # minutes; nothing is asked for more often, whatever the settings say
MAX_INTERVAL = 7 * 24 * 60

DEFAULT_WORDS = {
    INCLUDE: ["php", "go", "golang", "laravel", "symfony"],
    EXCLUDE: ["junior", "intern", "internship", "praksa", "praktikant", "стажер", "стажёр"],
}


@dataclass(frozen=True)
class Source:
    key: str            # stable id, stored with every job
    name: str
    country: str        # ISO code, e.g. RS
    url: str
    interval: int = 60  # minutes between reads
    enabled: bool = True
    builtin: bool = False
    note: str = ""


# Serbia (checked on the live feeds 22.10.2026). HelloWorld: ~15-20 latest IT jobs, ttl 15 min. Startit: ~10
# posts, curated, rarely changes. Infostud has no category feeds (only "20 newest" of all ~12 000 jobs and a news
# feed at /poslovi/94), so it is off by default: IT jobs of the Infostud group are on HelloWorld anyway.
BUILTIN: tuple[Source, ...] = (
    Source("helloworld", "HelloWorld.rs", "RS", "https://www.helloworld.rs/rss", 30, builtin=True,
           note="20 последних IT-вакансий, обновляется каждые 15 минут"),
    Source("startit", "Startit Poslovi", "RS", "https://startit.rs/poslovi/feed/", 360, builtin=True,
           note="кураторская доска, 6–10 вакансий"),
    Source("infostud", "Poslovi Infostud", "RS", "https://rss.infostud.com/poslovi/", 60, enabled=False,
           builtin=True, note="20 новейших вакансий всех профессий, без фильтра по IT; IT-вакансии группы Infostud есть на HelloWorld"),
)

COUNTRIES = {"RS": "Сербия", "AM": "Армения", "ME": "Черногория", "DE": "Германия", "PL": "Польша",
             "NL": "Нидерланды", "CY": "Кипр", "GE": "Грузия", "": "без страны"}

_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")


def country_name(code: str) -> str:
    return COUNTRIES.get(code, code)


def clamp_interval(minutes: int) -> int:
    return max(MIN_INTERVAL, min(int(minutes), MAX_INTERVAL))


def valid_url(url: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s/]+\.[^\s/]+(/\S*)?", url)) and len(url) <= 500


def _load(db, key: str, kind: type):
    try:
        value = json.loads(db.get(key) or ("{}" if kind is dict else "[]"))
    except ValueError:
        return kind()
    return value if isinstance(value, kind) else kind()


def overrides(db) -> dict:
    return _load(db, OVERRIDES, dict)


def extra(db) -> list[Source]:
    found = []
    for item in _load(db, EXTRA, list):
        try:
            source = Source(key=str(item["key"]), name=str(item["name"]), country=str(item.get("country", "")),
                            url=str(item["url"]), interval=clamp_interval(item.get("interval", 60)),
                            enabled=bool(item.get("enabled", True)))
        except (KeyError, TypeError, ValueError):
            continue
        if _KEY.fullmatch(source.key) and valid_url(source.url):
            found.append(source)
    return found


def all_sources(db) -> list[Source]:
    """Built-in feeds with the site's changes, then the feeds added on the site."""
    changes = overrides(db)
    result = []
    for source in BUILTIN:
        change = changes.get(source.key) or {}
        url = str(change.get("url") or source.url)
        result.append(replace(
            source,
            enabled=bool(change.get("enabled", source.enabled)),
            interval=clamp_interval(change.get("interval", source.interval)),
            url=url if valid_url(url) else source.url,
        ))
    taken = {s.key for s in result}
    return result + [s for s in extra(db) if s.key not in taken]


def save_extra(db, sources: list[Source]) -> None:
    db.set(EXTRA, json.dumps([{k: v for k, v in asdict(s).items() if k not in ("builtin", "note")}
                              for s in sources], ensure_ascii=False))


def parse_extra_lines(text: str) -> tuple[list[Source], list[str]]:
    """One feed per line: `RS | Name | https://url [| minutes]`, a leading `#` keeps it but switched off.
    Returns the feeds and the bad lines."""
    good, bad = [], []
    builtin = {s.key for s in BUILTIN}
    for line in text.splitlines():
        if not line.strip():
            continue
        enabled = not line.strip().startswith("#")
        parts = [p.strip() for p in line.strip().lstrip("#").split("|")]
        if len(parts) not in (3, 4) or not valid_url(parts[2]) or not re.fullmatch(r"[A-Za-z]{0,3}", parts[0]):
            bad.append(line.strip())
            continue
        try:
            interval = clamp_interval(int(parts[3])) if len(parts) == 4 and parts[3] else 60
        except ValueError:
            bad.append(line.strip())
            continue
        # The key comes from the name, so saving the same lines again keeps the jobs tied to their feed.
        base = re.sub(r"[^a-z0-9]+", "-", parts[1].lower()).strip("-")[:24]
        base = base if _KEY.fullmatch(base) else "feed"
        key, n = base, 1
        while key in builtin or key in {s.key for s in good}:
            n += 1
            key = f"{base}-{n}"
        good.append(Source(key, parts[1][:80] or parts[2], parts[0].upper(), parts[2], interval, enabled))
    return good, bad


def extra_lines(db) -> str:
    return "\n".join(f"{'' if s.enabled else '# '}{s.country} | {s.name} | {s.url} | {s.interval}" for s in extra(db))


def paused(db) -> bool:
    return db.get("paused") == "1" or db.get(PAUSED) == "1"


def words(db, key: str) -> list[str]:
    value = db.get(key)
    if value is None:
        return list(DEFAULT_WORDS[key])
    try:
        found = json.loads(value)
    except ValueError:
        return list(DEFAULT_WORDS[key])
    return [str(w) for w in found] if isinstance(found, list) else list(DEFAULT_WORDS[key])


def state(db, key: str) -> dict:
    return _load(db, STATE + key, dict)


def save_state(db, key: str, value: dict) -> None:
    db.set(STATE + key, json.dumps(value, ensure_ascii=False))
