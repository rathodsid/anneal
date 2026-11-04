"""The ONE API (D32) — FastAPI. `anneal-cli` and the console are thin clients that must not fork
behavior. A0 (docs/BACKEND-SPEC.md): typed Pydantic contracts (models.py), APIRouters (routers/),
OpenAPI for the generated frontend client, CORS via env, and the built frontend served same-origin.
Legacy `GET /runs/{id}/video` is removed (A1's /artifacts/{kind} is the only serve path).
"""
from __future__ import annotations

import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import DataError

from anneal_artifacts.store import LocalDiskStore
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

from .console import CONSOLE_HTML
from .routers import ALL as ALL_ROUTERS


def create_app(engine=None, artifacts=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # B1 startup reaper: an API restart mid-run leaves suite_runs stuck 'running' (the run
        # thread died with the process). Terminal-ize them as INTERRUPTED — distinct from ENV_*
        # (the env wasn't down; the server bounced) — which also releases the per-target launch lock.
        try:
            n = repo.reap_interrupted(app.state.engine)
            if n:
                print(f"[anneal] startup reaper: {n} interrupted suite_run(s) parked")
        except Exception:  # noqa: BLE001 — schema-less dummy engine (openapi dump) or DB down
            traceback.print_exc()
        yield

    app = FastAPI(title="Anneal", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine or get_engine()
    app.state.artifacts = artifacts or LocalDiskStore(os.getenv("ANNEAL_ARTIFACTS", "./.anneal/artifacts"))

    # Dev CORS: comma-separated origin allowlist (e.g. the Vite dev server). Prod needs none —
    # the built frontend is served same-origin below.
    origins = [o.strip() for o in os.getenv("ANNEAL_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["*"], allow_headers=["*"])

    for r in ALL_ROUTERS:
        app.include_router(r)

    # A malformed path id (GET /runs/not-a-uuid) reaches the DB as a cast error. That is "no such
    # thing", not a server fault — one handler, instead of typing every path param.
    @app.exception_handler(DataError)
    async def _malformed_id_is_404(request: Request, exc: DataError):
        return JSONResponse({"detail": "not found"}, status_code=404)

    # Health/readiness probe for orchestrators (k8s liveness/readiness, load balancers). Kept OUT of
    # the OpenAPI contract (include_in_schema=False) — it is an operational endpoint, not part of the
    # typed A0 surface. Readiness = the DB is reachable (a bounced DB → 503 so the LB drains this pod);
    # a process that can answer at all is live. Registered before the "/" static mount so it wins.
    @app.get("/health", include_in_schema=False)
    def health(response: Response):
        try:
            with app.state.engine.connect() as conn:
                conn.execute(text("select 1"))
            return {"status": "ok", "db": "ok"}
        except Exception:  # noqa: BLE001 — any DB failure is "not ready to serve"
            response.status_code = 503
            return {"status": "degraded", "db": "down"}

    # Frontend: prod opts in EXPLICITLY via ANNEAL_FRONTEND_DIST=frontend/dist (same-origin, one
    # deployable). No auto-detection — serving must not depend on what happens to be on disk (the m1
    # console gate runs from repo root). Unset → the v0 console keeps `/` alive as the transitional page.
    dist = os.getenv("ANNEAL_FRONTEND_DIST")
    if dist and Path(dist).is_dir():
        index_file = Path(dist) / "index.html"
        # The console is a history-mode SPA: deep links like /runs, /runs/{id}, /tests/{id} are client
        # routes, not files on disk — and some (GET /runs/{id}) collide with real API paths that serve
        # JSON. So the path alone can't decide what to return; the request's *intent* does. A top-level
        # browser navigation (Sec-Fetch-Mode: navigate, which the browser sets and a URL can't fake;
        # Accept: text/html for the rare client that omits it) must get the SPA shell so react-router
        # can take over. XHR/asset fetches to the same URLs fall through to the API / StaticFiles.
        # Server-owned browsable endpoints stay reachable by direct navigation.
        _SERVER_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")

        @app.middleware("http")
        async def spa_shell(request: Request, call_next):
            if request.method in ("GET", "HEAD") and not request.url.path.startswith(_SERVER_PATHS):
                nav = request.headers.get("sec-fetch-mode") == "navigate" or (
                    request.headers.get("sec-fetch-mode") is None
                    and "text/html" in request.headers.get("accept", "")
                )
                if nav:
                    # One URL, two representations chosen by request header (shell vs the JSON
                    # that GET /runs/{id} etc. serve to XHR). FileResponse auto-tags the shell
                    # with Last-Modified but no freshness directive, so a cache (the browser's
                    # own HTTP cache — RFC 9111 §4.2.2 heuristic freshness — or any CDN rule)
                    # may store this HTML and, lacking a Vary axis, replay it for the app's
                    # later same-URL JSON fetch → "Unexpected token '<'". Forbid caching the
                    # shell and declare the header it varies on. (The JSON side needs nothing:
                    # it carries no Last-Modified, so it is never heuristically cacheable.)
                    return FileResponse(
                        index_file, media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Vary": "Sec-Fetch-Mode, Accept"},
                    )
            return await call_next(request)

        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def console():
            return CONSOLE_HTML

    return app


app = create_app()
