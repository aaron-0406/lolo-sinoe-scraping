"""Repositorio de SINOE_ACCOUNT — el scraper lee cuentas due y actualiza
last_sync_*.

Ver Plan §6.1 (caché de sesión) + §7.2.bis (multitenant flow).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..db.models import SinoeAccount


@dataclass(frozen=True)
class DueAccount:
    """Snapshot mínimo de una cuenta due — lo suficiente para que el
    scheduler decida qué queue usar sin un segundo round-trip a BD.
    """

    id: int
    sync_requested_at: datetime | None
    last_sync_completed_at: datetime | None


class AccountRepository:
    """Métodos CRUD focalizados al flujo del scraper.

    El backend tiene su propio service para CRUD desde la UI; este repo es
    para uso exclusivo del worker/scheduler Python.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        # Cache de `get_s3_path_context` — el (customer_id, client_code) de
        # un CHB no cambia. Evita 1 query por sync (50 cuentas x 48 syncs/dia
        # = 2,400 queries/dia innecesarias).
        self._s3_ctx_cache: dict[int, tuple[int, str]] = {}

    def find_due(self, limit: int = 100) -> list[DueAccount]:
        """Devuelve cuentas que toca sincronizar AHORA.

        Lógica equivalente al `findAccountsDue` del backend service:
            is_active = true
            AND (sync_requested_at IS NOT NULL                      -- "Sync ahora" pendiente
                 OR last_sync_completed_at IS NULL                  -- nunca sincronizada
                 OR last_sync_completed_at < NOW() - INTERVAL sync_frequency_minutes MINUTE)

        Devuelve `DueAccount` (no rows enteras) con los campos que el
        scheduler necesita — evita N+1 al decidir queue. No expone blobs
        cifrados por el bus.
        """
        with self._session_factory() as session:
            sql = text(
                """
                SELECT
                  id_sinoe_account,
                  sync_requested_at,
                  last_sync_completed_at
                FROM SINOE_ACCOUNT
                WHERE is_active = 1
                  AND deleted_at IS NULL
                  AND (
                    sync_requested_at IS NOT NULL
                    OR last_sync_completed_at IS NULL
                    OR last_sync_completed_at < NOW() - INTERVAL sync_frequency_minutes MINUTE
                  )
                ORDER BY sync_requested_at DESC, last_sync_completed_at ASC
                LIMIT :limit
                """
            )
            rows = session.execute(sql, {"limit": limit}).fetchall()
            return [
                DueAccount(
                    id=row[0],
                    sync_requested_at=row[1],
                    last_sync_completed_at=row[2],
                )
                for row in rows
            ]

    def get_by_id(self, account_id: int) -> SinoeAccount | None:
        with self._session_factory() as session:
            return session.get(SinoeAccount, account_id)

    def claim_for_sync(self, account_id: int) -> SinoeAccount | None:
        """Reserva atómica: SELECT ... FOR UPDATE evita race entre workers.

        Setea `last_sync_started_at = NOW()` y limpia `sync_requested_at`.
        Devuelve la cuenta lockeada, o None si ya está siendo procesada
        por otro worker (last_sync_started_at < 5min).

        El groupKey de BullMQ + `FOR UPDATE` son defensa en profundidad:
        BullMQ serializa por account_id, pero si por algún glitch se
        encolan dos jobs simultáneos, el `FOR UPDATE` impide que ambos
        avancen.
        """
        with self._session_factory() as session:
            account: SinoeAccount | None = (
                session.query(SinoeAccount)
                .filter(SinoeAccount.id == account_id)
                .with_for_update()
                .one_or_none()
            )
            if not account:
                return None
            now = datetime.utcnow()
            # Si otro worker ya está procesando hace <5min, abortamos.
            # `last_sync_started_at == last_sync_completed_at` significa
            # que el sync anterior terminó normalmente.
            if (
                account.last_sync_started_at
                and (now - account.last_sync_started_at) < timedelta(minutes=5)
                and account.last_sync_completed_at != account.last_sync_started_at
            ):
                return None
            account.last_sync_started_at = now
            account.sync_requested_at = None
            session.commit()
            session.refresh(account)
            return account

    def mark_sync_complete(
        self,
        account_id: int,
        status: str,
        notifications_synced_total_increment: int = 0,
    ) -> None:
        with self._session_factory() as session:
            account = session.get(SinoeAccount, account_id)
            if not account:
                return
            account.last_sync_completed_at = datetime.utcnow()
            account.last_sync_status = status
            if status == "ok":
                account.consecutive_failure_count = 0
                account.notifications_synced_total += notifications_synced_total_increment
            else:
                account.consecutive_failure_count += 1
            session.commit()

    def update_cached_session(
        self,
        account_id: int,
        encrypted_state: bytes,
        expires_at: datetime,
    ) -> None:
        """Persiste el storage_state cifrado para reuso en el próximo sync."""
        with self._session_factory() as session:
            account = session.get(SinoeAccount, account_id)
            if not account:
                return
            account.cached_storage_state_blob = encrypted_state
            account.cached_session_expires_at = expires_at
            session.commit()

    def invalidate_cached_session(self, account_id: int) -> None:
        """Limpia caché de sesión — ej. si SINOE rechazó la sesión cacheada."""
        with self._session_factory() as session:
            account = session.get(SinoeAccount, account_id)
            if not account:
                return
            account.cached_storage_state_blob = None
            account.cached_session_expires_at = None
            session.commit()

    def get_s3_path_context(self, customer_has_bank_id: int) -> tuple[int, str] | None:
        """Resuelve `(customer_id, client_code)` para construir el S3 key.

        El path SINOE es `CHB/{customer_id}/{chb_id}/{client_code}/...`.
        Lo necesitamos al subir cada anexo.

        Cacheado por instancia: el customer_id de un CHB no cambia. Si en
        algún flow excepcional el CHB se reasignara, reiniciar el server
        vacía la caché.

        Devuelve `None` si el CHB no existe o no tiene customer asociado.
        Para SINOE NO sabemos a priori el `client_code` del expediente
        (la notif podría no estar matched). Por ahora usamos un código
        sintético basado en CHB; cuando la notif se matchee con un caseFile
        después, el path queda como histórico — lo importante es que sea
        único y reproducible.
        """
        cached = self._s3_ctx_cache.get(customer_has_bank_id)
        if cached is not None:
            return cached
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT customer_id_customer
                    FROM CUSTOMER_HAS_BANK
                    WHERE id_customer_has_bank = :chb
                    LIMIT 1
                    """
                ),
                {"chb": customer_has_bank_id},
            ).fetchone()
            if not row:
                return None
            customer_id = int(row[0])
            # Sintético — el path queda inmutable aunque la notif se
            # matchee con un caseFile después. Si más adelante queremos
            # paths matched, el cron de matching puede actualizar.
            client_code = f"sinoe-chb-{customer_has_bank_id}"
            ctx = (customer_id, client_code)
            self._s3_ctx_cache[customer_has_bank_id] = ctx
            return ctx
