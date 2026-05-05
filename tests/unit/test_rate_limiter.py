"""Unit tests del RateLimiter — token bucket asíncrono."""

from __future__ import annotations

import asyncio

import pytest

from lolo_sinoe.workers.rate_limiter import RateLimiter


async def test_acquire_within_capacity_does_not_sleep() -> None:
    """Con capacity holgado, las primeras N llamadas son inmediatas."""
    limiter = RateLimiter(rate=1.0, capacity=5.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for _ in range(5):
        await limiter.acquire()
    elapsed = loop.time() - t0
    # 5 acquires con capacity=5 deben ser instantáneos (<50ms).
    assert elapsed < 0.05


async def test_acquire_blocks_when_empty() -> None:
    """Una vez vaciada la capacity, el siguiente acquire espera al refill."""
    limiter = RateLimiter(rate=10.0, capacity=2.0)  # 10 tokens/seg
    await limiter.acquire()
    await limiter.acquire()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await limiter.acquire()
    elapsed = loop.time() - t0
    # Necesita al menos ~0.1s (1/10 del segundo) para tener 1 token de nuevo.
    assert elapsed >= 0.08, f"esperaba ≥0.08s, dio {elapsed:.3f}s"


async def test_acquire_more_than_capacity_raises() -> None:
    """Pedir más tokens que `capacity` debe levantar ValueError — imposible
    de satisfacer aunque esperemos infinito."""
    limiter = RateLimiter(rate=1.0, capacity=2.0)
    with pytest.raises(ValueError, match="imposible"):
        await limiter.acquire(tokens=3.0)


async def test_invalid_rate_raises() -> None:
    """rate=0 o negativo no tiene sentido."""
    with pytest.raises(ValueError, match="rate"):
        RateLimiter(rate=0.0)
    with pytest.raises(ValueError, match="rate"):
        RateLimiter(rate=-1.0)


async def test_concurrent_acquire_serializes() -> None:
    """Dos coroutines compitiendo por el mismo bucket deben recibir
    tokens en orden, no consumir más de los disponibles."""
    limiter = RateLimiter(rate=20.0, capacity=1.0)
    await limiter.acquire()  # vacía la capacity
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    async def grab() -> float:
        await limiter.acquire()
        return loop.time() - t0

    times = await asyncio.gather(grab(), grab(), grab())
    # 3 tokens a 20/s → ~0.05s entre cada uno → último ~0.15s
    assert max(times) >= 0.10, f"max wait too short: {max(times)}"
