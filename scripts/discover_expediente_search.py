"""Discover the SINOE 'N° Expediente' search filter.

Read-only exploration. Logs in once, enters the Casillas Electrónicas module,
and runs four scenarios against the expediente input field, capturing HTML +
screenshot + the AJAX request body produced by 'Buscar' on each.

Scenarios:
    A) expediente válido + ventana default (últimos 7 días)
       → expediente conocido ya visible en bandeja default.
    B) mismo expediente válido + fecha cerrada (24-30 abril)
       → confirmar si el filtro de expediente "se salta" o respeta la fecha.
    C) mismo expediente válido + ventana ancha (últimos 365 días)
       → caso normal de búsqueda histórica.
    D) expediente con formato inválido (`ABC-123`)
       → ¿el server devuelve growl? ¿el keyfilter cliente lo rechaza?

Side-effects: ninguno. Solo escribe en input y dispara Buscar (idéntico a lo
que ya hacen los filtros de fecha/estado).

Usage:
    uv run python scripts/discover_expediente_search.py [--expediente EXP]
"""

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Request, Response

from lolo_sinoe.auth import SinoeCredentials, login
from lolo_sinoe.browser import launch_browser
from lolo_sinoe.captcha import (
    CapSolverSolver,
    CaptchaSolver,
    FallbackSolver,
    TwoCaptchaSolver,
)
from lolo_sinoe.config import Settings, get_settings
from lolo_sinoe.exploration.sinoe_navigator import (
    SEL_FILTRO_EXPEDIENTE,
    apply_date_filter,
    apply_expediente_filter,
    enter_sinoe_module,
    list_notifications,
    parse_paginator,
    read_growl_messages,
)
from lolo_sinoe.logging import configure_logging, get_logger

logger = get_logger("discover_expediente")

OUT = Path("discovery_output/expediente_search")

DEFAULT_EXPEDIENTE = "00018-2024-0-1601-JR-CI-03"
INVALID_EXPEDIENTE = "ABC-123"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("_", s).strip("_")[:80] or "page"


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


@dataclass
class AjaxCall:
    url: str
    method: str
    post_data: str | None
    parsed_form: dict[str, str] = field(default_factory=dict)
    status: int | None = None
    response_excerpt: str | None = None
    ts: str = ""


