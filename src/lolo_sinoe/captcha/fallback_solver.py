"""FallbackSolver: try multiple CAPTCHA providers in order.

Same Protocol as the leaf solvers, so callers don't know they're using a chain.
"""

from lolo_sinoe.captcha.solver import CaptchaSolver
from lolo_sinoe.errors import CaptchaUnsolvable
from lolo_sinoe.logging import get_logger

logger = get_logger(__name__)


class FallbackSolver:
    """Try each solver in order; succeed if any succeeds."""

    def __init__(self, solvers: list[CaptchaSolver]) -> None:
        if not solvers:
            raise ValueError("FallbackSolver needs at least one underlying solver")
        self._solvers = solvers
        self.name = "+".join(s.name for s in solvers)

    async def solve(self, image_bytes: bytes) -> str:
        last_error: Exception | None = None
        for solver in self._solvers:
            try:
                logger.info("captcha_provider_attempt", provider=solver.name)
                return await solver.solve(image_bytes)
            except CaptchaUnsolvable as e:
                logger.warn(
                    "captcha_provider_failed",
                    provider=solver.name,
                    error=str(e),
                )
                last_error = e
                continue
        raise CaptchaUnsolvable(
            f"All {len(self._solvers)} providers failed. Last error: {last_error}"
        ) from last_error
