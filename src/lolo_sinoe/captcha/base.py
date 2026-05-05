"""Base shared by image-CAPTCHA solvers (2Captcha, CapSolver, etc.).

Centraliza el patrón retry-con-backoff + logging que los solvers concretos
usaban duplicado. Las subclases sólo implementan `_call_provider()` con la
llamada bloqueante a la API del proveedor — la base se ocupa del resto.
"""

from __future__ import annotations

import asyncio
import base64
from abc import ABC, abstractmethod
from typing import Any

from tenacity import AsyncRetrying, RetryError, stop_after_attempt, wait_exponential

from lolo_sinoe.errors import CaptchaUnsolvable
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


class BaseImageCaptchaSolver(ABC):
    """Plantilla para solvers de CAPTCHAs de imagen estática.

    Subclases implementan:
      - `name: str` (atributo de clase)
      - `_call_provider(b64_image: str) -> str` — llamada sync a la API,
        con cualquier parsing del response. Retorna la cadena solucionada
        o `""` para señalar respuesta vacía (la base la convierte en error).
    """

    name: str = "base"

    def __init__(self, max_retries: int = 3) -> None:
        self._max_retries = max_retries

    async def solve(self, image_bytes: bytes) -> str:
        """Resuelve un CAPTCHA con retry exponencial. La excepción final
        es siempre `CaptchaUnsolvable` (las concretas envuelven errores
        del proveedor)."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries),
                wait=wait_exponential(multiplier=2, min=2, max=20),
                reraise=False,
            ):
                with attempt:
                    logger.info(
                        "captcha_solve_attempt",
                        provider=self.name,
                        attempt=attempt.retry_state.attempt_number,
                        image_bytes=len(image_bytes),
                    )
                    text = await asyncio.to_thread(self._call_provider, b64)
                    text = (text or "").strip()
                    if not text:
                        raise CaptchaUnsolvable(f"{self.name} returned empty solution")
                    logger.info(
                        "captcha_solved",
                        provider=self.name,
                        attempt=attempt.retry_state.attempt_number,
                        captcha_solution=text,
                    )
                    return text
        except RetryError as e:
            raise CaptchaUnsolvable(
                f"{self.name} failed after {self._max_retries} attempts"
            ) from e
        # Defensive: AsyncRetrying always returns or raises in normal paths.
        raise CaptchaUnsolvable(f"{self.name} solver exited without result")

    @abstractmethod
    def _call_provider(self, b64_image: str) -> str:
        """Bloqueante. Llama a la API del proveedor y devuelve el texto.
        Las excepciones del proveedor se propagan — `tenacity` las captura
        para hacer retry."""
        raise NotImplementedError

    @staticmethod
    def _extract_text_from_dict(result: Any, keys: tuple[str, ...]) -> str:
        """Helper para parsear responses tipo `{"text": "..."}` o
        `{"solution": {"text": "..."}}`. Reusable por solvers que devuelven
        respuestas JSON con shape variable."""
        if not isinstance(result, dict):
            return ""
        for key in keys:
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        sol = result.get("solution")
        if isinstance(sol, dict):
            for key in keys:
                val = sol.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""
