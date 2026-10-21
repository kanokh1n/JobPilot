from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import selectors as sel
from .models import Vacancy

log = logging.getLogger(__name__)


STATE_FILE = "state.json"


class ApplyResult(str, Enum):
    APPLIED = "applied"
    ALREADY = "already"
    NEEDS_TEST = "needs_test"
    CAPTCHA = "captcha"
    FAILED = "failed"


class CaptchaError(Exception):
    """hh showed a captcha instead of the page: stop touching hh with this session."""


class HHBrowser:
    """One persistent Chromium profile per hh account, so the login survives restarts."""

    def __init__(self, profile_dir: Path, screenshots_dir: Path, headless: bool = True) -> None:
        self._profile_dir = profile_dir
        self._screenshots_dir = screenshots_dir
        self.screenshots_dir = screenshots_dir
        self._headless = headless
        self._pw = None
        self.context: BrowserContext | None = None
        self.last_captcha_shot: Path | None = None  # screenshot of the last captcha seen, for Telegram

    async def __aenter__(self) -> "HHBrowser":
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            str(self._profile_dir), headless=self._headless, locale="ru-RU",
            channel=os.environ.get("HH_BROWSER_CHANNEL") or None,  # e.g. "chrome" to use installed Chrome
        )
        state = self._profile_dir / STATE_FILE
        if state.exists():  # cookies exported from another machine (the browser profile itself is not portable)
            await self.context.add_cookies(json.loads(state.read_text())["cookies"])
        return self

    async def __aexit__(self, *exc) -> None:
        await self.context.close()
        await self._pw.stop()

    async def export_state(self) -> Path:
        """Write the session cookies to a portable file; the server loads it on start."""
        path = self._profile_dir / STATE_FILE
        await self.context.storage_state(path=str(path))
        path.chmod(0o600)
        return path

    async def has_captcha(self, page: Page, label: str) -> bool:
        """True when the page is a captcha; keeps a screenshot and the HTML so the selector can be checked."""
        if not await page.locator(sel.CAPTCHA).count():
            return False
        shot = self._screenshots_dir / f"captcha_{label}.png"
        try:
            await page.screenshot(path=str(shot))
            shot.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            self.last_captcha_shot = shot
        except Exception:
            log.exception("could not save the captcha page %s", page.url)
        log.warning("captcha on %s", page.url)
        return True

    async def is_logged_in(self, page: Page) -> bool:
        await page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        # Not signed in: hh redirects to the login page. The menu marker is unverified, so go by the URL.
        return "/account/login" not in page.url and "/login" not in page.url.split("?")[0]

    async def apply(self, vacancy: Vacancy, resume_title: str, letter: str) -> ApplyResult:
        page = await self.context.new_page()
        try:
            await page.goto(vacancy.url, wait_until="domcontentloaded")
            if await self.has_captcha(page, vacancy.id):
                return ApplyResult.CAPTCHA
            # An applied vacancy shows "again"/"view topic" instead of the respond button.
            if not await page.locator(sel.RESPOND_BUTTON).count():
                return ApplyResult.ALREADY
            await page.locator(sel.RESPOND_BUTTON).first.click()
            # Between the click and the letter window hh may ask about a vacancy in another country.
            confirm = page.get_by_role("button", name=sel.ANOTHER_COUNTRY_CONFIRM)
            try:
                await confirm.wait_for(timeout=4_000)
                await confirm.click()
            except PlaywrightTimeout:
                pass
            try:
                # The letter field is either open or hidden behind an "add cover letter" button.
                await page.locator(f'{sel.LETTER_INPUT}, {sel.POPUP_CLOSE}').first.wait_for(timeout=10_000)
                if not await page.locator(sel.LETTER_INPUT).count():
                    await page.get_by_role("button", name=sel.ADD_LETTER).click()
                    await page.wait_for_selector(sel.LETTER_INPUT, timeout=5_000)
            except PlaywrightTimeout:
                if await self.has_captcha(page, vacancy.id):
                    return ApplyResult.CAPTCHA
                if "test" in page.url:
                    return ApplyResult.NEEDS_TEST
                # No letter window: do not guess that the response went out, check the vacancy page.
                await page.screenshot(path=str(self._screenshots_dir / f"{vacancy.id}_noletter.png"))
                await page.goto(vacancy.url, wait_until="domcontentloaded")
                if await page.locator(sel.RESPOND_BUTTON).count():
                    log.error("no letter window and no response for %s", vacancy.url)
                    return ApplyResult.FAILED
                log.warning("response to %s was sent without a letter", vacancy.url)
                return ApplyResult.APPLIED
            if not await self._pick_resume(page, resume_title):
                await page.locator(sel.POPUP_CLOSE).first.click()
                log.error("resume %r not found in the popup of %s", resume_title, vacancy.url)
                return ApplyResult.FAILED
            await page.fill(sel.LETTER_INPUT, letter)
            await page.locator(sel.SUBMIT_BUTTON).first.click()
            await page.wait_for_timeout(2500)
            if await self.has_captcha(page, vacancy.id):
                return ApplyResult.CAPTCHA
            if await page.locator(sel.LETTER_INPUT).count():
                return ApplyResult.NEEDS_TEST  # the popup is still open: hh wants something more (test, questions)
            return ApplyResult.APPLIED
        except Exception:
            log.exception("apply to %s failed", vacancy.url)
            await page.screenshot(path=str(self._screenshots_dir / f"{vacancy.id}.png"))
            return ApplyResult.FAILED
        finally:
            await page.close()

    async def _pick_resume(self, page: Page, resume_title: str) -> bool:
        """Make `resume_title` the selected resume of the response popup; False when it cannot."""
        selected = page.locator(sel.RESUME_OPTION).first
        if resume_title.lower() in (await selected.inner_text()).lower():
            return True
        await selected.click()
        await page.wait_for_timeout(500)
        option = page.locator(f'[role="dialog"] >> text="{resume_title}"').last
        if not await option.count():
            return False
        await option.click()
        await page.wait_for_timeout(500)
        return resume_title.lower() in (await page.locator(sel.RESUME_OPTION).first.inner_text()).lower()


async def login_interactive(profile_dir: Path, screenshots_dir: Path) -> None:
    """Open a visible browser; the user signs in by hand (SMS code, captcha), then presses Enter."""
    async with HHBrowser(profile_dir, screenshots_dir, headless=False) as browser:
        page = await browser.context.new_page()
        try:
            await page.goto("https://hh.ru/account/login", wait_until="domcontentloaded", timeout=90_000)
        except PlaywrightTimeout:
            print("hh.ru is slow to open. Look at the browser window: sign in there if the page appears.")
        input("Sign in to hh.ru in the opened window, then press Enter here... ")
        logged_in = await browser.is_logged_in(page)
        print("Logged in:", logged_in)
        if logged_in:
            print("Session saved to", await browser.export_state())
