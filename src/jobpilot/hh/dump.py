from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from . import selectors as sel
from .apply import HHBrowser


async def _save(page: Page, out_dir: Path, name: str) -> None:
    (out_dir / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    print("saved", out_dir / f"{name}.html")


async def _save_frames(page: Page, out_dir: Path, name: str) -> None:
    """The response chat lives in an iframe, which page.content() does not include."""
    for number, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        try:
            (out_dir / f"{name}_frame{number}.html").write_text(await frame.content(), encoding="utf-8")
            print("saved frame", number, frame.url[:90] or "(no url)")
        except Exception as error:  # a frame may navigate away mid-read
            print("skipped frame", number, error)


async def dump_pages(profile_dir: Path, out_dir: Path, vacancy_url: str) -> None:
    """Save the pages the apply code has to understand, so the selectors can be checked offline.

    Nothing is sent to hh: the response window is opened by hand and closed without submitting.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    async with HHBrowser(profile_dir, out_dir, headless=False) as browser:
        page = await browser.context.new_page()
        await page.goto(vacancy_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await _save(page, out_dir, "1_vacancy")

        input("Click 'Откликнуться' yourself. The reply chat opens: click 'Добавить сопроводительное' there "
              "(type nothing, do NOT send), then press Enter... ")
        await _save(page, out_dir, "2_response_window")
        await _save_frames(page, out_dir, "2_response_window")

        await page.goto(sel.NEGOTIATIONS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await _save(page, out_dir, "3_negotiations")

        input("Click 'Перейти в чат' on any response, then the 'open in new tab' icon of the chat, "
              "wait for the chat tab to load, then press Enter... ")
        for number, tab in enumerate(browser.context.pages):
            if tab.url.startswith("http") and "chatik" in tab.url:
                await _save(tab, out_dir, f"4_chat_{number}")
                print("chat tab url:", tab.url.split("?")[0])

        await page.goto(sel.RESUMES_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await _save(page, out_dir, "5_resumes")
