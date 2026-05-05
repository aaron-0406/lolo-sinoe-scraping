"""Discover the SINOE Casillas Electrónicas bandeja interface.

Read-only exploration. Logs in (or reuses session.json if recent),
clicks the SINOE module from the hub, then captures HTML/screenshot
at each navigation step. Output goes to discovery_output/.

Strict rules:
- No submits other than the login.
- Only opens notifications confirmed as ALREADY READ.
- No marking-as-read, deleting, or any other state-changing click.

Usage:
    uv run python scripts/discover_bandeja.py [--max-notifications N]
"""

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from lolo_sinoe.auth import SinoeCredentials, login
from lolo_sinoe.browser import launch_browser
from lolo_sinoe.captcha import (
    CapSolverSolver,
    CaptchaSolver,
    FallbackSolver,
    TwoCaptchaSolver,
)
from lolo_sinoe.config import Settings, get_settings
from lolo_sinoe.logging import configure_logging, get_logger

logger = get_logger("discover_bandeja")

OUT = Path("discovery_output")


def _build_solver(s: Settings) -> CaptchaSolver:
    solvers: list[CaptchaSolver] = []
    if s.twocaptcha_api_key:
        solvers.append(
            TwoCaptchaSolver(
                api_key=s.twocaptcha_api_key.get_secret_value(),
                max_retries=s.captcha_max_retries,
            )
        )
    if s.capsolver_api_key:
        solvers.append(
            CapSolverSolver(
                api_key=s.capsolver_api_key.get_secret_value(),
                max_retries=s.captcha_max_retries,
            )
        )
    return FallbackSolver(solvers) if len(solvers) > 1 else solvers[0]


_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", s).strip("_")[:80] or "page"


