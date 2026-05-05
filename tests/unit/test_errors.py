"""Unit tests de la taxonomía de errores y el mapeo a `last_sync_status`."""

from __future__ import annotations

from lolo_sinoe.errors import (
    CaptchaUnsolvable,
    LoginFailed,
    SessionConflict,
    SinoeError,
    SinoeUnreachable,
    UnexpectedPageState,
    UnsafeOperation,
    error_to_status,
)


def test_each_error_class_declares_status() -> None:
    """Cada subclase de SinoeError debe declarar su `sync_log_status`
    para que `error_to_status` pueda mapear sin string-matching frágil."""
    cases: list[tuple[type[SinoeError], str]] = [
        (SinoeUnreachable, "sinoe_unreachable"),
        (LoginFailed, "login_failed"),
        (CaptchaUnsolvable, "captcha_unsolvable"),
        (SessionConflict, "session_conflict"),
        (UnexpectedPageState, "unexpected_dom"),
        (UnsafeOperation, "partial"),
    ]
    for cls, expected in cases:
        assert cls.sync_log_status == expected, f"{cls.__name__} → {cls.sync_log_status}"


def test_error_to_status_uses_isinstance() -> None:
    """No depender de string matching: la clase decide el ENUM."""
    assert error_to_status(LoginFailed("x")) == "login_failed"
    assert error_to_status(CaptchaUnsolvable("x")) == "captcha_unsolvable"
    assert error_to_status(SinoeUnreachable("x")) == "sinoe_unreachable"


def test_error_to_status_defaults_to_partial_for_non_sinoe() -> None:
    """Una excepción no SINOE (TimeoutError, ValueError, etc.) cae a `partial`."""
    assert error_to_status(ValueError("anything")) == "partial"
    assert error_to_status(RuntimeError("oops")) == "partial"
    assert error_to_status(TimeoutError()) == "partial"
