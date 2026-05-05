"""Tests del BrowserPool — mockeamos Playwright para no levantar Chromium real.

Cubrimos:
  - start() crea N browsers, stop() los cierra todos
  - acquire_context devuelve handles + cierra context al salir
  - reciclado tras max_pages_per_browser
  - workers concurrentes serializan en queue
  - acquire antes de start tira RuntimeError
  - acquire durante shutdown tira RuntimeError
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lolo_sinoe.browser import pool as pool_mod
from lolo_sinoe.browser.pool import BrowserPool


class _FakeBrowser:
    """Stand-in de `playwright.async_api.Browser` con los métodos que usamos."""

    def __init__(self) -> None:
        self.closed = False
        self._next_context_id = 0

    async def new_context(self, **_: Any) -> _FakeContext:
        self._next_context_id += 1
        return _FakeContext()

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def set_default_navigation_timeout(self, _: int) -> None:
        pass

    def set_default_timeout(self, _: int) -> None:
        pass

    async def new_page(self) -> Any:
        return MagicMock()

    async def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """Wrapper que devuelve un browser nuevo por cada `chromium.launch()`."""

    def __init__(self) -> None:
        self.launched: list[_FakeBrowser] = []
        self.stopped = False
        chromium = AsyncMock()
        chromium.launch = self._launch
        self.chromium = chromium

    async def _launch(self, **_: Any) -> _FakeBrowser:
        b = _FakeBrowser()
        self.launched.append(b)
        return b

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakePlaywright:
    """Reemplaza `async_playwright().start()` por nuestro fake."""
    fake = _FakePlaywright()

    class _Manager:
        async def start(self) -> _FakePlaywright:
            return fake

    def _factory() -> _Manager:
        return _Manager()

    monkeypatch.setattr(pool_mod, "async_playwright", _factory)
    return fake


# ── Tests ───────────────────────────────────────────────────────────────


async def test_start_creates_n_browsers(fake_playwright: _FakePlaywright) -> None:
    pool = BrowserPool(size=3)
    await pool.start()
    assert len(fake_playwright.launched) == 3
    await pool.stop()


async def test_stop_closes_all_browsers(fake_playwright: _FakePlaywright) -> None:
    pool = BrowserPool(size=2)
    await pool.start()
    await pool.stop()
    assert all(b.closed for b in fake_playwright.launched)
    assert fake_playwright.stopped is True


async def test_acquire_yields_handles_and_closes_context(
    fake_playwright: _FakePlaywright,
) -> None:
    pool = BrowserPool(size=1)
    await pool.start()
    try:
        async with pool.acquire_context() as handles:
            assert handles.browser is fake_playwright.launched[0]
            ctx = handles.context
        # Salir del with debe cerrar el context — el browser sigue vivo.
        assert ctx.closed is True  # type: ignore[attr-defined]
        assert fake_playwright.launched[0].closed is False
    finally:
        await pool.stop()


async def test_recycle_after_max_pages(fake_playwright: _FakePlaywright) -> None:
    """Tras `max_pages_per_browser` acquires, el browser se cierra y relaunch."""
    pool = BrowserPool(size=1, max_pages_per_browser=3)
    await pool.start()
    initial_browser = fake_playwright.launched[0]
    try:
        # 3 acquires con el browser inicial — el reciclado pasa al CUARTO
        # acquire (cuando pages_served alcanza el límite ANTES de servir).
        for _ in range(3):
            async with pool.acquire_context():
                pass
        assert initial_browser.closed is False
        assert len(fake_playwright.launched) == 1

        # 4to acquire dispara reciclado: cierra el viejo, launchea uno nuevo.
        async with pool.acquire_context():
            pass
        assert initial_browser.closed is True
        assert len(fake_playwright.launched) == 2
        assert fake_playwright.launched[1].closed is False
    finally:
        await pool.stop()


async def test_concurrent_acquires_serialize_when_pool_exhausted(
    fake_playwright: _FakePlaywright,
) -> None:
    """Con size=1 y 2 workers, el segundo espera a que el primero libere."""
    pool = BrowserPool(size=1)
    await pool.start()
    barrier = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with pool.acquire_context():
            order.append("first_acquired")
            await barrier.wait()
            order.append("first_releasing")

    async def second() -> None:
        # Pequeño delay para que `first` tome el browser primero.
        await asyncio.sleep(0.01)
        async with pool.acquire_context():
            order.append("second_acquired")

    try:
        task_first = asyncio.create_task(first())
        task_second = asyncio.create_task(second())
        # Dejar que ambas tareas progresen — `second` debe estar bloqueada.
        await asyncio.sleep(0.05)
        assert order == ["first_acquired"], f"orden inesperado: {order}"
        # Liberar `first` — `second` puede avanzar.
        barrier.set()
        await asyncio.gather(task_first, task_second)
        assert order == ["first_acquired", "first_releasing", "second_acquired"]
    finally:
        await pool.stop()


async def test_acquire_before_start_raises() -> None:
    pool = BrowserPool(size=1)
    with pytest.raises(RuntimeError, match="no fue iniciado"):
        async with pool.acquire_context():
            pass


async def test_acquire_during_shutdown_raises(fake_playwright: _FakePlaywright) -> None:
    pool = BrowserPool(size=1)
    await pool.start()
    # Forzamos el flag de shutdown sin completar — emula la ventana entre
    # stop() arrancando y los workers todavía intentando adquirir.
    pool._stopping = True
    try:
        with pytest.raises(RuntimeError, match="shutdown"):
            async with pool.acquire_context():
                pass
    finally:
        pool._stopping = False
        await pool.stop()


async def test_double_start_is_idempotent(fake_playwright: _FakePlaywright) -> None:
    pool = BrowserPool(size=2)
    await pool.start()
    await pool.start()  # no-op
    assert len(fake_playwright.launched) == 2
    await pool.stop()


async def test_invalid_size_raises() -> None:
    with pytest.raises(ValueError):
        BrowserPool(size=0)


async def test_context_close_failure_does_not_leak_browser(
    fake_playwright: _FakePlaywright,
) -> None:
    """Si el context.close() falla, el browser igual vuelve al pool —
    el contador eventualmente lo recicla."""
    pool = BrowserPool(size=1, max_pages_per_browser=10)
    await pool.start()

    # Patch: que el primer context falle al cerrarse.
    original_new_context = fake_playwright.launched[0].new_context

    async def flaky_new_context(**kwargs: Any) -> _FakeContext:
        ctx = await original_new_context(**kwargs)

        async def _bad_close() -> None:
            raise RuntimeError("simulated context close error")

        ctx.close = _bad_close  # type: ignore[method-assign]
        return ctx

    fake_playwright.launched[0].new_context = flaky_new_context  # type: ignore[method-assign]

    try:
        # No debería propagar — el pool loggea y sigue.
        with suppress(Exception):
            async with pool.acquire_context():
                pass
        # Browser sigue disponible para el próximo acquire.
        async with pool.acquire_context() as handles:
            assert handles.browser is fake_playwright.launched[0]
    finally:
        await pool.stop()