async def capture(
    page: Page,
    label: str,
    *,
    network_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "html").mkdir(exist_ok=True)
    (OUT / "screenshots").mkdir(exist_ok=True)
    (OUT / "network").mkdir(exist_ok=True)

    slug = _slug(label)
    html_path = OUT / "html" / f"{slug}.html"
    png_path = OUT / "screenshots" / f"{slug}.png"
    net_path = OUT / "network" / f"{slug}.json"

    html = await page.content()
    html_path.write_text(html, encoding="utf-8")
    try:
        await page.screenshot(path=str(png_path), full_page=True, timeout=10_000)
    except Exception as e:
        logger.warn("screenshot_failed", label=label, error=str(e))
    if network_events is not None:
        net_path.write_text(json.dumps(network_events, ensure_ascii=False, indent=2))

    title = ""
    try:
        title = await page.title()
    except Exception:
        pass

    info = {
        "label": label,
        "url": page.url,
        "title": title,
        "html_size": len(html),
        "html_path": str(html_path),
        "png_path": str(png_path),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    logger.info("captured", **info)
    return info


async def find_sinoe_module_link(page: Page) -> str | None:
    """Find the clickable element for the SINOE Casillas Electrónicas module."""
    js = """
    () => {
      const out = [];
      // Look for image + text combinations
      document.querySelectorAll('img[src*="sinoe" i], img[alt*="sinoe" i], img[alt*="casilla" i]').forEach(img => {
        const a = img.closest('a, button, [onclick], [role="button"]') || img.parentElement;
        if (a) out.push({tag: a.tagName, id: a.id || null, class: a.className || null, text: (a.textContent||'').trim().slice(0, 80)});
      });
      // Also look for any link with text Casilla/SINOE
      document.querySelectorAll('a, button, div, span').forEach(el => {
        const t = (el.textContent || '').trim().toUpperCase();
        if ((t.includes('CASILLA') || t.includes('SINOE')) && t.length < 50) {
          out.push({tag: el.tagName, id: el.id || null, class: el.className || null, text: t.slice(0, 80)});
        }
      });
      return out;
    }
    """
    candidates = await page.evaluate(js)
    logger.info("sinoe_link_candidates", count=len(candidates), candidates=candidates[:10])
    return None  # caller picks based on logged candidates


async def click_sinoe_module(page: Page) -> bool:
    """Click the SINOE Casillas Electrónicas commandlink on the hub.

    The hub link is a PrimeFaces ui-commandlink with stable text but unstable
    auto-generated id (j_idt38). Target it by text within the post-login form.
    """
    # Get the candidate's id dynamically — text is stable, id is not.
    js = """
    () => {
      const candidates = Array.from(
        document.querySelectorAll('a.ui-commandlink, form#frmNuevo a')
      );
      const sinoe = candidates.find(a => /Casillas\\s+Electr[oó]nicas/i.test(a.textContent || ''));
      return sinoe ? sinoe.id : null;
    }
    """
    target_id = await page.evaluate(js)
    if not target_id:
        logger.error("sinoe_module_link_not_found")
        return False

    selector = f"a[id='{target_id}']"
    logger.info("clicking_sinoe_module", selector=selector)
    try:
        async with page.expect_navigation(wait_until="networkidle", timeout=20_000):
            await page.click(selector)
    except Exception as e:
        logger.info("sinoe_module_click_no_nav", error=str(e))
    return True


async def harvest_filters(page: Page) -> list[dict[str, Any]]:
    """Find candidate filter controls (read/unread, dates, types)."""
    js = """
    () => {
      const out = [];
      document.querySelectorAll('button, a, label, span.ui-button-text, [role="tab"]').forEach(el => {
        const t = (el.textContent || '').trim().toUpperCase();
        if (t.length === 0 || t.length > 60) return;
        if (/LE[IÍ]DA|NO LE|SIN LEER|NUEVAS|TODAS|FILTRA|BUSCAR|HIST[OÓ]RICO|BANDEJA|RECIB|ENVIAD/.test(t)) {
          out.push({
            tag: el.tagName,
            id: el.id || null,
            class: el.className || null,
            text: t.slice(0, 60),
          });
        }
      });
      return out;
    }
    """
    return list(await page.evaluate(js))


async def harvest_table_state(page: Page) -> dict[str, Any]:
    """Extract any visible table on the page (headers, row count, first rows)."""
    js = """
    () => {
      const tables = Array.from(document.querySelectorAll('table'));
      return tables.map(t => {
        const headers = Array.from(t.querySelectorAll('thead th, thead td')).map(h => (h.textContent||'').trim());
        const rows = Array.from(t.querySelectorAll('tbody tr'));
        const sampleRows = rows.slice(0, 5).map(r => ({
          classes: r.className || '',
          dataAttrs: Object.fromEntries(Array.from(r.attributes).filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value])),
          cells: Array.from(r.querySelectorAll('td, th')).map(c => (c.textContent||'').trim().slice(0, 100)),
        }));
        return {
          id: t.id || null,
          class: t.className || null,
          rowCount: rows.length,
          headers: headers,
          sampleRows: sampleRows,
        };
      });
    }
    """
    return {"tables": await page.evaluate(js)}


async def harvest_links(page: Page) -> list[dict[str, Any]]:
    """All anchor and command-link elements with their text."""
    js = """
    () => {
      return Array.from(document.querySelectorAll('a, button')).slice(0, 200).map(el => ({
        tag: el.tagName,
        id: el.id || null,
        href: el.getAttribute('href') || null,
        onclick: (el.getAttribute('onclick') || '').slice(0, 200),
        text: (el.textContent || '').trim().slice(0, 100),
      })).filter(x => x.text.length > 0);
    }
    """
    return list(await page.evaluate(js))


async def harvest_downloadables(page: Page) -> list[dict[str, Any]]:
    """All links that look like file downloads."""
    js = """
    () => {
      return Array.from(document.querySelectorAll('a[href]')).filter(a => {
        const h = a.href.toLowerCase();
        const t = (a.textContent || '').toLowerCase();
        return /\\.(pdf|docx?|xlsx?|zip|rar)$/.test(h) ||
               /descargar|download|cedula|c[eé]dula|anexo|resoluci/.test(t);
      }).map(a => ({
        url: a.href,
        text: (a.textContent || '').trim().slice(0, 200),
        download: a.getAttribute('download') || null,
      }));
    }
    """
    return list(await page.evaluate(js))


async def parse_bandeja_rows(page: Page) -> list[dict[str, Any]]:
    """Parse the visible rows of the bandeja table.

    Returns a list of dicts with: index, row_key, is_read, n_notif, expediente,
    sumilla, oj, fecha, ver_anexos_button_id.
    """
    js = """
    () => {
      const rows = Array.from(document.querySelectorAll('tbody#frmBusqueda\\\\:tblLista_data > tr[data-ri]'));
      return rows.map(r => {
        const tds = r.querySelectorAll('td');
        const img = r.querySelector('img[src*="notificacion"]');
        const isRead = img && img.src.includes('notificacion-abierta');
        const verBtn = r.querySelector('button[id*=":j_idt"]') || r.querySelector('button[title*="anexos" i]');
        return {
          index: parseInt(r.getAttribute('data-ri') || '-1', 10),
          row_key: r.getAttribute('data-rk') || null,
          is_read: !!isRead,
          read_state_text: img ? (img.alt || img.src) : '',
          n_notif: tds[3] ? tds[3].textContent.trim() : '',
          expediente: tds[4] ? tds[4].textContent.trim() : '',
          sumilla: tds[5] ? tds[5].textContent.trim() : '',
          oj: tds[6] ? tds[6].textContent.trim() : '',
          fecha: tds[7] ? tds[7].textContent.trim() : '',
          ver_anexos_id: verBtn ? verBtn.id : null,
        };
      });
    }
    """
    return list(await page.evaluate(js))


async def click_row_attachments(page: Page, btn_id: str) -> bool:
    """Click 'Ver anexos' button on a specific row. Waits for AJAX update."""
    selector = f"button[id='{btn_id}']"
    try:
        loc = page.locator(selector)
        if await loc.count() == 0:
            return False
        await loc.click()
        # PrimeFaces AJAX — no navigation. Wait for the modal/anexos to update.
        await page.wait_for_function(
            "document.querySelector('div[id$=\":dlgListaAnexos\"]') && "
            "(document.querySelector('div[id$=\":dlgListaAnexos\"]').style.display !== 'none' || "
            " !document.querySelector('div[id$=\":dlgListaAnexos\"]').classList.contains('ui-overlay-hidden'))",
            timeout=10_000,
        )
        return True
    except Exception as e:
        logger.warn("click_row_attachments_failed", btn_id=btn_id, error=str(e))
        return False


async def parse_anexos_dialog(page: Page) -> dict[str, Any]:
    """Read the contents of the anexos dialog after it has been populated."""
    js = """
    () => {
      const dialog = document.querySelector('div[id$=":dlgListaAnexos"]');
      if (!dialog) return {found: false};
      const rows = Array.from(dialog.querySelectorAll('tbody[id$=":tblListaAnexos_data"] tr'));
      const downloadAll = dialog.querySelector('button[id$=":btnDescargaTodo"], a[id$=":btnDescargaTodo"]');
      // Generic: any download-looking link/button inside the dialog
      const links = Array.from(dialog.querySelectorAll('a[href], button[onclick]')).map(el => ({
        tag: el.tagName,
        id: el.id || null,
        href: el.getAttribute('href') || null,
        onclick: (el.getAttribute('onclick') || '').slice(0, 300),
        text: (el.textContent || '').trim().slice(0, 100),
      }));
      const items = rows.map(r => {
        const tds = Array.from(r.querySelectorAll('td')).map(td => (td.textContent || '').trim());
        const inner_links = Array.from(r.querySelectorAll('a, button')).map(el => ({
          tag: el.tagName,
          id: el.id || null,
          href: el.getAttribute('href') || null,
          onclick: (el.getAttribute('onclick') || '').slice(0, 300),
          text: (el.textContent || '').trim().slice(0, 80),
        }));
        return {cells: tds, links: inner_links};
      });
      return {
        found: true,
        item_count: rows.length,
        download_all_button: downloadAll ? {id: downloadAll.id, tag: downloadAll.tagName} : null,
        items: items,
        all_links_in_dialog: links.slice(0, 30),
      };
    }
    """
    return dict(await page.evaluate(js))


async def close_anexos_dialog(page: Page) -> None:
    """Close the anexos dialog using the PrimeFaces widget API.

    `PF('dialogVarAnexos').hide()` is the stable way (widget var is set by the
    developer in markup, not auto-generated). Also forces the modal overlay
    to be hidden in case PF leaves it stuck.
    """
    try:
        await page.evaluate(
            """
            () => {
                if (typeof PF === 'function') {
                    try { PF('dialogVarAnexos').hide(); } catch(e) {}
                }
                // Force-hide overlay even if PF didn't.
                document.querySelectorAll(
                    'div.ui-widget-overlay, div[id$=":dlgListaAnexos_modal"]'
                ).forEach(el => { el.style.display = 'none'; });
                document.querySelectorAll('div[id$=":dlgListaAnexos"]').forEach(el => {
                    el.style.display = 'none';
                    el.classList.add('ui-overlay-hidden');
                });
            }
            """
        )
        await page.wait_for_timeout(500)
    except Exception as e:
        logger.warn("close_anexos_failed", error=str(e))


async def apply_date_filter(page: Page, fecha_inicio: str, fecha_final: str) -> None:
    """Set date filter values (dd/mm/yyyy) and click Buscar.

    PrimeFaces Calendar inputs have `readonly` so users go through the picker.
    We bypass via locator.evaluate() — runs JS directly on the element,
    setting value + firing the input/change/blur events PF listens to.
    Order matters: input → change → blur (PF Calendar commits on blur).
    """
    set_value_js = """
    (el, value) => {
      el.removeAttribute('readonly');
      el.removeAttribute('aria-readonly');
      el.value = value;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('blur', {bubbles: true}));
      // Sometimes PF reads a hidden sibling input — set both if found.
      const id = el.id || '';
      const baseId = id.replace(/_input$/, '');
      const hidden = document.getElementById(baseId);
      if (hidden && hidden !== el) {
        hidden.value = value;
        hidden.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }
    """
    ini = page.locator("input[id$=':filter_fechaInicio_input']").first
    await ini.evaluate(set_value_js, fecha_inicio)

    fin = page.locator("input[id$=':filter_fechaFinal_input']").first
    await fin.evaluate(set_value_js, fecha_final)

    await page.wait_for_timeout(500)

    buscar_js = """
    () => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => /buscar/i.test(b.textContent || ''));
      if (!btn) return false;
      btn.click();
      return true;
    }
    """
    await page.evaluate(buscar_js)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_500)


