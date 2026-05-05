"""Token bucket rate limiter compartido entre workers.

Plan §4.1: máximo `5 reqs/60s` global a `casillas.pj.gob.pe` para no
parecer DoS y no agotar la sesión. Se aplica al inicio de cada sync
(antes del login y antes de cada navegación pesada). Como el server
arranca con `worker_concurrency=2`, dos workers comparten un mismo
limiter — el bucket es process-wide.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class _State:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token bucket asíncrono. Thread-safe a nivel asyncio (un único
    event loop por proceso, lo cual cumple el modelo de FastAPI/uvicorn).

    Args:
        rate: tokens por segundo. Para `5/60s` pasar `5/60`.
        capacity: cantidad máxima de tokens (burst). Default = `rate * 60`
            así un sync puede gastar varios tokens si la cola estuvo
            inactiva un rato.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate debe ser > 0")
        self._rate = rate
        self._capacity = capacity if capacity is not None else max(1.0, rate * 60)
        self._lock = asyncio.Lock()
        self._state = _State(tokens=self._capacity, last_refill=0.0)

    async def acquire(self, tokens: float = 1.0) -> None:
        """Bloquea hasta tener `tokens` disponibles. Reentrante seguro."""
        if tokens > self._capacity:
            raise ValueError(f"tokens pedidos ({tokens}) > capacity ({self._capacity}) — imposible")
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:
                now = loop.time()
                if self._state.last_refill == 0.0:
                    self._state.last_refill = now
                # Refill proporcional al tiempo pasado.
                elapsed = now - self._state.last_refill
                self._state.tokens = min(self._capacity, self._state.tokens + elapsed * self._rate)
                self._state.last_refill = now
                if self._state.tokens >= tokens:
                    self._state.tokens -= tokens
                    return
                # No alcanzan — calcular sleep mínimo para que alcancen.
                deficit = tokens - self._state.tokens
                sleep_s = deficit / self._rate
            # Sleep fuera del lock para no serializar a otros consumidores.
            await asyncio.sleep(sleep_s)


# Singleton global de fábrica — el SharedResources crea uno y lo reusa.
SINOE_DEFAULT_RATE_PER_SECOND = 5.0 / 60.0  # 5 reqs por minuto
