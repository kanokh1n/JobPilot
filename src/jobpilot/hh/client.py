"""hh.ru through hh-applicant-tool by Sergey M. (https://github.com/s3rgeym/hh-applicant-tool), used unchanged.

The library talks to hh like the official Android app: the app's API for search, responses and resumes, and
the chat app's XHR API with the site cookies it saves at sign-in. No browser, so the small server is enough.
One HHClient per account; its token and cookies live in data/hh/<account>/ (token.json, cookies.txt), written
by `jobpilot hh-auth`. The library is synchronous, so every call runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from functools import cached_property
from pathlib import Path
from typing import Any

from hh_applicant_tool.api import errors as api_errors
from hh_applicant_tool.errors import CaptchaError as XHRCaptcha
from hh_applicant_tool.tool import HHApplicantTool

from .apply import ApplyResult, CaptchaError
from .models import Vacancy
from .search import parse_vacancy

log = logging.getLogger(__name__)

# Error values of POST /negotiations that mean "nothing to send", not a failure.
ALREADY = {"already_applied"}
NEEDS_TEST = {"test_required", "letter_required"}


class _Tool(HHApplicantTool):
    """The library asks a person for the captcha text (a window or the terminal); the server has nobody to
    ask, so the picture is saved for Telegram and the call gives up with our CaptchaError."""

    captcha_shot: Path | None = None
    last_captcha_shot: Path | None = None

    def _solve_captcha(self, image_data: bytes) -> str:
        if self.captcha_shot is not None:
            try:
                self.captcha_shot.parent.mkdir(parents=True, exist_ok=True)
                self.captcha_shot.write_bytes(image_data)
                self.last_captcha_shot = self.captcha_shot
            except OSError:
                log.exception("could not save the captcha picture")
        raise CaptchaError("hh asked for a captcha")


class HHClient:
    def __init__(self, config_dir: Path, account: str, screenshots_dir: Path | None = None) -> None:
        self.account = account
        self._config_dir = config_dir
        self._screenshots_dir = screenshots_dir
        self._resume_ids: dict[str, str] = {}

    @cached_property
    def tool(self) -> _Tool:
        # captcha_handling=False: the app API raises CaptchaRequired instead of opening the captcha page.
        tool = _Tool(config_dir=self._config_dir, profile_id=self.account, captcha_handling=False)
        tool.create_config_dir()
        if self._screenshots_dir is not None:
            tool.captcha_shot = self._screenshots_dir / f"captcha_{self.account}.png"
        return tool

    @property
    def last_captcha_shot(self) -> Path | None:
        return self.tool.last_captcha_shot

    @last_captcha_shot.setter
    def last_captcha_shot(self, value: Path | None) -> None:
        self.tool.last_captcha_shot = value

    @property
    def signed_in(self) -> bool:
        return bool(self.tool.api_client.access_token)

    async def _run(self, fn, *args, **kwargs):
        """Run a library call in a thread; any captcha comes out as our CaptchaError."""
        def call():
            try:
                return fn(*args, **kwargs)
            except api_errors.CaptchaRequired as error:
                raise CaptchaError(error.captcha_url) from error
            except XHRCaptcha as error:
                raise CaptchaError("hh asked for a captcha") from error
            finally:
                try:
                    self.tool.save_cookies()
                except Exception:
                    log.exception("hh/%s cookies not saved", self.account)

        return await asyncio.to_thread(call)

    async def get(self, endpoint: str, **params) -> dict:
        return await self._run(self.tool.api_client.get, endpoint, params)

    async def me(self) -> dict:
        return await self.get("/me")

    # ------------------------------------------------------------------ search and responses

    async def search(self, text: str, params: dict[str, str] | None = None,
                     period_days: int = 1, max_pages: int = 3) -> list[Vacancy]:
        """Vacancies of a query, newest first, without archived ones, ones we already answered (hh marks them
        with `relations`) and ones that answer on the employer's own site."""
        found: list[Vacancy] = []
        for page in range(max_pages):
            data = await self.get("/vacancies", text=text, period=period_days, per_page=50, page=page,
                                  order_by="publication_time", **(params or {}))
            for item in data.get("items") or []:
                if item.get("archived") or item.get("relations") or item.get("response_url"):
                    continue
                found.append(parse_vacancy(item))
            if page + 1 >= data.get("pages", 1):
                break
        return found

    async def resume_id(self, title: str) -> str | None:
        """Id of the published resume with this title (case does not matter)."""
        if title.lower() not in self._resume_ids:
            data = await self.get("/resumes/mine")
            for resume in data.get("items") or []:
                if (resume.get("status") or {}).get("id") == "published":
                    self._resume_ids[(resume.get("title") or "").lower()] = resume["id"]
        return self._resume_ids.get(title.lower())

    async def apply(self, vacancy: Vacancy, resume_title: str, letter: str) -> ApplyResult:
        try:
            resume = await self.resume_id(resume_title)
            if resume is None:
                log.error("hh/%s: no published resume titled %r", self.account, resume_title)
                return ApplyResult.FAILED
            await self._run(self.tool.api_client.post, "/negotiations",
                            {"resume_id": resume, "vacancy_id": vacancy.id, "message": letter},
                            delay=random.uniform(1, 3))
            return ApplyResult.APPLIED
        except CaptchaError:
            return ApplyResult.CAPTCHA
        except api_errors.LimitExceeded:
            return ApplyResult.LIMIT
        except api_errors.Redirect:
            return ApplyResult.NEEDS_TEST  # hh sends the response to a form on the employer's site
        except api_errors.ApiError as error:
            values = {e.get("value") for e in error.data.get("errors") or [] if isinstance(e, dict)}
            if values & ALREADY:
                return ApplyResult.ALREADY
            if values & NEEDS_TEST:
                return ApplyResult.NEEDS_TEST
            log.error("hh/%s apply to %s: %s", self.account, vacancy.url, error)
            return ApplyResult.FAILED
        except Exception:
            log.exception("hh/%s apply to %s failed", self.account, vacancy.url)
            return ApplyResult.FAILED

    async def negotiations(self, pages: int = 2) -> dict[str, tuple[str, str | None]]:
        """{vacancy_id: (state, chat_id)} of the newest responses.

        The state uses the names of the old negotiations page, which the stages already know: an unanswered
        response is "not-viewed" or "viewed", the others keep hh's id (invitation, interview, discard, hired).
        """
        result: dict[str, tuple[str, str | None]] = {}
        for page in range(pages):
            data = await self.get("/negotiations", page=page, per_page=100)
            for item in data.get("items") or []:
                vacancy = (item.get("vacancy") or {}).get("id")
                if not vacancy:
                    continue
                state = (item.get("state") or {}).get("id") or "unknown"
                if state == "response":
                    state = "viewed" if item.get("viewed_by_opponent") else "not-viewed"
                chat = item.get("chat_id")
                result[str(vacancy)] = (state, str(chat) if chat else None)
            if page + 1 >= data.get("pages", 1):
                break
        return result

    async def raise_resumes(self) -> int:
        """Raise every resume hh lets raise now (once per 4 hours); returns how many went up."""
        data = await self.get("/resumes/mine")
        raised = 0
        for resume in data.get("items") or []:
            if not resume.get("can_publish_or_update"):
                continue
            try:
                await self._run(self.tool.api_client.post, f"/resumes/{resume['id']}/publish")
                raised += 1
            except api_errors.ApiError as error:
                log.warning("hh/%s resume %s not raised: %s", self.account, resume.get("title"), error)
        return raised

    # ------------------------------------------------------------------ chats (chatik.hh.ru)

    @cached_property
    def _chat_host(self) -> str:
        state = self.tool.get_initial_state(self.tool.base_url + "/applicant/my_resumes")
        return state["config"]["externalMicroFrontendHosts"]["chatik"]

    @cached_property
    def _chat_page(self) -> str:
        response = self.tool.session.get(f"{self._chat_host}/?platform=xhh&dest=iframe")
        response.raise_for_status()
        return response.url

    def _xhr(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
             referer: str | None = None) -> dict:
        source = "resume_profile_front"
        answer = self.tool.xhr_request(
            method, self._chat_host + path, params=params, json=json, referer_url=referer or self._chat_page,
            additional_headers={"X-hhtmFrom": source, "X-hhtmFromLabel": source, "X-hhtmSource": "app",
                                "X-hhtmSourceLabel": source},
            xsrf_token=self.tool.xsrf_token,
        )
        status = answer.get("http_status", 200)
        if status in (301, 302, 303, 401, 403):
            raise RuntimeError(f"hh/{self.account}: not signed in to the chats, run jobpilot hh-auth")
        if status >= 300:
            raise RuntimeError(f"{path}: HTTP {status}")
        return answer

    async def chat_list(self, cursor: str | None = None) -> dict:
        """One page of the chat list: {"chats": {"items", "nextFrom"}, "chatsDisplayInfo", "resources"}."""
        params = {"filterUnread": "false", "filterHasTextMessage": "false", "do_not_track_session_events": "true"}
        if cursor:
            params["from"] = cursor
        return await self._run(self._xhr, "GET", "/chatik/api/chats", params=params)

    async def chat_data(self, chat_id: str) -> dict:
        """The chat with all its messages (the chat app pages them, newest page first)."""
        def read() -> dict:
            params: dict[str, Any] = {"chatId": chat_id, "do_not_track_session_events": "true"}
            referer = f"{self._chat_host}/chat/{chat_id}"
            data = self._xhr("GET", "/chatik/api/chat_data", params=params, referer=referer)
            messages = (data.get("chat") or {}).get("messages") or {}
            items = list(messages.get("items") or [])
            while messages.get("hasMore") and items:
                params["lastMessageId"] = items[0]["id"]
                older = self._xhr("GET", "/chatik/api/chat_data", params=params, referer=referer)
                messages = (older.get("chat") or {}).get("messages") or {}
                page = [m for m in messages.get("items") or [] if m["id"] != items[0]["id"]]
                if not page:
                    break
                items = page + items
            if data.get("chat"):
                data["chat"].setdefault("messages", {})["items"] = items
            return data

        return await self._run(read)

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._run(self._xhr, "POST", "/chatik/api/send",
                        json={"chatId": int(chat_id), "text": text, "idempotencyKey": str(uuid.uuid4())},
                        referer=f"{self._chat_host}/chat/{chat_id}")

    async def unread(self) -> int:
        data = await self._run(self._xhr, "GET", "/chatik/api/unread")
        return int(data.get("unreadCount") or 0)


class HHClients:
    """One client per account for the whole worker, so the session cookies and resume ids are reused."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._clients: dict[str, HHClient] = {}

    def get(self, account: str) -> HHClient:
        if account not in self._clients:
            self._clients[account] = HHClient(self._data_dir / "hh", account, self._data_dir / "screenshots")
        return self._clients[account]
