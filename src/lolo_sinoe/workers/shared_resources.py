"""Recursos compartidos entre workers — KMS client, S3 client, repos,
rate limiter, browser pool, sessionmaker.

Singletons inicializados al arrancar el server. Los workers los inyectan
via SharedResources en lugar de instanciarlos por job.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..browser import BrowserPool
from ..captcha import build_captcha_solver
from ..captcha.solver import CaptchaSolver
from ..config import Settings
from ..persistence.db.engine import init_engine
from ..persistence.kms.kms_client import KmsClient
from ..persistence.repositories.account_repo import AccountRepository
from ..persistence.repositories.notification_repo import (
    AttachmentRepository,
    NotificationRepository,
)
from ..persistence.repositories.sync_log_repo import SyncLogRepository
from ..persistence.s3.s3_client import S3Client
from .rate_limiter import SINOE_DEFAULT_RATE_PER_SECOND, RateLimiter


@dataclass
class SharedResources:
    """Container de instancias compartidas. Inicializar UNA vez.

    El `browser_pool` requiere `await pool.start()` antes del primer uso —
    no lo hacemos en `from_settings` para mantener este método sync. Lo
    arranca/cierra el lifespan de FastAPI.
    """

    settings: Settings
    kms: KmsClient
    s3: S3Client
    captcha_solver: CaptchaSolver
    accounts: AccountRepository
    notifications: NotificationRepository
    attachments: AttachmentRepository
    sync_logs: SyncLogRepository
    sinoe_rate_limiter: RateLimiter
    browser_pool: BrowserPool
    session_factory: sessionmaker[Session]

    @classmethod
    def from_settings(cls, settings: Settings) -> SharedResources:
        if not settings.multitenant_mode:
            raise RuntimeError(
                "SharedResources requiere multitenant_mode=true. "
                "Para CLI dev usar `python -m lolo_sinoe.cli login`."
            )
        if not settings.db_url:
            raise RuntimeError("SINOE_DB_URL no configurado")

        engine = init_engine(settings.db_url, settings.db_pool_max)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        kms = KmsClient(
            aws_region=settings.aws_region,
            aws_profile=settings.aws_profile,
            fallback_master_key_b64=(
                settings.kms_fallback_master_key.get_secret_value()
                if settings.kms_fallback_master_key
                else None
            ),
            fallback_master_key_old_b64=(
                settings.kms_fallback_master_key_old.get_secret_value()
                if settings.kms_fallback_master_key_old
                else None
            ),
        )
        s3 = S3Client(
            aws_region=settings.aws_region,
            aws_profile=settings.aws_profile,
            aws_access_key_id=(
                settings.aws_access_key_id.get_secret_value()
                if settings.aws_access_key_id
                else None
            ),
            aws_secret_access_key=(
                settings.aws_secret_access_key.get_secret_value()
                if settings.aws_secret_access_key
                else None
            ),
            bucket=settings.aws_bucket_name,
        )
        captcha_solver = build_captcha_solver(settings)

        return cls(
            settings=settings,
            kms=kms,
            s3=s3,
            captcha_solver=captcha_solver,
            accounts=AccountRepository(SessionLocal),
            notifications=NotificationRepository(SessionLocal),
            attachments=AttachmentRepository(SessionLocal),
            sync_logs=SyncLogRepository(SessionLocal),
            sinoe_rate_limiter=RateLimiter(SINOE_DEFAULT_RATE_PER_SECOND),
            browser_pool=BrowserPool(
                size=settings.browser_pool_size,
                headless=settings.headless,
            ),
            session_factory=SessionLocal,
        )

    def db_health(self) -> str:
        """Returns 'ok' if DB is reachable, error string otherwise.
        Wrapper público — el `/health` del API server lo invoca sin
        tener que romper encapsulamiento."""
        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))
            return "ok"
        except Exception as e:  # pragma: no cover — exposed only to /health
            return f"error: {str(e)[:100]}"
