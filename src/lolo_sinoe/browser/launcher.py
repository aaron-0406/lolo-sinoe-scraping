"""Async browser launcher with sane defaults for SINOE."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from lolo_sinoe.browser.stealth_args import STEALTH_ARGS
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


@dataclass
class BrowserHandles:
    browser: Browser
    context: BrowserContext
    page: Page


@asynccontextmanager
async def launch_browser(
    *,
    headless: bool = False,
    nav_timeout_ms: int = 30_000,
    storage_state: str | None = None,
) -> AsyncIterator[BrowserHandles]:
    """Launch a Chromium browser with a fresh context and one page.

    Yields a BrowserHandles bundle. Cleans up on exit even if an exception is raised.

    Args:
        headless: Run without UI. Default False to make dev/debugging easier.
        nav_timeout_ms: Default navigation timeout for the page.
        storage_state: Optional path to a saved storage state JSON to reuse cookies.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=STEALTH_ARGS,
        )
        if storage_state is not None:
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="es-PE",
                timezone_id="America/Lima",
                storage_state=storage_state,
            )
        else:
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="es-PE",
                timezone_id="America/Lima",
            )
        context.set_default_navigation_timeout(nav_timeout_ms)
        context.set_default_timeout(nav_timeout_ms)

        page = await context.new_page()

        logger.info(
            "browser_launched",
            headless=headless,
            nav_timeout_ms=nav_timeout_ms,
            reused_storage_state=storage_state is not None,
        )

        try:
            yield BrowserHandles(browser=browser, context=context, page=page)
        finally:
            await context.close()
            await browser.close()
            logger.info("browser_closed")
