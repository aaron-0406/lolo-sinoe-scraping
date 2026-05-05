"""Error taxonomy for SINOE operations.

Cada subclase declara `sync_log_status: str` — el ENUM exacto del schema
`SINOE_SYNC_LOG.status` / `SINOE_ACCOUNT.last_sync_status`. Permite
clasificar errores por `isinstance` (ver `error_to_status`) en lugar de
matching de strings frágil sobre el `str(e)`.
"""


class SinoeError(Exception):
    """Base error for any SINOE-related failure."""

    sync_log_status: str = "partial"


class SinoeUnreachable(SinoeError):
    """SINOE site is down, network failed, or page never loaded."""

    sync_log_status = "sinoe_unreachable"


class LoginFailed(SinoeError):
    """Credentials are invalid or rejected by SINOE."""

    sync_log_status = "login_failed"


class CaptchaUnsolvable(SinoeError):
    """CAPTCHA solver gave up after all retries."""

    sync_log_status = "captcha_unsolvable"


class SessionExpired(SinoeError):
    """The session was active but is no longer valid."""

    sync_log_status = "session_conflict"


class SessionConflict(SinoeError):
    """Two sessions tried to use the same casilla simultaneously."""

    sync_log_status = "session_conflict"


class UnexpectedPageState(SinoeError):
    """The DOM does not match expectations — SINOE may have changed."""

    sync_log_status = "unexpected_dom"


class UnsafeOperation(SinoeError):
    """An exploration step would have caused a side-effect (e.g. opening an unread notif)."""

    sync_log_status = "partial"


def error_to_status(exc: BaseException) -> str:
    """Mapea una excepción al ENUM `last_sync_status`. Errores no SINOE
    caen a `"partial"` — el sync se considera degradado pero no fallido
    de forma específica.
    """
    if isinstance(exc, SinoeError):
        return exc.sync_log_status
    return "partial"
