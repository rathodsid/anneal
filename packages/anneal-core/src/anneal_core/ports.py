"""The seams (Protocols) the executor reaches through — how D31's zero-LLM cold lane is auditable.

`Interpreter.cold(...)` takes no Healer/Author (§1.1); `.warm`/`.hot` receive concrete, LLM-backed
implementations *injected* from anneal-heal / anneal-authoring. Because the executor depends only on
these Protocols — never on anneal-llm — an accidental `import anneal_llm` in the cold path fails at
import time in CI (verify.sh M0), not at runtime in production.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """D34 — local-disk in v1. Stores trace/video/screenshot/network artifacts, returns a ref."""

    def put(self, run_id: str, name: str, data: bytes) -> str: ...

    def path_for(self, run_id: str, name: str) -> str: ...


@runtime_checkable
class StepEventSink(Protocol):
    """D33 — one step_event row per completed step. DB-backed impl inserts; a test double collects.
    A sink MAY also implement `emit_started(run_result_id, event)` (B2 live grain: a 'running' row at
    step START) — the interpreter duck-type-checks for it, so collectors that only want completed
    events simply don't define it."""

    def emit(self, run_result_id: str, event: dict[str, Any]) -> None: ...


@runtime_checkable
class Healer(Protocol):
    """WARM lane (M3). Ladder-first heal from captured fallbacks; LLM-over-DOM only if the ladder fails."""

    async def heal(self, step: Any, page: Any) -> Any: ...


@runtime_checkable
class Author(Protocol):
    """HOT lane (M2). Drives the perceive→decide→act loop that authors a Bundle from NL."""

    async def act(self, page: Any, context: Any) -> Any: ...


@runtime_checkable
class LlmClient(Protocol):
    """A15+B6 seam. Provider fallback lives behind this; anneal-executor never sees it."""

    def complete(self, system: str, user: str, **kw: Any) -> str: ...
