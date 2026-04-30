"""Verify secret redaction in structlog processor."""

from lolo_sinoe.logging import _redact_secrets


def test_redacts_password() -> None:
    out = _redact_secrets(None, "info", {"password": "supersecret"})
    assert "supersecret" not in str(out)
    assert out["password"].startswith("<redacted")


def test_redacts_captcha_solution() -> None:
    out = _redact_secrets(None, "info", {"captcha_solution": "ABCDE"})
    assert "ABCDE" not in str(out)


def test_redacts_api_key() -> None:
    out = _redact_secrets(None, "info", {"twocaptcha_api_key": "abc123def456"})
    assert "abc123def456" not in str(out)


def test_does_not_redact_safe_keys() -> None:
    out = _redact_secrets(None, "info", {"casilla": "12345", "url": "https://example.com"})
    assert out["casilla"] == "12345"
    assert out["url"] == "https://example.com"


def test_redacts_empty_secret_distinctively() -> None:
    out = _redact_secrets(None, "info", {"password": ""})
    assert out["password"] == "<empty>"
