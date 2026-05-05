"""FastAPI server — bind 127.0.0.1:8001 (sólo localhost).

Endpoints:
- GET /health    → estado de DB + Redis + browser pool
- GET /metrics   → Prometheus text format (in-memory counters)
- GET /          → dashboard HTML auto-refresh (cuentas + últimos syncs)

NO expone routes_internal — el backend coordina via flag en BD, no via
HTTP al scraper (Plan §7.1).

El server además arranca el WorkerManager + Scheduler en background tasks.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import text

from ..config import get_settings
from ..scheduler.scheduler import Scheduler
from ..workers.shared_resources import SharedResources
from ..workers.worker_manager import WorkerManager

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks de FastAPI."""
    settings = get_settings()
    if not settings.multitenant_mode:
        raise RuntimeError(
            "API server requiere SINOE_MULTITENANT_MODE=true. "
            "Para CLI dev usar `python -m lolo_sinoe.cli login`."
        )
    resources = SharedResources.from_settings(settings)
    worker_manager = WorkerManager(resources)
    scheduler = Scheduler(resources)

    # Browser pool: arranca antes que los workers para que `acquire_context`
    # no falle por race con un pool aún no iniciado.
    await resources.browser_pool.start()
    await worker_manager.start()
    await scheduler.start()

    app.state.resources = resources
    app.state.worker_manager = worker_manager
    app.state.scheduler = scheduler

    logger.info("sinoe_api_started", port=settings.api_bind_port)
    yield
    logger.info("sinoe_api_stopping")
    # Orden inverso: scheduler para de encolar → workers terminan jobs en
    # vuelo → browser pool cierra los Chromium. Si paramos el pool primero,
    # los workers en vuelo crashean al intentar `acquire_context`.
    await scheduler.stop()
    await worker_manager.stop()
    await resources.browser_pool.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check — verifica DB + Redis. Útil para curl manual durante
    debugging local.

    Devuelve `status: "ok"` solo si TODOS los chequeos pasan; cualquier
    falla degrada a `"degraded"` y deja detalle en `checks[X]`.
    """
    resources: SharedResources = app.state.resources
    worker_manager: WorkerManager = app.state.worker_manager

    checks: dict[str, str] = {}
    overall = "ok"

    db_status = resources.db_health()
    checks["db"] = db_status
    if db_status != "ok":
        overall = "degraded"

    redis_status = await worker_manager.redis_health()
    checks["redis"] = redis_status
    if redis_status != "ok":
        overall = "degraded"

    return {"status": overall, "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Prometheus-style text format. Counters in-memory, no DB query.
    Para POC sin scrape externo: útil para curl manual al debuggear."""
    return (
        "# HELP sinoe_scraper_up Whether the scraper process is running\n"
        "# TYPE sinoe_scraper_up gauge\n"
        "sinoe_scraper_up 1\n"
    )


# ─── Dashboard HTML ──────────────────────────────────────────────────────
# Vista mínima self-contained — sin frontend framework, sin JS. Auto-refresh
# vía meta-tag cada 5s. Lee directo de DB (cuentas + últimos sync logs) +
# Redis (queue depths) cada request. Pensado para tener el server local
# abierto en una pestaña mientras corren los syncs.


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    delta = datetime.utcnow() - dt
    if delta < timedelta(seconds=60):
        return f"{int(delta.total_seconds())}s ago"
    if delta < timedelta(minutes=60):
        return f"{int(delta.total_seconds() / 60)}m ago"
    if delta < timedelta(hours=24):
        return f"{int(delta.total_seconds() / 3600)}h ago"
    return dt.strftime("%Y-%m-%d %H:%M")


_STATUS_COLORS: dict[str, str] = {
    "ok": "#16a34a",
    "running": "#0ea5e9",
    "never": "#94a3b8",
    "partial": "#eab308",
    "login_failed": "#dc2626",
    "captcha_unsolvable": "#dc2626",
    "sinoe_unreachable": "#dc2626",
    "session_conflict": "#f97316",
    "unexpected_dom": "#f97316",
}


