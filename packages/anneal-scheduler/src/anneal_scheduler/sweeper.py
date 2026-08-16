"""Suite-end sweeper (§10.2, A16, D15) — DETERMINISTIC, no LLM. Two paths, per HLD §8/§10.2:

  1. PRIMARY — replay templated DELETEs against the ids the create responses RETURNED (recorded in the
     run's own network log at create time). These ids belong to entities THIS run created, so the D15
     guard holds by provenance — nothing outside the run is touched.
  2. FALLBACK (A16 janitor) — where no create-id was observed, GET the mutating collections derived
     from the run's own network log and DELETE exactly the entities whose serialized identity carries
     one of this suite run's tokens. One delete per pass, re-scan to verify, bounded passes.

Both derive endpoints from the run's OWN traffic (never a hardcoded route — target-agnostic).

The deletes replay through an AUTHENTICATED browser context (the run's seeded storage_state), using
its APIRequestContext (`ctx.request`) so the session cookies — and hence the workspace/tenant scoping
the app derives from them — are attached exactly as the app itself sends them. A bare unauthenticated
client cannot operate on a workspace-scoped, auth-gated app (it 401s / "no workspace"), which is why a
naked HTTP client left live residue. When there is no session (an open app like the golden fixture),
the context simply carries no cookies and behaves like a plain client.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from anneal_db import repositories as repo

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# an id-like trailing segment (pure digits, uuid, alpha+3digits, long alnum) — a version like `v2` is NOT
_ID_SEG = re.compile(r"^(\d+|[a-f0-9][a-f0-9-]{7,}|[a-z]+\d{3,}|[a-z0-9]{12,})$", re.I)


def _collection_paths(network: list[dict]) -> list[str]:
    """Mutation-target collections from the network log — e.g. POST /api/<res> → /api/<res>; a trailing
    id segment is dropped (DELETE /api/<res>/<id> → /api/<res>). Derived from the run's own traffic."""
    paths: set[str] = set()
    for e in network:
        if (e.get("method") or "").upper() not in _MUTATING:
            continue
        segs = urlsplit(e.get("url") or "").path.rstrip("/").split("/")
        if segs and _ID_SEG.match(segs[-1]):
            segs = segs[:-1]
        coll = "/".join(segs)
        if coll:
            paths.add(coll)
    return sorted(paths)


def _entities(data):
    """Yield (id, entity) for the two common collection shapes: {id: {...}} or [ {id|_id: …} ]."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield k, v
    elif isinstance(data, list):
        for v in data:
            if isinstance(v, dict):
                yield v.get("id") or v.get("_id"), v


async def sweep_suite_run(engine, *, suite_run_id, target, browser, storage_state=None,
                          max_passes=100) -> int:
    """Remove every residue entity this suite run created/mutated. Returns the count swept.

    `browser` is the suite's already-open Playwright browser; `storage_state` is the run's seeded
    session (D14) so the replayed deletes carry the app's own auth/workspace scoping. Both paths are
    deterministic (no LLM) and derive every endpoint from the run's own network log."""
    tokens = [t for t in repo.suite_run_tokens(engine, suite_run_id=suite_run_id) if t]
    if not tokens:
        return 0
    network = repo.suite_run_network(engine, suite_run_id=suite_run_id)
    swept, done = 0, set()

    ctx = await browser.new_context(base_url=target["base_url"], storage_state=storage_state)
    try:
        req = ctx.request                               # APIRequestContext — carries the context cookies
        # 1. PRIMARY — DELETE the recorded create-response ids (this run's entities, D15 by provenance)
        for e in network:
            cid = e.get("created_id")
            if not cid:
                continue
            coll = urlsplit(e.get("url") or "").path.rstrip("/")   # the create COLLECTION path
            key = (coll, str(cid))
            if not coll or key in done:
                continue
            done.add(key)
            try:
                if (await req.delete(f"{coll}/{cid}")).status < 300:
                    swept += 1
            except Exception:
                pass

        # 2. FALLBACK (A16 janitor) — token-scan the mutating collections for anything the primary missed
        for path in _collection_paths(network):
            for _ in range(max_passes):                 # one delete per pass, re-scan to verify
                try:
                    r = await req.get(path)
                    if r.status >= 400:
                        break
                    data = await r.json()
                except Exception:
                    break
                victim = next((eid for eid, ent in _entities(data)
                               if eid is not None and (path, str(eid)) not in done
                               and any(tok in json.dumps(ent) for tok in tokens)), None)
                if victim is None:
                    break                                # collection clean of this run's residue
                done.add((path, str(victim)))
                try:
                    if (await req.delete(f"{path}/{victim}")).status < 300:
                        swept += 1
                except Exception:
                    break
    finally:
        await ctx.close()
    return swept
