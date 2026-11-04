"""A0 acceptance gates (docs/BACKEND-SPEC.md §Wave A) — the typed API contract.

DONE for A0 == these pass: /openapi.json describes every endpoint with typed request/response
schemas (no untyped dict bodies), the legacy /video endpoint is gone, and live handlers validate
against the models on a real DB. Additive gate — strengthens verify.sh, weakens nothing.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app
from anneal_db.engine import get_engine

# The full API surface at A0. A new endpoint MUST be added here + modeled + API.md regenerated —
# this exact-match set is what keeps code, OpenAPI, and docs from drifting apart again.
EXPECTED_PATHS = {
    "/projects/{project}/suites/{suite_id}/runs": {"post"},
    "/projects/{project}/tests": {"get", "post"},           # post = author (M1); get = catalog (A2)
    "/projects/{project}/tests/{test_id}/run": {"post"},
    "/projects/{project}/runs/select": {"post"},
    "/projects/{project}/runs": {"get"},
    "/runs/{run_result_id}": {"get"},
    "/runs/{run_result_id}/steps": {"get"},
    "/runs/{run_result_id}/artifacts/{kind}": {"get"},          # A1 evidence serving
    "/runs/{run_result_id}/events": {"get"},                    # B1 SSE (run)
    "/suite_runs/{suite_run_id}/events": {"get"},               # B1 SSE (suite, multiplexed)
    "/projects/{project}/incidents": {"get"},
    "/incidents/{incident_id}": {"get"},
    "/incidents/{incident_id}/ruling": {"post"},
    "/projects/{project}/changelog": {"get"},
    "/export/{test_id}/{version}": {"get"},
    # A2 read-holes
    "/tests/{test_id}": {"get", "patch"},                   # patch = labels (catalog metadata)
    # labels: the catalog's cross-cutting axis + running one as a run (target derived from members)
    "/projects/{project}/labels": {"get"},
    "/projects/{project}/labels/{label}/runs": {"post"},
    "/tests/{test_id}/versions": {"get"},
    "/tests/{test_id}/versions/{version}": {"get"},
    "/tests/{test_id}/golden": {"get", "post", "delete"},       # B4 golden reference recording
    "/tests/{test_id}/golden/video": {"get"},
    "/projects/{project}/suites": {"get", "post"},          # post = A4 suite create
    "/suites/{suite_id}": {"get", "patch"},                 # patch = A4 membership/rename
    "/runs/{run_result_id}/pin": {"post"},                  # A4 reviewer pin/unpin
    "/suite_runs/{suite_run_id}": {"get"},
    "/projects/{project}/targets": {"get", "post"},         # post = onboard (auth/multi-platform §3.6)
    "/targets/{target_id}/territory": {"get"},
    "/projects/{project}/sessions": {"get"},
}
_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def spec():
    return create_app(engine=object(), artifacts=object()).openapi()


@pytest.mark.api_contract
def test_openapi_describes_exactly_the_api_surface(spec):
    got = {p: {m for m in ops if m in _METHODS} for p, ops in spec["paths"].items()}
    assert got == EXPECTED_PATHS, f"OpenAPI drifted from the expected surface:\n{got}"


@pytest.mark.api_contract
def test_every_operation_has_a_typed_response_schema(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in _METHODS:
                continue
            if "204" in op["responses"]:                        # 204 No Content has no body by design
                continue
            success = op["responses"].get("200") or op["responses"].get("202") or {}
            content = success.get("content", {})
            assert content, f"{method.upper()} {path} declares no success content at all"
            if set(content) == {"application/json"}:            # JSON-only ops must be model-typed
                schema = content["application/json"].get("schema", {})
                assert schema and schema != {}, f"{method.upper()} {path} has no typed success schema"


@pytest.mark.api_contract
def test_every_post_body_is_a_typed_model(spec):
    """No more untyped `dict = Body(...)` — every POST body is a named component schema (or a typed
    multipart form for a file upload, B4)."""
    for path, ops in spec["paths"].items():
        body = ops.get("post", {}).get("requestBody")
        if body is None:
            continue  # launch/run take query params only
        content = body["content"]
        if "multipart/form-data" in content:               # B4 golden upload — typed form schema
            assert content["multipart/form-data"].get("schema"), f"POST {path} multipart has no schema"
            continue
        schema = content["application/json"]["schema"]
        ref = schema.get("$ref", "") or str(schema.get("anyOf", ""))
        assert "#/components/schemas/" in ref, f"POST {path} body is not a named typed schema"


@pytest.mark.api_contract
def test_legacy_video_endpoint_removed(spec):
    # the legacy run-video route specifically — the golden's /tests/{id}/golden/video (B4) is fine
    assert "/runs/{run_result_id}/video" not in spec["paths"], "legacy /runs/{id}/video must be gone (A0)"
    assert not any(p.endswith("/runs/{run_result_id}/video") for p in spec["paths"])


# ── live smoke on a real DB: handlers actually validate against the models ──────────────────────
@pytest.mark.api_contract
def test_malformed_id_is_404_not_500():
    """A non-UUID path id is a DB cast error (sqlalchemy DataError); the API answers 404, never a 500
    with a psycopg traceback — one app-level handler covers every id-taking route."""
    app = create_app(engine=get_engine())
    with TestClient(app) as tc:
        for path in ("/runs/not-a-uuid", "/suite_runs/abc", "/tests/nope", "/incidents/x"):
            r = tc.get(path)
            assert r.status_code == 404, (path, r.status_code, r.text[:200])
            assert r.json() == {"detail": "not found"}


@pytest.mark.api_contract
def test_handlers_validate_against_models_live():
    app = create_app(engine=get_engine())
    with TestClient(app) as tc:
        assert isinstance(tc.get("/projects/golden/runs").json(), list)          # RunListItem[]
        assert isinstance(tc.get("/projects/golden/incidents").json(), list)     # Incident[]
        assert isinstance(tc.get("/projects/golden/changelog").json(), list)     # ChangelogEntry[]
        assert tc.get(f"/runs/{uuid.uuid4()}").status_code == 404
        assert tc.get(f"/runs/{uuid.uuid4()}/video").status_code == 404          # route truly gone
        # typed body validation: bad ruling value is a schema 422, not a handler branch
        r = tc.post(f"/incidents/{uuid.uuid4()}/ruling", json={"ruling": "maybe"})
        assert r.status_code == 422
        # transitional fallback console still serves / until frontend/dist is deployed
        assert "Anneal" in tc.get("/").text
