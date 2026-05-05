"""Captcha solvers + builder canónico.

Exporta el Protocol, los solvers concretos, el FallbackSolver chain y la
función `build_captcha_solver(settings)` — fuente única para armar la
cadena de proveedores. Tanto el CLI legacy como `SharedResources` la
invocan, evitando lazy imports cross-módulo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lolo_sinoe.captcha.capsolver_solver import CapSolverSolver
from lolo_sinoe.captcha.fallback_solver import FallbackSolver
from lolo_sinoe.captcha.solver import CaptchaSolver
from lolo_sinoe.captcha.twocaptcha_solver import TwoCaptchaSolver

if TYPE_CHECKING:
    from lolo_sinoe.config import Settings

__all__ = [
    "CapSolverSolver",
    "CaptchaSolver",
    "FallbackSolver",
    "TwoCaptchaSolver",
    "build_captcha_solver",
]


def build_captcha_solver(settings: Settings) -> CaptchaSolver:
    """Construye la cadena de solvers según `settings.captcha_provider_order`.

    - Si solo un proveedor está configurado, devuelve el solver puro.
    - Si dos están configurados, devuelve un `FallbackSolver` con el orden
      especificado.
    - Si ninguno, levanta `RuntimeError` claro.
    """
    requested = [
        p.strip().lower()
        for p in settings.captcha_provider_order.split(",")
        if p.strip()
    ]
    available: list[CaptchaSolver] = []
    for provider in requested:
        if provider == "2captcha" and settings.twocaptcha_api_key is not None:
            available.append(
                TwoCaptchaSolver(
                    api_key=settings.twocaptcha_api_key.get_secret_value(),
                    max_retries=settings.captcha_max_retries,
                )
            )
        elif provider == "capsolver" and settings.capsolver_api_key is not None:
            available.append(
                CapSolverSolver(
                    api_key=settings.capsolver_api_key.get_secret_value(),
                    max_retries=settings.captcha_max_retries,
                )
            )
    if not available:
        raise RuntimeError(
            "No CAPTCHA provider configured. Set SINOE_TWOCAPTCHA_API_KEY and/or "
            "SINOE_CAPSOLVER_API_KEY, and ensure SINOE_CAPTCHA_PROVIDER_ORDER includes them."
        )
    if len(available) == 1:
        return available[0]
    return FallbackSolver(available)
