from __future__ import annotations

import html
import re

from .models import Vacancy

TAG_RE = re.compile(r"<[^>]+>")


def parse_vacancy(item: dict) -> Vacancy:
    """A vacancy of the hh API search answer."""
    snippet = item.get("snippet") or {}
    text = " ".join(filter(None, [snippet.get("requirement"), snippet.get("responsibility")]))
    return Vacancy(
        id=str(item["id"]),
        name=item["name"],
        employer=(item.get("employer") or {}).get("name", ""),
        url=item["alternate_url"],
        snippet=html.unescape(TAG_RE.sub("", text)),
        published_at=item.get("published_at", ""),
        has_test=bool(item.get("has_test")),
    )
