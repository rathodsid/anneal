"""The ambiguity interview (D26). Unanswered questions BLOCK the freeze — an interview answer is
never guessed (GOAL rule 6). A pre-authoring LLM pass lists the decisions a tester MUST resolve to
author deterministically that the NL (plus any answers so far) does not already settle.
"""
from __future__ import annotations

from anneal_llm.client import complete_json

INTERVIEW_SYSTEM = """You are triaging a natural-language web-QA flow before it is authored into a
deterministic test. List ONLY decisions the flow leaves genuinely UNRESOLVED that would change WHAT THE
TEST ASSERTS or WHICH existing record it acts on. The author observes the live app and picks concrete
controls, so only an open decision that changes the test's MEANING is blocking. When unsure whether a
question is blocking, do NOT ask.

A decision is NOT blocking (do NOT ask) when:
- The flow already NAMES the value/option (e.g. "select the option named \"Pro\"" — the choice is given,
  not open).
- The flow explicitly grants FREE choice ("choose any option", "pick one of the choices") AND the
  verification does not depend on which one — e.g. it later checks that "AN option is selected", not that
  a SPECIFIC one is. Any valid choice satisfies the test equally, so it is settled.
- It concerns interaction MECHANICS the author discovers live, not the test's meaning (whether a delete
  has a confirmation dialog, a control's exact label, how a dropdown opens).
- It is the NAME/label of a NEW item the flow creates — the author generates a unique one automatically.
- The flow states a specific verification to perform — author it AS WRITTEN. Do NOT question whether the
  check is sensible or propose an alternative (do not, e.g., ask whether to verify an empty state
  instead). A check that fails by the flow's design (verifying a never-created item) is intentional.

A decision IS blocking (ask) only when the flow leaves it open AND it changes the test's meaning:
- The flow acts on / asserts a SPECIFIC option or record but does not identify which, and the choice
  changes what is asserted (e.g. "assign one of the options, then verify THAT option is shown" — which
  option determines the assertion) or which record is acted on ("delete the record" when several exist
  and none is identified).
- A required value the flow implies but does not give, where different values would assert differently.

Return ONLY JSON: {"questions": ["<question>", ...]}. If nothing is blocking, return an empty list."""


def open_questions(nl: str, answers: dict | None = None, model: str | None = None) -> list[str]:
    ans = ""
    if answers:
        ans = "\n\nAnswers already provided:\n" + "\n".join(f"- {q}: {a}" for q, a in answers.items())
    resp = complete_json(INTERVIEW_SYSTEM, f'Flow:\n"""{nl}"""{ans}', **({"model": model} if model else {}))
    qs = resp.get("questions") or []
    return [q for q in qs if isinstance(q, str) and q.strip()]


def merge_answers(nl: str, answers: dict | None) -> str:
    if not answers:
        return nl
    return nl + "\n\nClarifications (use these exact decisions):\n" + \
        "\n".join(f"- {q} → {a}" for q, a in answers.items())
