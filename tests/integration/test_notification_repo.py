# ruff: noqa: RUF001, RUF002, RUF003 -- Unicode dashes are the actual content under test
"""Integration tests del NotificationRepository — dedupe + matching.

Cubre los dos comportamientos críticos del Plan §6.4:
1. UNIQUE de BD evita duplicados aunque el caller falle (idempotencia).
2. `match_orphan_notifications` actualiza FK con normalización Unicode.

REQUIERE: SINOE_TEST_DB_URL apuntando a staging + SINOE_TEST_CHB_ID válido.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import text

from lolo_sinoe.persistence.repositories.notification_repo import NotificationRepository

pytestmark = pytest.mark.live


@pytest.fixture
def test_account_id(db_session, test_chb_id: int):
    """Cuenta SINOE de prueba — minimal, solo lo necesario para FK."""
    db_session.execute(
        text(
            """
            INSERT INTO SINOE_ACCOUNT (
              customer_has_bank_id_sinoe_account, casilla_number, alias,
              encrypted_password_blob, encrypted_dek, kms_key_id,
              consent_acceptance_at, consent_version, consent_acceptor_user_id,
              is_active, sync_frequency_minutes, last_sync_status,
              consecutive_failure_count, notifications_synced_total,
              created_at, updated_at
            ) VALUES (
              :chb, :casilla, 'test', :z, :z, 'LOCAL:fallback',
              NOW(), 'v1', 1, 1, 30, 'never', 0, 0, NOW(), NOW()
            )
            """
        ),
        {"chb": test_chb_id, "casilla": f"T-{datetime.utcnow().timestamp()}", "z": b"\x00"},
    )
    aid = int(db_session.execute(text("SELECT LAST_INSERT_ID()")).scalar() or 0)
    db_session.commit()
    yield aid
    db_session.execute(
        text("DELETE FROM SINOE_NOTIFICATION WHERE sinoe_account_id_sinoe_notification = :aid"),
        {"aid": aid},
    )
    db_session.execute(
        text("DELETE FROM SINOE_ACCOUNT WHERE id_sinoe_account = :id"), {"id": aid}
    )
    db_session.commit()


@pytest.fixture
def test_sync_log_id(db_session, test_account_id: int, test_chb_id: int):
    db_session.execute(
        text(
            """
            INSERT INTO SINOE_SYNC_LOG (
              sinoe_account_id, customer_has_bank_id, started_at, status,
              notifications_seen, notifications_new, notifications_updated,
              attachments_downloaded, attachments_skipped_dedupe,
              bytes_downloaded, captcha_solves_consumed, session_was_reused,
              trigger_kind
            ) VALUES (
              :aid, :chb, NOW(), 'running',
              0, 0, 0, 0, 0, 0, 0, 0, 'manual'
            )
            """
        ),
        {"aid": test_account_id, "chb": test_chb_id},
    )
    sid = int(db_session.execute(text("SELECT LAST_INSERT_ID()")).scalar() or 0)
    db_session.commit()
    yield sid


def _row(account_id: int, chb_id: int, sync_log_id: int, **overrides: Any) -> dict:
    base = dict(
        account_id=account_id,
        customer_has_bank_id=chb_id,
        n_notificacion="N-12345",
        n_expediente="00001-2024-0-1801-JR-CI-01",
        sumilla="Resolución n.º 1",
        organo_jurisdiccional="1° JUZGADO CIVIL",
        fecha_ingreso_casilla=datetime(2026, 5, 1, 10, 0),
        fecha_surte_efecto=date(2026, 5, 2),
        fecha_inicio_plazo=date(2026, 5, 3),
        estado_lectura_sinoe="no_leida",
        sync_log_id=sync_log_id,
    )
    base.update(overrides)
    return base


def test_upsert_idempotent(
    session_factory, test_account_id: int, test_chb_id: int, test_sync_log_id: int
) -> None:
    """Llamar 2× con el mismo n_notif debe devolver la MISMA row, no duplicar."""
    repo = NotificationRepository(session_factory)
    n1 = repo.upsert_notification(**_row(test_account_id, test_chb_id, test_sync_log_id))
    n2 = repo.upsert_notification(**_row(test_account_id, test_chb_id, test_sync_log_id))
    assert n1.id == n2.id


def test_find_existing_n_notifs(
    session_factory, test_account_id: int, test_chb_id: int, test_sync_log_id: int
) -> None:
    """Para early-stop: tras insertar N-A, find_existing(['N-A','N-B']) → {'N-A'}."""
    repo = NotificationRepository(session_factory)
    repo.upsert_notification(
        **_row(test_account_id, test_chb_id, test_sync_log_id, n_notificacion="N-A")
    )
    existing = repo.find_existing_n_notifs(test_account_id, ["N-A", "N-B"])
    assert existing == {"N-A"}


def test_match_orphan_notifications_normalizes_dashes(
    session_factory, db_session, test_account_id: int, test_chb_id: int, test_sync_log_id: int
) -> None:
    """Notif con guión unicode (–) debe matchear caseFile con guión ASCII (-)."""
    # Crear caseFile con guión ASCII
    db_session.execute(
        text(
            """
            INSERT INTO JUDICIAL_CASE_FILE (
              number_case_file, customer_has_bank_id_judicial_case_file,
              client_id_judicial_case_file, judicial_court_id_judicial_court,
              judicial_subject_id_judicial_subject, judicial_proceeding_type_id_judicial_proceeding_type,
              process_status, created_at, updated_at
            ) VALUES (
              '00099-2024-0-1801-JR-CI-01', :chb, 1, 1, 1, 1, 'TRAMITE', NOW(), NOW()
            )
            """
        ),
        {"chb": test_chb_id},
    )
    case_file_id = int(db_session.execute(text("SELECT LAST_INSERT_ID()")).scalar() or 0)
    db_session.commit()

    try:
        # Insertar notif con guión EN-DASH (–) en el mismo número
        repo = NotificationRepository(session_factory)
        repo.upsert_notification(
            **_row(
                test_account_id,
                test_chb_id,
                test_sync_log_id,
                n_notificacion="N-MATCH-1",
                n_expediente="00099–2024–0–1801–JR–CI–01",
            )
        )

        matched = repo.match_orphan_notifications(test_chb_id)
        assert matched >= 1

        # Verificar que la FK quedó seteada
        result = db_session.execute(
            text(
                """
                SELECT judicial_case_file_id_sinoe_notification
                FROM SINOE_NOTIFICATION
                WHERE n_notificacion = 'N-MATCH-1'
                  AND sinoe_account_id_sinoe_notification = :aid
                """
            ),
            {"aid": test_account_id},
        ).scalar()
        assert result == case_file_id
    finally:
        db_session.execute(
            text("DELETE FROM JUDICIAL_CASE_FILE WHERE id_judicial_case_file = :id"),
            {"id": case_file_id},
        )
        db_session.commit()
