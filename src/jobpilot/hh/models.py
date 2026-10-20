from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    name: str
    queries: tuple[str, ...]
    resumes: dict[str, str]  # language -> resume title exactly as shown on hh
    include: tuple[str, ...] = ()  # apply only if the title/snippet has any of these words
    exclude: tuple[str, ...] = ()  # never apply if it has any of these
    start_limit: int = 20
    max_limit: int = 40
    ramp_step: int = 5
    search_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Vacancy:
    id: str
    name: str
    employer: str
    url: str
    snippet: str
    published_at: str
    has_test: bool = False