class AjaxRecorder:
    """Captures POSTs to *.xhtml that look like JSF AJAX requests.

    Per scenario, call `start()`, run the action, then `stop()` and read
    `calls`. Each call carries the parsed `application/x-www-form-urlencoded`
    body so we can see the JSF source/process/update params + the value the
    server actually received.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self.calls: list[AjaxCall] = []
        self._pending: dict[str, AjaxCall] = {}
        self._active = False
        self._body_tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.calls = []
        self._pending = {}
        self._active = True
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)

    def stop(self) -> None:
        self._active = False
        try:
            self._page.remove_listener("request", self._on_request)
            self._page.remove_listener("response", self._on_response)
        except Exception:
            pass

    @staticmethod
    def _is_jsf_ajax(req: Request) -> bool:
        if req.method != "POST":
            return False
        if ".xhtml" not in req.url:
            return False
        # JSF AJAX adds Faces-Request header; in PrimeFaces it's the same.
        h = {k.lower(): v for k, v in req.headers.items()}
        return h.get("faces-request") == "partial/ajax" or "javax.faces.partial.ajax" in (
            req.post_data or ""
        )

    def _on_request(self, req: Request) -> None:
        if not self._active or not self._is_jsf_ajax(req):
            return
        post = req.post_data
        parsed: dict[str, str] = {}
        if post:
            from urllib.parse import parse_qsl

            parsed = dict(parse_qsl(post, keep_blank_values=True))
        call = AjaxCall(
            url=req.url,
            method=req.method,
            post_data=post,
            parsed_form=parsed,
            ts=datetime.now(UTC).isoformat(),
        )
        self._pending[req.url + "|" + (post or "")] = call
        self.calls.append(call)

    def _on_response(self, resp: Response) -> None:
        if not self._active:
            return
        # Match by url+post_data of the originating request.
        try:
            req = resp.request
            key = req.url + "|" + (req.post_data or "")
        except Exception:
            return
        call = self._pending.pop(key, None)
        if call is None:
            return
        call.status = resp.status

        async def _capture_body() -> None:
            try:
                body = await resp.text()
                call.response_excerpt = body[:1500]
            except Exception:
                pass

        self._body_tasks.append(asyncio.create_task(_capture_body()))


async def capture(page: Page, label: str) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "html").mkdir(exist_ok=True)
    (OUT / "screenshots").mkdir(exist_ok=True)

    slug = _slug(label)
    html_path = OUT / "html" / f"{slug}.html"
    png_path = OUT / "screenshots" / f"{slug}.png"

    html = await page.content()
    html_path.write_text(html, encoding="utf-8")
    try:
        await page.screenshot(path=str(png_path), full_page=True, timeout=10_000)
    except Exception as e:
        logger.warn("screenshot_failed", label=label, error=str(e))

    title = ""
    try:
        title = await page.title()
    except Exception:
        pass

    return {
        "label": label,
        "url": page.url,
        "title": title,
        "html_size": len(html),
        "html_path": str(html_path),
        "png_path": str(png_path),
        "captured_at": datetime.now(UTC).isoformat(),
    }


async def read_input_state(page: Page) -> dict[str, Any]:
    """Inspect the expediente input post-action."""
    return dict(
        await page.evaluate(
            """() => {
              const el = document.querySelector('input[id$=":filter_nroSolicitud"]');
              if (!el) return {found: false};
              return {
                found: true,
                id: el.id,
                value_committed: el.value,
                maxlength: el.maxLength,
                disabled: el.disabled,
                readonly: el.readOnly,
                classes: el.className,
              };
            }"""
        )
    )


async def collect_rows_summary(page: Page) -> dict[str, Any]:
    rows = await list_notifications(page)
    pag = await parse_paginator(page)
    return {
        "row_count": len(rows),
        "rows": [
            {
                "row_index": r.row_index,
                "n_notif": r.n_notif,
                "expediente": r.expediente,
                "is_read": r.is_read,
                "fecha": r.fecha,
                "organo": r.organo,
            }
            for r in rows
        ],
        "paginator": {
            "found": pag.found,
            "text": pag.text,
            "current_page": pag.current_page,
            "total_pages": pag.total_pages,
            "total_records": pag.total_records,
        },
    }


async def reset_expediente_input(page: Page) -> None:
    """Clear the expediente input between scenarios so each is independent."""
    await page.locator(SEL_FILTRO_EXPEDIENTE).first.evaluate(
        "(el) => { el.value = ''; el.dispatchEvent(new Event('change', {bubbles:true})); }"
    )


async def reset_dates_to_default_window(page: Page) -> None:
    """Reset the date filter to the default 'last 7 days' window."""
    today = date.today()
    week_ago = today - timedelta(days=7)
    await apply_date_filter(
        page,
        week_ago.strftime("%d/%m/%Y"),
        today.strftime("%d/%m/%Y"),
    )


async def run_scenario(
    page: Page,
    *,
    name: str,
    label: str,
    expediente: str,
    pre_action: str = "",
    keyboard_typing: bool = False,
) -> dict[str, Any]:
    """Run one scenario and return findings.

    pre_action: opcional, descripción libre de qué se hizo antes (fecha, etc.).
    keyboard_typing: si True, escribe carácter por carácter con Page.keyboard
        para detectar el keyfilter de cliente; si False, usa el bypass JS.
    """
    logger.info(
        "scenario_start",
        scenario=name,
        expediente=expediente,
        pre_action=pre_action,
        keyboard_typing=keyboard_typing,
    )
    rec = AjaxRecorder(page)
    rec.start()
    try:
        if keyboard_typing:
            # Limpia + escribe con teclado real (PF keyfilter activo).
            await page.locator(SEL_FILTRO_EXPEDIENTE).first.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            for ch in expediente:
                await page.keyboard.type(ch, delay=20)
            await page.locator(SEL_FILTRO_EXPEDIENTE).first.evaluate(
                "(el) => { el.dispatchEvent(new Event('blur', {bubbles:true})); }"
            )
            input_after_typing = await read_input_state(page)
            # Ahora dispara Buscar manualmente (evita la rama JS-bypass del helper).
            await page.evaluate(
                """() => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => /buscar/i.test(b.textContent || ''));
                    if (btn) btn.click();
                }"""
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_timeout(800)
        else:
            input_after_typing = None
            await apply_expediente_filter(page, expediente)
            # apply_expediente_filter ya espera networkidle + 500ms.
    finally:
        # Esperá un cachito a que las response bodies se capturen.
        await asyncio.sleep(0.5)
        rec.stop()

    growl = await read_growl_messages(page)
    input_state = await read_input_state(page)
    cap = await capture(page, label)
    rows_info = await collect_rows_summary(page)

    return {
        "scenario": name,
        "pre_action": pre_action,
        "expediente_attempted": expediente,
        "keyboard_typing": keyboard_typing,
        "input_after_typing": input_after_typing,
        "input_after_search": input_state,
        "growl_messages": growl,
        "capture": cap,
        "results": rows_info,
        "ajax_calls": [asdict(c) for c in rec.calls],
        "ajax_count": len(rec.calls),
    }


async def main(expediente: str) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    OUT.mkdir(parents=True, exist_ok=True)

    findings: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "casilla": settings.casilla,
        "expediente_under_test": expediente,
        "invalid_expediente": INVALID_EXPEDIENTE,
        "scenarios": [],
        "selectors_observed": {
            "input": SEL_FILTRO_EXPEDIENTE,
            "buscar_btn": "button:has-text('Buscar')",
        },
    }

    creds = SinoeCredentials(
        casilla=settings.casilla,
        password=settings.password.get_secret_value(),
    )
    solver = _build_solver(settings)

    async with launch_browser(
        headless=settings.headless,
        nav_timeout_ms=settings.nav_timeout_ms,
    ) as h:
        # Step 1 — login (consume 1 captcha).
        await login(
            h.page,
            creds,
            solver,
            login_url=settings.login_url,
            captcha_max_retries=settings.captcha_max_retries,
        )
        findings["login_landed_url"] = h.page.url
        logger.info("login_ok", url=h.page.url)

        # Step 2 — entrar al módulo SINOE.
        ok = await enter_sinoe_module(h.page)
        if not ok:
            logger.error("could_not_enter_sinoe_module")
            findings["error"] = "could_not_enter_sinoe_module"
            (OUT / "findings.json").write_text(
                json.dumps(findings, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            return
        await asyncio.sleep(2)
        findings["sinoe_landing_url"] = h.page.url
        await capture(h.page, "00_landing_baseline")

        # Step 3 — pre-snapshot del input (atributos + estilos vivos).
        baseline_input = await read_input_state(h.page)
        findings["input_baseline"] = baseline_input
        # Tooltip statico desde DOM (id frmBusqueda:j_idt54 — observado).
        tooltip_text = await h.page.evaluate(
            """() => {
              // Buscar el div tooltip (PrimeFaces lo deja en DOM).
              const candidates = Array.from(document.querySelectorAll('div.ui-tooltip, div[class*="tooltip"]'));
              const out = candidates.map(d => (d.textContent || '').trim()).filter(t => /Formato\\s+Expediente/i.test(t));
              return out[0] || null;
            }"""
        )
        findings["format_tooltip_text"] = tooltip_text

        # ---- Scenario A: expediente válido, ventana default ----
        await reset_expediente_input(h.page)
        sc_a = await run_scenario(
            h.page,
            name="A_valid_default_window",
            label="A_valid_default_window",
            expediente=expediente,
            pre_action="ventana de fecha por defecto (últimos 7 días, pre-poblada por SINOE)",
        )
        findings["scenarios"].append(sc_a)

        # ---- Scenario B: mismo expediente, fecha cerrada 24-30 abril ----
        await reset_expediente_input(h.page)
        await apply_date_filter(h.page, "24/04/2026", "30/04/2026")
        sc_b = await run_scenario(
            h.page,
            name="B_valid_april_window",
            label="B_valid_april_window",
            expediente=expediente,
            pre_action="fecha 24/04/2026 → 30/04/2026 (ventana de 7 días en abril)",
        )
        findings["scenarios"].append(sc_b)

        # ---- Scenario C: mismo expediente, ventana ancha 365 días ----
        await reset_expediente_input(h.page)
        today = date.today()
        year_ago = today - timedelta(days=365)
        await apply_date_filter(
            h.page,
            year_ago.strftime("%d/%m/%Y"),
            today.strftime("%d/%m/%Y"),
        )
        sc_c = await run_scenario(
            h.page,
            name="C_valid_year_window",
            label="C_valid_year_window",
            expediente=expediente,
            pre_action=f"fecha {year_ago.strftime('%d/%m/%Y')} → {today.strftime('%d/%m/%Y')} (365 días)",
        )
        findings["scenarios"].append(sc_c)

        # ---- Scenario D: expediente con formato inválido (typing real) ----
        await reset_expediente_input(h.page)
        await reset_dates_to_default_window(h.page)
        sc_d = await run_scenario(
            h.page,
            name="D_invalid_format_keyboard",
            label="D_invalid_format_keyboard",
            expediente=INVALID_EXPEDIENTE,
            pre_action="ventana default; tipeo carácter por carácter para que el keyfilter cliente actúe",
            keyboard_typing=True,
        )
        findings["scenarios"].append(sc_d)

        # ---- Step 4 — write findings JSON ----
        (OUT / "findings.json").write_text(
            json.dumps(findings, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(
            "discovery_done",
            findings_path=str(OUT / "findings.json"),
            scenarios=len(findings["scenarios"]),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expediente",
        type=str,
        default=DEFAULT_EXPEDIENTE,
        help=f"Expediente válido para los escenarios A/B/C (default: {DEFAULT_EXPEDIENTE}).",
    )
    args = parser.parse_args()
    asyncio.run(main(expediente=args.expediente))
