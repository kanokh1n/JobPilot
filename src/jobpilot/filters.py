from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

VACANCY_MARKERS = (
    "вакан", "vacanc", "hiring", "we are looking", "we're looking", "looking for",
    "ищем", "требуется", "в команду", "#job", "#work", "open position",
)


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _has_word(text: str, keyword: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(_norm(keyword)) + r"(?!\w)"
    return re.search(pattern, _norm(text)) is not None


@dataclass
class Filters:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    require_marker: bool = True
    min_length: int = 80

    def match(self, text: str) -> bool:
        low = _norm(text)
        if len(low) < self.min_length:
            return False
        if self.require_marker and not any(m in low for m in VACANCY_MARKERS):
            return False
        if any(_has_word(low, kw) for kw in self.exclude):
            return False
        if self.include and not any(_has_word(low, kw) for kw in self.include):
            return False
        return True


def text_hash(text: str) -> str:
    """Hash of the normalized start of a post, so reposts across channels collapse."""
    normalized = re.sub(r"[^\w]+", " ", text.lower()).strip()[:500]
    return hashlib.sha1(normalized.encode()).hexdigest()


def split_keywords(raw: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"[,\n]", raw) if part.strip()]
