"""Navigation helpers specific to the SINOE Casillas Electrónicas module.

All read-only — these helpers do NOT click anything that mutates state on
SINOE's side (no marking-as-read, no folder moves, no deletions).

Verified empirically against casilla 126877 on 2026-04-30.
See `investigacion/02-arquitectura/sinoe-bandeja-y-anexos.md` for the DOM map.
"""

from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


# Stable selectors verified 2026-04-30 (see arquitectura doc).
SEL_USERNAME_INPUT = "input[placeholder='Usuario']"
SEL_FECHA_INICIO = "input[id$=':filter_fechaInicio_input']"
SEL_FECHA_FINAL = "input[id$=':filter_fechaFinal_input']"
SEL_ESTADO_REVISION = "select[id$=':estadoRevision']"
SEL_BUSCAR_BTN = "button:has-text('Buscar')"
# Pese al `nroSolicitud`, este input es la búsqueda por **N° de Expediente**
# (label "N° Expediente"). Verificado 2026-05-01 en HTML capturado.
SEL_FILTRO_EXPEDIENTE = "input[id$=':filter_nroSolicitud']"
SEL_TBL_LISTA_DATA = "tbody[id$=':tblLista_data']"
SEL_PAGINATOR_TOP = "[id$=':tblLista_paginator_top']"
SEL_DLG_ANEXOS = "div[id$=':dlgListaAnexos']"
SEL_TBL_ANEXOS_DATA = "tbody[id$=':tblListaAnexos_data']"


@dataclass
class BandejaRow:
    row_index: int
    row_key: str | None
    is_read: bool
    n_notif: str
    expediente: str
    sumilla: str
    organo: str
    fecha: str
    ver_anexos_button_id: str | None


@dataclass
class AnexoItem:
    tipo: str  # 'Cédula' | 'Resolución' | 'Anexo' | etc.
    identificacion: str  # e.g. "366978-2026-00002"
    paginas: int
    peso_text: str  # e.g. "44.84 KB"
    descarga_button_id: str | None


@dataclass
class PaginatorState:
    found: bool
    text: str | None
    current_page: int | None
    total_pages: int | None
    total_records: int | None
    has_next: bool
    has_prev: bool
    current_page_size: int | None


# ---- Hub → SINOE module ----


async def enter_sinoe_module(page: Page) -> bool:
    """Click the 'Casillas Electrónicas' tile from the post-login hub.

    Selects by visible text (the auto-generated id `j_idtNN` is volatile).
    """
    js = """
    () => {
      const link = Array.from(
        document.querySelectorAll('a.ui-commandlink, form#frmNuevo a')
      ).find(a => /Casillas\\s+Electr[oó]nicas/i.test(a.textContent || ''));
      return link ? link.id : null;
    }
    """
    target_id = await page.evaluate(js)
    if not target_id:
        logger.error("sinoe_module_link_not_found")
        return False

    logger.info("entering_sinoe_module", id=target_id)
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
            await page.click(f"a[id='{target_id}']")
    except Exception as e:
        logger.info("sinoe_module_no_explicit_nav", error=str(e))

    # PrimeFaces a veces hace AJAX en lugar de full nav. La bandeja se
    # renderea despues. Esperar al table o al paginator antes de seguir;
    # si ninguno aparece en 20s, asumimos bandeja vacia / DOM diferente
    # y dejamos que la lógica downstream (parse_rows) lo detecte.
    try:
        await page.wait_for_selector(
            "tbody[id$=':tblLista_data'], [id$=':tblLista_paginator_top']",
            state="attached",
            timeout=20_000,
        )
        logger.info("sinoe_bandeja_loaded")
    except Exception as e:
        logger.warning("sinoe_bandeja_wait_timeout", error=str(e))

    return True


# ---- Bandeja: parse rows ----


