# lolo-sinoe-scraping

Scraper Python para SINOE (Sistema de Notificaciones Electrónicas, Poder Judicial del Perú — `https://casillas.pj.gob.pe/sinoe/`).

**Estado:** sync end-to-end armado en código (login + iterate + download + S3 + DB). Pendiente corrida live contra SINOE real para validar selectores empíricamente.

## Stack

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (Chromium)
- CAPTCHA: [2Captcha](https://2captcha.com/) y/o [CapSolver](https://www.capsolver.com/) — al menos uno; si los dos están configurados se usa fallback chain
- SQLAlchemy 2.x + PyMySQL — conecta directo al `db_lolo` del backend
- boto3 — KMS (decrypt creds + encrypt session blob) + S3 (anexos)
- Redis (BRPOP queues internas)
- FastAPI/uvicorn — health/metrics local-only
- pydantic v2 + pydantic-settings, structlog, tenacity, typer
- uv como gestor de paquetes

## Modos de operación

| Modo | Cómo arranca | Lee creds de | Caso de uso |
|---|---|---|---|
| **Legacy CLI** (single-tenant) | `uv run lolo-sinoe login` | `.env` (`SINOE_CASILLA`, `SINOE_PASSWORD`) | Sesión interactiva del dev — explorar SINOE con su propia casilla, debuggear selectores. Sin BD ni KMS. |
| **Multitenant API server** | `uv run python -m lolo_sinoe.api` | `SINOE_ACCOUNT` en `db_lolo`, password descifrado via KMS | Lo que corre 24/7 en la laptop de operación. Procesa N cuentas del estudio. |

El validador de `Settings` exige los campos correctos según `SINOE_MULTITENANT_MODE`.

## Prerequisitos

```bash
brew install uv
uv sync
uv run playwright install chromium
```

## Configuración

```bash
cp .env.example .env
# editar .env
```

### Modo legacy / dev

```env
SINOE_CASILLA=12345
SINOE_PASSWORD=tu_password
SINOE_TWOCAPTCHA_API_KEY=...
# o SINOE_CAPSOLVER_API_KEY=...
```

### Modo multitenant

```env
SINOE_MULTITENANT_MODE=true
SINOE_DB_URL=mysql+pymysql://lolo_sinoe_scraper:***@<rds-host>:3306/db_lolo
SINOE_KMS_KEY_ID=arn:aws:kms:us-west-2:<account>:key/<id>
# para POC sin AWS, alternativamente:
# SINOE_KMS_FALLBACK_MASTER_KEY=<base64 32 bytes>
SINOE_AWS_REGION=us-west-2
SINOE_AWS_PROFILE=viktoria-prod
SINOE_REDIS_URL=redis://localhost:6379
SINOE_WORKER_CONCURRENCY=2
SINOE_BROWSER_POOL_SIZE=2
SINOE_TWOCAPTCHA_API_KEY=...
```

> ⚠️ `.env` está en `.gitignore`. **Nunca** commitear credenciales.

## Uso

### Legacy CLI

```bash
uv run lolo-sinoe login
uv run lolo-sinoe login --save-session ./session.json
uv run lolo-sinoe explore --max-pages 50 --max-depth 3
```

Exit codes: `0` ok, `2` LoginFailed, `3` CaptchaUnsolvable, `4` SinoeUnreachable, `5` UnexpectedPageState.

### Multitenant API server

```bash
docker compose up -d redis     # Redis local
uv run python -m lolo_sinoe.api
# expone:
#   GET http://127.0.0.1:8001/health   → estado db + redis
#   GET http://127.0.0.1:8001/metrics  → Prometheus text format
# arranca en background:
#   - WorkerManager (N=worker_concurrency) consumiendo sinoe:priority/monitor/initial
#   - Scheduler (cada 60s) → encola cuentas due en BD
```

> El módulo de entrada es `lolo_sinoe.api` (su `__main__.py` invoca `uvicorn.run`).
> `lolo_sinoe.api.server` sólo define `app` — invocarlo directo no arranca el server.

El backend no habla HTTP con el scraper (Plan §7.1). Coordina escribiendo `SINOE_ACCOUNT.sync_requested_at = NOW()` — el scheduler lo detecta en su próximo tick y mete la cuenta en `sinoe:priority`.

## Arquitectura del sync (multitenant)

```
Scheduler (cron 60s)
  │ find_due() → DueAccount[] (1 query, sin N+1)
  ▼
Redis queues (sinoe:priority / monitor / initial)
  │ BRPOP
  ▼
ScrapeWorker.process(job)
  │ 1. claim_for_sync (SELECT ... FOR UPDATE — race-safe)
  │ 2. get_s3_path_context (1 lookup)
  │ 3. SyncLogRepository.start (status=running)
  │ 4. KMS open_session (1 Decrypt para DEK)
  │ 5. RateLimiter.acquire (5 reqs/60s a SINOE)
  │ 6. launch_browser con cached_storage_state
  │ 7. ¿sesión viva? → reusar : full re-login + persist new blob
  ▼
SyncEngine.run
  │ enter_sinoe_module
  │ change_page_size(50)
  │ for page in paginator (DESC por fecha):
  │   list_notifications
  │   find_existing_n_notifs (1 SELECT batch)
  │   bulk_upsert (INSERT batch + UPDATE matching)
  │   bump_last_seen para las ya conocidas
  │   for each NUEVA leída:
  │     open_anexos_modal
  │     list_anexos
  │     existing_idents_for (skip ya descargados)
  │     for each anexo:
  │       click → expect_download → bytes
  │       compute_sha256 + pypdf metadata (firma + páginas)
  │       s3.upload_attachment (fuera de tx — lección CEJ)
  │       AttachmentRepository.create
  │     close_anexos_modal
  │   early-stop si ninguna nueva en la página
  ▼
SyncLogRepository.finish (status final + métricas)
AccountRepository.mark_sync_complete
```

## Restricciones operacionales

- **Sólo abrimos notificaciones que SINOE ya marcó como leídas** para no producir el side-effect "no leída → leída" en la casilla del usuario (constraint operacional 2026-04-30). Las no leídas se persisten desde el listado pero los anexos se bajan en el sync siguiente, una vez que el usuario las abrió en SINOE.
- **Rate limit 5 reqs/60s** global a `casillas.pj.gob.pe`. Compartido entre todos los workers.
- **Acciones de escritura sobre SINOE: cero.** No marcamos como leído, no presentamos escritos, no editamos perfil.

## Tests

```bash
uv run pytest                 # unit only (default)
LIVE_TESTS=1 uv run pytest    # incluye integration (DB staging) y SINOE real
uv run ruff check src/ tests/
uv run mypy src/
```

Para tests integration:

```env
SINOE_TEST_DB_URL=mysql+pymysql://...   # NUNCA prod — el conftest tira si detecta "prod" en la URL
SINOE_TEST_CHB_ID=<int>                  # CHB pre-existente en staging
SINOE_TEST_FALLBACK_KEY=<base64 32B>    # opcional — para KMS fallback
```

## Marco legal pendiente

La **Directiva 006-2015-CE-PJ Num. 7.2.2** califica las credenciales de SINOE como "personales e intransferibles". Operar este scraper con credenciales propias del operador en POC es defendible; multi-tenant con credenciales de clientes en producción **sigue siendo un bloqueador legal a resolver** antes de comercializar.
