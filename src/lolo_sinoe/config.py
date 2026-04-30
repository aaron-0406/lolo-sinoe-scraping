"""Configuration loaded from environment / .env."""

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

    casilla: str = Field(..., description="Número de casilla electrónica SINOE")
    password: SecretStr = Field(..., description="Contraseña de la casilla")

    twocaptcha_api_key: SecretStr | None = Field(
        default=None, description="API key de 2Captcha (al menos uno entre 2Captcha y CapSolver)"
    )
    capsolver_api_key: SecretStr | None = Field(
        default=None, description="API key de CapSolver (al menos uno entre 2Captcha y CapSolver)"
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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None
