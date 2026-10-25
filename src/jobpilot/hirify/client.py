"""Hirify Agent API (https://api.hirify.me/api/agent, contract: /openapi.json), with the personal API key."""
from __future__ import annotations

from urllib.parse import parse_qsl

import httpx

API_URL = "https://api.hirify.me/api/agent"
SITE_URL = "https://hirify.me/jobs/"


class HirifyError(Exception):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.code = status, code


class HirifyClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=API_URL, timeout=60,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw) -> tuple[object, dict]:
        response = await self._client.request(method, path, **kw)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or body.get("ok") is False:
            error = body.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code", "") if isinstance(error, dict) else ""
            raise HirifyError(response.status_code, message or response.text[:200], code)
        return body.get("data"), body.get("meta") or {}

    async def me(self) -> dict:
        return (await self._call("GET", "/me"))[0]

    async def profiles(self) -> list[dict]:
        return (await self._call("GET", "/profiles"))[0] or []

    async def search(self, filter_query: str, page: int = 1, per_page: int = 50) -> tuple[list[dict], dict]:
        """Compact vacancy cards of a filter query string (see filters.guide), newest pages first."""
        params = [*parse_qsl(filter_query), ("page", page), ("per_page", per_page)]
        data, meta = await self._call("GET", "/vacancies", params=params)
        return data or [], meta

    async def vacancy(self, vacancy_id: int | str) -> dict:
        """The full vacancy; counts against the daily read allowance."""
        return (await self._call("GET", f"/vacancies/{vacancy_id}"))[0]

    async def reveal(self, vacancy_id: int | str) -> dict:
        """Where to apply to a vacancy Hirify does not take applications for; counts against contact reveals."""
        return (await self._call("POST", f"/vacancies/{vacancy_id}/reveal"))[0] or {}

    async def apply(self, slug: str, profile_id: int, cover_letter: str) -> dict:
        """Apply through Hirify; costs one application unit."""
        data, _ = await self._call("POST", f"/vacancies/{slug}/apply",
                                   json={"profile_id": profile_id, "cover_letter": cover_letter})
        return data or {}
