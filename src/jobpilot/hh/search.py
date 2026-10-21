from __future__ import annotations

import asyncio
import html
import logging
import re
from urllib.parse import urlencode

import httpx

from .apply import CaptchaError
from .limits import short_pause_seconds
from .models import Vacancy

log = logging.getLogger(__name__)

API_URL = "https://api.hh.ru"
TAG_RE = re.compile(r"<[^>]+>")


def parse_vacancy(item: dict) -> Vacancy:
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


class HHSearch:
    """Read-only client for the public vacancy search, which hh still serves."""

    def __init__(self, user_agent: str, token: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        headers = {"User-Agent": user_agent, "HH-User-Agent": user_agent}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=30)

    async def search(self, text: str, params: dict[str, str] | None = None,
                     period_days: int = 1, max_pages: int = 3) -> list[Vacancy]:
        found: list[Vacancy] = []
        for page in range(max_pages):
            query = {
                "text": text, "period": period_days, "per_page": 50, "page": page,
                "order_by": "publication_time", **(params or {}),
            }
            response = await self._client.get("/vacancies", params=query)
            response.raise_for_status()
            data = response.json()
            found.extend(parse_vacancy(item) for item in data["items"] if not item.get("archived"))
            if page + 1 >= data.get("pages", 1):
                break
        return found

    async def aclose(self) -> None:
        await self._client.aclose()


class WebSearch:
    """Vacancy search through the hh.ru site in the account's own browser.

    api.hh.ru answers 403 to anonymous clients, so this reads the public search
    page instead. The data-qa names below are UNVERIFIED against the live page.
    """

    ID_RE = re.compile(r"/vacancy/(\d+)")

    def __init__(self, browser, sleep=asyncio.sleep) -> None:
        self._browser = browser
        self._sleep = sleep

    async def search(self, text: str, params: dict[str, str] | None = None,
                     period_days: int = 1, max_pages: int = 3) -> list[Vacancy]:
        found: list[Vacancy] = []
        page = await self._browser.context.new_page()
        try:
            for number in range(max_pages):
                await self._sleep(short_pause_seconds())  # hh flags bursts of page loads
                query = {
                    "text": text, "search_period": period_days, "items_on_page": 50,
                    "page": number, "order_by": "publication_time", **(params or {}),
                }
                await page.goto("https://hh.ru/search/vacancy?" + urlencode(query),
                                wait_until="domcontentloaded", timeout=90_000)
                if await self._browser.has_captcha(page, "search"):
                    raise CaptchaError(page.url)
                try:
                    await page.wait_for_selector('[data-qa="vacancy-serp__vacancy"]', timeout=15_000)
                except Exception:
                    break
                items = await page.locator('[data-qa="vacancy-serp__vacancy"]').all()
                for item in items:
                    link = item.locator('[data-qa="serp-item__title"]').first
                    href = (await link.get_attribute("href")) or ""
                    match = self.ID_RE.search(href)
                    if not match:
                        continue
                    employer = item.locator('[data-qa="vacancy-serp__vacancy-employer"]').first
                    parts = []
                    for qa in ("requirement", "responsibility"):
                        node = item.locator(f'[data-qa="vacancy-serp__vacancy_snippet_{qa}"]').first
                        if await node.count():
                            parts.append(await node.inner_text())
                    found.append(Vacancy(
                        id=match.group(1),
                        name=(await link.inner_text()).strip(),
                        employer=(await employer.inner_text()).strip() if await employer.count() else "",
                        url=f"https://hh.ru/vacancy/{match.group(1)}",
                        snippet=" ".join(parts),
                        published_at="",
                    ))
                if len(items) < 50:
                    break
        finally:
            await page.close()
        return found

    async def aclose(self) -> None:
        return None
