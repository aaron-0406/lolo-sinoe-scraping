"""Browser pool reusable entre workers — minimiza launches de Chromium.

El patrón anterior (`launch_browser` context manager) launcheaba un proceso
Chromium nuevo por cada sync. Para 50 cuentas x 48 syncs/dia = 2,400 launches
con ~2-3s cada uno = 1.5-2h diarias en cold-starts.

Este pool mantiene N browsers vivos. Cada `acquire_context()` toma un browser
de la cola, crea un BrowserContext fresco con `storage_state` específico, y
devuelve el browser al pool al salir. El reciclado ocurre cuando un browser
ha servido `max_pages_per_browser` contexts (default 20, paridad con el CEJ
scraper) — defensivo contra memory leaks acumulados de Playwright.

Concurrencia: si N=2 y hay 3 workers compitiendo, el tercero espera en
`Queue.get()` hasta que uno libere. Por diseño no creamos más browsers que
`size` — escalar concurrencia requiere subir `SINOE_BROWSER_POOL_SIZE`.

Para CLI legacy (`lolo-sinoe login`/`explore`) se sigue usando
`launch_browser` directo: no vale la pena montar un pool para una corrida
única.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from playwright.async_api import (
    Browser,
    Playwright,
    StorageState,
    async_playwright,
)

from lolo_sinoe.browser.launcher import BrowserHandles
from lolo_sinoe.browser.stealth_args import STEALTH_ARGS
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


# Defaults compartidos con `launch_browser` — `_CONTEXT_DEFAULTS` no se
# importa para evitar acoplamiento; redeclaramos los mismos valores acá.
#
# user_agent: SINOE BLOQUEA peticiones cuyo UA contenga "HeadlessChrome"
# (default de Playwright cuando headless=True) — el server cuelga la
# request hasta timeout. Forzamos un UA de Chrome stable para evitar el
# block, headless o no.
_CONTEXT_DEFAULTS: dict[str, Any] = {
    "viewport": {"width": 1366, "height": 768},
    "locale": "es-PE",
    "timezone_id": "America/Lima",
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Reciclado por browser para evitar leaks acumulados (Plan §4.1).
DEFAULT_MAX_PAGES_PER_BROWSER = 20


@dataclass
class _PooledBrowser:
    """Wrapper interno con bookkeeping para reciclar."""

    browser: Browser
    pages_served: int = 0


class BrowserPool:
    """Pool fijo de Chromium browsers reusables.

    Lifecycle:
      - `start()` — arranca Playwright + lanza `size` browsers iniciales.
      - `acquire_context(...)` — toma un browser, crea un context con el
        storage_state pedido, devuelve `BrowserHandles`. Al salir cierra
        SOLO el context (no el browser) y devuelve el browser a la queue.
      - `stop()` — cierra todos los browsers + apaga Playwright.

    El pool es reusable across syncs. NO compartas contexts entre cuentas
    porque las cookies se cruzan — siempre `acquire_context()` por cuenta.
    """

    def __init__(
        self,
        size: int = 2,
        max_pages_per_browser: int = DEFAULT_MAX_PAGES_PER_BROWSER,
        headless: bool = True,
    ) -> None:
        if size < 1:
            raise ValueError(f"BrowserPool size debe ser ≥1, dado {size}")
        self._size = size
        self._max_pages = max_pages_per_browser
        self._headless = headless
        self._playwright: Playwright | None = None
        self._available: asyncio.Queue[_PooledBrowser] = asyncio.Queue(maxsize=size)
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        """Arranca Playwright y crea los `size` browsers iniciales.
        Idempotente — segundas llamadas no-op."""
        if self._started:
            return
        self._playwright = await async_playwright().start()
        for _ in range(self._size):
            browser = await self._launch_browser()
            self._available.put_nowait(_PooledBrowser(browser=browser))
        self._started = True
        logger.info(
            "browser_pool_started",
            size=self._size,
            max_pages_per_browser=self._max_pages,
            headless=self._headless,
        )

    async def stop(self) -> None:
        """Drena la queue cerrando cada browser, luego apaga Playwright."""
        if not self._started or self._stopping:
            return
        self._stopping = True
        # Drenar — los workers que estén esperando en `acquire_context`
        # van a fallar al obtener un PooledBrowser cancelado, lo cual es
        # OK durante shutdown.
        drained: list[_PooledBrowser] = []
        while not self._available.empty():
            drained.append(self._available.get_nowait())
        for pooled in drained:
            try:
                await pooled.browser.close()
            except Exception as e:
                logger.warning("browser_close_failed_during_shutdown", error=str(e))
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._started = False
        self._stopping = False
        logger.info("browser_pool_stopped")

    @asynccontextmanager
    async def acquire_context(
        self,
        *,
        storage_state: str | dict[str, Any] | None = None,
        nav_timeout_ms: int = 30_000,
    ) -> AsyncIterator[BrowserHandles]:
        """Reserva un browser, crea un context fresco, yield handles.

        Al salir del `async with`:
          1. cierra el context (cookies + cache de la cuenta).
          2. incrementa el contador del browser.
          3. si llegó al límite de pages, lo recicla (close + relaunch).
          4. devuelve el browser a la queue.

        El browser NO se cierra entre acquires — es la optimización entera.
        """
        if not self._started:
            raise RuntimeError("BrowserPool no fue iniciado. Llamar `start()` primero.")
        if self._stopping:
            raise RuntimeError("BrowserPool está en shutdown — no se aceptan acquires.")

        pooled = await self._available.get()
        try:
            # Reciclar ANTES del context — si el browser está saturado,
            # arrancar uno nuevo en lugar de servir un context degradado.
            if pooled.pages_served >= self._max_pages:
                logger.info(
                    "browser_pool_recycling",
                    pages_served=pooled.pages_served,
                    max_pages=self._max_pages,
                )
                try:
                    await pooled.browser.close()
                except Exception as e:
                    logger.warning("browser_close_failed_during_recycle", error=str(e))
                pooled.browser = await self._launch_browser()
                pooled.pages_served = 0

            context_kwargs: dict[str, Any] = dict(_CONTEXT_DEFAULTS)
            if storage_state is not None:
                context_kwargs["storage_state"] = (
                    cast(StorageState, storage_state)
                    if isinstance(storage_state, dict)
                    else storage_state
                )
            context = await pooled.browser.new_context(**context_kwargs)
            context.set_default_navigation_timeout(nav_timeout_ms)
            context.set_default_timeout(nav_timeout_ms)
            page = await context.new_page()

            logger.info(
                "browser_pool_context_created",
                pages_served=pooled.pages_served,
                reused_storage_state=storage_state is not None,
            )

            try:
                yield BrowserHandles(browser=pooled.browser, context=context, page=page)
            finally:
                # Cerrar el context aunque haya excepción — los recursos
                # del browser se reusan; solo descartamos cookies/page.
                try:
                    await context.close()
                except Exception as e:
                    logger.warning("context_close_failed", error=str(e))
        finally:
            pooled.pages_served += 1
            # Devolvemos siempre — incluso si el context falló al crearse.
            # El próximo acquire lo recibirá; si el browser quedó zombi,
            # el contador eventualmente lo recicla.
            self._available.put_nowait(pooled)

    async def _launch_browser(self) -> Browser:
        assert self._playwright is not None
        return await self._playwright.chromium.launch(
            headless=self._headless,
            args=STEALTH_ARGS,
        )