async def detect_validation_message(page: Page) -> str | None:
    """Return any error/validation toast message visible on the page."""
    js = """
    () => {
      const messages = [];
      // PrimeFaces growl messages
      document.querySelectorAll('.ui-growl-message, .ui-messages-error-summary, .ui-messages-error-detail, .ui-message-error').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t) messages.push(t.slice(0, 300));
      });
      // Generic error containers
      document.querySelectorAll('[class*="error"]:not(:empty), [class*="warn"]:not(:empty)').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t && t.length < 300) messages.push(t.slice(0, 300));
      });
      return messages.slice(0, 5);
    }
    """
    msgs = await page.evaluate(js)
    return " | ".join(msgs) if msgs else None


async def parse_paginator_state(page: Page) -> dict[str, Any]:
    """Read pagination text and detect available navigation buttons."""
    js = """
    () => {
      const out = {found: false};
      const wrapper = document.querySelector('[id$=":tblLista_paginator_top"], [id$=":tblLista_paginator_bottom"]');
      if (!wrapper) return out;
      out.found = true;
      out.text = (wrapper.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 300);

      // PrimeFaces typical paginator buttons
      const findBtn = (sel) => {
        const el = wrapper.querySelector(sel);
        if (!el) return null;
        return {
          tag: el.tagName,
          class: el.className || null,
          text: (el.textContent || '').trim(),
          disabled: el.classList.contains('ui-state-disabled'),
        };
      };
      out.first = findBtn('.ui-paginator-first');
      out.prev  = findBtn('.ui-paginator-prev');
      out.next  = findBtn('.ui-paginator-next');
      out.last  = findBtn('.ui-paginator-last');

      // Page numbers list
      out.pages = Array.from(wrapper.querySelectorAll('.ui-paginator-page')).map(el => ({
        text: (el.textContent || '').trim(),
        active: el.classList.contains('ui-state-active'),
      }));

      // Page size select if any
      const sizeSel = wrapper.querySelector('select.ui-paginator-rpp-options');
      if (sizeSel) {
        out.page_size_options = Array.from(sizeSel.options).map(o => o.value);
        out.current_page_size = sizeSel.value;
      }

      return out;
    }
    """
    return dict(await page.evaluate(js))


