"""Scheduler interno del scraper — corre cada 60s.

Lee `SINOE_ACCOUNT.find_due()` y mete jobs en queues Redis. Es el ÚNICO
componente que encola — el backend coordina via flag `sync_requested_at`
en BD (Plan §7.1), no via HTTP al scraper (sería imposible con NAT
residencial).
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
import structlog

from ..workers.shared_resources import SharedResources
from ..workers.worker_manager import (
    QUEUE_INITIAL,
    QUEUE_MONITOR,
    QUEUE_PRIORITY,
    enqueue_job,
)

logger = structlog.get_logger(__name__)

TICK_INTERVAL_SECONDS = 60


class Scheduler:
    def __init__(self, resources: SharedResources) -> None:
        self._r = resources
        self._redis: aioredis.Redis | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._redis = aioredis.from_url(self._r.settings.redis_url, decode_responses=True)
        self._task = asyncio.create_task(self._loop())
        logger.info("sinoe_scheduler_started", interval_s=TICK_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._redis:
            await self._redis.aclose()
        logger.info("sinoe_scheduler_stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.exception("sinoe_scheduler_tick_error", error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        assert self._redis is not None
        # `find_due()` ahora devuelve DueAccount con los campos necesarios
        # para decidir la queue — sin N+1 al `get_by_id` por cuenta.
        due = self._r.accounts.find_due()
        if not due:
            return

        priority_count = 0
        cron_count = 0
        for account in due:
            if account.sync_requested_at:
                # Solicitado manual desde la UI → priority queue
                queue = QUEUE_PRIORITY
                trigger = "manual"
                priority_count += 1
            elif account.last_sync_completed_at is None:
                # Primera vez → initial queue
                queue = QUEUE_INITIAL
                trigger = "cron"
                cron_count += 1
            else:
                queue = QUEUE_MONITOR
                trigger = "cron"
                cron_count += 1
            await enqueue_job(self._redis, queue, account.id, trigger_kind=trigger)

        logger.info(
            "sinoe_scheduler_enqueued",
            total=len(due),
            priority=priority_count,
            cron=cron_count,
        )