async def list_notifications(page: Page) -> list[BandejaRow]:
    """Parse all visible rows from the bandeja table (current page)."""
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => {
          const rows = Array.from(
            document.querySelectorAll('tbody[id$=":tblLista_data"] > tr[data-ri]')
          );
          return rows.map(r => {
            const tds = r.querySelectorAll('td');
            const img = r.querySelector('img[src*="notificacion"]');
            const verBtn = r.querySelector('button[title="Ver anexos"]');
            const sumillaTextarea = tds[5]?.querySelector('textarea');
            return {
              row_index: parseInt(r.getAttribute('data-ri') || '-1', 10),
              row_key: r.getAttribute('data-rk') || null,
              is_read: !!(img && img.src.includes('notificacion-abierta')),
              n_notif: tds[3]?.textContent?.trim() || '',
              expediente: tds[4]?.textContent?.trim() || '',
              sumilla: (sumillaTextarea?.value || tds[5]?.textContent || '').trim(),
              organo: tds[6]?.textContent?.trim() || '',
              fecha: tds[7]?.textContent?.trim() || '',
              ver_anexos_button_id: verBtn?.id || null,
            };
          });
        }
        """
    )
    return [BandejaRow(**r) for r in raw]


# ---- Filters ----


async def apply_estado_filter(page: Page, value: str) -> None:
    """Set the 'Estado de Revisión' filter and trigger Buscar.

    value: '' = Todos, '0' = No Leído, '1' = Leído.
    """
    await page.evaluate(
        """(v) => {
            const sel = document.querySelector('select[id$=":estadoRevision"]');
            if (sel) {
                sel.value = v;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""",
        value,
    )
    await _click_buscar(page)


async def apply_date_filter(
    page: Page,
    fecha_inicio: str,
    fecha_final: str,
) -> None:
    """Set date filter inputs (dd/mm/yyyy format) and trigger Buscar.

    PrimeFaces Calendar inputs are readonly; we bypass via locator.evaluate.
    The PF Calendar widget commits its model on `blur`, so we fire that event.

    Note: the UI shows a hint "El periodo máximo de búsqueda es 31 días" but
    that is INFORMATIONAL — verified empirically that ranges >31 days are
    accepted. Still, prefer 31-day windows for client-friendly behavior.
    """
    set_value_js = """
    (el, value) => {
      el.removeAttribute('readonly');
      el.removeAttribute('aria-readonly');
      el.value = value;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('blur', {bubbles: true}));
      const baseId = (el.id || '').replace(/_input$/, '');
      const hidden = document.getElementById(baseId);
      if (hidden && hidden !== el) {
        hidden.value = value;
        hidden.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }
    """
    await page.locator(SEL_FECHA_INICIO).first.evaluate(set_value_js, fecha_inicio)
    await page.locator(SEL_FECHA_FINAL).first.evaluate(set_value_js, fecha_final)
    await page.wait_for_timeout(300)
    await _click_buscar(page)


async def apply_expediente_filter(
    page: Page,
    expediente: str,
) -> None:
    """Set the 'N° Expediente' input and trigger Buscar.

    El input lleva la clase PrimeFaces `keyfilter-expediente` que restringe la
    entrada por teclado a `[0-9A-Z\\-]`. Asignar `el.value = …` desde JS
    bypasea ese filtro de cliente — el server revalida el formato y, si es
    inválido, devuelve un growl en `#listaMensajes_container`.

    Formato esperado (de tooltip oficial `frmBusqueda:j_idt54`):
        99999-9999-9-9999-AZ-AZ-99
        99999-9999-99-9999-AZ-AZ-99

    No toca las fechas: el caller decide si las amplía antes con
    `apply_date_filter` o no. Sin tocarlas, SINOE solo busca dentro de la
    ventana default ("últimos 7 días" según el growl en landing).
    """
    set_value_js = """
    (el, value) => {
      el.value = value;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('blur', {bubbles: true}));
    }
    """
    await page.locator(SEL_FILTRO_EXPEDIENTE).first.evaluate(
        set_value_js, expediente.strip().upper()
    )
    await page.wait_for_timeout(200)
    await _click_buscar(page)


async def read_growl_messages(page: Page) -> list[str]:
    """Read any visible PrimeFaces growl messages (validation/info toasts)."""
    raw: list[str] = await page.evaluate(
        """
        () => {
          const out = [];
          document.querySelectorAll(
            '#listaMensajes_container .ui-growl-message, '
            + '.ui-growl-message, '
            + '.ui-messages-error-summary, '
            + '.ui-messages-error-detail'
          ).forEach(el => {
            const t = (el.textContent || '').trim();
            if (t) out.push(t.slice(0, 400));
          });
          return out;
        }
        """
    )
    return list(raw)


async def _click_buscar(page: Page) -> None:
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
    await page.wait_for_timeout(500)


# ---- Pagination ----


async def parse_paginator(page: Page) -> PaginatorState:
    """Read paginator state from `tblLista_paginator_top`."""
    raw: dict[str, Any] = await page.evaluate(
        """
        () => {
          const w = document.querySelector('[id$=":tblLista_paginator_top"]');
          if (!w) return {found: false};
          const text = (w.querySelector('.ui-paginator-current')?.textContent || '').trim();
          const isDisabled = (sel) => {
            const el = w.querySelector(sel);
            return el ? el.classList.contains('ui-state-disabled') : true;
          };
          // Parse "Registros: N - [ Página : P/T ]" from the current text.
          let total = null, page_num = null, total_pages = null;
          const m = text.match(/Registros:\\s*(\\d+).*P[aá]gina\\s*:\\s*(\\d+)\\/(\\d+)/);
          if (m) {
            total = parseInt(m[1], 10);
            page_num = parseInt(m[2], 10);
            total_pages = parseInt(m[3], 10);
          }
          const sizeSel = w.querySelector('.ui-paginator-rpp-options');
          return {
            found: true,
            text: text,
            total_records: total,
            current_page: page_num,
            total_pages: total_pages,
            has_next: !isDisabled('.ui-paginator-next'),
            has_prev: !isDisabled('.ui-paginator-prev'),
            current_page_size: sizeSel ? parseInt(sizeSel.value, 10) : null,
          };
        }
        """
    )
    if not raw.get("found"):
        return PaginatorState(
            found=False,
            text=None,
            current_page=None,
            total_pages=None,
            total_records=None,
            has_next=False,
            has_prev=False,
            current_page_size=None,
        )
    return PaginatorState(
        found=True,
        text=raw.get("text"),
        current_page=raw.get("current_page"),
        total_pages=raw.get("total_pages"),
        total_records=raw.get("total_records"),
        has_next=bool(raw.get("has_next")),
        has_prev=bool(raw.get("has_prev")),
        current_page_size=raw.get("current_page_size"),
    )


async def go_to_next_page(page: Page) -> bool:
    """Click 'next' on the paginator. Returns False if no next page."""
    pag = await parse_paginator(page)
    if not pag.found or not pag.has_next:
        return False
    loc = page.locator(f"{SEL_PAGINATOR_TOP} .ui-paginator-next").first
    if await loc.count() == 0:
        return False
    await loc.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(500)
    return True


async def change_page_size(page: Page, size: int) -> None:
    """Change rows per page. Available: 15, 20, 30, 40, 50, 100."""
    sel = page.locator(f"{SEL_PAGINATOR_TOP} .ui-paginator-rpp-options").first
    await sel.select_option(value=str(size))
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass


async def iterate_all_pages(
    page: Page,
    *,
    max_pages: int = 100,
) -> list[BandejaRow]:
    """Walk through every paginator page and collect rows."""
    all_rows: list[BandejaRow] = []
    seen_keys: set[str] = set()
    for page_num in range(1, max_pages + 1):
        rows = await list_notifications(page)
        for r in rows:
            if r.row_key and r.row_key not in seen_keys:
                seen_keys.add(r.row_key)
                all_rows.append(r)
        logger.info("page_collected", page=page_num, rows=len(rows), total=len(all_rows))
        if not await go_to_next_page(page):
            break
    return all_rows


# ---- Anexos modal ----


async def open_anexos_modal(page: Page, row: BandejaRow) -> bool:
    """Click 'Ver anexos' on a specific row.

    Side-effect aceptado (Aaron 2026-05-04): si la notif estaba "no leída"
    en SINOE, abrir el modal puede marcarla como leída server-side. VIKTORIA
    mantiene su propio estado vía `markedReadInViktoriaAt`.
    """
    if not row.ver_anexos_button_id:
        return False
    sel = f"button[id='{row.ver_anexos_button_id}']"
    loc = page.locator(sel).first
    if await loc.count() == 0:
        return False
    await loc.click()
    try:
        await page.wait_for_function(
            "() => { const d = document.querySelector('div[id$=\":dlgListaAnexos\"]');"
            " return d && getComputedStyle(d).display !== 'none'; }",
            timeout=10_000,
        )
    except Exception:
        return False
    return True


async def list_anexos(page: Page) -> list[AnexoItem]:
    """Read the contents of the open anexos dialog.

    El modal abre con `ui-datatable-empty-message` ("No se encontraron
    registros") como placeholder y se popula via AJAX. Reintentamos hasta
    `_LIST_ANEXOS_RETRIES` veces con `_LIST_ANEXOS_RETRY_DELAY_MS` entre
    intentos. Si tras eso sigue vacío, devolvemos lista vacía — la notif
    realmente no tiene anexos en SINOE.
    """
    _LIST_ANEXOS_RETRIES = 6
    _LIST_ANEXOS_RETRY_DELAY_MS = 700

    for attempt in range(1, _LIST_ANEXOS_RETRIES + 1):
        result = await _list_anexos_once(page)
        # Detectar placeholder PrimeFaces "ui-datatable-empty-message"
        is_placeholder_only = (
            len(result) == 1
            and not result[0].identificacion
            and not result[0].descarga_button_id
        )
        if not is_placeholder_only:
            if attempt > 1:
                logger.info("anexos_listed_after_retry", attempt=attempt, count=len(result))
            return result
        if attempt < _LIST_ANEXOS_RETRIES:
            await page.wait_for_timeout(_LIST_ANEXOS_RETRY_DELAY_MS)
    # Tras todos los reintentos sigue vacío — la notif no tiene anexos.
    logger.info("anexos_empty_confirmed", retries=_LIST_ANEXOS_RETRIES)
    return []


async def _list_anexos_once(page: Page) -> list[AnexoItem]:
    """Single read pass of the anexos dialog table."""
    raw: list[dict[str, Any]] = await page.evaluate(
        """
        () => {
          const rows = Array.from(document.querySelectorAll(
            'tbody[id$=":tblListaAnexos_data"] tr'
          ));
          return rows.map(r => {
            const tds = r.querySelectorAll('td');
            const btn = r.querySelector('button[id$=":clDescarga"], a[id$=":clDescarga"]');
            // Debug: dump del DOM de la fila para diagnosticar layouts atípicos
            // (ej. "no hay anexos" placeholder, columnas corridas, botón con
            // id distinto). Capturamos textContent de cada td y todos los
            // botones/links de la fila.
            const dom_debug = {
              td_count: tds.length,
              td_texts: Array.from(tds).map(td => (td.textContent || '').trim().slice(0, 80)),
              all_buttons: Array.from(r.querySelectorAll('button, a')).map(b => ({
                tag: b.tagName,
                id: b.id || null,
                title: b.getAttribute('title') || null,
                href: b.getAttribute('href') || null,
                text: (b.textContent || '').trim().slice(0, 40),
              })),
              row_class: r.getAttribute('class') || null,
              row_role: r.getAttribute('role') || null,
            };
            return {
              tipo: tds[0]?.textContent?.trim() || '',
              identificacion: tds[1]?.textContent?.trim() || '',
              paginas: parseInt(tds[2]?.textContent?.trim() || '0', 10),
              peso_text: tds[3]?.textContent?.trim() || '',
              descarga_button_id: btn?.id || null,
              _dom_debug: dom_debug,
            };
          });
        }
        """
    )
    # Stripeamos el debug del payload antes de mapear al dataclass; lo logueamos
    # aparte si vino con info útil (idents vacíos o sin botón).
    for r in raw:
        debug = r.pop("_dom_debug", None)
        if debug is not None and (not r.get("identificacion") or not r.get("descarga_button_id")):
            logger.debug("anexo_row_dom_debug", row=r, dom=debug)
    return [AnexoItem(**r) for r in raw]


async def close_anexos_modal(page: Page) -> None:
    """Close the anexos dialog using the PrimeFaces widget API.

    `dialogVarAnexos` is the widget var (developer-set, stable).
    Force-hide overlays as a defensive backup.
    """
    await page.evaluate(
        """
        () => {
            if (typeof PF === 'function') {
                try { PF('dialogVarAnexos').hide(); } catch(e) {}
            }
            document.querySelectorAll(
                'div.ui-widget-overlay, div[id$=":dlgListaAnexos_modal"]'
            ).forEach(el => { el.style.display = 'none'; });
            document.querySelectorAll('div[id$=":dlgListaAnexos"]').forEach(el => {
                el.style.display = 'none';
            });
        }
        """
    )
    await page.wait_for_timeout(400)
