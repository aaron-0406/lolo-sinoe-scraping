"""2Captcha-based image CAPTCHA solver."""

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


class TwoCaptchaSolver:
    name: str = "2captcha"

    def __init__(self, api_key: str, max_retries: int = 3) -> None:
        # Lazy import so unit tests can mock without 2captcha installed at import time.
        from twocaptcha import TwoCaptcha

        self._client = TwoCaptcha(api_key)
        self._max_retries = max_retries

    async def solve(self, image_bytes: bytes) -> str:
        """Send image to 2Captcha and return the recognized text.

        Retries with exponential backoff up to self._max_retries times.
        """
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
                        attempt=attempt.retry_state.attempt_number,
                        image_bytes=len(image_bytes),
                    )
                    result = await asyncio.to_thread(self._submit, b64)
                    code = str(result.get("code", "")).strip()
                    if not code:
                        raise CaptchaUnsolvable("2Captcha returned empty code")
                    logger.info(
                        "captcha_solved",
                        attempt=attempt.retry_state.attempt_number,
                        captcha_solution=code,
                    )
                    return code
        except RetryError as e:
            raise CaptchaUnsolvable(
                f"2Captcha failed after {self._max_retries} attempts"
            ) from e

        # Defensive: AsyncRetrying always returns or raises.
        raise CaptchaUnsolvable("2Captcha solver exited without result")

    def _submit(self, b64_image: str) -> dict[str, Any]:
        return self._client.normal(b64_image)  # type: ignore[no-any-return]
