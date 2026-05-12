"""Repositorio de SINOE_SYNC_LOG — audit trail de cada corrida del scraper.

Una row por sync. Permite ver historial en la UI (Mockups §3.4) y métricas
de salud (captcha rate, sesión reused, errores).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from ..db.models import SinoeSyncLog


class SyncLogRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(
        self,
        *,
        account_id: int,
        customer_id: int,
        trigger_kind: str,
        worker_id: str | None = None,
    ) -> int:
        """Crea row inicial en estado `running`. Devuelve el sync_log_id
        que el worker usa luego para `notif.first_seen_in_sync_id`."""
        with self._session_factory() as session:
            log = SinoeSyncLog(
                sinoe_account_id=account_id,
                customer_id=customer_id,
                started_at=datetime.utcnow(),
                status="running",
                trigger_kind=trigger_kind,
                worker_id=worker_id,
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            return log.id

    def finish(
        self,
        sync_log_id: int,
        *,
        status: str,
        notifications_seen: int = 0,
        notifications_new: int = 0,
        notifications_updated: int = 0,
        attachments_downloaded: int = 0,
        attachments_skipped_dedupe: int = 0,
        bytes_downloaded: int = 0,
        captcha_solves_consumed: int = 0,
        session_was_reused: bool = False,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Cierra la row con métricas finales."""
        with self._session_factory() as session:
            log = session.get(SinoeSyncLog, sync_log_id)
            if not log:
                return
            log.ended_at = datetime.utcnow()
            log.status = status
            log.notifications_seen = notifications_seen
            log.notifications_new = notifications_new
            log.notifications_updated = notifications_updated
            log.attachments_downloaded = attachments_downloaded
            log.attachments_skipped_dedupe = attachments_skipped_dedupe
            log.bytes_downloaded = bytes_downloaded
            log.captcha_solves_consumed = captcha_solves_consumed
            log.session_was_reused = session_was_reused
            log.error_kind = error_kind
            log.error_message = error_message
            session.commit()
