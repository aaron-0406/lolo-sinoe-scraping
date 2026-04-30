"""Per-page recording: HTML + screenshot + network log."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Request, Response

from lolo_sinoe.exploration.types import (
    DownloadInfo,
    FormInfo,
    LinkInfo,
    TableInfo,
    VisitedPage,
)
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def url_to_slug(url: str, max_len: int = 100) -> str:
    """Turn a URL into a filesystem-safe slug."""
    slug = _SLUG_RE.sub("_", url).strip("_")
    if len(slug) > max_len:
        slug = slug[:max_len]
    return slug or "page"


class NetworkLogger:
    """Captures requests/responses on a page until detached."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._events: list[dict[str, Any]] = []

        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, req: Request) -> None:
        self._events.append(
            {
                "type": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "headers": dict(req.headers),
                "post_data": req.post_data,
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    def _on_response(self, resp: Response) -> None:
        self._events.append(
            {
                "type": "response",
                "status": resp.status,
                "url": resp.url,
                "headers": dict(resp.headers),
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    def detach(self) -> None:
        try:
            self._page.remove_listener("request", self._on_request)
            self._page.remove_listener("response", self._on_response)
        except Exception:
            pass

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


async def record_page(
    page: Page,
    *,
    output_dir: Path,
    reached_from: str | None,
    depth: int,
    network_events: list[dict[str, Any]] | None = None,
) -> VisitedPage:
    """Capture HTML + screenshot + network log for the current page."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    screenshots_dir = output_dir / "screenshots"
    network_dir = output_dir / "network"
    pages_dir.mkdir(exist_ok=True)
    screenshots_dir.mkdir(exist_ok=True)
    network_dir.mkdir(exist_ok=True)

    url = page.url
    slug = url_to_slug(url)

    html_path = pages_dir / f"{slug}.html"
    screenshot_path = screenshots_dir / f"{slug}.png"
    network_path = network_dir / f"{slug}.json"

    html = await page.content()
    html_path.write_text(html, encoding="utf-8")

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as e:
        logger.warn("screenshot_failed", url=url, error=str(e))

    if network_events is not None:
        network_path.write_text(
            json.dumps(network_events, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    title = await page.title()

    forms = await _extract_forms(page)
    links = await _extract_links(page, base_url=url)
    downloadables = await _extract_downloadables(page)
    tables = await _extract_tables(page)
    jsf_components = await _extract_jsf_components(page)

    visited = VisitedPage(
        url=url,
        title=title,
        reached_from=reached_from,
        depth=depth,
        visited_at=datetime.now(UTC),
        html_path=html_path,
        screenshot_path=screenshot_path,
        network_log_path=network_path,
        forms_found=forms,
        links_found=links,
        downloadables_found=downloadables,
        tables_found=tables,
        jsf_components=jsf_components,
    )

    logger.info(
        "page_recorded",
        url=url,
        title=title,
        depth=depth,
        forms=len(forms),
        links=len(links),
        downloadables=len(downloadables),
        tables=len(tables),
    )
    return visited


async def _extract_forms(page: Page) -> list[FormInfo]:
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll("form")).map(f => ({
            action: f.action || "",
            method: (f.method || "GET").toUpperCase(),
            fields: Array.from(f.querySelectorAll("input, select, textarea"))
                .map(i => i.name || i.id || "")
                .filter(Boolean),
        }))
        """
    )
    return [
        FormInfo(action=r["action"], method=r["method"], field_names=r["fields"]) for r in raw
    ]


async def _extract_links(page: Page, *, base_url: str) -> list[LinkInfo]:
    from urllib.parse import urlparse

    base_host = urlparse(base_url).hostname or ""
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll("a[href]")).map(a => ({
            href: a.href,
            text: (a.textContent || "").trim().slice(0, 200),
        }))
        """
    )
    out: list[LinkInfo] = []
    for r in raw:
        host = urlparse(r["href"]).hostname or ""
        out.append(
            LinkInfo(
                href=r["href"],
                text=r["text"],
                is_internal=(host == base_host),
            )
        )
    return out


async def _extract_downloadables(page: Page) -> list[DownloadInfo]:
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll("a[href]"))
            .filter(a => /\\.(pdf|docx?|xlsx?|zip)$/i.test(a.href) ||
                         a.download ||
                         /descargar|download|anexo|cedula/i.test(a.textContent || ""))
            .map(a => ({
                url: a.href,
                text: (a.textContent || "").trim().slice(0, 200),
                kind: (a.href.match(/\\.(pdf|docx?|xlsx?|zip)/i) || [,"unknown"])[1].toLowerCase(),
            }))
        """
    )
    return [
        DownloadInfo(url=r["url"], text=r["text"], inferred_kind=r["kind"]) for r in raw
    ]


async def _extract_tables(page: Page) -> list[TableInfo]:
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll("table")).map(t => {
            const headers = Array.from(t.querySelectorAll("thead th, thead td"))
                .map(h => (h.textContent || "").trim());
            const rows = Array.from(t.querySelectorAll("tbody tr"));
            const firstRow = rows[0]
                ? Array.from(rows[0].querySelectorAll("td, th"))
                    .map(c => (c.textContent || "").trim().slice(0, 100))
                : [];
            return { headers, rowCount: rows.length, firstRow };
        })
        """
    )
    return [
        TableInfo(headers=r["headers"], row_count=r["rowCount"], sample_first_row=r["firstRow"])
        for r in raw
    ]


async def _extract_jsf_components(page: Page) -> list[str]:
    raw: list[str] = await page.evaluate(
        """
        () => {
            const out = new Set();
            document.querySelectorAll("[id]").forEach(el => {
                const id = el.id;
                if (id && id.includes(":")) out.add(id);
            });
            return Array.from(out).slice(0, 200);
        }
        """
    )
    return raw
