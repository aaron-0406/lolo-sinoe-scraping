"""Entry point del API server.

Uso:
    python -m lolo_sinoe.api  # arranca FastAPI + workers + scheduler
"""

from __future__ import annotations

import uvicorn

from ..config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "lolo_sinoe.api.server:app",
        host=settings.api_bind_host,
        port=settings.api_bind_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
