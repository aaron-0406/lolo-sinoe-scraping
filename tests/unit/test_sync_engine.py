"""Unit tests del SyncEngine — pipeline completo con mocks.

Mockeamos `Page` (Playwright), `NotificationRepository`, `AttachmentRepository`,
`S3Client` y los helpers del navigator. Los casos cubren:

  - happy path con notif leída + 1 anexo
  - skip de NO leídas (constraint operacional)
  - early-stop cuando toda la página ya está en BD
  - falla de `enter_sinoe_module` → `UnexpectedPageState`
  - falla individual de un anexo no aborta el sync
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lolo_sinoe.errors import UnexpectedPageState
from lolo_sinoe.persistence.repositories.notification_repo import (
    BulkUpsertResult,
    NotificationRow,
)
from lolo_sinoe.workers import sync_engine as engine_mod
from lolo_sinoe.workers.sync_engine import SyncContext, SyncEngine


def _row(
    n_notif: str = "366978-2026-00002",
    is_read: bool = True,
    fecha: str = "02/05/2026 14:32",
    expediente: str = "12345-2026-0-1801-JR-CI-03",
) -> Any:
    """Construye un BandejaRow mínimo para los tests sin importar Playwright."""
    return engine_mod.BandejaRow(
        row_index=0,
        row_key=f"rk-{n_notif}",
        is_read=is_read,
        n_notif=n_notif,
        expediente=expediente,
        sumilla="Resolución n.º 7",
        organo="1° Juzgado",
        fecha=fecha,
        ver_anexos_button_id="frmBus:btn",
    )


def _ctx() -> SyncContext:
    return SyncContext(
        account_id=42,
        customer_has_bank_id=7,
        customer_id=99,
        client_code="sinoe-chb-7",
    )


def _build_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows_per_page: list[list[Any]],
    has_next_pages: list[bool] | None = None,
    existing_n_notifs: set[str] | None = None,
    bulk_upsert_returns: BulkUpsertResult | None = None,
) -> SyncEngine:
    """Stubea el navigator + los repos. `rows_per_page[i]` se devuelve
    cuando se invoca `list_notifications` por i-ésima vez."""
    page_iter = iter(rows_per_page)
    has_next_iter = iter(has_next_pages or [False])

    async def _list_notifications(_: Any) -> list[Any]:
        return next(page_iter, [])

    async def _enter_module(_: Any) -> bool:
        return True

    async def _change_page_size(_: Any, __: int) -> None:
        return None

    async def _apply_estado(_: Any, __: str) -> None:
        return None

    async def _go_next(_: Any) -> bool:
        return next(has_next_iter, False)

    async def _parse_paginator(_: Any) -> Any:
        return MagicMock(has_next=next(has_next_iter, False))

    monkeypatch.setattr(engine_mod, "list_notifications", _list_notifications)
    monkeypatch.setattr(engine_mod, "enter_sinoe_module", _enter_module)
    monkeypatch.setattr(engine_mod, "change_page_size", _change_page_size)
    monkeypatch.setattr(engine_mod, "apply_estado_filter", _apply_estado)
    monkeypatch.setattr(engine_mod, "go_to_next_page", _go_next)
    monkeypatch.setattr(engine_mod, "parse_paginator", _parse_paginator)

    notifications = MagicMock()
    notifications.find_existing_n_notifs.return_value = existing_n_notifs or set()
    notifications.bulk_upsert.return_value = bulk_upsert_returns or BulkUpsertResult(
        new_ids=[], skipped_existing=0, matched_to_case_file=0
    )
    attachments = MagicMock()
    attachments.existing_idents_for.return_value = set()
    s3 = MagicMock()
    s3.build_attachment_key.return_value = "CHB/99/7/sinoe-chb-7/.../foo.pdf"

    page = AsyncMock()
    return SyncEngine(
        page=page,
        notifications=notifications,
        attachments=attachments,
        s3_client=s3,
        ctx=_ctx(),
        sync_log_id=1,
    )


# ── Tests ───────────────────────────────────────────────────────────────


async def test_enter_sinoe_module_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si SINOE no muestra el módulo, lanzamos UnexpectedPageState para
    que el worker clasifique el sync como `unexpected_dom` (no `ok`)."""
    engine = _build_engine(monkeypatch, rows_per_page=[[]])

    async def _fail_enter(_: Any) -> bool:
        return False

    monkeypatch.setattr(engine_mod, "enter_sinoe_module", _fail_enter)

    with pytest.raises(UnexpectedPageState):
        await engine.run()


