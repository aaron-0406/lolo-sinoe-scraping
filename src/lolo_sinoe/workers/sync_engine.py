"""Motor de sync end-to-end de una cuenta SINOE.

Separa la lógica de "qué hacer en SINOE para una cuenta" del shell del
worker (claim cuenta, KMS, etc.). Permite testear el flujo con mocks de
`page` sin tocar BullMQ ni KMS.

Pipeline (Plan §6.4 + §2.2.1):

  1. Entrar al módulo "Casillas Electrónicas" desde el hub post-login.
  2. Filtrar bandeja por estado="Leído" — sólo abrimos notifs ya leídas
     para no producir el side-effect del cambio "no leída → leída"
     (constraint operacional 2026-04-30).
  3. Subir page size a 50 — minimiza clicks de paginación.
  4. Loop por página:
       - leer rows
       - early-stop si TODA la página ya está en BD (orden DESC)
       - bulk_upsert de las nuevas
       - bump_last_seen de las ya conocidas
       - para cada nueva: abrir modal de anexos, descargar uno por uno,
         extraer metadata PDF (firma + páginas), subir a S3, persistir
         en SINOE_NOTIFICATION_ATTACHMENT.
       - cerrar modal.
  5. Devolver métricas para SINOE_SYNC_LOG.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Any

import structlog
from playwright.async_api import Page

from ..errors import UnexpectedPageState
from ..exploration.sinoe_navigator import (
    AnexoItem,
    BandejaRow,
    apply_estado_filter,
    change_page_size,
    close_anexos_modal,
    enter_sinoe_module,
    go_to_next_page,
    list_anexos,
    list_notifications,
    open_anexos_modal,
    parse_paginator,
)
from ..persistence.repositories.notification_repo import (
    AttachmentRepository,
    NotificationRepository,
    NotificationRow,
)
from ..persistence.s3.s3_client import S3Client
from .rate_limiter import RateLimiter

__all__ = [
    "PAGE_SIZE",
    "AnexoItem",
    "BandejaRow",
    "NotificationRow",
    "SyncContext",
    "SyncEngine",
    "SyncMetrics",
    "compute_attachment_metadata",
    "compute_sha256",
]

logger = structlog.get_logger(__name__)


PAGE_SIZE = 50
JITTER_MIN_S = 0.5
JITTER_MAX_S = 1.0


@dataclass
class SyncMetrics:
    notifications_seen: int = 0
    notifications_new: int = 0
    notifications_updated: int = 0
    attachments_downloaded: int = 0
    attachments_skipped_dedupe: int = 0
    bytes_downloaded: int = 0
    notifications_matched_to_case_file: int = 0
    pages_visited: int = 0
    early_stopped: bool = False


@dataclass
class SyncContext:
    """Datos por sync que el motor necesita para construir paths S3 y
    persistir filas. Lo arma el worker tras cargar la cuenta.

    Para `customer_id` y `client_code` el worker hace un solo lookup en
    BD (las notifs ya tienen denormalizado `customer_has_bank_id` para
    queries cross-CHB, pero el path S3 requiere customer + client).
    """

    account_id: int
    customer_has_bank_id: int
    customer_id: int
    client_code: str


# ── Parsers ─────────────────────────────────────────────────────────────


_DATE_PATTERNS = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
]


def _parse_fecha_ingreso(raw: str) -> datetime | None:
    """SINOE muestra `dd/mm/yyyy HH:MM` o `dd/mm/yyyy` según versión.
    Devuelve `None` si nada matchea — el caller decide si skipear o usar
    `now()` como fallback (preferido fallar rápido).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


_TIPO_NORMALIZE = {
    "cedula": "cedula",
    "cédula": "cedula",
    "resolucion": "resolucion",
    "resolución": "resolucion",
    "anexo": "anexo",
    "escrito": "escrito",
}


def _normalize_anexo_tipo(raw: str) -> str:
    """Mapea el `tipo` (texto del PJ) al ENUM del schema."""
    key = (raw or "").strip().lower()
    return _TIPO_NORMALIZE.get(key, "otro")


# ── PDF metadata ────────────────────────────────────────────────────────


def _has_digital_signature(reader_root: dict[str, Any]) -> bool:
    """Detecta firma digital PKCS#7 en el PDF.

    El PJ de Perú incrusta la firma como `/AcroForm > /Fields[].FT == /Sig`.
    """
    form = reader_root.get("/AcroForm") if reader_root else None
    if not form:
        return False
    fields = form.get("/Fields") or []
    for f in fields:
        obj = f.get_object() if hasattr(f, "get_object") else f
        if obj.get("/FT") == "/Sig":
            return True
    return False


