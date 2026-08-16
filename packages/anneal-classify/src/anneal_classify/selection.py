"""Change-driven test selection (§11). Given an NL change description, score every test
and explain the pick.

A11's shape, held exactly: candidate surfaces are drawn from the OBSERVED inventory only (closed
vocabulary — a change can only implicate surfaces the suite actually touches, never a hallucinated
one); the score is **semantic 0.6 + IDF-structural 0.4**; the **0.45** threshold selects; unmapped
tests are included cold-start; every pick carries a human-readable reason.

DETERMINISTIC (no LLM in the gate path): the "semantic" component is lexical overlap between the change
and each test's coverage descriptor, and the "structural" component is IDF-weighted surface overlap.
An embedding/LLM semantic scorer is a drop-in for the `_semantic` function — a signal provider that
extends the same pipe (§11), it does not replace it. Target-agnostic: surfaces + descriptors come from
the tests' own bundles, never from hardcoded routes.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urlsplit

from anneal_db import repositories as repo

_THRESHOLD = 0.45
_W_SEMANTIC, _W_STRUCTURAL = 0.6, 0.4
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "then", "that", "this",
         "is", "are", "be", "it", "its", "as", "at", "by", "from", "into", "new", "now", "any", "add",
         "open", "verify", "confirm", "check", "page", "list", "form", "test", "flow", "app", "when"}
_STRUCTURAL = {"api", "v1", "v2", "v3", "app", "www", "id"}


def _toks(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _surfaces(bundle: dict) -> set[str]:
    """The routes/endpoints a test touches — goto routes + network paths — id-templated, so selection
    is stable across per-entity ids (the surface inventory, §11)."""
    out: set[str] = set()

    def _norm(path: str) -> str:
        # id-like segments → {id}; a version segment (v1/v2, one digit) is NOT an id and survives
        segs = ["{id}" if re.fullmatch(r"\d+|[a-f0-9-]{8,}|[a-z]+\d{3,}|[a-z0-9]{12,}|\{[a-z_]+\}", s or "", re.I)
                else s for s in (path or "").rstrip("/").split("/")]
        return "/".join(segs)

    for s in bundle.get("steps", []):
        if s.get("action") == "goto" and s.get("arg"):
            out.add(_norm(urlsplit(s["arg"]).path or s["arg"]))
        for cond in ((s.get("postcondition") or {}).get("all") or []):
            if cond.get("kind") == "network" and cond.get("path"):
                out.add(_norm(cond["path"]))
            if cond.get("kind") == "url" and cond.get("match"):
                raw = cond["match"].replace(".*", "").replace("[^/]+", "{id}")
                raw = re.sub(r"[\\^$]", "", raw)
                out.add(_norm(urlsplit(raw).path or raw))
    return {s for s in out if s}


def _surface_words(surface: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", surface.lower())
            if len(w) >= 3 and w not in _STRUCTURAL and not re.fullmatch(r"\{?[a-z_]*id\}?|\d+", w)}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))            # overlap coefficient — forgiving for short text


def select(engine, *, project_id, change_nl: str) -> list[dict]:
    """Return every test with its selection score, decision, and human-readable reason (highest first)."""
    tests = repo.project_tests_with_head_bundle(engine, project_id=project_id)
    change_toks = _toks(change_nl)

    per, df = [], Counter()
    for t in tests:
        surfs = _surfaces(t["bundle"])
        intent = (t["bundle"].get("intent") or {})
        desc = " ".join([intent.get("descriptor", "")] +
                        [s.get("verbatim", "") for s in intent.get("sources", [])])
        per.append({"test_id": t["test_id"], "test_key": t["test_key"], "surfaces": surfs,
                    "desc_toks": _toks(desc)})
        for s in surfs:
            df[s] += 1

    n = max(1, len(per))
    idf = {s: math.log(1 + n / c) for s, c in df.items()}
    # closed vocabulary: only surfaces that actually exist can be implicated by the change
    candidates = {s for s in df if _surface_words(s) & change_toks}
    cand_mass = sum(idf[s] for s in candidates) or 1.0

    picks = []
    for t in per:
        semantic = _overlap(change_toks, t["desc_toks"])
        shared = t["surfaces"] & candidates
        structural = sum(idf[s] for s in shared) / cand_mass if candidates else 0.0
        score = _W_SEMANTIC * semantic + _W_STRUCTURAL * min(1.0, structural)
        cold = len(t["surfaces"]) == 0
        selected = cold or score >= _THRESHOLD
        picks.append({"test_id": t["test_id"], "test_key": t["test_key"], "score": round(score, 3),
                      "selected": selected, "cold_start": cold,
                      "reason": _reason(cold, selected, score, change_toks & t["desc_toks"], shared)})
    picks.sort(key=lambda p: (p["selected"], p["score"]), reverse=True)
    return picks


def _reason(cold, selected, score, shared_terms, shared_surfaces) -> str:
    if cold:
        return "unmapped test (no observed surfaces) — included cold-start as a safety net (§11)"
    bits = []
    if shared_terms:
        bits.append("coverage mentions " + ", ".join(sorted(shared_terms))[:80])
    if shared_surfaces:
        bits.append("touches changed surface(s) " + ", ".join(sorted(shared_surfaces))[:80])
    why = "; ".join(bits) or "no term or surface overlap with the change"
    verb = "selected" if selected else "skipped"
    return f"{verb} (score {score:.2f}): {why}"
