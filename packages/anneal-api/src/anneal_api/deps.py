"""Router dependencies — the engine + artifact store are created once in create_app and stashed on
app.state; routers resolve them per-request so the whole tree stays testable via create_app(engine=…).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request


def _engine(request: Request) -> Any:
    return request.app.state.engine


def _artifacts(request: Request) -> Any:
    return request.app.state.artifacts


Engine = Annotated[Any, Depends(_engine)]
Artifacts = Annotated[Any, Depends(_artifacts)]
