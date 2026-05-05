"""Configuration loaded from environment / .env.

Soporta dual-mode (Plan §7.2.bis):

* **Modo legacy / dev local (single-tenant)**: el CLI interactivo
  (`python -m lolo_sinoe.cli login`) lee `SINOE_CASILLA` + `SINOE_PASSWORD`
  del `.env` y los pasa directo a `auth/login.py`. Útil para que un dev
  pruebe el scraper con su propia casilla sin tocar BD ni KMS.

* **Modo multitenant (prod)**: el server (`python -m lolo_sinoe.api.server`)
  ignora casilla/password del env, conecta a `db_lolo` con SQLAlchemy, lee
  cuentas de `SINOE_ACCOUNT` y descifra passwords vía KMS al momento del
  login. Es lo que corre 24/7 en la laptop de operación.

El validador `_validate_mode` se asegura de que el set de campos requeridos
coincida con el modo activo: si es legacy exige `casilla`/`password`, si es
multitenant exige `db_url`/`kms_key_id` (o `kms_fallback_master_key` para
POC sin AWS).
"""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SINOE_",
        case_sensitive=False,
        extra="ignore",
    )

    # ───── Modo legacy / dev local — single-tenant CLI ──────────────────
    # Opcionales: si `multitenant_mode=False` y son None, el CLI tira error
    # claro al arrancar.
    casilla: str | None = Field(default=None, description="Modo legacy: número de casilla del dev")
    password: SecretStr | None = Field(default=None, description="Modo legacy: password del dev")

    # ───── Modo multitenant — prod ──────────────────────────────────────
    multitenant_mode: bool = Field(
        default=False,
        description="True activa BD + KMS, ignora casilla/password del env",
    )
    db_url: str | None = Field(
        default=None,
        description="Multitenant: SQLAlchemy URL al RDS (mysql+pymysql://...)",
    )
    db_pool_max: int = Field(default=10, description="Multitenant: tamaño del pool SQLAlchemy")
    kms_key_id: str | None = Field(
        default=None,
        description="Multitenant: ARN de la KMS key para descifrar creds",
    )
    kms_fallback_master_key: SecretStr | None = Field(
        default=None,
        description=(
            "POC sin AWS: master key 32-byte b64 para fallback AES-GCM. "
            "Usar SOLO en dev — migrar a KMS antes de cliente externo."
        ),
    )
    kms_fallback_master_key_old: SecretStr | None = Field(
        default=None,
        description=(
            "Opcional. Key vieja durante una rotación de fallback. Si está "
            "seteada, el scraper intenta descifrar primero con la current "
            "y si falla cae a esta. Una vez corrido el endpoint de rotate "
            "del backend, esta variable se puede borrar del env."
        ),
    )
    aws_region: str = Field(default="us-west-2")
    aws_profile: str | None = Field(
        default=None,
        description="Multitenant: nombre del perfil AWS local (`aws configure --profile X`)",
    )
    # Alternativa al `aws_profile`: pasar creds explícitas via env (copiadas
    # del backend lolo-backend/.env donde se llaman AWS_PUBLIC_KEY/AWS_SECRET_KEY).
    # Si ambas están seteadas, tienen prioridad sobre el profile.
    aws_access_key_id: SecretStr | None = Field(
        default=None,
        description="AWS access key (alternativa a aws_profile). Equivale a AWS_PUBLIC_KEY del backend.",
    )
    aws_secret_access_key: SecretStr | None = Field(
        default=None,
        description="AWS secret key (alternativa a aws_profile). Equivale a AWS_SECRET_KEY del backend.",
    )
    aws_bucket_name: str = Field(
        default="archivosstorage",
        description="S3 bucket donde se suben los anexos. Tiene que matchear AWS_BUCKET_NAME del backend.",
    )

    worker_concurrency: int = Field(default=2, description="N workers BullMQ — laptop friendly")
    browser_pool_size: int = Field(default=2, description="Playwright pool size")
    redis_url: str = Field(default="redis://localhost:6379")
    api_bind_host: str = Field(default="127.0.0.1")
    api_bind_port: int = Field(default=8001)

    # ───── Captcha — compartido entre ambos modos ───────────────────────
    twocaptcha_api_key: SecretStr | None = Field(
        default=None,
        description="API key de 2Captcha (al menos uno entre 2Captcha y CapSolver)",
    )
    capsolver_api_key: SecretStr | None = Field(
        default=None,
        description="API key de CapSolver (al menos uno entre 2Captcha y CapSolver)",
    )
    captcha_provider_order: str = Field(
        default="2captcha,capsolver",
        description="Orden de prueba: csv de '2captcha' y/o 'capsolver'",
    )

    login_url: str = "https://casillas.pj.gob.pe/sinoe/login.xhtml"
    headless: bool = False
    nav_timeout_ms: int = 30_000
    captcha_max_retries: int = 3
    log_level: str = "INFO"
    log_format: str = "console"
    exploration_output_dir: Path = Path("exploration_output")

    @model_validator(mode="after")
    def _at_least_one_captcha_provider(self) -> "Settings":
        if not self.twocaptcha_api_key and not self.capsolver_api_key:
            raise ValueError(
                "At least one of SINOE_TWOCAPTCHA_API_KEY or SINOE_CAPSOLVER_API_KEY must be set"
            )
        return self

    @model_validator(mode="after")
    def _validate_mode(self) -> "Settings":
        """Garantiza que los campos requeridos para el modo activo estén seteados."""
        if self.multitenant_mode:
            if not self.db_url:
                raise ValueError("Multitenant mode requires SINOE_DB_URL")
            if not self.kms_key_id and not self.kms_fallback_master_key:
                raise ValueError(
                    "Multitenant mode requires SINOE_KMS_KEY_ID or "
                    "SINOE_KMS_FALLBACK_MASTER_KEY (POC dev only)"
                )
        else:
            # Legacy CLI mode — necesita casilla + password, pero algunos
            # comandos (ej. `migrate`, `--help`) no requieren login. Por
            # eso la validación es soft acá: solo warning, el login real
            # tira error si falta.
            pass
        return self

    def require_legacy_credentials(self) -> tuple[str, str]:
        """Lee `casilla` + `password` plain, falla con error claro si no están.

        Llamar SOLO desde el CLI legacy. El modo multitenant nunca usa esto
        — las creds se leen de BD + KMS en `workers/scrape_worker.py`.
        """
        if self.multitenant_mode:
            raise RuntimeError(
                "require_legacy_credentials llamado en modo multitenant. "
                "El scraper en modo prod lee creds de SINOE_ACCOUNT, no del .env."
            )
        if not self.casilla or not self.password:
            raise RuntimeError(
                "Modo legacy del CLI requiere SINOE_CASILLA y SINOE_PASSWORD en .env. "
                "Si querés correr en modo multitenant, setear SINOE_MULTITENANT_MODE=true."
            )
        return self.casilla, self.password.get_secret_value()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None
