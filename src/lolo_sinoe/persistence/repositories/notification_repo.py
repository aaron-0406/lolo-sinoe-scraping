"""Repositorio de SINOE_NOTIFICATION + attachments + match con CASE_FILE.

Implementa la estrategia de dedupe a 2 niveles del Plan §6.4:
- `find_existing_n_notifs(account_id, n_notifs[]) → set` para early-stop
  de paginación (defensa #2 — ahorra requests a SINOE).
- `upsert_notification(...)` con la UNIQUE de BD como defensa #1 (último
  recurso si la heurística falla).
- `bulk_upsert(rows[], ...)` para sync inicial donde llegan ~50+ notifs
  de una vez — minimiza round-trips a RDS.
- `bump_last_seen(...)` para notifs ya conocidas (sirve para retención
  futura: notifs viejas que SINOE deja de mostrar pueden archivarse).
- `match_orphan_notifications(...)` ejecuta el SQL con normalización
  Unicode espejado de `lolo-backend/src/app/judicial/services/sinoe-matching.service.ts`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import CursorResult, bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import SinoeNotification, SinoeNotificationAttachment


# SQL de normalización del número de expediente para matching. Mantener
# sincronizado con `sinoe-matching.service.ts::SQL_NORMALIZE` en el backend
# Node — fuente única de la lógica de comparación.
#
#   1. UPPER + TRIM
#   2. Eliminar TODOS los espacios internos
#   3. Reemplazar 5 guiones unicode (U+2010..U+2014) por guion ASCII
def _normalize_sql(col: str) -> str:
    return (
        f"UPPER(TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col}, "
        f"CHAR(0x2010 USING utf8mb4), '-'), "
        f"CHAR(0x2011 USING utf8mb4), '-'), "
        f"CHAR(0x2012 USING utf8mb4), '-'), "
        f"CHAR(0x2013 USING utf8mb4), '-'), "
        f"CHAR(0x2014 USING utf8mb4), '-'), "
        f"' ', '')))"
    )


# Matching customer-scoped (post-migration backend 20260512100000): el
# expediente puede vivir en CUALQUIER cartera del mismo customer — joineamos
# JCF → CHB para verificar que el CHB del expediente pertenezca al customer
# de la notif. Resuelve el bug histórico de notifs huérfanas cuando una
# casilla SINOE recibía expedientes dispersos en varias carteras.
_MATCH_SINGLE_SQL = text(
    f"""
    UPDATE SINOE_NOTIFICATION sn
    JOIN JUDICIAL_CASE_FILE jcf
      ON {_normalize_sql("sn.n_expediente")}
       = {_normalize_sql("jcf.number_case_file")}
    JOIN CUSTOMER_HAS_BANK chb
      ON chb.id_customer_has_bank = jcf.customer_has_bank_id
     AND chb.customer_id_customer = sn.customer_id
    SET sn.judicial_case_file_id_sinoe_notification = jcf.id_judicial_case_file
    WHERE sn.id_sinoe_notification = :id
      AND sn.judicial_case_file_id_sinoe_notification IS NULL
    """
)

_MATCH_BULK_SQL = text(
    f"""
    UPDATE SINOE_NOTIFICATION sn
    JOIN JUDICIAL_CASE_FILE jcf
      ON {_normalize_sql("sn.n_expediente")}
       = {_normalize_sql("jcf.number_case_file")}
    JOIN CUSTOMER_HAS_BANK chb
      ON chb.id_customer_has_bank = jcf.customer_has_bank_id
     AND chb.customer_id_customer = sn.customer_id
    SET sn.judicial_case_file_id_sinoe_notification = jcf.id_judicial_case_file
    WHERE sn.judicial_case_file_id_sinoe_notification IS NULL
      AND sn.deleted_at IS NULL
      AND sn.customer_id = :customer_id
    """
)

_FIND_EXISTING_SQL = text(
    """
    SELECT n_notificacion FROM SINOE_NOTIFICATION
    WHERE sinoe_account_id_sinoe_notification = :account_id
      AND n_notificacion IN :n_notifs
    """
).bindparams(bindparam("n_notifs", expanding=True))

# Lectura del FK al expediente para un batch de notif IDs. Se usa después
# del bulk_upsert+match para saber a qué `case-file/{id}/` apuntar el S3
# key del anexo. Lookup separado (no JOIN en el upsert) porque el match
# corre como UPDATE post-INSERT y SQLAlchemy no nos devuelve la FK
# actualizada en `new_ids`.
_GET_CASE_FILE_IDS_SQL = text(
    """
    SELECT id_sinoe_notification, judicial_case_file_id_sinoe_notification
    FROM SINOE_NOTIFICATION
    WHERE id_sinoe_notification IN :ids
    """
).bindparams(bindparam("ids", expanding=True))

_BUMP_LAST_SEEN_SQL = text(
    """
    UPDATE SINOE_NOTIFICATION
    SET last_seen_in_sync_id = :sync_id, updated_at = NOW()
    WHERE sinoe_account_id_sinoe_notification = :account_id
      AND n_notificacion IN :n_notifs
    """
).bindparams(bindparam("n_notifs", expanding=True))

# Detección de inconsistencias para backfill de anexos: dado un batch de
# n_notifs ya existentes en BD, devuelve las que no tienen NINGÚN attachment
# asociado. Esas son candidatas a re-descargar (ej. notifs upserted antes de
# que el scraper soportara descarga para no-leídas).
_FIND_MISSING_ATTACHMENTS_SQL = text(
    """
    SELECT sn.n_notificacion, sn.id_sinoe_notification
    FROM SINOE_NOTIFICATION sn
    LEFT JOIN SINOE_NOTIFICATION_ATTACHMENT sna
      ON sna.sinoe_notification_id = sn.id_sinoe_notification
    WHERE sn.sinoe_account_id_sinoe_notification = :account_id
      AND sn.n_notificacion IN :n_notifs
    GROUP BY sn.id_sinoe_notification, sn.n_notificacion
    HAVING COUNT(sna.id_sinoe_notification_attachment) = 0
    """
).bindparams(bindparam("n_notifs", expanding=True))


@dataclass(frozen=True)
class NotificationRow:
    """DTO para upsert masivo. Mismo shape que los kwargs de `upsert_notification`."""

    n_notificacion: str
    n_expediente: str
    sumilla: str
    organo_jurisdiccional: str
    fecha_ingreso_casilla: datetime
    fecha_surte_efecto: date
    fecha_inicio_plazo: date
    estado_lectura_sinoe: str
    sinoe_row_uuid: str | None = None


@dataclass
class BulkUpsertResult:
    new_ids: list[int]
    skipped_existing: int
    matched_to_case_file: int


class NotificationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ── Lectura ─────────────────────────────────────────────────────────

    def find_existing_n_notifs(self, account_id: int, n_notifs: list[str]) -> set[str]:
        """Early-stop helper: dado un batch de n_notif vistos en una página,
        devuelve cuáles ya están en BD."""
        if not n_notifs:
            return set()
        with self._session_factory() as session:
            result = session.execute(
                _FIND_EXISTING_SQL,
                {"account_id": account_id, "n_notifs": list(n_notifs)},
            )
            return {row[0] for row in result.fetchall()}

    def find_missing_attachments(
        self, account_id: int, n_notifs: list[str]
    ) -> dict[str, int]:
        """Dado un batch de n_notif existentes, devuelve mapping
        `n_notif → id_sinoe_notification` para los que no tienen ningún
        attachment en BD. El caller decide si abrir el modal en SINOE para
        re-descargar (típicamente sí cuando SINOE muestra botón "Ver anexos")."""
        if not n_notifs:
            return {}
        with self._session_factory() as session:
            result = session.execute(
                _FIND_MISSING_ATTACHMENTS_SQL,
                {"account_id": account_id, "n_notifs": list(n_notifs)},
            )
            return {row[0]: row[1] for row in result.fetchall()}

    def get_case_file_ids(self, notif_ids: list[int]) -> dict[int, int | None]:
        """Mapping `id_sinoe_notification → judicial_case_file_id` (o None
        si la notif quedó huérfana). Llamar después de `bulk_upsert` o de
        `find_missing_attachments` para enrutear el anexo al folder S3
        correcto del expediente cuando el match ya se resolvió."""
        if not notif_ids:
            return {}
        with self._session_factory() as session:
            result = session.execute(
                _GET_CASE_FILE_IDS_SQL,
                {"ids": list(notif_ids)},
            )
            return {row[0]: row[1] for row in result.fetchall()}

    # ── Escritura ───────────────────────────────────────────────────────

    def upsert_notification(
        self,
        *,
        account_id: int,
        customer_id: int,
        n_notificacion: str,
        n_expediente: str,
        sumilla: str,
        organo_jurisdiccional: str,
        fecha_ingreso_casilla: datetime,
        fecha_surte_efecto: date | datetime,
        fecha_inicio_plazo: date | datetime,
        estado_lectura_sinoe: str,
        sync_log_id: int,
        sinoe_row_uuid: str | None = None,
    ) -> SinoeNotification:
        """Insert si no existe, retorna la row (existente o nueva). Idempotente
        — el UNIQUE `(account_id, n_notif)` garantiza que dos upserts
        concurrentes no duplican.

        Para batches grandes preferí `bulk_upsert` — esto hace 1 round-trip
        adicional por notif para verificar existencia.
        """
        with self._session_factory() as session:
            notif = self._upsert_one(
                session,
                account_id=account_id,
                customer_id=customer_id,
                n_notificacion=n_notificacion,
                n_expediente=n_expediente,
                sumilla=sumilla,
                organo_jurisdiccional=organo_jurisdiccional,
                fecha_ingreso_casilla=fecha_ingreso_casilla,
                fecha_surte_efecto=fecha_surte_efecto,
                fecha_inicio_plazo=fecha_inicio_plazo,
                estado_lectura_sinoe=estado_lectura_sinoe,
                sync_log_id=sync_log_id,
                sinoe_row_uuid=sinoe_row_uuid,
            )
            # Match dentro de la misma transacción que el insert: si el
            # match falla, se rollbackea TODO (no queda notif orphan sin
            # FK al case file).
            session.execute(_MATCH_SINGLE_SQL, {"id": notif.id})
            session.commit()
            session.refresh(notif)
            return notif

    def bulk_upsert(
        self,
        rows: Iterable[NotificationRow],
        *,
        account_id: int,
        customer_id: int,
        sync_log_id: int,
    ) -> BulkUpsertResult:
        """Upsert de múltiples notifs.

        Optimización clave del Plan §6.4: en lugar de N round-trips a RDS
        (uno por notif), hacemos un SELECT batch + INSERTs row-por-row con
        savepoint para tolerar UNIQUE collisions concurrentes (sin abortar
        el batch entero). Tras los INSERTs, un solo UPDATE de matching.

        Retorna `BulkUpsertResult` con conteos para alimentar SINOE_SYNC_LOG.
        """
        rows_list = list(rows)
        if not rows_list:
            return BulkUpsertResult(new_ids=[], skipped_existing=0, matched_to_case_file=0)

        with self._session_factory() as session:
            n_notifs = [r.n_notificacion for r in rows_list]
            existing = {
                row[0]
                for row in session.execute(
                    _FIND_EXISTING_SQL,
                    {"account_id": account_id, "n_notifs": list(n_notifs)},
                ).fetchall()
            }

            new_ids: list[int] = []
            race_collisions = 0
            now = datetime.utcnow()
            for r in rows_list:
                if r.n_notificacion in existing:
                    continue
                notif = self._build_model(
                    account_id=account_id,
                    customer_id=customer_id,
                    sync_log_id=sync_log_id,
                    row=r,
                    now=now,
                )
                # Savepoint por row: si otro proceso insertó la misma
                # n_notificacion entre nuestro SELECT y este INSERT,
                # el `flush` tira IntegrityError. Lo capturamos y seguimos
                # con la siguiente — sin abortar el batch entero.
                try:
                    with session.begin_nested():
                        session.add(notif)
                        session.flush()
                    new_ids.append(notif.id)
                except IntegrityError:
                    race_collisions += 1

            session.commit()

            # Match post-batch (un solo UPDATE para todo el CHB — el WHERE
            # `judicial_case_file_id IS NULL` filtra a las recién insertadas
            # + cualquier huérfana previa). Idempotente.
            matched = 0
            if new_ids:
                cursor = session.execute(_MATCH_BULK_SQL, {"customer_id": customer_id})
                if isinstance(cursor, CursorResult):
                    matched = cursor.rowcount or 0
                session.commit()

            return BulkUpsertResult(
                new_ids=new_ids,
                skipped_existing=len(existing) + race_collisions,
                matched_to_case_file=matched,
            )

    def bump_last_seen(self, account_id: int, n_notifs: list[str], sync_log_id: int) -> None:
        """Actualiza `last_seen_in_sync_id` de notifs ya conocidas. Sirve
        para retention: notifs viejas que SINOE deja de mostrar se pueden
        archivar tras X meses sin verlas."""
        if not n_notifs:
            return
        with self._session_factory() as session:
            session.execute(
                _BUMP_LAST_SEEN_SQL,
                {
                    "sync_id": sync_log_id,
                    "account_id": account_id,
                    "n_notifs": list(n_notifs),
                },
            )
            session.commit()

    # ── Match con CASE_FILE ─────────────────────────────────────────────

    def match_orphan_notifications(self, customer_id: int) -> int:
        """Re-matchea TODAS las notifs huérfanas del CHB. Idempotente.

        Mismo SQL que el backend Node — usar tras `bulk_upsert` o desde un
        endpoint admin para retro-matchear cuando se crea/edita un caseFile.
        """
        with self._session_factory() as session:
            cursor = session.execute(_MATCH_BULK_SQL, {"customer_id": customer_id})
            session.commit()
            if isinstance(cursor, CursorResult):
                return cursor.rowcount or 0
            return 0

    # ── Helpers privados ────────────────────────────────────────────────

    @staticmethod
    def _build_model(
        *,
        account_id: int,
        customer_id: int,
        sync_log_id: int,
        row: NotificationRow,
        now: datetime,
    ) -> SinoeNotification:
        return SinoeNotification(
            sinoe_account_id=account_id,
            customer_id=customer_id,
            n_notificacion=row.n_notificacion,
            sinoe_row_uuid=row.sinoe_row_uuid,
            n_expediente=row.n_expediente,
            sumilla=row.sumilla,
            organo_jurisdiccional=row.organo_jurisdiccional,
            fecha_ingreso_casilla=row.fecha_ingreso_casilla,
            fecha_surte_efecto=row.fecha_surte_efecto.date()
            if isinstance(row.fecha_surte_efecto, datetime)
            else row.fecha_surte_efecto,
            fecha_inicio_plazo=row.fecha_inicio_plazo.date()
            if isinstance(row.fecha_inicio_plazo, datetime)
            else row.fecha_inicio_plazo,
            estado_lectura_sinoe_at_scrape=row.estado_lectura_sinoe,
            priority="medium",
            first_seen_in_sync_id=sync_log_id,
            last_seen_in_sync_id=sync_log_id,
            created_at=now,
            updated_at=now,
        )

    def _upsert_one(
        self,
        session: Session,
        *,
        account_id: int,
        customer_id: int,
        n_notificacion: str,
        n_expediente: str,
        sumilla: str,
        organo_jurisdiccional: str,
        fecha_ingreso_casilla: datetime,
        fecha_surte_efecto: date | datetime,
        fecha_inicio_plazo: date | datetime,
        estado_lectura_sinoe: str,
        sync_log_id: int,
        sinoe_row_uuid: str | None,
    ) -> SinoeNotification:
        """Lógica interna sin commit. El caller decide cuándo commitear."""
        existing = (
            session.query(SinoeNotification)
            .filter_by(sinoe_account_id=account_id, n_notificacion=n_notificacion)
            .one_or_none()
        )
        if existing:
            existing.last_seen_in_sync_id = sync_log_id
            return existing
        notif = self._build_model(
            account_id=account_id,
            customer_id=customer_id,
            sync_log_id=sync_log_id,
            row=NotificationRow(
                n_notificacion=n_notificacion,
                n_expediente=n_expediente,
                sumilla=sumilla,
                organo_jurisdiccional=organo_jurisdiccional,
                fecha_ingreso_casilla=fecha_ingreso_casilla,
                fecha_surte_efecto=fecha_surte_efecto,
                fecha_inicio_plazo=fecha_inicio_plazo,
                estado_lectura_sinoe=estado_lectura_sinoe,
                sinoe_row_uuid=sinoe_row_uuid,
            ),
            now=datetime.utcnow(),
        )
        session.add(notif)
        session.flush()
        return notif


class AttachmentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def exists(self, sinoe_notification_id: int, identificacion_anexo: str) -> bool:
        with self._session_factory() as session:
            return (
                session.query(SinoeNotificationAttachment)
                .filter_by(
                    sinoe_notification_id=sinoe_notification_id,
                    identificacion_anexo=identificacion_anexo,
                )
                .first()
                is not None
            )

    def existing_idents_for(self, sinoe_notification_id: int, candidates: list[str]) -> set[str]:
        """Devuelve los `identificacion_anexo` que ya existen — útil para
        skip masivo antes de descargar anexos uno por uno."""
        if not candidates:
            return set()
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    SELECT identificacion_anexo FROM SINOE_NOTIFICATION_ATTACHMENT
                    WHERE sinoe_notification_id = :nid
                      AND identificacion_anexo IN :idents
                    """
                ).bindparams(bindparam("idents", expanding=True)),
                {"nid": sinoe_notification_id, "idents": list(candidates)},
            )
            return {row[0] for row in result.fetchall()}

    def create(
        self,
        *,
        sinoe_notification_id: int,
        customer_id: int,
        tipo: str,
        identificacion_anexo: str,
        numero_paginas: int | None,
        peso_bytes: int | None,
        s3_key: str,
        sha256: str,
        mime_type: str = "application/pdf",
        tiene_firma_digital: bool,
    ) -> SinoeNotificationAttachment:
        with self._session_factory() as session:
            att = SinoeNotificationAttachment(
                sinoe_notification_id=sinoe_notification_id,
                customer_id=customer_id,
                tipo=tipo,
                identificacion_anexo=identificacion_anexo,
                numero_paginas=numero_paginas,
                peso_bytes=peso_bytes,
                s3_key=s3_key,
                sha256=sha256,
                mime_type=mime_type,
                tiene_firma_digital=tiene_firma_digital,
                synced_at=datetime.utcnow(),
            )
            session.add(att)
            session.commit()
            session.refresh(att)
            return att
