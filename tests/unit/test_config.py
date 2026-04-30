"""Test config loading from env vars."""

import pytest
from pydantic import SecretStr, ValidationError

from lolo_sinoe.config import Settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid leaking actual env values from CI/dev shell into test cases.
    for key in [
        "SINOE_CASILLA",
        "SINOE_PASSWORD",
        "SINOE_TWOCAPTCHA_API_KEY",
        "SINOE_CAPSOLVER_API_KEY",
        "SINOE_CAPTCHA_PROVIDER_ORDER",
    ]:
        monkeypatch.delenv(key, raising=False)
    reset_settings_cache()


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "12345")
    monkeypatch.setenv("SINOE_PASSWORD", "secret")
    monkeypatch.setenv("SINOE_TWOCAPTCHA_API_KEY", "key123")

    s = Settings(_env_file=None)
    assert s.casilla == "12345"
    assert isinstance(s.password, SecretStr)
    assert s.password.get_secret_value() == "secret"
    assert s.twocaptcha_api_key is not None
    assert s.twocaptcha_api_key.get_secret_value() == "key123"


def test_settings_password_is_secretstr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "12345")
    monkeypatch.setenv("SINOE_PASSWORD", "supersecret")
    monkeypatch.setenv("SINOE_TWOCAPTCHA_API_KEY", "key")

    s = Settings(_env_file=None)
    assert "supersecret" not in repr(s)
    assert "supersecret" not in str(s)


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "x")
    monkeypatch.setenv("SINOE_PASSWORD", "y")
    monkeypatch.setenv("SINOE_TWOCAPTCHA_API_KEY", "z")

    s = Settings(_env_file=None)
    assert s.headless is False
    assert s.nav_timeout_ms == 30_000
    assert s.captcha_max_retries == 3
    assert s.login_url.startswith("https://casillas.pj.gob.pe/")
    assert s.captcha_provider_order == "2captcha,capsolver"


def test_settings_capsolver_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "x")
    monkeypatch.setenv("SINOE_PASSWORD", "y")
    monkeypatch.setenv("SINOE_CAPSOLVER_API_KEY", "cs")

    s = Settings(_env_file=None)
    assert s.twocaptcha_api_key is None
    assert s.capsolver_api_key is not None
    assert s.capsolver_api_key.get_secret_value() == "cs"


def test_settings_both_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "x")
    monkeypatch.setenv("SINOE_PASSWORD", "y")
    monkeypatch.setenv("SINOE_TWOCAPTCHA_API_KEY", "tc")
    monkeypatch.setenv("SINOE_CAPSOLVER_API_KEY", "cs")

    s = Settings(_env_file=None)
    assert s.twocaptcha_api_key is not None
    assert s.capsolver_api_key is not None


def test_settings_no_provider_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINOE_CASILLA", "x")
    monkeypatch.setenv("SINOE_PASSWORD", "y")
    # Neither captcha provider set — must fail validation.
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
