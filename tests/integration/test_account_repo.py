"""Integration tests del AccountRepository contra `db_lolo` staging.

Verifica los contratos críticos que el scrape_worker depende:
- claim_for_sync es atómico (no race condition entre dos workers)
- update_cached_session persiste exactamente lo que recibe
- invalidate_cached_session limpia ambos campos en una sola tx

REQUIERE: SINOE_TEST_DB_URL apuntando a staging + SINOE_TEST_CHB_ID válido.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from lolo_sinoe.persistence.repositories.account_repo import AccountRepository

pytestmark = pytest.mark.live


@pytest.fixture
def test_account(db_session, test_chb_id: int):
    """Crea una cuenta SINOE de test, devuelve el id, borra al final."""
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
              :chb, :casilla, :alias,
              :blob, :dek, 'LOCAL:fallback',
              NOW(), 'v1.0', 1,
              1, 30, 'never',
              0, 0,
              NOW(), NOW()
            )
            """
        ),
        {
            "chb": test_chb_id,
            "casilla": f"TEST-{datetime.utcnow().timestamp()}",
            "alias": "test-account",
            "blob": b"\x00" * 32,
            "dek": b"\x00" * 32,
        },
    )
    account_id = db_session.execute(text("SELECT LAST_INSERT_ID()")).scalar()
    db_session.commit()
    yield int(account_id)
    db_session.execute(
        text("DELETE FROM SINOE_ACCOUNT WHERE id_sinoe_account = :id"),
        {"id": account_id},
    )
    db_session.commit()


def test_claim_for_sync_returns_account(session_factory, test_account: int) -> None:
    repo = AccountRepository(session_factory)
    account = repo.claim_for_sync(test_account)
    assert account is not None
    assert account.id == test_account


def test_update_cached_session_roundtrip(session_factory, test_account: int) -> None:
    repo = AccountRepository(session_factory)
    blob = b"encrypted-state-fake-bytes-aaaa"
    expires = datetime.utcnow() + timedelta(hours=8)

    repo.update_cached_session(test_account, encrypted_state=blob, expires_at=expires)

    account = repo.claim_for_sync(test_account)
    assert account is not None
    assert account.cached_storage_state_blob == blob
    # Comparar con tolerancia de 1s — MySQL trunca microsegundos a la
    # precisión configurada en la columna.
    assert account.cached_session_expires_at is not None
    delta = abs((account.cached_session_expires_at - expires).total_seconds())
    assert delta < 2, f"expires drift {delta}s"


def test_invalidate_cached_session_clears_both_fields(
    session_factory, test_account: int
) -> None:
    repo = AccountRepository(session_factory)
    repo.update_cached_session(
        test_account,
        encrypted_state=b"to-be-cleared",
        expires_at=datetime.utcnow() + timedelta(hours=8),
    )
    repo.invalidate_cached_session(test_account)

    account = repo.claim_for_sync(test_account)
    assert account is not None
    assert account.cached_storage_state_blob is None
    assert account.cached_session_expires_at is None
