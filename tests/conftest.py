"""Shared M0 test harness. The golden app + postgres are brought up by verify.sh (docker compose);
these fixtures compile the fixture Bundle and provide an in-memory StepEventSink test double (§4)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from anneal_core.bundle import Bundle
from anneal_core.compile import compile
from anneal_core.jsoncutil import loads
from anneal_core.stepplan import CompileContext

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_URL = os.getenv("ANNEAL_GOLDEN_URL", "http://localhost:8099")
RUN_TOKEN = "ab12cd34ef"          # ^[a-z0-9]{10}$


from _dbguard import assert_wipeable_db  # noqa: E402  (shared with test modules' _wipe)


@pytest.fixture(scope="session", autouse=True)
def _guard_db_target():
    """Runs once before any test in the session — the single chokepoint that stops a destructive suite
    from ever touching a non-disposable database."""
    assert_wipeable_db()
    yield


@pytest.fixture(scope="session", autouse=True)
def _ensure_chromium():
    """verify.sh's frozen bring-up installs deps but not browsers; ensure chromium once (idempotent)."""
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=False, capture_output=True, timeout=600)
    except Exception:
        pass


class CollectingSink:
    """The §4 StepEventSink test double — collects events in memory instead of inserting rows."""
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, run_result_id: str, event: dict) -> None:
        self.events.append(event)


@pytest.fixture
def collecting_sink() -> CollectingSink:
    return CollectingSink()


@pytest.fixture
def login_module() -> list[dict]:
    return json.loads((ROOT / "fixtures" / "modules" / "login.v4.json").read_text())["steps"]


@pytest.fixture
def compile_ctx(login_module) -> CompileContext:
    return CompileContext(token=RUN_TOKEN, today=date(2026, 7, 9), date_format="%Y-%m-%d",
                          params={}, modules={("login", 4): login_module})


@pytest.fixture
def bundle() -> Bundle:
    data = loads((ROOT / "fixtures" / "campaign-crud.bundle.jsonc").read_text())
    return Bundle.model_validate(data)


@pytest.fixture
def plan(bundle, compile_ctx):
    return compile(bundle, compile_ctx)
