"""Async browser launcher with sane defaults for SINOE."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    StorageState,
    async_playwright,
)

from lolo_sinoe.browser.stealth_args import STEALTH_ARGS
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


@dataclass
class BrowserHandles:
    browser: Browser
    context: BrowserContext
    page: Page


# Defaults compartidos entre contexts — se reusan con o sin storage_state
# para evitar duplicación.
_CONTEXT_DEFAULTS: dict[str, Any] = {
    "viewport": {"width": 1366, "height": 768},
    "locale": "es-PE",
    "timezone_id": "America/Lima",
}


@asynccontextmanager
async def launch_browser(
    *,
    headless: bool = False,
    nav_timeout_ms: int = 30_000,
    storage_state: str | dict[str, Any] | None = None,
) -> AsyncIterator[BrowserHandles]:
    """Launch a Chromium browser with a fresh context and one page.

    Yields a BrowserHandles bundle. Cleans up on exit even if an exception is raised.

    Args:
        headless: Run without UI. Default False to make dev/debugging easier.
        nav_timeout_ms: Default navigation timeout for the page.
        storage_state: Optional storage state to reuse cookies — accepts a path
            (str) for CLI dev, or a dict (Playwright `StorageState`) for the
            multitenant worker (cached blob descifrado en memoria, nunca toca disco).
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=STEALTH_ARGS)

        context_kwargs: dict[str, Any] = dict(_CONTEXT_DEFAULTS)
        if storage_state is not None:
            # Playwright acepta str (path) o StorageState (dict). El cast es
            # seguro: el dict viene de `BrowserContext.storage_state()`
            # cifrado/descifrado en memoria sin transformaciones.
            context_kwargs["storage_state"] = (
                cast(StorageState, storage_state)
                if isinstance(storage_state, dict)
                else storage_state
            )
        context = await browser.new_context(**context_kwargs)
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
