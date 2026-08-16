"""M0 audit (marker: m0_audit) — the target-agnostic grep + vendored-import checks.

A17: the platform engine (anneal-core + anneal-executor) carries ZERO target-specific paths. Target
facts live only in TargetProfile rows and TerritoryNotes (D25); the fixture is test data, excluded.
Also asserts the vendored COPY files import cleanly and that importing the executor never pulls in
anneal-llm (D31 — belt-and-suspenders to verify.sh's structural check).
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# target-specific tokens that must never appear in ENGINE code (core + executor src).
# NOT generic words; these are app/target facts the golden app or a live target owns.
TOKENS = [
    "campaign-v2", "campaigns-table",
    "btn-add-campaign", "select-campaign-agent", "8099", "/api/v2",
]

ENGINE_DIRS = [
    ROOT / "packages" / "anneal-core" / "src",
    ROOT / "packages" / "anneal-executor" / "src",
]


def _clear_pycache(root: Path) -> None:
    for pc in root.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)


@pytest.mark.m0_audit
def test_engine_has_no_target_specific_paths():
    pattern = re.compile("|".join(re.escape(t) for t in TOKENS), re.IGNORECASE)
    hits: list[str] = []
    for root in ENGINE_DIRS:
        _clear_pycache(root)
        for py in root.rglob("*.py"):                      # text only — never .pyc
            for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{py.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "target-specific tokens found in engine code:\n" + "\n".join(hits)


@pytest.mark.m0_audit
def test_vendored_copy_files_import_cleanly():
    # A1 → resolver; A8 → actions/export; A15 → llm seam; core compiler.
    from anneal_core import compile as _compile          # noqa: F401
    from anneal_executor import actions, export, resolve
    from anneal_llm import client as _llm                 # noqa: F401

    assert hasattr(resolve, "resolve") and hasattr(resolve, "wait_hidden")
    assert hasattr(actions, "robust_click") and hasattr(actions, "assert_")
    assert hasattr(export, "to_script")
    assert hasattr(_llm, "complete") and hasattr(_llm, "complete_json")


@pytest.mark.m0_audit
def test_executor_import_does_not_pull_in_anneal_llm():
    # importing the whole executor surface must not import anneal_llm (D31). Run in a CLEAN
    # subprocess so an earlier in-process `import anneal_llm` (the vendored-import test) can't
    # pollute sys.modules and mask a real violation.
    import subprocess
    code = (
        "import anneal_executor.actions, anneal_executor.evidence, anneal_executor.export, "
        "anneal_executor.guard, anneal_executor.interpreter, anneal_executor.network, "
        "anneal_executor.resolve, anneal_executor.waits, sys; "
        "assert 'anneal_llm' not in sys.modules, 'executor transitively imported anneal_llm (D31)'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"D31 violation:\n{r.stdout}\n{r.stderr}"


@pytest.mark.m0_audit
def test_python_bugs_classify_as_engine_fault_not_tool_error():
    """§7 / boundary-scoping: a Python programming error (OUR code is broken) must classify as
    ENGINE_FAULT — loud — never TOOL_ERROR. This is what lets a caller (e.g. the re-author verify
    replay, _verify_green) re-surface an engine bug instead of laundering it into a red verdict /
    a false product_bug. A genuine tool/actionability error stays TOOL_ERROR (a real triage signal)."""
    from anneal_core.errors import ErrorClass, classify_exception
    for exc in (TypeError("x"), AttributeError("x"), KeyError("x"), IndexError("x"), NameError("x"),
                ZeroDivisionError("x")):
        assert classify_exception(exc, "verify") is ErrorClass.ENGINE_FAULT, exc
        assert classify_exception(exc, "act") is ErrorClass.ENGINE_FAULT, exc     # phase-independent
    # not an engine bug → unchanged behavioural/tool classification (never falsely promoted to ENGINE_FAULT)
    assert classify_exception(RuntimeError("element not interactable"), "act") is ErrorClass.TOOL_ERROR