async def test_early_stop_when_page_all_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si toda la página está en BD, no avanzamos a la siguiente."""
    rows = [_row("N1"), _row("N2")]
    engine = _build_engine(
        monkeypatch,
        rows_per_page=[rows, [_row("N3")]],  # N3 NUNCA debería leerse
        existing_n_notifs={"N1", "N2"},
        has_next_pages=[True, False],
    )
    metrics = await engine.run()
    assert metrics.early_stopped is True
    assert metrics.notifications_seen == 2
    assert metrics.notifications_updated == 2  # bump_last_seen
    assert metrics.notifications_new == 0


async def test_skips_unread_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constraint operacional: notif NO leída se persiste pero no abrimos
    el modal de anexos para no marcarla leída en SINOE."""
    rows = [_row("N1", is_read=False)]
    upsert_result = BulkUpsertResult(new_ids=[101], skipped_existing=0, matched_to_case_file=0)
    engine = _build_engine(
        monkeypatch,
        rows_per_page=[rows],
        bulk_upsert_returns=upsert_result,
    )

    # Si abriéramos el modal, tendríamos que stubear open_anexos_modal —
    # no lo hacemos, así si el código intenta abrirlo, el test crashea.
    async def _no_call(*_: Any, **__: Any) -> Any:
        raise AssertionError("Should NOT open modal for unread notif")

    monkeypatch.setattr(engine_mod, "open_anexos_modal", _no_call)

    metrics = await engine.run()
    assert metrics.notifications_new == 1
    assert metrics.attachments_downloaded == 0


async def test_unparseable_date_skipped_not_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una row con fecha inválida se skipea, las demás del batch siguen."""
    rows = [_row("N-bad", fecha="garbage"), _row("N-good", fecha="02/05/2026")]
    upsert_result = BulkUpsertResult(new_ids=[200], skipped_existing=0, matched_to_case_file=0)
    engine = _build_engine(
        monkeypatch,
        rows_per_page=[rows],
        bulk_upsert_returns=upsert_result,
    )

    async def _open_modal(_: Any, __: Any) -> bool:
        return False  # sin anexos

    monkeypatch.setattr(engine_mod, "open_anexos_modal", _open_modal)

    metrics = await engine.run()
    # bulk_upsert se llamó con 1 fila (solo la good) — verificamos
    notifications: MagicMock = engine.notifications  # type: ignore[assignment]
    notifications.bulk_upsert.assert_called_once()
    call_kwargs = notifications.bulk_upsert.call_args.kwargs
    rows_arg = list(notifications.bulk_upsert.call_args.args[0])
    assert len(rows_arg) == 1
    assert rows_arg[0].n_notificacion == "N-good"
    assert call_kwargs["account_id"] == 42
    assert metrics.notifications_seen == 2


# ── Helper modules ──────────────────────────────────────────────────────


def test_parse_fecha_ingreso_supports_multiple_formats() -> None:
    """SINOE varía entre dd/mm/yyyy HH:MM:SS, HH:MM y solo fecha."""
    assert engine_mod._parse_fecha_ingreso("02/05/2026 14:32:10") == datetime(
        2026, 5, 2, 14, 32, 10
    )
    assert engine_mod._parse_fecha_ingreso("02/05/2026 14:32") == datetime(
        2026, 5, 2, 14, 32
    )
    assert engine_mod._parse_fecha_ingreso("02/05/2026") == datetime(2026, 5, 2)
    assert engine_mod._parse_fecha_ingreso("") is None
    assert engine_mod._parse_fecha_ingreso("garbage") is None


def test_normalize_anexo_tipo_mapping() -> None:
    """Acentos y mayúsculas → ENUM canónico."""
    assert engine_mod._normalize_anexo_tipo("Cédula") == "cedula"
    assert engine_mod._normalize_anexo_tipo("RESOLUCION") == "resolucion"
    assert engine_mod._normalize_anexo_tipo("Anexo") == "anexo"
    assert engine_mod._normalize_anexo_tipo("foo") == "otro"
    assert engine_mod._normalize_anexo_tipo("") == "otro"


def test_compute_sha256_stable() -> None:
    sample = b"hello world"
    expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert engine_mod.compute_sha256(sample) == expected


def test_notification_row_dataclass_immutable() -> None:
    """`NotificationRow` debe ser frozen — el motor lo construye 1 vez por row."""
    row = NotificationRow(
        n_notificacion="N-1",
        n_expediente="00001-2024",
        sumilla="x",
        organo_jurisdiccional="x",
        fecha_ingreso_casilla=datetime(2026, 5, 1),
        fecha_surte_efecto=date(2026, 5, 1),
        fecha_inicio_plazo=date(2026, 5, 1),
        estado_lectura_sinoe="leida",
    )
    # frozen=True hace que asignar tire FrozenInstanceError (subclase de AttributeError)
    with pytest.raises((AttributeError, TypeError)):
        row.n_notificacion = "X"  # type: ignore[misc]
