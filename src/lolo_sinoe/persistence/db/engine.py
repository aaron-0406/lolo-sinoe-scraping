"""SQLAlchemy engine compartido — apunta al mismo `db_lolo` que el backend.

El scraper usa un user MySQL dedicado `lolo_sinoe_scraper` con permisos
solo a tablas `SINOE_*` y SELECT en `JUDICIAL_CASE_FILE` (Plan §7.2).
Pool máximo conservador (10) — corre en laptop con concurrencia 2.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Engine | None = None


def init_engine(db_url: str, pool_max: int = 10) -> Engine:
    """Inicializa el engine global. Idempotente — segundas llamadas no-op.

    El engine es global porque MySQL+SQLAlchemy mantiene un pool por engine;
    crear varios duplica conexiones a RDS innecesariamente. El caller arma
    su propio `sessionmaker(bind=engine)` y lo inyecta donde haga falta
    (ver `SharedResources.from_settings`).
    """
    global _engine
    if _engine is not None:
        return _engine
    _engine = create_engine(
        db_url,
        pool_size=pool_max,
        max_overflow=2,
        pool_pre_ping=True,  # detecta conexiones zombi (RDS reconecta)
        pool_recycle=3600,
        echo=False,
    )
    return _engine
