# lolo-sinoe-scraping

Login + exploration tool for SINOE (Sistema de Notificaciones Electrónicas, Poder Judicial del Perú — `https://casillas.pj.gob.pe/sinoe/`).

**Estado:** POC inicial (login + crawler read-only) — no apto para producción.

## Stack

- Python 3.11+
- [Playwright](https://playwright.dev/python/) (Chromium)
- CAPTCHA: [2Captcha](https://2captcha.com/) y/o [CapSolver](https://www.capsolver.com/) — al menos uno; si los dos están configurados se usa fallback chain
- Pydantic v2 + pydantic-settings
- structlog + tenacity + typer
- uv como gestor de paquetes

## Prerequisitos

```bash
brew install uv          # si no lo tenés
uv sync                  # instala deps + crea venv
uv run playwright install chromium
```

## Configuración

Copiar `.env.example` a `.env` y completar:

```bash
cp .env.example .env
# editar .env con casilla, password, y al menos una API key de CAPTCHA
```

> ⚠️ `.env` está en `.gitignore`. **Nunca** commitear credenciales.

### CAPTCHA: 2Captcha vs CapSolver

El scraper acepta cualquiera de los dos proveedores (mismo patrón que `lolo-cej-scraping`):

- **`SINOE_TWOCAPTCHA_API_KEY`**: usar 2Captcha (puede ser la misma key del proyecto cej-scraping).
- **`SINOE_CAPSOLVER_API_KEY`**: usar CapSolver.
- **Si los dos están configurados**, el scraper arma una `FallbackSolver` chain: prueba el primero del orden definido en `SINOE_CAPTCHA_PROVIDER_ORDER` (default `2captcha,capsolver`) y, si falla, prueba el siguiente.
- **Al menos uno** debe estar configurado o el config validator levanta un error al arrancar.

## Uso

### Login

```bash
uv run lolo-sinoe login
uv run lolo-sinoe login --save-session ./session.json
uv run lolo-sinoe login --headless
```

Exit codes: `0` ok, `2` LoginFailed, `3` CaptchaUnsolvable, `4` SinoeUnreachable, `5` UnexpectedPageState.

### Exploración (login + crawl read-only)

```bash
uv run lolo-sinoe explore --max-pages 50 --max-depth 3
# output en exploration_output/ (gitignored)
# reporte: exploration_output/REPORT.md
```

El crawler:
- Solo navega vía `page.goto(url)`. Nunca hace clicks en submits.
- Filtra texto destructivo en links ("enviar", "presentar", "marcar como leído", etc.).
- Whitelist de hosts: solo `casillas.pj.gob.pe`.
- Throttle ≥ 2s entre navegaciones.
- Caps absolutos: `max_pages` y `max_depth`.

### Capturar el HTML del login para verificar selectores

```bash
uv run python scripts/capture_login_html.py
# escribe tests/fixtures/login_page.html y .png
```

## Tests

```bash
uv run pytest                 # solo unit (default)
LIVE_TESTS=1 uv run pytest    # incluye tests que pegan a SINOE real
uv run ruff check .
uv run mypy src/
```

## Constraint operacional importante

El crawler de exploración **solo abre notificaciones ya marcadas como leídas**. Las no leídas se observan únicamente desde el listado, sin abrirlas, para no producir el side-effect de pasar el estado a "leído" en SINOE.

## Out of scope (no implementado en esta iteración)

- Scraping productivo de bandeja con persistencia.
- Descarga real de cédulas y anexos.
- Modelos DB en `lolo-backend`.
- Endpoints REST.
- Queue / scheduler.
- Re-login automático al expirar sesión.
- Multi-tenant + KMS.
- Cómputo de plazos legales (Art. 155-C + TC).
- Métricas/Grafana, Docker, CI live.

Cada uno irá en un plan separado en `investigacion/PLANES DE IMPLEMENTACION/`.

## Marco legal pendiente

La **Directiva 006-2015-CE-PJ Num. 7.2.2** califica las credenciales de SINOE como "personales e intransferibles". Operar este scraper con credenciales propias del operador (Aaron) en un POC es defendible; multi-tenant con credenciales de clientes en producción **sigue siendo un bloqueador legal a resolver** antes de comercializar.
