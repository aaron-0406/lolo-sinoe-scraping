"""Pure-Python tests for sinoe_navigator (no browser required)."""

import pytest

from lolo_sinoe.exploration.sinoe_navigator import BandejaRow, open_anexos_modal


class _StubLocator:
    async def count(self) -> int:
        return 0

    async def click(self) -> None:
        return None

    @property
    def first(self) -> "_StubLocator":
        return self


class _StubPage:
    def locator(self, *_: object, **__: object) -> _StubLocator:
        return _StubLocator()


async def test_open_anexos_accepts_unread_row() -> None:
    """Decisión 2026-05-04: side-effect de marcar leído en SINOE es aceptado.
    Para notifs no-leídas con botón de anexos, abrimos el modal igual."""
    unread = BandejaRow(
        row_index=0,
        row_key="x",
        is_read=False,
        n_notif="123-2026",
        expediente="00001-2024-0-1601-JR-CI-01",
        sumilla="Test",
        organo="Juzgado X",
        fecha="2026-04-30 12:00:00",
        ver_anexos_button_id="frmBusqueda:tblLista:0:j_idt112",
    )
    page = _StubPage()
    # No raise — la operación procede; el resultado depende del stub de page.
    # Como _StubPage.locator no resuelve, esperamos False (no encontró el botón).
    result = await open_anexos_modal(page, unread)  # type: ignore[arg-type]
    assert result is False


async def test_open_anexos_returns_false_when_button_missing() -> None:
    """If a read row has no button id, return False instead of raising."""
    read_no_button = BandejaRow(
        row_index=0,
        row_key="x",
        is_read=True,
        n_notif="123-2026",
        expediente="00001-2024-0-1601-JR-CI-01",
        sumilla="Test",
        organo="Juzgado X",
        fecha="2026-04-30 12:00:00",
        ver_anexos_button_id=None,
    )
    page = _StubPage()
    result = await open_anexos_modal(page, read_no_button)  # type: ignore[arg-type]
    assert result is False
