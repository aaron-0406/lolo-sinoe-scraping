"""Error taxonomy for SINOE operations."""


class SinoeError(Exception):
    """Base error for any SINOE-related failure."""


class SinoeUnreachable(SinoeError):
    """SINOE site is down, network failed, or page never loaded."""


class LoginFailed(SinoeError):
    """Credentials are invalid or rejected by SINOE."""


class CaptchaUnsolvable(SinoeError):
    """CAPTCHA solver gave up after all retries."""


class SessionExpired(SinoeError):
    """The session was active but is no longer valid."""


class UnexpectedPageState(SinoeError):
    """The DOM does not match expectations — SINOE may have changed."""


class UnsafeOperation(SinoeError):
    """An exploration step would have caused a side-effect (e.g. opening an unread notif)."""
