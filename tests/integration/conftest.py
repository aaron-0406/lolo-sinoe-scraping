"""Fixtures para tests de integration del scraper SINOE.

Estos tests requieren BD real (staging) y opcionalmente AWS KMS. Se
ejecutan SOLO con `LIVE_TESTS=1` (ver `tests/conftest.py`). Para CI sin
infra, los tests están marcados `@pytest.mark.live` y se skipean.

Configuración esperada en env (cuando `LIVE_TESTS=1`):

  SINOE_TEST_DB_URL=mysql+pymysql://...      # BD staging dedicada
  SINOE_TEST_FALLBACK_KEY=<base64 32 bytes>  # Para KMS fallback (sin AWS)
  SINOE_TEST_CHB_ID=<int>                    # CHB de prueba pre-existente

NUNCA apuntar `SINOE_TEST_DB_URL` a producción — los tests crean/borran
filas en `SINOE_ACCOUNT` y `SINOE_NOTIFICATION`.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(scope="session")
def staging_db_url() -> str:
    """URL de la BD staging — required en modo live, falla rápido si no está."""
    url = os.environ.get("SINOE_TEST_DB_URL")
    if not url:
        pytest.skip("SINOE_TEST_DB_URL no configurado")
    if "prod" in url.lower() or "production" in url.lower():
        raise RuntimeError(
            f"REFUSE TO RUN: SINOE_TEST_DB_URL parece apuntar a prod ({url[:30]}...)"
        )
    return url


@pytest.fixture(scope="session")
def db_engine(staging_db_url: str):
    """Engine SQLAlchemy compartido — pool chico para tests."""
    engine = create_engine(staging_db_url, pool_size=2, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Iterator[Session]:
    """Session por test, transaccional — rollback al final para no
    contaminar otros tests."""
    SessionLocal = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def session_factory(db_engine):
    """Factory para repos que esperan `sessionmaker` (no una session ya abierta)."""
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False)


@pytest.fixture(scope="session")
def test_chb_id() -> int:
    """CHB pre-existente en staging para asociar tests. Falla si no está."""
    chb = os.environ.get("SINOE_TEST_CHB_ID")
    if not chb:
        pytest.skip("SINOE_TEST_CHB_ID no configurado")
    return int(chb)


@pytest.fixture(scope="session")
def fallback_master_key_b64() -> str:
    """Master key de prueba para KMS fallback. Generada por test si no
    está en env (no necesitamos persistirla entre tests)."""
    key = os.environ.get("SINOE_TEST_FALLBACK_KEY")
    if not key:
        import base64

        key = base64.b64encode(secrets.token_bytes(32)).decode()
    return key
