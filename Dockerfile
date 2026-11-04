# Anneal — production image for the API (which also hosts the in-process scheduler +
# Playwright browser workers; see ARCHITECTURE.md §2). One deployable.
#
# Build:  docker build -t anneal-api .
# Run:    docker run -p 8080:8080 \
#           -e ANNEAL_DB_URL=postgresql+psycopg://user:pass@host:5432/anneal \
#           -e ANNEAL_VAULT_KEY=<fernet-key> \
#           -e ANTHROPIC_API_KEY=<...> \
#           -v anneal-artifacts:/var/lib/anneal/artifacts \
#           anneal-api
# Migrations are NOT run on boot by default (ARCHITECTURE.md §7) — run once per deploy:
#           docker run --rm -e ANNEAL_DB_URL=... anneal-api uv run alembic upgrade head
# Or, for a single-pod deploy, set ANNEAL_MIGRATE_ON_BOOT=1 to migrate before the server starts.

# ── Stage 1: build the frontend (same-origin: VITE_API_BASE left unset → "") ──
FROM node:20-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci
COPY frontend/ ./frontend/
RUN cd frontend && npm run build   # emits /app/frontend/dist

# ── Stage 2: python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# uv, the workspace manager (pin a version in your own pipeline for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependency + workspace sources. --no-dev drops the pytest harness only; playwright,
# alembic, psycopg and cryptography are real package deps and are kept.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --no-dev

# Chromium + its OS libraries, matching the locked Playwright version.
RUN uv run playwright install --with-deps chromium

# Runtime assets: migrations (for the one-shot migrate command), schemas, built SPA.
COPY alembic.ini ./
COPY migrations ./migrations
COPY schemas ./schemas
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh
COPY --from=frontend /app/frontend/dist ./frontend/dist

ENV ANNEAL_FRONTEND_DIST=/app/frontend/dist \
    ANNEAL_ARTIFACTS=/var/lib/anneal/artifacts
RUN mkdir -p /var/lib/anneal/artifacts

EXPOSE 8080
# Entrypoint optionally runs migrations first (ANNEAL_MIGRATE_ON_BOOT=1), then execs this CMD.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "anneal_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
