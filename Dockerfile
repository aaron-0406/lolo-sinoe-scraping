# Mirror conceptual del Dockerfile de lolo-cej-scraping (TS+Puppeteer),
# adaptado a Python + Playwright. Misma estrategia: imagen base oficial
# con Chromium ya preinstalado (mcr.microsoft.com/playwright/python).
#
# En POC el scraper corre nativo en la laptop del operador (Plan §7).
# Este Dockerfile sirve para:
# 1. docker-compose local (Redis junto al scraper, opcional).
# 2. Migración futura a EC2 (Plan §7.3) — entonces se usa en serio.

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Instalar uv para gestionar deps Python
RUN pip install --no-cache-dir uv==0.5.13

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/

# Bind solo a localhost — en EC2 cambiar a 0.0.0.0 + ALB delante
EXPOSE 8001

CMD ["uv", "run", "python", "-m", "lolo_sinoe.api"]
