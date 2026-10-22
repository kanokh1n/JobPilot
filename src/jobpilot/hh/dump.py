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


CHAT_URL = "https://hh.ru/chat"


async def dump_chats(profile_dir: Path, out_dir: Path, open_chats: int = 0) -> None:
    """Save what the hh chat app loads: every JSON answer of its API (chatik.hh.ru) and the page itself.

    The chat list is scrolled to load its next page. Read-only: nothing is typed or sent, but opening a
    chat (`open_chats`) marks it as read on hh.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    async with HHBrowser(profile_dir, out_dir) as browser:
        page = await browser.context.new_page()
        urls: list[str] = []

        async def keep(response) -> None:
            if "chatik" not in response.url or "json" not in (response.headers.get("content-type") or ""):
                return
            try:
                body = await response.text()
            except Exception:  # redirects and aborted requests have no body
                return
            name = f"api_{len(urls):02d}.json"
            (out_dir / name).write_text(body, encoding="utf-8")
            urls.append(f"{name} {response.request.method} {response.url}")

        page.on("response", keep)
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(6000)
        print("chat app url:", page.url.split("?")[0])
        await _save(page, out_dir, "chats_list")

        links = page.locator('a[href*="/chat/"]')
        print("chat links before scrolling:", await links.count())
        for _ in range(4):  # bringing the last chat into view loads the next page of the list
            if not await links.count():
                break
            await links.last.scroll_into_view_if_needed()
            await page.wait_for_timeout(2500)
        print("chat links after scrolling:", await links.count())

        hrefs = []
        for link in await links.all():
            href = await link.get_attribute("href") or ""
            if href and "/chat/-" not in href and href not in hrefs:
                hrefs.append(href)
        for number, href in enumerate(hrefs[:open_chats]):
            await page.goto(href if href.startswith("http") else "https://hh.ru" + href,
                            wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(5000)
            await _save(page, out_dir, f"chat_{number}")
        (out_dir / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
        print("api answers saved:", len(urls))
