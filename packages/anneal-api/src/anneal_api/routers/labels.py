"""Labels — the catalog's cross-cutting axis, and running one as a run.

A label is "a suite you don't have to maintain": suite membership is a hand-kept uuid array, so a test
authored today belongs to nothing until someone edits it. Labels make membership a query.

Two decisions live here rather than in the clients:

1. **The target is DERIVED, not asked for.** Every test already records its canonical environment
   (`test_version.verified_against`), so a label run resolves its own target. Putting that rule in the
   API — not in the console and again in the CLI — is what keeps thin clients from forking behaviour
   (D32). A label spanning two canonical targets is refused with the split, because whichever target
   were chosen, the rest would run read-only, unable to heal or re-author.
2. **Launch reuses the ONE launch path** — a `label:<name>` suite plus `create_and_launch`, exactly as
   the console's single-test run does. `create_suite` upserts on (project, name), so repeated runs of a
   label reuse one suite row, while each run's `run_task` rows still freeze that run's real membership.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from anneal_db import repositories as repo

from ..deps import Artifacts, Engine
from ..models import LabelCount, LaunchAccepted, Trigger
from ..suites_launch import TargetBusy, create_and_launch

router = APIRouter(tags=["labels"])


@router.get("/projects/{project}/labels", response_model=list[LabelCount])
def labels_list(project: str, eng: Engine):
    return repo.list_labels(eng, project_id=project)


@router.post("/projects/{project}/labels/{label}/runs", response_model=LaunchAccepted,
             status_code=202,
             responses={404: {"description": "no test carries this label"},
                        409: {"description": "target busy (active suite_run) — or the label spans "
                                             "several canonical targets; body carries the split"}})
def run_label(project: str, label: str, eng: Engine, art: Artifacts,
              target_id: str | None = Query(None), trigger: Trigger = Query("api")):
    label = label.strip().lower()
    members = repo.tests_with_label(eng, project_id=project, label=label)
    if not members:
        # Fail before a suite_run exists: an operator typo must not become a parked run they have to
        # decode. (The saved-suite EMPTY_SUITE park stays as-is — an empty *saved* suite must never
        # pass a deploy gate vacuously.)
        #
        # But distinguish WHY there is nothing to run: "no test carries this label" and "every test
        # carrying it is sidelined" are different operator problems, and reporting the second as the
        # first sends someone hunting for a typo that isn't there.
        held = repo.tests_with_label(eng, project_id=project, label=label, runnable_only=False)
        if held:
            raise HTTPException(status_code=404, detail=(
                f"nothing runnable carries label {label!r}: all {len(held)} test(s) that carry it are "
                f"quarantined or have no frozen version"))
        raise HTTPException(status_code=404, detail=f"no tests carry label {label!r}")

    if target_id is None:
        target_id = _derive_target(eng, project=project, label=label, members=members)

    suite = repo.create_suite(eng, project_id=project, name=f"label:{label}",
                              member_test_ids=[m["id"] for m in members])
    try:
        sr = create_and_launch(eng, art, project=project, suite_id=suite, target_id=target_id,
                               mode="saved", trigger=trigger)
    except TargetBusy as e:
        raise HTTPException(status_code=409, detail={
            "error": "target busy", "active_suite_run_id": e.active_suite_run_id}) from None
    return {"suite_run_id": sr}


def _derive_target(eng, *, project: str, label: str, members: list[dict]) -> str:
    """Resolve the members' canonical target keys to ONE target id, or refuse with the split."""
    by_key = {t["key"]: t for t in repo.list_targets(eng, project_id=project)}
    counts: dict[str | None, int] = {}
    for m in members:
        counts[m["verified_against"]] = counts.get(m["verified_against"], 0) + 1

    if len(counts) == 1:
        key = next(iter(counts))
        target = by_key.get(key)
        if target is not None:
            return str(target["id"])

    # Either the label spans environments, or its canonical target no longer exists in this project.
    # Both are the operator's call to make, with the counts in front of them.
    raise HTTPException(status_code=409, detail={
        "error": "label spans targets",
        "label": label,
        "split": [{"target_key": key,
                   "target_id": str(by_key[key]["id"]) if key in by_key else None,
                   "test_count": n}
                  for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))],
    })
