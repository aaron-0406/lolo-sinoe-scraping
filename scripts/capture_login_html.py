"""Helper: open SINOE login page in headed mode and dump HTML + screenshot.

Used to verify selectors empirically before relying on auth/selectors.py.
Usage:
    uv run python scripts/capture_login_html.py
"""

import asyncio
from pathlib import Path

from lolo_sinoe.browser import launch_browser
from lolo_sinoe.config import get_settings
from lolo_sinoe.logging import configure_logging, get_logger


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("scripts.capture")

    fixtures = Path("tests/fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    html_path = fixtures / "login_page.html"
    png_path = fixtures / "login_page.png"

    async with launch_browser(
        headless=False,
        nav_timeout_ms=settings.nav_timeout_ms,
    ) as h:
        log.info("navigating", url=settings.login_url)
        await h.page.goto(settings.login_url, wait_until="networkidle")
        await asyncio.sleep(2)
        html = await h.page.content()
        html_path.write_text(html, encoding="utf-8")
        await h.page.screenshot(path=str(png_path), full_page=True)
        log.info("captured", html=str(html_path), png=str(png_path))


if __name__ == "__main__":
    asyncio.run(main())