async def click_paginator(page: Page, action: str) -> bool:
    """Click 'next' / 'prev' / 'first' / 'last' on the paginator."""
    sel_map = {
        "first": ".ui-paginator-first",
        "prev": ".ui-paginator-prev",
        "next": ".ui-paginator-next",
        "last": ".ui-paginator-last",
    }
    sel = sel_map.get(action)
    if not sel:
        return False
    try:
        loc = page.locator(f"[id$=':tblLista_paginator_top'] {sel}").first
        if await loc.count() == 0:
            return False
        # Skip if disabled
        cls = await loc.get_attribute("class") or ""
        if "ui-state-disabled" in cls:
            logger.info("paginator_button_disabled", action=action)
            return False
        await loc.click()
        await page.wait_for_load_state("networkidle", timeout=10_000)
        await asyncio.sleep(1)
        return True
    except Exception as e:
        logger.warn("paginator_click_failed", action=action, error=str(e))
        return False


async def apply_estado_filter(page: Page, value: str) -> None:
    """Apply the 'Estado de Revisión' dropdown filter and click search.

    value: '' = Todos, '0' = No Leído, '1' = Leído.
    """
    # PrimeFaces select: visible label + hidden select. Use the select directly.
    js = (
        "(value) => {"
        "  const sel = document.querySelector('select[id$=\":estadoRevision_input\"], select[id$=\":estadoRevision\"]');"
        "  if (!sel) return false;"
        "  sel.value = value;"
        "  sel.dispatchEvent(new Event('change', {bubbles: true}));"
        "  return true;"
        "}"
    )
    ok = await page.evaluate(js, value)
    if not ok:
        logger.warn("estado_filter_select_not_found")
        return
    # Click "Buscar" button to apply
    buscar_js = """
    () => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => /buscar/i.test(b.textContent || ''));
      if (!btn) return false;
      btn.click();
      return true;
    }
    """
    await page.evaluate(buscar_js)
    await page.wait_for_load_state("networkidle", timeout=10_000)


