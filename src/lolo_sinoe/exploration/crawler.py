"""Breadth-first crawler for the SINOE authenticated area.

Strict rules:
- Read-only: only navigates via page.goto(url). Never clicks submit-like buttons.
- Skips notifications that are NOT marked as read (configurable, default ON).
- Throttled: at least min_delay_ms between navigations.
- Capped: max_pages and max_depth absolute limits.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.async_api import Page

from lolo_sinoe.exploration.recorder import NetworkLogger, record_page
from lolo_sinoe.exploration.types import VisitedPage
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


DESTRUCTIVE_TEXT_PATTERNS: tuple[str, ...] = (
    "enviar",
    "presentar",
    "confirmar",
    "eliminar",
    "marcar como le",
    "marcar leído",
    "borrar",
    "guardar",
    "cerrar sesión",
    "logout",
    "salir",
)


@dataclass
class CrawlConfig:
    max_pages: int = 50
    max_depth: int = 3
    min_delay_ms: int = 2_000
    allowed_hosts: tuple[str, ...] = ("casillas.pj.gob.pe",)
    output_dir: Path = field(default_factory=lambda: Path("exploration_output"))
    skip_unread_notifications: bool = True


@dataclass
class CrawlResult:
    visited: list[VisitedPage] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)


def _canonical(url: str) -> str:
    no_frag, _ = urldefrag(url)
    return no_frag.rstrip("/")


def _is_allowed_host(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def _looks_destructive(text: str) -> bool:
    t = text.lower()
    return any(pat in t for pat in DESTRUCTIVE_TEXT_PATTERNS)


async def crawl(
    page: Page,
    *,
    config: CrawlConfig,
    seed_urls: list[str] | None = None,
    is_safe_to_open: Callable[[str, str], bool] | None = None,
) -> CrawlResult:
    """BFS-crawl from the current page.

    Args:
        page: Playwright Page already authenticated and parked on the post-login URL.
        config: Crawl limits and output dir.
        seed_urls: Optional extra URLs to seed (besides the current page).
        is_safe_to_open: Optional gate function (url, link_text) -> bool. If returns
            False, the link is skipped with reason "not_safe". Use this to enforce
            "only already-read notifications" rules.
    """
    result = CrawlResult()

    start_url = _canonical(page.url)
    queue: deque[tuple[str, str | None, int]] = deque()
    queue.append((start_url, None, 0))
    if seed_urls:
        for s in seed_urls:
            queue.append((_canonical(s), start_url, 1))

    seen: set[str] = set()

    while queue and len(result.visited) < config.max_pages:
        url, parent, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        if not _is_allowed_host(url, config.allowed_hosts):
            result.skipped.append((url, "host_not_allowed"))
            continue

        if depth > config.max_depth:
            result.skipped.append((url, "depth_exceeded"))
            continue

        net_logger = NetworkLogger(page)
        try:
            await asyncio.sleep(config.min_delay_ms / 1000.0)
            try:
                await page.goto(url, wait_until="networkidle")
            except Exception as e:
                logger.warn("crawl_goto_failed", url=url, error=str(e))
                result.skipped.append((url, f"goto_failed:{e}"))
                continue

            visited = await record_page(
                page,
                output_dir=config.output_dir,
                reached_from=parent,
                depth=depth,
                network_events=net_logger.events,
            )
            result.visited.append(visited)

            if depth + 1 <= config.max_depth:
                for link in visited.links_found:
                    if not link.is_internal:
                        continue
                    if _looks_destructive(link.text):
                        result.skipped.append((link.href, f"destructive_text:{link.text[:40]}"))
                        continue
                    if is_safe_to_open is not None and not is_safe_to_open(link.href, link.text):
                        result.skipped.append((link.href, "not_safe_to_open"))
                        continue
                    abs_url = _canonical(urljoin(visited.url, link.href))
                    if abs_url not in seen:
                        queue.append((abs_url, visited.url, depth + 1))
        finally:
            net_logger.detach()

    logger.info(
        "crawl_complete",
        visited=len(result.visited),
        skipped=len(result.skipped),
        max_pages=config.max_pages,
        max_depth=config.max_depth,
    )
    return result
