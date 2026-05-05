"""Manager que arranca N workers en paralelo procesando jobs de Redis.

POC nota: BullMQ usa Redis nativo. Para Python, en lugar de hacer mirror
con `bullmq-py` (lib joven, ABI puede cambiar), usamos un patrón más
simple: workers que hacen LPUSH/BRPOP sobre listas Redis. Mismo modelo
de queue, sin acoplarse a BullMQ específico. El backend coordina via
flags en BD (Plan §7.1) — no encola jobs en Redis directamente.

Si en el futuro queremos compartir cola con CEJ scraper (que sí usa
BullMQ), migrar a `bullmq-py` o reescribir CEJ a este patrón.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import redis.asyncio as aioredis
import structlog

from .scrape_worker import ScrapeWorker
from .shared_resources import SharedResources

logger = structlog.get_logger(__name__)

QUEUE_PRIORITY = "sinoe:priority"
QUEUE_MONITOR = "sinoe:monitor"
QUEUE_INITIAL = "sinoe:initial"

# Orden de prioridad — el worker hace BRPOP a la primera no vacía.
QUEUES_BY_PRIORITY = [QUEUE_PRIORITY, QUEUE_MONITOR, QUEUE_INITIAL]


class WorkerManager:
    """Arranca N workers en asyncio que consumen jobs hasta shutdown."""

    def __init__(self, resources: SharedResources) -> None:
        self._r = resources
        self._redis: aioredis.Redis | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._redis = aioredis.from_url(self._r.settings.redis_url, decode_responses=True)
        n = self._r.settings.worker_concurrency
        logger.info("sinoe_workers_starting", count=n)
        for i in range(n):
            task = asyncio.create_task(
                self._worker_loop(worker_id=f"w{i + 1}-{uuid.uuid4().hex[:6]}")
            )
            self._tasks.append(task)

    async def stop(self) -> None:
        logger.info("sinoe_workers_stopping")
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._redis:
            await self._redis.aclose()

    async def redis_health(self) -> str:
        """Returns 'ok' si Redis responde a PING, error string si no.
        Wrapper público para `/health` — evita reach into `_redis`."""
        if self._redis is None:
            return "not_started"
        try:
            await self._redis.ping()
            return "ok"
        except Exception as e:  # pragma: no cover
            return f"error: {str(e)[:100]}"

    async def _worker_loop(self, worker_id: str) -> None:
        assert self._redis is not None
        worker = ScrapeWorker(self._r, worker_id=worker_id)
        logger.info("sinoe_worker_loop_started", worker_id=worker_id)
        while not self._stop.is_set():
            try:
                # BRPOP con timeout 5s — chequea stop signal periódicamente
                result = await self._redis.brpop(QUEUES_BY_PRIORITY, timeout=5)
                if not result:
                    continue
                _, payload = result
                job_data = json.loads(payload)
                logger.info(
                    "sinoe_worker_job_received",
                    worker_id=worker_id,
                    account_id=job_data.get("account_id"),
                )
                await worker.process(job_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("sinoe_worker_loop_error", worker_id=worker_id, error=str(e))
                # Backoff antes de reintentar para no agarrarse en un loop loco
                await asyncio.sleep(2)
        logger.info("sinoe_worker_loop_stopped", worker_id=worker_id)


async def enqueue_job(
    redis: aioredis.Redis,
    queue: str,
    account_id: int,
    trigger_kind: str = "cron",
) -> None:
    """Helper para que el scheduler meta jobs en la cola."""
    payload = json.dumps({"account_id": account_id, "trigger_kind": trigger_kind})
    await redis.lpush(queue, payload)