def _status_badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#94a3b8")
    safe = html.escape(status)
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:3px;'
        f'background:{color}22;color:{color};font-size:11px;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.5px;">{safe}</span>'
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Dashboard HTML — vista de operación local."""
    resources: SharedResources = app.state.resources
    worker_manager: WorkerManager = app.state.worker_manager

    # Cuentas
    accounts_rows: list[Any] = []
    try:
        with resources.session_factory() as session:
            accounts_rows = session.execute(
                text(
                    """
                    SELECT
                      id_sinoe_account, casilla_number, alias,
                      is_active, sync_frequency_minutes,
                      last_sync_started_at, last_sync_completed_at, last_sync_status,
                      consecutive_failure_count, notifications_synced_total,
                      sync_requested_at, cached_session_expires_at
                    FROM SINOE_ACCOUNT
                    WHERE deleted_at IS NULL
                    ORDER BY last_sync_completed_at DESC, id_sinoe_account ASC
                    LIMIT 100
                    """
                )
            ).fetchall()
    except Exception as e:
        logger.exception("dashboard_accounts_query_failed", error=str(e))

    # Sync logs recientes
    logs_rows: list[Any] = []
    try:
        with resources.session_factory() as session:
            logs_rows = session.execute(
                text(
                    """
                    SELECT
                      id_sinoe_sync_log, sinoe_account_id, started_at, ended_at, status,
                      notifications_seen, notifications_new, notifications_updated,
                      attachments_downloaded, attachments_skipped_dedupe, bytes_downloaded,
                      session_was_reused
                    FROM SINOE_SYNC_LOG
                    ORDER BY id_sinoe_sync_log DESC
                    LIMIT 30
                    """
                )
            ).fetchall()
    except Exception as e:
        logger.exception("dashboard_logs_query_failed", error=str(e))

    # Queue depths
    queue_depths: dict[str, int | str] = {}
    try:
        redis = worker_manager._redis  # noqa: SLF001
        if redis is not None:
            for q in ("sinoe:priority", "sinoe:monitor", "sinoe:initial"):
                queue_depths[q] = await redis.llen(q)
        else:
            queue_depths = {"sinoe:priority": "?", "sinoe:monitor": "?", "sinoe:initial": "?"}
    except Exception as e:
        queue_depths = {"error": str(e)}

    # Health
    db_status = resources.db_health()
    redis_status = await worker_manager.redis_health()

    # Render
    settings = get_settings()
    title = f"SINOE scraper — local dashboard (port {settings.api_bind_port})"

    accounts_html_rows: list[str] = []
    for r in accounts_rows:
        (
            acc_id,
            casilla,
            alias,
            is_active,
            freq,
            started,
            completed,
            status,
            failures,
            notifs_total,
            requested,
            session_expires,
        ) = r
        accounts_html_rows.append(
            "<tr>"
            f"<td><code>{acc_id}</code></td>"
            f"<td><code>{html.escape(str(casilla))}</code></td>"
            f"<td>{html.escape(alias or '')}</td>"
            f"<td>{'✅' if is_active else '⛔'}</td>"
            f"<td>{freq}m</td>"
            f"<td>{_status_badge(status)}</td>"
            f"<td>{html.escape(_fmt_dt(started))}</td>"
            f"<td>{html.escape(_fmt_dt(completed))}</td>"
            f"<td>{notifs_total}</td>"
            f"<td>{failures}</td>"
            f"<td>{'⏳' if requested else ''}</td>"
            f"<td>{'🍪' if session_expires and session_expires > datetime.utcnow() else '—'}</td>"
            "</tr>"
        )

    logs_html_rows: list[str] = []
    for r in logs_rows:
        (
            log_id,
            acc_id,
            started,
            ended,
            status,
            seen,
            new,
            updated,
            downloaded,
            skipped,
            bytes_,
            reused,
        ) = r
        duration = ""
        if started and ended:
            d = (ended - started).total_seconds()
            duration = f"{d:.1f}s"
        elif started and status == "running":
            d = (datetime.utcnow() - started).total_seconds()
            duration = f"{d:.0f}s (running)"
        logs_html_rows.append(
            "<tr>"
            f"<td><code>{log_id}</code></td>"
            f"<td><code>{acc_id}</code></td>"
            f"<td>{_status_badge(status)}</td>"
            f"<td>{html.escape(_fmt_dt(started))}</td>"
            f"<td>{html.escape(duration)}</td>"
            f"<td>{seen}</td>"
            f"<td><b>{new}</b></td>"
            f"<td>{updated}</td>"
            f"<td>{downloaded}</td>"
            f"<td>{skipped}</td>"
            f"<td>{bytes_:,}</td>"
            f"<td>{'♻️' if reused else '🔑'}</td>"
            "</tr>"
        )

    queue_html = "".join(
        f'<span style="margin-right:16px;"><code>{html.escape(k)}</code>: '
        f"<b>{v}</b></span>"
        for k, v in queue_depths.items()
    )

    db_color = "#16a34a" if db_status == "ok" else "#dc2626"
    redis_color = "#16a34a" if redis_status == "ok" else "#dc2626"

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --bg: #0f172a; --fg: #f8fafc; --muted: #94a3b8; --card: #1e293b;
    --seam: #334155; --gold: #c4a962;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--fg); font-size: 13px;
  }}
  h1 {{ font-family: Georgia, serif; font-weight: 500; font-size: 20px;
       margin: 0 0 4px 0; }}
  h2 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px;
        color: var(--gold); font-weight: 600; margin: 24px 0 8px 0; }}
  .meta {{ color: var(--muted); font-size: 11px;
           text-transform: uppercase; letter-spacing: 0.8px; }}
  .card {{ background: var(--card); border: 1px solid var(--seam);
           border-radius: 4px; padding: 12px 16px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase;
        letter-spacing: 0.8px; color: var(--muted); font-weight: 600;
        padding: 8px 10px; border-bottom: 1px solid var(--seam); }}
  td {{ padding: 10px; border-bottom: 1px solid #1f2937; vertical-align: middle; }}
  tr:hover td {{ background: #1f2937; }}
  code {{ font-family: 'JetBrains Mono', ui-monospace, monospace;
          font-size: 12px; color: var(--gold); }}
  .empty {{ color: var(--muted); padding: 20px; text-align: center; font-style: italic; }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%;
          margin-right:6px; vertical-align: middle; }}
  .footer {{ color: var(--muted); font-size: 11px; margin-top: 24px; text-align: center; }}
</style>
</head>
<body>
<h1>SINOE scraper</h1>
<div class="meta">
  Local dashboard · auto-refresh 5s ·
  <span class="dot" style="background:{db_color}"></span>db {db_status} ·
  <span class="dot" style="background:{redis_color}"></span>redis {redis_status} ·
  workers: {settings.worker_concurrency} ·
  pool: {settings.browser_pool_size}
</div>

<h2>Queues</h2>
<div class="card">{queue_html if queue_html else '<span class="empty">sin datos</span>'}</div>

<h2>Cuentas ({len(accounts_rows)})</h2>
<div class="card">
{(
    '<table><thead><tr>'
    '<th>ID</th><th>Casilla</th><th>Alias</th><th>Activa</th>'
    '<th>Cada</th><th>Estado</th><th>Started</th><th>Completed</th>'
    '<th>Notifs</th><th>Falla</th><th>Req</th><th>Sesión</th>'
    '</tr></thead><tbody>'
    + ''.join(accounts_html_rows) +
    '</tbody></table>'
) if accounts_rows else '<div class="empty">No hay cuentas en SINOE_ACCOUNT</div>'}
</div>

<h2>Sync logs (últimos {len(logs_rows)})</h2>
<div class="card">
{(
    '<table><thead><tr>'
    '<th>ID</th><th>Cuenta</th><th>Estado</th><th>Started</th><th>Duración</th>'
    '<th>Vistas</th><th>Nuevas</th><th>Updated</th>'
    '<th>Anexos</th><th>Skip dedup</th><th>Bytes</th><th>Sesión</th>'
    '</tr></thead><tbody>'
    + ''.join(logs_html_rows) +
    '</tbody></table>'
) if logs_rows else '<div class="empty">Sin sync logs aún. Disparar uno seteando SINOE_ACCOUNT.sync_requested_at = NOW().</div>'}
</div>

<div class="footer">
  /health · /metrics · this is /
</div>
</body>
</html>"""
