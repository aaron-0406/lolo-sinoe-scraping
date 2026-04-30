"""CAPTCHA solver interface.

Defines the Protocol that all solvers must implement so we can swap
2Captcha for CapSolver / ImageTyperz / a manual solver in tests.
"""

from typing import Protocol


class CaptchaSolver(Protocol):
    name: str

    async def solve(self, image_bytes: bytes) -> str:
        """Solve a static-image CAPTCHA. Returns the recognized text.

        Raises:
            CaptchaUnsolvable: if all retries are exhausted.
        """
        ...