async def main(max_notifications: int) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    findings: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "casilla": settings.casilla,
        "max_notifications": max_notifications,
        "steps": [],
    }

    network_events: list[dict[str, Any]] = []

    creds = SinoeCredentials(
        casilla=settings.casilla,
        password=settings.password.get_secret_value(),
    )
    solver = _build_solver(settings)

    async with launch_browser(
        headless=settings.headless,
        nav_timeout_ms=settings.nav_timeout_ms,
    ) as h:
        # Listen to all network from the start.
        h.page.on("request", lambda r: network_events.append({
            "type": "request",
            "method": r.method,
            "url": r.url,
            "resource_type": r.resource_type,
            "ts": datetime.now(UTC).isoformat(),
        }))
        h.page.on("response", lambda r: network_events.append({
            "type": "response",
            "status": r.status,
            "url": r.url,
            "ts": datetime.now(UTC).isoformat(),
        }))

        # ---- Step 1: Login ----
        await login(
            h.page,
            creds,
            solver,
            login_url=settings.login_url,
            captcha_max_retries=settings.captcha_max_retries,
        )
        info = await capture(h.page, "01_hub_post_login", network_events=network_events)
        info["links"] = (await harvest_links(h.page))[:50]
        findings["steps"].append({"step": 1, "name": "hub_post_login", **info})

        # Discover the SINOE module link (exploratory)
        await find_sinoe_module_link(h.page)

        # ---- Step 2: Click SINOE Casillas Electrónicas ----
        clicked = await click_sinoe_module(h.page)
        if not clicked:
            logger.error("could_not_find_sinoe_module")
            findings["steps"].append({"step": 2, "name": "click_sinoe_module", "ok": False})
        else:
            await asyncio.sleep(2)
            info = await capture(h.page, "02_sinoe_landing", network_events=network_events)
            info["filters"] = await harvest_filters(h.page)
            info["tables"] = (await harvest_table_state(h.page))["tables"]
            info["downloadables"] = await harvest_downloadables(h.page)
            findings["steps"].append({"step": 2, "name": "sinoe_landing", "ok": True, **info})

            # ---- Step 3: Parse the bandeja rows ----
            rows = await parse_bandeja_rows(h.page)
            findings["steps"].append({"step": 3, "name": "bandeja_rows_parsed", "rows": rows})
            logger.info(
                "bandeja_rows",
                total=len(rows),
                read_count=sum(1 for r in rows if r["is_read"]),
                unread_count=sum(1 for r in rows if not r["is_read"]),
            )

            # ---- Step 3.5: Apply wider date filter to enable pagination test ----
            # We know April 1-30 returns 17 records / 2 pages from prior runs.
            try:
                await apply_date_filter(h.page, "01/04/2026", "30/04/2026")
                cap = await capture(
                    h.page, "03b_filter_april_for_pagination", network_events=network_events
                )
                rows_april = await parse_bandeja_rows(h.page)
                pag = await parse_paginator_state(h.page)
                # Re-read what value SINOE actually committed to the inputs
                committed = await h.page.evaluate("""() => ({
                    inicio: document.querySelector('input[id$=":filter_fechaInicio_input"]')?.value,
                    final: document.querySelector('input[id$=":filter_fechaFinal_input"]')?.value
                })""")
                findings["steps"].append({
                    "step": "3b",
                    "name": "filter_date_april_for_pagination",
                    "applied_inicio": "01/04/2026",
                    "applied_final": "30/04/2026",
                    "committed_inicio": committed.get("inicio"),
                    "committed_final": committed.get("final"),
                    "rows_count": len(rows_april),
                    "paginator": pag,
                    **cap,
                })
                logger.info(
                    "filter_april_applied",
                    rows=len(rows_april),
                    committed=committed,
                    paginator_text=pag.get("text"),
                )

                # ---- Step 3.6: Iterate paginator next-button across all pages ----
                pages_visited = [{
                    "page_num": 1,
                    "rows_count": len(rows_april),
                    "paginator_text": pag.get("text"),
                    "rows": [
                        {"n_notif": r["n_notif"], "fecha": r["fecha"], "is_read": r["is_read"]}
                        for r in rows_april
                    ],
                }]
                for page_iter in range(2, 10):
                    if not pag.get("next") or pag["next"].get("disabled"):
                        logger.info("pagination_no_more_pages", current=page_iter - 1)
                        break
                    ok = await click_paginator(h.page, "next")
                    if not ok:
                        break
                    new_rows = await parse_bandeja_rows(h.page)
                    new_pag = await parse_paginator_state(h.page)
                    label = f"03c_page{page_iter}"
                    cap_p = await capture(h.page, label, network_events=network_events)
                    pages_visited.append({
                        "page_num": page_iter,
                        "rows_count": len(new_rows),
                        "paginator_text": new_pag.get("text"),
                        "rows": [
                            {"n_notif": r["n_notif"], "fecha": r["fecha"], "is_read": r["is_read"]}
                            for r in new_rows
                        ],
                        "capture": cap_p,
                    })
                    pag = new_pag
                    if not new_pag.get("next") or new_pag["next"].get("disabled"):
                        break
                findings["steps"].append({
                    "step": "3c",
                    "name": "paginator_iteration",
                    "total_pages_visited": len(pages_visited),
                    "pages": pages_visited,
                })

                # Reset to page 1 so subsequent tests start clean
                await click_paginator(h.page, "first")
                await asyncio.sleep(1)
            except Exception as e:
                logger.warn("filter_april_or_pagination_failed", error=str(e))

            # ---- Step 3.7: Test 31-day limit by trying 32 days ----
            try:
                await apply_date_filter(h.page, "29/03/2026", "30/04/2026")  # 33 days
                cap = await capture(h.page, "03d_filter_33days_limit_test", network_events=network_events)
                msg = await detect_validation_message(h.page)
                committed = await h.page.evaluate("""() => ({
                    inicio: document.querySelector('input[id$=":filter_fechaInicio_input"]')?.value,
                    final: document.querySelector('input[id$=":filter_fechaFinal_input"]')?.value
                })""")
                rows_33 = await parse_bandeja_rows(h.page)
                findings["steps"].append({
                    "step": "3d",
                    "name": "filter_33days_limit_test",
                    "applied_inicio": "29/03/2026",
                    "applied_final": "30/04/2026",
                    "committed_inicio": committed.get("inicio"),
                    "committed_final": committed.get("final"),
                    "validation_message": msg,
                    "rows_count": len(rows_33),
                    **cap,
                })
                logger.info(
                    "filter_33days",
                    msg=msg,
                    rows=len(rows_33),
                    committed=committed,
                    rejected_or_capped=msg is not None or committed.get("inicio") != "29/03/2026",
                )
            except Exception as e:
                logger.warn("filter_33days_failed", error=str(e))

            # ---- Step 3.8: Reset to default range before anexos exploration ----
            try:
                await apply_date_filter(h.page, "24/04/2026", "30/04/2026")
                await asyncio.sleep(1)
            except Exception:
                pass

            # Re-parse rows after reset for the anexos exploration
            rows = await parse_bandeja_rows(h.page)

            # ---- Step 4: For up to N read rows, click Ver Anexos and capture ----
            read_rows = [r for r in rows if r["is_read"] and r["ver_anexos_id"]]
            iter_results = []
            for i, row in enumerate(read_rows[:max_notifications]):
                logger.info(
                    "exploring_read_notification",
                    i=i,
                    n_notif=row["n_notif"],
                    expediente=row["expediente"],
                )
                ok = await click_row_attachments(h.page, row["ver_anexos_id"])
                if not ok:
                    iter_results.append({"row": row, "ok": False, "anexos": None})
                    continue
                await asyncio.sleep(1)
                anexos = await parse_anexos_dialog(h.page)
                label = f"03_anexos_row{i:02d}_{_slug(row['n_notif'])}"
                cap_info = await capture(h.page, label, network_events=network_events)
                iter_results.append({
                    "row": row,
                    "ok": True,
                    "anexos": anexos,
                    "capture": cap_info,
                })
                await close_anexos_dialog(h.page)
                await asyncio.sleep(1)

            findings["steps"].append({
                "step": 4,
                "name": "explore_read_anexos",
                "explored_count": len(iter_results),
                "results": iter_results,
            })

            # ---- Step 5: Try clicking the row itself (not the button) for first read row ----
            if read_rows:
                first = read_rows[0]
                try:
                    selector = (
                        f"tr[data-rk='{first['row_key']}'] td:nth-child(4)"
                        if first.get("row_key")
                        else f"tr[data-ri='{first['index']}'] td:nth-child(4)"
                    )
                    logger.info("clicking_row_for_detail", selector=selector)
                    await h.page.locator(selector).first.click(timeout=5_000)
                    await asyncio.sleep(2)
                    cap = await capture(h.page, "04_after_row_click", network_events=network_events)
                    findings["steps"].append({
                        "step": 5,
                        "name": "row_click_detail",
                        "selector_used": selector,
                        **cap,
                    })
                except Exception as e:
                    logger.warn("row_click_failed", error=str(e))
                    findings["steps"].append({
                        "step": 5,
                        "name": "row_click_detail",
                        "ok": False,
                        "error": str(e),
                    })

            # ---- Step 6: Apply filter "Leído" then capture ----
            try:
                await apply_estado_filter(h.page, "1")
                await asyncio.sleep(2)
                cap = await capture(h.page, "05_filter_leidos", network_events=network_events)
                rows_filtered = await parse_bandeja_rows(h.page)
                findings["steps"].append({
                    "step": 6,
                    "name": "filter_leidos",
                    "rows_count": len(rows_filtered),
                    "all_read": all(r["is_read"] for r in rows_filtered),
                    **cap,
                })
            except Exception as e:
                logger.warn("filter_leidos_failed", error=str(e))

            # ---- Step 8: Apply filter "No Leído" — observe only, do not open any ----
            try:
                await apply_estado_filter(h.page, "0")
                await asyncio.sleep(2)
                cap = await capture(h.page, "06_filter_no_leidos_OBSERVE_ONLY", network_events=network_events)
                rows_unread = await parse_bandeja_rows(h.page)
                findings["steps"].append({
                    "step": 7,
                    "name": "filter_no_leidos_observe_only",
                    "rows_count": len(rows_unread),
                    "all_unread": all(not r["is_read"] for r in rows_unread),
                    "rows_summary": [
                        {
                            "n_notif": r["n_notif"],
                            "expediente": r["expediente"],
                            "fecha": r["fecha"],
                        } for r in rows_unread
                    ],
                    **cap,
                })
            except Exception as e:
                logger.warn("filter_no_leidos_failed", error=str(e))

        # ---- Step 8: write findings JSON + network log ----
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "findings.json").write_text(
            json.dumps(findings, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (OUT / "network_full.json").write_text(
            json.dumps(network_events, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(
            "discovery_done",
            findings_path=str(OUT / "findings.json"),
            network_events=len(network_events),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-notifications", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(max_notifications=args.max_notifications))
