"""Regression for the content-negotiated SPA shell (app.py `spa_shell` middleware).

The console is a history-mode SPA whose deep links (`/runs/{id}`, …) collide with real JSON
API paths on the ONE shared namespace (D32). The middleware serves the HTML shell only to a
browser navigation and falls through to the API for XHR. The trap this guards: the shell is one
representation of a URL that ALSO serves JSON, so it MUST be uncacheable + declare its Vary axis —
otherwise the browser HTTP cache replays the cached shell for the app's later same-URL JSON fetch,
producing `Couldn't load run: SyntaxError: Unexpected token '<', "<!doctype "...`.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    monkeypatch.setenv("ANNEAL_FRONTEND_DIST", str(dist))
    app = create_app(engine=object(), artifacts=object())
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.api_contract
def test_nav_shell_is_uncacheable_and_declares_vary(client):
    # A top-level browser navigation to a client route that also serves JSON.
    r = client.get("/runs/some-id", headers={"sec-fetch-mode": "navigate",
                                             "accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The fix: never let a shared cache store this shell (else it cross-serves the JSON fetch)…
    assert r.headers.get("cache-control") == "no-store"
    # …and, for any cache that does store it, key it on the negotiation headers.
    assert "sec-fetch-mode" in r.headers.get("vary", "").lower()


@pytest.mark.api_contract
def test_xhr_to_colliding_url_is_not_served_the_shell(client):
    # openapi-fetch sends no Accept (browser default */*) and Sec-Fetch-Mode: cors — must fall
    # through to the API, never the HTML shell.
    r = client.get("/runs/some-id", headers={"sec-fetch-mode": "cors", "accept": "*/*"})
    assert not r.headers["content-type"].startswith("text/html")
