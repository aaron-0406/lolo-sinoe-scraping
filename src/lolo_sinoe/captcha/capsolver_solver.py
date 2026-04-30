"""CapSolver-based image CAPTCHA solver.

Same Protocol as TwoCaptchaSolver — drop-in alternative or fallback.
"""

import asyncio
import base64
from typing import Any

from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)

from lolo_sinoe.errors import CaptchaUnsolvable
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


class CapSolverSolver:
    name: str = "capsolver"

    def __init__(self, api_key: str, max_retries: int = 3) -> None:
        # Lazy import to keep test isolation clean.
        import capsolver

        self._capsolver = capsolver
        self._capsolver.api_key = api_key
        self._max_retries = max_retries

    async def solve(self, image_bytes: bytes) -> str:
        """Send image to CapSolver and return the recognized text."""
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
                    result = await asyncio.to_thread(self._submit, b64)
                    text = self._extract_text(result)
                    if not text:
                        raise CaptchaUnsolvable(f"CapSolver returned no text: {result}")
                    logger.info(
                        "captcha_solved",
                        provider=self.name,
                        attempt=attempt.retry_state.attempt_number,
                        captcha_solution=text,
                    )
                    return text
        except RetryError as e:
            raise CaptchaUnsolvable(
                f"CapSolver failed after {self._max_retries} attempts"
            ) from e

        raise CaptchaUnsolvable("CapSolver exited without result")

    def _submit(self, b64_image: str) -> dict[str, Any]:
        return self._capsolver.solve(  # type: ignore[no-any-return]
            {
                "type": "ImageToTextTask",
                "body": b64_image,
            }
        )

    @staticmethod
    def _extract_text(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return ""
        sol = result.get("solution") or result
        for key in ("text", "answer", "captcha"):
            val = sol.get(key) if isinstance(sol, dict) else None
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