def _safe_extract(extractor: Any, *args: Any, default: Any) -> Any:
    """Helper: ejecuta un callable, devuelve `default` si lanza."""
    try:
        return extractor(*args)
    except Exception:
        return default


def compute_attachment_metadata(file_bytes: bytes) -> tuple[bool, int | None]:
    """Devuelve `(tiene_firma_digital, numero_paginas)`.

    Best-effort — si pypdf no puede parsear, devuelve `(False, None)` para
    no abortar el sync (el archivo se sube a S3 igual).
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        logger.warning("pypdf_not_installed", error=str(e))
        return False, None

    try:
        reader = PdfReader(BytesIO(file_bytes))
    except Exception as e:
        logger.warning("pdf_metadata_extraction_failed", error=str(e))
        return False, None

    num_paginas: int | None = _safe_extract(lambda r: len(r.pages), reader, default=None)
    tiene_firma: bool = _safe_extract(
        lambda r: _has_digital_signature(r.trailer.get("/Root", {})),
        reader,
        default=False,
    )
    return tiene_firma, num_paginas


def compute_sha256(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


# ── Engine ──────────────────────────────────────────────────────────────


@dataclass
class SyncEngine:
    """Ejecuta el pipeline completo. El worker instancia uno por sync."""

    page: Page
    notifications: NotificationRepository
    attachments: AttachmentRepository
    s3_client: S3Client
    ctx: SyncContext
    sync_log_id: int
    rate_limiter: RateLimiter | None = None
    metrics: SyncMetrics = field(default_factory=SyncMetrics)

    async def run(self) -> SyncMetrics:
        """Pipeline end-to-end. El worker pasa la `page` ya lista (sesión
        válida o re-login). Errores propagan al caller para clasificar
        contra el ENUM `last_sync_status`.

        Lanza `UnexpectedPageState` si SINOE redirige a un layout que no
        sabemos navegar (ej. cambio de UI). El worker lo clasifica como
        `unexpected_dom` en `last_sync_status`.
        """
        await self._tick_rate_limit()
        if not await enter_sinoe_module(self.page):
            raise UnexpectedPageState(
                f"No se pudo entrar al módulo Casillas Electrónicas para la cuenta {self.ctx.account_id}"
            )

        # Filtro estado="" (Todos): traemos leídas y no-leídas. Las no-leídas
        # también necesitan tener sus anexos descargados — abrir el modal en
        # SINOE puede marcarlas como leídas server-side, side-effect aceptado.
        try:
            await self._tick_rate_limit()
            await apply_estado_filter(self.page, "")
        except Exception as e:
            logger.info("estado_filter_skipped", error=str(e))

        # Maximizar page size una sola vez — silencioso si falla
        # (algunas cuentas vienen con paginator distinto).
        try:
            await change_page_size(self.page, PAGE_SIZE)
        except Exception as e:
            logger.info("page_size_change_skipped", error=str(e))

        await self._iterate_pages()
        return self.metrics

    async def _tick_rate_limit(self) -> None:
        """Consume 1 token del rate limiter compartido — protege a SINOE
        de ráfagas. Si no hay limiter (tests), no-op."""
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()

    async def _iterate_pages(self) -> None:
        page_num = 0
        while True:
            page_num += 1
            self.metrics.pages_visited = page_num
            rows = await list_notifications(self.page)
            if not rows:
                logger.info("empty_page_stop", page=page_num)
                break

            self.metrics.notifications_seen += len(rows)

            n_notifs = [r.n_notif for r in rows if r.n_notif]
            existing = self.notifications.find_existing_n_notifs(self.ctx.account_id, n_notifs)
            new_rows = [r for r in rows if r.n_notif and r.n_notif not in existing]
            existing_rows = [r for r in rows if r.n_notif in existing]

            if existing_rows:
                self.notifications.bump_last_seen(
                    self.ctx.account_id,
                    [r.n_notif for r in existing_rows],
                    self.sync_log_id,
                )
                self.metrics.notifications_updated += len(existing_rows)

                # Backfill de inconsistencias: si SINOE muestra el botón
                # "Ver anexos" pero en BD no hay ningún attachment, intentamos
                # re-descargar. Cubre notifs upserteadas por versiones previas
                # que skipeaban anexos para no-leídas.
                rows_with_anexos = [
                    r for r in existing_rows if r.ver_anexos_button_id
                ]
                if rows_with_anexos:
                    missing = self.notifications.find_missing_attachments(
                        self.ctx.account_id,
                        [r.n_notif for r in rows_with_anexos],
                    )
                    if missing:
                        # Lookup batch del FK al expediente — para enrutear el
                        # anexo a `case-file/{id}/...` cuando la notif ya está
                        # matched (típico en backfill: la notif vino de un sync
                        # previo y mientras tanto se creó el expediente).
                        case_file_ids = self.notifications.get_case_file_ids(
                            list(missing.values())
                        )
                        logger.info(
                            "attachment_backfill_pending",
                            count=len(missing),
                            n_notifs=list(missing.keys()),
                        )
                        for row in rows_with_anexos:
                            notif_id = missing.get(row.n_notif)
                            if notif_id is None:
                                continue
                            try:
                                await self._download_attachments_for(
                                    row,
                                    notif_id,
                                    case_file_ids.get(notif_id),
                                )
                            except Exception as e:
                                logger.exception(
                                    "attachment_backfill_failed",
                                    n_notif=row.n_notif,
                                    error=str(e),
                                )

            if new_rows:
                inserted_ids = await self._upsert_new_rows(new_rows)
                # Después del bulk_upsert el SQL post-batch ya popó el FK al
                # expediente para las notifs que matchearon. Lo leemos en una
                # query batch para enrutear el anexo al folder correcto.
                non_null_ids = [i for i in inserted_ids if i is not None]
                case_file_ids = self.notifications.get_case_file_ids(non_null_ids)
                # Descargar anexos por cada notif nueva, leída o no en SINOE.
                # Side-effect aceptado: abrir el modal puede marcar como leída
                # en SINOE las que estaban no leídas. VIKTORIA mantiene su
                # propio estado de lectura (`markedReadInViktoriaAt`).
                for row, notif_id in zip(new_rows, inserted_ids, strict=True):
                    if notif_id is None:
                        continue
                    try:
                        await self._download_attachments_for(
                            row,
                            notif_id,
                            case_file_ids.get(notif_id),
                        )
                    except Exception as e:
                        # No abortar el sync por un anexo problemático —
                        # dejamos error en logs y seguimos. El próximo sync
                        # los reintenta porque el row de attachment falta.
                        logger.exception(
                            "attachment_download_failed",
                            n_notif=row.n_notif,
                            error=str(e),
                        )

            # Early-stop: si NADA en esta página fue nuevo, las páginas
            # siguientes (más viejas) tampoco van a serlo (orden DESC).
            if not new_rows:
                self.metrics.early_stopped = True
                logger.info("early_stop", page=page_num, reason="no_new_in_page")
                break

            pag = await parse_paginator(self.page)
            if not pag.has_next:
                break
            await self._tick_rate_limit()
            ok = await go_to_next_page(self.page)
            if not ok:
                break

    async def _upsert_new_rows(self, rows: list[BandejaRow]) -> list[int | None]:
        """Bulk upsert con parsing de fechas. Devuelve ids alineados 1:1
        con `rows` — `None` si la fila se skipeó por dato faltante o si
        otra ejecución concurrente la insertó primero (UNIQUE collision).

        Asume que el caller ya filtró los rows que existían en BD; lo que
        llega acá debería ser todo "nuevo" desde el punto de vista de
        este worker.
        """
        processable: list[NotificationRow | None] = []
        for r in rows:
            fecha_ingreso = _parse_fecha_ingreso(r.fecha)
            if not fecha_ingreso:
                logger.warning("skip_row_unparseable_date", n_notif=r.n_notif, fecha=r.fecha)
                processable.append(None)
                continue
            # SINOE no expone fecha_surte_efecto/fecha_inicio_plazo separadas
            # en el listado — el cómputo legal lo hace el backend. Acá
            # usamos fecha_ingreso como placeholder (DATE-only).
            fecha_base: date = fecha_ingreso.date()
            processable.append(
                NotificationRow(
                    n_notificacion=r.n_notif,
                    n_expediente=r.expediente,
                    sumilla=r.sumilla,
                    organo_jurisdiccional=r.organo,
                    fecha_ingreso_casilla=fecha_ingreso,
                    fecha_surte_efecto=fecha_base,
                    fecha_inicio_plazo=fecha_base,
                    estado_lectura_sinoe="leida" if r.is_read else "no_leida",
                    sinoe_row_uuid=r.row_key,
                )
            )

        valid = [p for p in processable if p is not None]
        if not valid:
            return [None] * len(rows)

        result = self.notifications.bulk_upsert(
            valid,
            account_id=self.ctx.account_id,
            customer_has_bank_id=self.ctx.customer_has_bank_id,
            sync_log_id=self.sync_log_id,
        )
        self.metrics.notifications_new += len(result.new_ids)
        self.metrics.notifications_matched_to_case_file += result.matched_to_case_file

        # Mapeo orden-preservado: bulk_upsert insertó las `valid` en orden,
        # `new_ids` viene en ese mismo orden. Las que devolvieron None en
        # processable mantienen None en el output. Si hubo race condition
        # (otro worker insertó primero), `new_ids` tendrá menos elementos
        # que `valid` — los faltantes quedan como None y se reintentan
        # en el próximo sync.
        ids_iter = iter(result.new_ids)
        return [next(ids_iter, None) if p is not None else None for p in processable]

    async def _download_attachments_for(
        self,
        row: BandejaRow,
        notification_id: int,
        case_file_id: int | None,
    ) -> None:
        """Abre el modal de anexos, los descarga uno por uno, persiste.

        `case_file_id` viene del FK actualizado tras el bulk_upsert+match (o
        del fetch posterior en backfill). Se propaga al S3 key para que los
        anexos de notifs ya matched caigan en `case-file/{id}/...` desde el
        primer upload, evitando que vivan en `unmatched/...` para siempre.
        """
        logger.debug(
            "download_attachments_start",
            n_notif=row.n_notif,
            notification_id=notification_id,
            case_file_id=case_file_id,
            ver_anexos_button_id=row.ver_anexos_button_id,
            is_read=row.is_read,
        )
        if not await open_anexos_modal(self.page, row):
            logger.info("no_anexos_modal", n_notif=row.n_notif)
            return

        try:
            anexos = await list_anexos(self.page)
            if not anexos:
                return

            idents = [a.identificacion for a in anexos if a.identificacion]
            already = self.attachments.existing_idents_for(notification_id, idents)
            self.metrics.attachments_skipped_dedupe += len(already)

            for anexo in anexos:
                if not anexo.identificacion or anexo.identificacion in already:
                    continue
                if not anexo.descarga_button_id:
                    logger.info("anexo_no_download_button", anexo=anexo.identificacion)
                    continue
                try:
                    await self._download_one(row, anexo, notification_id, case_file_id)
                except Exception as e:
                    logger.exception(
                        "single_attachment_failed",
                        n_notif=row.n_notif,
                        anexo=anexo.identificacion,
                        error=str(e),
                    )
                # Jitter entre anexos para no parecer bot agresivo
                await asyncio.sleep(random.uniform(JITTER_MIN_S, JITTER_MAX_S))
        finally:
            try:
                await close_anexos_modal(self.page)
            except Exception:
                pass

    async def _download_one(
        self,
        row: BandejaRow,
        anexo: AnexoItem,
        notification_id: int,
        case_file_id: int | None,
    ) -> None:
        """Click en el botón de descarga, espera download, persiste S3 + BD."""
        async with self.page.expect_download(timeout=30_000) as dl_info:
            await self.page.click(f"button[id='{anexo.descarga_button_id}']")
        download = await dl_info.value
        path = await download.path()
        if path is None:
            logger.warning("download_no_path", anexo=anexo.identificacion)
            return
        file_bytes = path.read_bytes()

        tiene_firma, num_paginas = compute_attachment_metadata(file_bytes)
        sha = compute_sha256(file_bytes)
        tipo = _normalize_anexo_tipo(anexo.tipo)

        s3_key = self.s3_client.build_attachment_key(
            customer_id=self.ctx.customer_id,
            chb_id=self.ctx.customer_has_bank_id,
            client_code=self.ctx.client_code,
            case_file_id=case_file_id,
            n_notificacion=row.n_notif,
            tipo=tipo,
            identificacion_anexo=anexo.identificacion,
        )

        # Subida a S3 fuera de transacción BD (lección clave del CEJ scraper —
        # evita exhaustion del pool DB por uploads largos).
        await asyncio.to_thread(
            self.s3_client.upload_attachment,
            s3_key=s3_key,
            file_bytes=file_bytes,
        )

        self.attachments.create(
            sinoe_notification_id=notification_id,
            customer_has_bank_id=self.ctx.customer_has_bank_id,
            tipo=tipo,
            identificacion_anexo=anexo.identificacion,
            numero_paginas=num_paginas if num_paginas is not None else anexo.paginas or None,
            peso_bytes=len(file_bytes),
            s3_key=s3_key,
            sha256=sha,
            tiene_firma_digital=tiene_firma,
        )
        self.metrics.attachments_downloaded += 1
        self.metrics.bytes_downloaded += len(file_bytes)
