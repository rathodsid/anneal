"""B4 acceptance gates (docs/BACKEND-SPEC.md §Wave B) — the golden reference recording.

DONE for B4 == a per-test golden video is auto-seeded at freeze from a CLEAN cold replay of the frozen
bundle (source='auto'); a reviewer upload OVERRIDES it (source='manual'); the bytes serve as video/webm
and metadata reads back; delete removes it. The manual/endpoint layer is proven with a synthetic clip;
the auto-seed is proven for real via record_golden on the fixture bundle (a replay — no LLM authoring).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from anneal_api.app import create_app
from anneal_db import repositories as repo
from anneal_db.engine import get_engine

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_URL = "http://localhost:8099"
WEBM = b"\x1aE\xdf\xa3 fake-webm-bytes for the golden upload round-trip"


@pytest.mark.api_contract
def test_manual_upload_serve_replace_delete():
    engine = get_engine()
    key = f"b4-manual-{uuid.uuid4().hex[:6]}"
    test_id, _ = repo.upsert_test(engine, project_id="golden", test_key=key,
                                  bundle={"persona": "member"}, verified_against=key)
    with TestClient(create_app(engine=engine)) as tc:
        assert tc.get(f"/tests/{test_id}/golden").status_code == 404          # none yet
        assert tc.get(f"/tests/{test_id}/golden/video").status_code == 404

        up = tc.post(f"/tests/{test_id}/golden",
                     files={"file": ("ref.webm", WEBM, "video/webm")})
        assert up.status_code == 200, up.text
        assert up.json()["source"] == "manual" and up.json()["reflects_version"] == 1

        meta = tc.get(f"/tests/{test_id}/golden").json()
        assert meta["source"] == "manual" and meta["test_id"] == str(test_id)
        vid = tc.get(f"/tests/{test_id}/golden/video")
        assert vid.status_code == 200 and vid.content == WEBM
        assert vid.headers["content-type"] == "video/webm"

        # replace overwrites (still one row, still manual)
        tc.post(f"/tests/{test_id}/golden", files={"file": ("ref2.webm", WEBM + b"2", "video/webm")})
        assert tc.get(f"/tests/{test_id}/golden/video").content == WEBM + b"2"

        assert tc.delete(f"/tests/{test_id}/golden").status_code == 204
        assert tc.get(f"/tests/{test_id}/golden").status_code == 404
        assert tc.delete(f"/tests/{test_id}/golden").status_code == 404       # idempotent-ish


@pytest.mark.api_contract
async def test_auto_seed_from_clean_replay(tmp_path):
    """The real auto-seed path: record_golden does a clean cold replay of the frozen fixture bundle
    against the golden app and stores a playable video in the golden slot (source='auto'). No LLM."""
    from anneal_artifacts.store import LocalDiskStore
    from anneal_authoring.service import record_golden
    from anneal_core.jsoncutil import loads as jsonc_loads

    engine = get_engine()
    artifacts = LocalDiskStore(str(tmp_path / "artifacts"))
    key = f"b4-auto-{uuid.uuid4().hex[:6]}"
    target_id = repo.seed_project_target(engine, project_id="golden", project_name="Golden",
                                         target_key=key, base_url=GOLDEN_URL,
                                         profile={"probe_path": "/campaigns"})
    login = json.loads((ROOT / "fixtures" / "modules" / "login.v4.json").read_text())
    repo.upsert_module(engine, project_id="golden", ref="login", version=4, steps=login["steps"])
    bundle = jsonc_loads((ROOT / "fixtures" / "campaign-crud.bundle.jsonc").read_text())
    test_id, version = repo.upsert_test(engine, project_id="golden", test_key=key, bundle=bundle,
                                        verified_against=key)
    target = repo.get_target(engine, target_id=target_id)
    storage_state = repo.get_storage_state(engine, project_id="golden", persona=bundle["persona"])

    ok = await record_golden(engine, artifacts, test_id=test_id, project_id="golden", target=target,
                             bundle=bundle, version=version, start_path="/campaigns",
                             storage_state=storage_state)
    assert ok, "clean-replay golden seed did not produce a video"

    g = repo.get_golden(engine, test_id=test_id)
    assert g["source"] == "auto" and g["reflects_version"] == version
    assert Path(g["video_path"]).is_file() and Path(g["video_path"]).stat().st_size > 0

    with TestClient(create_app(engine=engine, artifacts=artifacts)) as tc:
        assert tc.get(f"/tests/{test_id}/golden").json()["source"] == "auto"
        assert tc.get(f"/tests/{test_id}/golden/video").headers["content-type"] == "video/webm"
