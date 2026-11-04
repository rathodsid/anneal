"""The balanced-brace JSON extractor (A15) — pure unit tests, no network. Marked m0_audit so the
cheap CI job runs them: the extractor is the seam every hot/warm-lane decision passes through."""
from __future__ import annotations

import pytest

from anneal_llm.client import _extract_json

pytestmark = pytest.mark.m0_audit


def test_plain_json():
    assert _extract_json('{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}


def test_fenced_block():
    assert _extract_json('```json\n{"ref": 3, "verb": "click"}\n```') == {"ref": 3, "verb": "click"}


def test_json_wrapped_in_prose():
    raw = 'Sure — here is the pick:\n{"ref": 0, "reason": "matches"}\nLet me know if you need more.'
    assert _extract_json(raw) == {"ref": 0, "reason": "matches"}


def test_brace_inside_string_value_with_trailing_text():
    assert _extract_json('{"a": "}"} trailing text {not json') == {"a": "}"}


def test_array_in_prose():
    assert _extract_json("questions: [\"which name?\", \"verify how?\"] done") == ["which name?", "verify how?"]


def test_no_json_raises():
    with pytest.raises(ValueError):          # json.JSONDecodeError is a ValueError
        _extract_json("no json here at all")
