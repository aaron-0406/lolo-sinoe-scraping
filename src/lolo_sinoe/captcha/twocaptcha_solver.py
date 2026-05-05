"""2Captcha-based image CAPTCHA solver."""

from __future__ import annotations

from .base import BaseImageCaptchaSolver


class TwoCaptchaSolver(BaseImageCaptchaSolver):
    name: str = "2captcha"

    def __init__(self, api_key: str, max_retries: int = 3) -> None:
        super().__init__(max_retries=max_retries)
        # Lazy import so unit tests can mock without 2captcha installed.
        from twocaptcha import TwoCaptcha

        self._client = TwoCaptcha(api_key)

    def _call_provider(self, b64_image: str) -> str:
        """API de 2Captcha devuelve `{"captchaId": "...", "code": "ABCDE"}`.
        Sólo nos importa `code`."""
        result = self._client.normal(b64_image)
        return str(result.get("code", "")) if isinstance(result, dict) else ""
