"""Structured logging via structlog. Redacts secrets automatically."""

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "pwd",
        "passwd",
        "twocaptcha_api_key",
        "captcha_api_key",
        "api_key",
        "token",
        "auth_token",
        "authorization",
        "captcha_solution",
    }
)


def _redact_secrets(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Redact any value whose key looks like a secret."""
    for key in list(event_dict.keys()):
        if key.lower() in SECRET_KEYS:
            value = event_dict[key]
            if value is None or value == "":
                event_dict[key] = "<empty>"
            else:
                event_dict[key] = f"<redacted:len={len(str(value))}>"
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Configure structlog and stdlib logging.

    Args:
        level: Standard logging level name.
        fmt: "console" for human-readable, "json" for production.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]

    if fmt == "json":
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
