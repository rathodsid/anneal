"""The HOT lane author (§6.3, D24): NL → a draft Bundle by walking the LIVE app.

perceive (A2) → decide (B3 index-pick over the closed-vocab inventory — the LLM chooses a ref + verb,
never a selector) → act (executor primitives) → capture the locator + fallbacks (A6), rejecting
volatile testids (A3). Values the flow invents (the created item's name) are authored with a concrete
token, then VOLATILE-IZED to `{{run.token}}` at assembly (B1/D5) so replay uses a fresh token each run.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import string
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit

_DEBUG = bool(os.getenv("ANNEAL_AUTHOR_DEBUG"))
_log = logging.getLogger(__name__)

from anneal_core.errors import EngineFault, LocatorAmbiguous, LocatorUnresolved, PolicyStop
from anneal_core.rungs import derive_rungs
from anneal_executor import actions
from anneal_executor.guard import guard
from anneal_executor.resolve import resolve
from anneal_executor.settle import await_settled, await_transition
from anneal_llm.client import complete_json

from .perception import INVENTORY_K, inventory_text, rank_inventory, snapshot_interactive
from .prompts import ACT_PICK_SYSTEM, ASSERT_PICK_SYSTEM, SCAFFOLD_SYSTEM, VERIFY_ACT_SYSTEM

_ALNUM = string.ascii_lowercase + string.digits
_ID_SEG = re.compile(r"^(\d+|[a-f0-9]{8}-[a-f0-9-]{4,}|[a-z]+\d{3,}|[a-z0-9]{16,})$", re.I)

# A mutation (non-GET) is COMMITTED by an explicit commit interaction — a click/keypress/select/upload
# — never by TYPING into a field (a fill types text; it filters/searches via GET) or hovering. Used to
# gate the network postcondition, the destructive flag, and the post-commit transition wait. Verb-keyed,
# target-agnostic.
_COMMIT_VERBS = ("click", "keypress", "select", "upload")


class AuthoringIncomplete(Exception):
    """The hot lane could not GROUND a step in the observed inventory: ACT_PICK found no matching
    control (ref absent/out of range) or was asked to navigate to a path never seen on the app. B3
    anti-fabrication: the author FAILS HONESTLY (an authoring walk-failure → honest red) instead of
    inventing a selector or a `goto` URL. Caught by the red path's `_WALK_FAILURE`; surfaces as a
    non-freeze in the initial author. This is NOT an engine fault — the engine works; it simply cannot
    author this flow against this app (e.g. a control behind a collapsed/hidden surface recon can't see)."""


def _norm_path(p: str | None) -> str:
    """The path component of a URL/href, trailing slash stripped, for observed-vs-emitted comparison."""
    path = urlsplit(p or "").path.rstrip("/")
    return path or "/"


def _auth_token() -> str:
    return "z" + "".join(secrets.choice(_ALNUM) for _ in range(7))   # distinctive, unlikely to collide


def _templatize_path(path: str) -> str:
    """Network path: id-like segments → `{id}` (the compiler expands to `[^/]+`, §5.2)."""
    return "/".join("{id}" if _ID_SEG.match(seg) else seg for seg in path.split("/"))


def _url_regex(path: str) -> str:
    """URL postcondition MATCH is a regex (not `{id}`-templated): id segments → `[^/]+`, literals escaped."""
    return ".*" + "/".join("[^/]+" if _ID_SEG.match(seg) else re.escape(seg) for seg in path.split("/"))


def _volatile_testid(testid: str) -> bool:
    return bool(re.search(r"\d{4,}", testid))     # A3: an id with a 4+-digit run rots every run


# STRICT toggle detection for the effect-oracle seam: return the checked-state ONLY for a genuine
# switch/checkbox/radio (ARIA `aria-checked`, a native checkbox/radio, or an ARIA toggle role), else
# null. Deliberately does NOT fall back to `data-state` (a tab/accordion/popover also carries it) — a
# false toggle-detection would emit a `checked` oracle for a non-toggle click. Generic web semantics.
_TOGGLE_STATE_JS = """el => {
  const ac = el.getAttribute('aria-checked');
  if (ac === 'true' || ac === 'false') return ac === 'true';
  const r = (el.getAttribute('role') || '').toLowerCase();
  if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return !!el.checked;
  if ((r === 'switch' || r === 'checkbox' || r === 'radio') && typeof el.checked === 'boolean')
    return el.checked;
  return null;
}"""


def synthesize_effect_oracle(verb: str, value, target: dict, *, pre_state=None, post_state=None):
    """SEAM (oracle-falsifiability Prong A) — the falsifiable postcondition proving a VALUE-ENTERING
    step's effect actually landed: the value the step set is now REFLECTED in its own control, and
    would be ABSENT on a clean/pre or wrong state. Returns one postcondition `cond` dict, or None when
    no falsifiable effect can be grounded (the caller must NOT invent one — D26; an unobservable effect
    is a Prong-B / interview surface, never a silent rubber-stamp).

    This is the single place that answers "what proves THIS step did its job", by verb:
      - click  → on a genuine toggle/switch/radio/checkbox, the effect is the control's new
                 CHECKED-STATE: assert `checked` == the value it holds AFTER the act (fed via
                 post_state={"checked": <bool>}). On a clean/pre state the toggle sits in its default,
                 so the oracle is falsifiable. A click on a non-toggle control has no checked-state
                 (post_state carries no `checked`) → None (never invent one, D26).
      - fill   → `value_is`: the typed value must be redisplayed in the field (input_value == value).
      - select → the chosen option must be VISIBLE inside the control. A combobox trigger reflects its
                 choice as visible TEXT, not an input value, so `value_is` (input_value) can't see it —
                 assert `element visible` on the option text SCOPED WITHIN the trigger. Requires a
                 stable testid to scope to; without one there is no falsifiable, non-ambiguous anchor
                 (the option text alone can match a list elsewhere), so we return None rather than
                 freeze a weak oracle.

    `pre_state`/`post_state` are the seam for the agentic author loop: it FEEDS its post-act observation
    here so the effect is grounded in what actually changed rather than inferred structurally. Keep new
    state-changing verbs' effect-oracles in THIS function.
    Target-agnostic: keys on the observed value + the control's own identity, never an app noun."""
    del pre_state                                # pre-state reserved for Prong B's before/after gate
    if verb == "click":
        # a toggle click enters no typed value; its effect is the checked-state the loop observed.
        # The target is the control the act already resolved uniquely (A3 rejects a volatile testid at
        # capture), so it is a safe, non-ambiguous anchor for the oracle.
        if post_state is not None and post_state.get("checked") is not None:
            return {"kind": "checked", "checked": bool(post_state["checked"]), "target": target}
        return None
    if value is None or not str(value).strip():
        return None
    if verb == "fill":
        # Ground the oracle in the OBSERVED post-state when the agentic loop feeds it (§6 seam): the
        # executor checks `input_value() == equals` EXACTLY on replay, so `equals` must be what the field
        # REDISPLAYED after the fill (an app may reformat "1" → "1 concurrent"), not the merely intended
        # value. post_state={"value": <redisplayed>} → assert that; {"value": None/""} means the app did
        # not redisplay it (a cleared search box / masked field) → NO value_is (never a check that reds on
        # every replay, D26). post_state=None = no observation fed (a non-loop caller / the unit contract)
        # → fall back to the intended value.
        if post_state is not None:
            observed = (post_state.get("value") or "").strip()
            return {"kind": "value_is", "equals": observed, "target": target} if observed else None
        return {"kind": "value_is", "equals": value, "target": target}
    if verb == "select":
        # scope to the trigger by its OWN stable testid — the only non-ambiguous anchor for "this
        # control now shows the choice". A volatile/absent testid gives no replayable scope → no oracle.
        if target.get("strategy") == "testid" and not _volatile_testid(target.get("selector") or ""):
            return {"kind": "element", "state": "visible",
                    "target": {"strategy": "text", "selector": value,
                               "within": {"testid": target["selector"]}}}
        return None
    return None


def _implicit_role(d: dict) -> str | None:
    """The accessible role, from an explicit role attr or inferred from the tag (a native <button>
    carries no role attr but IS role=button — without this, a named native control has only its
    ambiguous visible text to resolve by)."""
    if d.get("role"):
        return d["role"]
    tag, typ = d.get("tag"), (d.get("type") or "").lower()
    if tag == "button":
        return "button"
    if tag == "a" and d.get("href"):
        return "link"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        if typ in ("checkbox", "radio", "submit", "reset", "button"):
            return {"submit": "button", "reset": "button", "button": "button"}.get(typ, typ)
        return "textbox"
    return None


def _target_from_descriptor(d: dict) -> dict:
    """Build a testid-first Target (A6 fallbacks captured), rejecting a volatile testid (A3).
    Prefers role+accessible-name over bare visible text so a named control resolves unambiguously."""
    role = _implicit_role(d)
    name = d.get("name")
    fb: dict = {}
    if name:
        fb["text"] = name
    elif d.get("text"):
        fb["text"] = d["text"]
    if d.get("placeholder"):
        fb["placeholder"] = d["placeholder"]

    testid = d.get("testid")
    if testid and not _volatile_testid(testid):
        primary = {"strategy": "testid", "selector": testid}
        if role and (name or d.get("text")):
            fb.setdefault("role", role)
    elif role and (name or d.get("text")):
        primary = {"strategy": "role", "selector": role}
        fb["text"] = name or d.get("text")            # the accessible name for the role rung
    elif d.get("placeholder"):
        primary = {"strategy": "placeholder", "selector": d["placeholder"]}
        fb.pop("placeholder", None)
    elif d.get("text"):
        primary = {"strategy": "text", "selector": d["text"]}
    elif role:
        primary = {"strategy": "role", "selector": role}
    else:
        primary = {"strategy": "css", "selector": d.get("tag", "*")}

    target = dict(primary)
    # A combobox/listbox OPTION is itself a `<li>` (∈ ROW_SEL), so rowFor self-matches and would scope
    # the option to a row bearing its OWN label — "an option inside a row named X" resolves to nothing
    # (the option IS the row). Row-scoping (A2 row-relative) is for a control nested INSIDE a data row
    # (a table row's action button), never for the option element itself. Skip it for options.
    if role != "option" and d.get("row") and d["row"].get("label"):
        # the row's DISTINCTIVE identity is its first cell — drop trailing cells/controls (tabs, the
        # kebab glyph) so has_text matches the one row cleanly at replay (A2 row-relative).
        raw = d["row"]["label"]
        label = re.split(r"[\t\n]", raw)[0].strip() or raw.strip()
        target["within"] = {"row_label": label}
    fb = {k: v for k, v in fb.items() if v and not (k == primary["strategy"])}
    if fb:
        target["fallbacks"] = fb
    return target


def _is_editable_control(d: dict) -> bool:
    """An input/editable control DISPLAYS a typed value; it is NEVER the thing a 'verify X appears'
    oracle should key on — its value coincidentally equalling X (e.g. the search box you just typed X
    into) is not evidence X is present. Keyed on generic tags/roles, target-agnostic."""
    return (d.get("tag") in ("input", "textarea", "select")
            or d.get("role") in ("textbox", "searchbox", "combobox", "spinbutton"))


def _reground_assert_target(raw: list[dict], text_val: str) -> dict:
    """P5 (oracle-falsifiability Prong A′): the ASSERT picker names the thing to verify by its bare
    VISIBLE TEXT, but a bare-text rung that matches >1 element now fails the positive-replay gate (so a
    legit test won't freeze). When the observed page carries a control whose OWN identity IS that text,
    reuse its descriptor to build a stable-testid / role+name locator instead — the same unique-resolution
    discipline `_target_from_descriptor` gives the ACT lane (one chooser, both lanes).

    Match on EXACT identity only (name or full text equals text_val), never loose containment — a loose
    match risks pointing the oracle at the wrong element (and check #3 would then red a legit freeze).
    No confident, more-specific match → keep the bare text (never worse than before)."""
    exact = [d for d in raw
             if (d.get("name") == text_val or (d.get("text") or "").strip() == text_val)
             and not _is_editable_control(d)]     # a control whose VALUE == X is not X "appearing"
    pick = (next((d for d in exact if d.get("testid") and not _volatile_testid(d["testid"])), None)
            or next((d for d in exact if d.get("role") and (d.get("name") or d.get("text"))), None))
    return _target_from_descriptor(pick) if pick else {"strategy": "text", "selector": text_val}


async def _ground_scope(page, scope_role: str | None, text_val: str) -> dict | None:
    """Ground an assert's container scope in OBSERVATION — the B3 principle applied to scopes.

    ASSERT_PICK may infer a `scope_role` from a noun in the step wording (e.g. "the … list" → a
    `role=list` container). That role is committed to the frozen oracle ONLY if the fully-scoped
    locator actually resolves (>=1) on the page the assert runs on — the SAME locator the cold-replay
    resolver builds (`page.get_by_role(role).get_by_text(text)`, resolve.py). A scope that matches
    nothing can only ever red the assert (a container that is not how this app marks up its regions —
    e.g. a nav grouped by headings, not an ARIA list); drop it and keep the bare text target, which the
    resolver still gates on count()==1.

    NOT an assert-pass gate (so N1/oracle-honesty is untouched): when the bare target ALSO resolves to
    nothing — a never-created negative control, or a genuinely absent element — the scope is dropped but
    the OUTCOME is unchanged (the assert still reds honestly and still freezes). Only a PHANTOM scope
    (target present unscoped, gone once scoped) is repaired. Target-agnostic: role/text only."""
    if not scope_role:
        return None
    try:
        scoped_n = await page.get_by_role(scope_role).get_by_text(text_val).count()
    except Exception:                                    # a bogus role string / detached page → not grounded
        scoped_n = 0
    return {"role": scope_role} if scoped_n else None


def _target_ns(target: dict):
    fb = target.get("fallbacks") or {}
    return SimpleNamespace(strategy=target["strategy"], selector=target["selector"],
                           fallbacks=SimpleNamespace(testid=fb.get("testid"), role=fb.get("role"),
                                                     text=fb.get("text"), placeholder=fb.get("placeholder"),
                                                     css=fb.get("css")))


def _scope_ns(target: dict):
    w = target.get("within")
    if not w:
        return None
    return SimpleNamespace(testid=w.get("testid"), role=w.get("role"), row_label=w.get("row_label"))


# P4 (oracle-falsifiability Prong A′): a transient live region self-dismisses — walk the element and its
# ancestors for the ARIA live-region SEMANTICS (a web standard, target-agnostic — never a class name / app
# noun). Runs in the page, tolerant of a detached node.
_TRANSIENT_JS = """(el) => {
  for (let n = el; n; n = n.parentElement) {
    const r = ((n.getAttribute && n.getAttribute('role')) || '').toLowerCase();
    const l = ((n.getAttribute && n.getAttribute('aria-live')) || '').toLowerCase();
    if (r === 'alert' || r === 'status') return true;
    if (l === 'polite' || l === 'assertive') return true;
  }
  return false;
}"""


async def _assert_target_is_transient(page, target: dict) -> bool:
    """An assert keyed on a TRANSIENT live region — a toast/snackbar (`role=alert`/`status`) or any
    `aria-live` announcement — is a self-dismissing signal, not a durable outcome. It slips the whole
    freeze gate: the negative control sees it ABSENT on the pre-state (falsifiable) and the positive
    replay sees it PRESENT right after its step (green), yet it DRIFTS on a later real run once it has
    auto-dismissed. So reject it at synthesis — assert the durable outcome it announces instead (P4). A
    target that cannot be resolved here is NOT flagged (a never-true oracle is the positive replay's job)."""
    try:
        loc, _ = await resolve(page, derive_rungs(_target_ns(target)), 2000, _scope_ns(target))
        return bool(await loc.first.evaluate(_TRANSIENT_JS))
    except Exception:
        return False


class _NetRec:
    def __init__(self, host: str):
        self.host = host
        self.events: list[dict] = []

    def on_response(self, resp):
        try:
            u = urlsplit(resp.url)
            if u.netloc == self.host and resp.request.method != "GET":
                self.events.append({"method": resp.request.method, "path": u.path, "status": resp.status})
        except Exception:
            pass


@dataclass
class AuthorResult:
    bundle: dict
    questions: list[str] = field(default_factory=list)
    network_log: list[dict] = field(default_factory=list)
    territory: dict = field(default_factory=dict)   # D25 recon: routes + control idioms observed
    skipped_steps: list[dict] = field(default_factory=list)  # RC4: steps that could not be grounded
    #                                                          even after active seeking — AUDITABLE,
    #                                                          never a silent drop (surfaced on freeze)


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


# ── recon-time behavioral progressive disclosure (B7/A2, Option 1) ────────────────────────────────
# Create forms often live behind a click (a modal/portal overlay or a separate page). A rest snapshot
# can't see those fields, so the scaffold plans blind. The reveal probe clicks generic create-OPENER
# controls to disclose them. ALL heuristics are generic web semantics (action verbs, positioning, hrefs) —
# no app literal, domain noun, or app choreography.
_OPENER_RE = re.compile(r"\b(?:add|create|new)\b|(?:^|\s)\+(?:\s|$)", re.I)
_DESTRUCTIVE_RE = re.compile(r"\b(?:delete|remove|archive|clear|reset|discard|destroy|trash)\b", re.I)
_REVEAL_CAP = 4          # bounded, deterministic candidate probing

_FIELD_JS = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(); const c=getComputedStyle(el);
    return r.width>1 && r.height>1 && c.display!=='none' && c.visibility!=='hidden'; };
  const inOverlay = el => {                     // a field inside a fixed/absolute portal-sized container
    for (let a=el; a && a!==document.body; a=a.parentElement) {
      const c=getComputedStyle(a);
      if (c.position==='fixed' || c.position==='absolute') {
        const r=a.getBoundingClientRect(); if (r.width>=300 && r.height>=200) return true; } }
    return false; };
  const SEL='input,textarea,select,[role=combobox],[role=textbox]';
  return [...document.querySelectorAll(SEL)].filter(vis).map(e => ({
    key: e.getAttribute('data-testid') || e.getAttribute('aria-label') || e.getAttribute('placeholder')
         || e.getAttribute('name') || (e.tagName.toLowerCase()+':'+(e.getAttribute('type')||'')),
    overlay: inOverlay(e) }));
}"""


def _is_opener(e: dict) -> bool:
    """A generic create-OPENER control (add/create/new/+), never a destructive one — the ONLY controls the
    read-only reveal probe may click (gate #2). Target-agnostic: matches action verbs, no domain nouns."""
    label = f"{e.get('text') or ''} {e.get('name') or ''}".strip()
    if not label or _DESTRUCTIVE_RE.search(label) or not _OPENER_RE.search(label):
        return False
    return e.get("tag") in ("button", "a") or (e.get("role") or "") in ("button", "link", "menuitem")


def _elem_sig(elems: list[dict]) -> tuple:
    """Stable identity signature of an inventory — to PROVE a reveal probe restored the page exactly."""
    return tuple(sorted((e.get("testid") or "", e.get("role") or e.get("tag") or "",
                         (e.get("text") or e.get("name") or "")[:40]) for e in elems))


async def _fields(page) -> list[dict]:
    try:
        return await page.evaluate(_FIELD_JS)
    except Exception:
        return []


async def _restore_recon(page, start_path, base_sig, opener):
    """Return to the entry page after a probe and PROVE the baseline is reproduced (clean restore). An
    unclean restore leaves residual state — the perturbation the determinism drive killed → fail LOUD."""
    try:
        await page.goto(start_path, wait_until="domcontentloaded", timeout=30000)
        await await_settled(page)      # compare the signature on a hydrated page (a 300ms fixed wait
        #                                on an un-hydrated SPA gave a partial snapshot → spurious fault)
    except Exception as exc:
        raise EngineFault(f"reveal probe could not restore {start_path!r} after opening {opener!r}: {exc}")
    if _elem_sig(await snapshot_interactive(page)) != base_sig:
        raise EngineFault(f"reveal probe left residual state after opening {opener!r} — unclean restore")


async def _probe_reveals(page, base_elems, *, start_path, base_sig):
    """Behavioral progressive disclosure (Option 1). For each generic create-OPENER candidate (deterministic
    order, bounded), click it READ-ONLY and detect a SIGNIFICANT reveal — form fields appearing in a
    fixed/absolute OVERLAY, or after a ROUTE change. An IN-FLOW reveal (fields in normal flow, same URL) is
    NOT significant → discarded, so a plain inline show/hide perturbs nothing. Safety (gate #2): only
    openers are clicked; a non-GET during a probe is BLOCKED at the network layer (never reaches the target)
    AND the control is excluded + logged LOUD — a reveal probe never mutates; every probe RESTORES and
    re-proves the baseline signature, else the run fails LOUD. Returns significant reveals."""
    base_fields = {f["key"] for f in await _fields(page)}
    candidates = [e for e in base_elems if _is_opener(e)][:_REVEAL_CAP]
    reveals: list[dict] = []
    for desc in candidates:
        opener = (desc.get("text") or desc.get("name") or "").strip()[:40]
        url0, kind, revealed_inv = page.url, None, None
        blocked = {"mutate": False}

        async def _readonly_guard(route):            # gate #2: PREVENT a mutation, don't just detect it
            if route.request.method == "GET":
                await route.continue_()
            else:                                    # a reveal may only DISCLOSE; a write never leaves recon
                blocked["mutate"] = True
                await route.abort()

        await page.route("**/*", _readonly_guard)
        click_ok = True
        try:
            target = _target_from_descriptor(desc)
            loc, _ = await resolve(page, derive_rungs(_target_ns(target)), 5000, _scope_ns(target))
            await actions.robust_click(loc)
            await page.wait_for_timeout(600)
        except Exception as exc:                     # a probe that can't complete cleanly → exclude
            _log.warning("reveal probe of %r aborted (%s) — excluded", opener, type(exc).__name__)
            click_ok = False
        finally:
            await page.unroute("**/*", _readonly_guard)
        if click_ok:
            if blocked["mutate"]:
                _log.error("reveal probe of %r attempted a non-GET — BLOCKED and excluded; a reveal probe "
                           "must never mutate a live target (read-only invariant, gate #2)", opener)
            else:
                new = [f for f in await _fields(page) if f["key"] not in base_fields]
                kind = "route" if page.url != url0 else ("overlay" if any(f["overlay"] for f in new) else None)
                if new and kind:
                    revealed_inv = inventory_text(await snapshot_interactive(page))[:1400]
            if revealed_inv:
                reveals.append({"opener": opener, "kind": kind, "inv": revealed_inv})
        await _restore_recon(page, start_path, base_sig, opener)
    return reveals


_SEEK_STEPS = 3               # bounded active-seek passes before conceding a step is ungroundable
_SEEK_SETTLE_MS = 500         # let an async re-render (search/filter list refresh) populate before re-looking


def _groundable(pick: dict, inv: list[dict]) -> bool:
    ref = pick.get("ref")
    return (pick.get("verb") in ("click", "fill", "select", "hover", "keypress")
            and isinstance(ref, int) and 0 <= ref < len(inv))


def _record_ungrounded(step, reason, scaffold_flagged, skipped_steps, test_id, *, kind="ungrounded"):
    """RC4 — NEVER a silent skip. Record an ungrounded step (auditable, surfaced on freeze); FAIL LOUD
    when it is the flow's declared IRREVERSIBLE action (silently dropping a delete/confirm corrupts the
    flow). Shared by BOTH grounding gates — the perception pick AND the locator resolution — so a control
    the LLM named but that cannot resolve to a unique live element is handled identically, never a crash.

    `kind` separates a FAILURE-to-perform an intended interaction (`ungrounded` — a control the flow
    named but the engine could not act on) from a DELIBERATE, safe reduction the engine chose
    (`over_plan` — a redundant auto-persist 'save', a transient-toast assert). The freeze coverage gate
    reads this: an `ungrounded` drop is a hollow-green risk and refuses to freeze; an `over_plan` drop
    is benign and only surfaced. Target-agnostic — keyed on the engine's own drop rationale, no app noun."""
    skipped_steps.append({"intent": step["text"], "destructive": scaffold_flagged,
                          "reason": reason, "kind": kind})
    if scaffold_flagged:
        raise AuthoringIncomplete(
            f"step {step['text']!r}: {reason} — this is the flow's declared destructive/irreversible "
            f"action; refusing to freeze a bundle that silently drops it.")
    _log.warning("author[%s]: RECORDED-SKIP step %r — %s", test_id, step["text"], reason)


def ungrounded_drops(skipped_steps: list[dict]) -> list[dict]:
    """The freeze coverage gate's predicate. Returns the drops that are FAILED intended interactions —
    a control the flow named that the engine could not perform — as opposed to the engine's DELIBERATE,
    safe over-plan reductions (`kind == "over_plan"`: a redundant auto-persist 'save', a transient-toast
    assert). A non-empty result means the frozen bundle would be a hollow shell: calibration only ever
    sees the SURVIVING steps, so those dropped acts never affect its falsifiability/positive-replay
    verdict, and the bundle would freeze GREEN without the intent having happened. The freeze must refuse
    that (RC4 at the boundary). Target-agnostic: keyed on the engine's own drop kind, never an app noun."""
    return [s for s in skipped_steps if s.get("kind") != "over_plan"]


async def _row_scope_count(page, within) -> int | None:
    """Count the rows a within:{row_label} act-scope matches, using resolve._scope_root's OWN logic so
    the check reflects the locator that will actually be built. Returns None when the target has no row
    scope (nothing to validate). Exactly one match is the only deterministic case for a row-relative act:
    0 = the target row is not present, >1 = ambiguous among residue/sibling rows (never guess which)."""
    if not within or not within.get("row_label"):
        return None
    role = within.get("role") or "row"
    try:
        return await page.get_by_role(role).filter(has_text=within["row_label"]).count()
    except Exception:
        return 0


def _match_route(step_text: str, observed_paths) -> str | None:
    """Nav-grounding: pick the OBSERVED route whose path best matches a navigation step's intent, by
    token overlap on the path's OWN segments. So 'go back to the <section> list page' resolves to the
    observed '/<section>' path instead of silently defaulting to '/' (which, on an app that redirects
    the root to a different default section, derails every later step onto the WRONG page).
    Returns None when nothing overlaps → the caller records/seeks, never a blind home navigation.
    Target-agnostic: generic token overlap, no app noun. Ignores the bare root so it never 'matches'."""
    toks = set(re.findall(r"[a-z0-9]+", step_text.lower()))
    best, best_score = None, 0
    for p in observed_paths:
        if _norm_path(p) == "/":
            continue                                     # the root is never a positive section match
        segs = set(re.findall(r"[a-z0-9]+", p.lower()))
        score = len(segs & toks)
        if score > best_score:
            best, best_score = p, score
    return best if best_score else None


async def _seek_target(page, step, inv_text_of, model):
    """RC3 active seeking. A single snapshot can miss a control that (a) has not RENDERED yet — an async
    re-render (a search/filter re-populating the list) lands hundreds of ms after the prior action's
    fixed settle — or (b) sits below the perception window. Re-perceive IN PLACE after a bounded settle
    (lets the deferred DOM arrive), and on later passes scroll to reach below-fold controls. Deliberately
    NO clicks: toggling a collapsed section risks opening a portal that overlays the re-perception, and a
    control only reachable by an unrecorded expand would not replay — so seeking never mutates the page.
    Returns the grounded (pick, inv) or (None, last_inv). Bounded; never navigates."""
    inv = []
    for i in range(_SEEK_STEPS):
        await page.wait_for_timeout(_SEEK_SETTLE_MS)   # wait for the async re-render to settle
        if i >= 1:                                     # later passes: also reach below-fold controls
            await page.evaluate("() => window.scrollBy(0, Math.round(window.innerHeight * 0.85))")
            await page.wait_for_timeout(150)
        inv = rank_inventory(await snapshot_interactive(page), step["text"], k=INVENTORY_K)
        pick = complete_json(ACT_PICK_SYSTEM, f'Step: "{step["text"]}"\n\nINVENTORY:\n{inv_text_of(inv)}',
                             **({"model": model} if model else {}))
        if _groundable(pick, inv):
            return pick, inv
    return None, inv


def _verify_turns() -> int:
    """Bound on the per-step observe→act→verify→correct loop (§5.1). An AUTHOR-time cost knob (each extra
    turn is ~1 ACT_PICK + up to 1 VERIFY call); replay is unaffected. Default 2 (one attempt + one
    correction). Env-overridable so a spike can measure whether a 3rd turn recovers real cases."""
    try:
        return max(1, int(os.getenv("ANNEAL_AUTHOR_VERIFY_TURNS", "2")))
    except ValueError:
        return 2


async def _verify_act(page, step, verb, value, desc, locator, url_before, model):
    """Post-act self-verification (§5.1): did THIS act achieve the step's intent? Returns
    (satisfied: bool, post_state: dict | None, feedback: str).

    `post_state` is the STRUCTURED observation of what the act produced — the same evidence the
    effect-oracle needs (fed straight into `synthesize_effect_oracle(post_state=...)`, §6 seam). Its
    shape is deliberately open so new verbs can extend it without reworking the caller: today `fill`
    emits {"value": <redisplayed>}; a future toggle/tab oracle will emit {"aria_pressed": ...} /
    {"aria_selected": ...} through the same slot. None = nothing to feed (the seam falls back to its
    structural logic, e.g. a select's option-visible oracle).

    Hybrid by whether a deterministic ground truth exists:
    - fill → DETERMINISTIC read-back: the field must now hold the value we typed (exact string compare —
      no LLM, no hallucination surface). A field the app CLEARS/masks (empty read-back) counts as done
      but yields no value_is (post_state={"value": None}). This read-back doubles as the oracle.
    - select / click / hover / keypress → LLM VERIFY: an open-ended effect (a surface opened, a choice
      reflected) has no single value to read back — this is where judgment earns its cost, and exactly
      where the engine mis-grounds today. A combobox choice is reflected as visible text, not an input
      value, so select routes here too rather than guessing at an unreadable value."""
    if verb == "fill" and value is not None:
        try:
            cur = await locator.first.input_value()
        except Exception:
            cur = None
        if cur is not None:
            v, c = str(value).strip(), cur.strip()
            if v == c or (v and v in c):
                return True, {"value": cur}, ""            # value stuck → confirmed; feed it as the oracle
            if not c:
                return True, {"value": None}, ""           # app cleared it (a search box) → done, no oracle
            return False, None, f"the field now shows {cur!r}, not the intended {value!r}"
        return True, {"value": None}, ""                   # not a readable input → accept, but no oracle

    # LLM VERIFY (open-ended effects, and selects whose value isn't cleanly readable)
    now = rank_inventory(await snapshot_interactive(page), step["text"], k=INVENTORY_K)
    did = f"{verb} on {desc.get('testid') or desc.get('role') or desc.get('tag') or '?'}"
    d_txt = desc.get("text") or desc.get("name")
    if d_txt:
        did += f" ({d_txt!r})"
    if value:
        did += f", value={value!r}"
    url_note = "the URL changed" if page.url != url_before else "the URL did not change"
    res = complete_json(
        VERIFY_ACT_SYSTEM,
        f'Step: "{step["text"]}"\n\nACTION TAKEN: {did}; {url_note}.\n\nPAGE NOW:\n{inventory_text(now)}',
        **({"model": model} if model else {}))
    return bool(res.get("satisfied", True)), None, str(res.get("reason") or "")


async def author_bundle(page, *, nl, project_id, test_id, persona, target_key, start_path="/",
                        territory=None, model=None) -> AuthorResult:
    name_token = _auth_token()
    item_name = f"Item {name_token}"

    # ── recon FIRST (B7): navigate to the app entry, observe, and REVEAL behind-click surfaces so the
    #    scaffold is grounded in controls hidden behind a create opener (a modal/portal overlay or a new
    #    page), not just what is visible at rest. See _probe_reveals (Option-1 behavioral disclosure). ──
    # KNOWN GENERALITY GAP (do NOT pretend it's covered): the significant-reveal filter records only an
    # OVERLAY (fixed/absolute portal) or a ROUTE change. An IN-FLOW accordion/expander that discloses
    # fields in normal document flow is — by positioning — indistinguishable from a plain inline show/hide,
    # so it is NOT captured. A future platform using that idiom for a required form would need this
    # extended. Accepted for the current corpus; to be revisited via the cross-domain holdout.
    await page.goto(start_path, wait_until="domcontentloaded", timeout=30000)
    # SPA-readiness: on a client-rendered app domcontentloaded is an empty shell that hydrates (and may
    # client-redirect) seconds later. Without settling here, recon captures 0 controls → empty
    # TerritoryNotes and a scaffold planned blind. Settle before perceiving, and read `host` from the
    # post-redirect URL so the network recorder scopes to the app's real origin.
    await await_settled(page)
    host = urlsplit(page.url).netloc
    recon_elems = await snapshot_interactive(page)
    recon_inv = inventory_text(rank_inventory(recon_elems, "", k=INVENTORY_K))   # bounded overview (no step yet)
    base_sig = _elem_sig(recon_elems)
    reveals = await _probe_reveals(page, recon_elems, start_path=start_path, base_sig=base_sig)
    territory = {
        "probe_path": start_path,
        "routes": sorted({e["href"] for e in recon_elems if e.get("href")}),
        "widgets": sorted({e["role"] for e in recon_elems if e.get("role")}),
        "recon_sample": recon_inv[:1200],
    }
    if reveals:                                       # absent when nothing significant is revealed, so a
        territory["revealed"] = [{"opener": r["opener"], "kind": r["kind"]} for r in reveals]  # plain
    recon = f"\n\nLive recon of {start_path} (controls on the entry page):\n{recon_inv[:2500]}"
    if reveals:                                       # inline-form app's persisted territory is unchanged
        recon += ("\n\nControls revealed by opening a create control (a form behind a modal/overlay or a "
                  "new page — open it FIRST, then act on these fields):\n" +
                  "\n".join(f"- after opening {r['opener']!r}:\n{r['inv']}" for r in reveals))
    if territory:
        recon += f"\n\nTerritory notes:\n{territory}"

    scaffold = complete_json(
        SCAFFOLD_SYSTEM,
        f'Flow to test (verbatim):\n"""{nl}"""\n\nIf the flow creates a named item and no name is '
        f'pinned, use this EXACT literal name everywhere it is referenced: "{item_name}".{recon}',
        **({"model": model} if model else {}))
    steps_plan = scaffold.get("steps", [])
    title = scaffold.get("title") or nl[:80]
    if _DEBUG:
        print(f"[author] SCAFFOLD plan ({len(steps_plan)} steps):", flush=True)
        for i, s in enumerate(steps_plan):
            print(f"   [{i}] {s.get('type')}{' DESTR' if s.get('destructive') else ''}: {s.get('text')}",
                  flush=True)
    skipped_steps: list[dict] = []                    # RC4: ungroundable steps — recorded, never silent

    bundle_steps: list[dict] = [{
        "id": "s1", "intent": "open the app", "action": "goto", "arg": start_path,
        "postcondition": {"all": [{"kind": "url", "match": ".*" + re.escape(start_path)}], "budget_ms": 10000},
    }]

    rec = _NetRec(host)
    page.on("response", rec.on_response)
    network_log: list[dict] = []
    # B3 anti-fabrication: a `goto` may only target a path the engine has actually SEEN on the app —
    # the start path, a route linked in recon, or (accumulated below) any href in a step's inventory.
    # A path the LLM infers from the step's wording is never observed → AuthoringIncomplete (honest red).
    observed_paths = {_norm_path(start_path)} | {_norm_path(h) for h in territory["routes"]}
    sid = 2
    mutating = False
    destructive_intent = False           # the flow intends an irreversible action (scaffold-flagged, D15)
    destructive_step = None              # the act that PERFORMED it (fired the mutation) — bound at replay
    flagged_act_step = None              # the act of the scaffold's OWN flagged step (first fallback)
    last_act_step = None                 # fail-closed fallback: the last act while the intent is active

    for step in steps_plan:
        await await_settled(page)                     # perceive a hydrated/settled DOM, never a shell
        raw = await snapshot_interactive(page)
        # RANK the observed controls by relevance to THIS step's text before showing ACT_PICK a
        # bounded slice — so the control the step names can't be truncated off a dense page (RC1).
        inv = rank_inventory(raw, step["text"], k=INVENTORY_K)
        inv_text = inventory_text(inv)
        # hrefs feed the B3 anti-fabrication goto check — accumulate from the FULL observation, not the
        # ranked slice, so a real route is never dropped just because it ranked below the budget.
        observed_paths |= {_norm_path(e["href"]) for e in raw if e.get("href")}
        if step.get("type") == "assert":
            pick = complete_json(ASSERT_PICK_SYSTEM,
                                 f'Step: "{step["text"]}"\n\nINVENTORY:\n{inv_text}',
                                 **({"model": model} if model else {}))
            text_val = pick.get("text") or item_name
            # PL-5 / D7 falsifiability: a "verify created" present-assert must key on the created
            # entity's OWN token-bearing identity, not a static attribute (a type label, a section
            # header, persistent chrome) that is present regardless of what the flow did — a static
            # oracle rubber-stamps, and D7 calibration correctly refuses to freeze it. When the flow has
            # created a named item (mutating) and the picker latched onto a string that does NOT carry
            # this run's token, assert the created name instead — the one signal whose presence proves
            # the create happened (and which fails on the never-created clean state). GROUNDED IN
            # OBSERVATION (the B3 / Layer-1 principle): only re-key when item_name is actually on the
            # page, so a pinned name or a legit secondary assert (e.g. a success toast) — where
            # item_name was never the created identity — is NOT re-pointed at a never-created name (which
            # would freeze then red on every real replay). Target- and verb-agnostic: keyed on the run
            # token, never an app noun.
            if (pick.get("kind") != "value_is" and (pick.get("state") or "visible") == "visible"
                    and mutating and name_token not in (text_val or "")):
                try:
                    created_present = await page.get_by_text(item_name).count() > 0
                except Exception:
                    created_present = False
                if created_present:
                    text_val = item_name
            # LAYER 1: ground the container scope in observation (drop a phantom scope_role) — see
            # _ground_scope. Uses text_val AFTER the falsifiability re-key so the check reflects the
            # locator that will actually be frozen.
            scope = await _ground_scope(page, pick.get("scope_role"), text_val)
            if pick.get("kind") == "value_is":
                assert_obj = {"equals": pick.get("value") or "",
                              "target": {"strategy": "text", "selector": text_val}}
            else:
                # P5 (Prong A′): prefer a stable-testid / role+name locator over the bare-text pick so the
                # oracle resolves UNIQUELY — a bare-text rung matching >1 element now reds the positive
                # replay (check #3), so grounding it here keeps a legit test freezable.
                assert_obj = {"state": pick.get("state") or "visible",
                              "target": _reground_assert_target(raw, text_val)}
            if scope:
                assert_obj["target"]["within"] = scope
            # P4 (Prong A′): never freeze an oracle keyed on a transient toast/announcement — it drifts
            # once it auto-dismisses and the freeze gate cannot see it (green on the positive replay).
            if await _assert_target_is_transient(page, assert_obj["target"]):
                _record_ungrounded(
                    step, "verification targets a transient live region (ARIA role=alert/status or "
                    "aria-live) — a toast/announcement auto-dismisses and is not a durable oracle; assert "
                    "the durable outcome it announces instead", scaffold_flagged=False,
                    skipped_steps=skipped_steps, test_id=test_id, kind="over_plan")
                continue
            bundle_steps.append({"id": f"s{sid}", "intent": step["text"], "action": "assert",
                                 "assert": assert_obj, "postcondition": None, "reload_on_miss": True,
                                 "budget_ms": 10000})
            sid += 1
            continue

        # ── act (bounded observe→act→verify→correct loop, §5.1/D24) ──
        # HOT authoring spends intelligence at AUTHOR time (replay stays cold, D31): a non-destructive
        # in-page act runs a real loop — pick → act → RE-PERCEIVE and confirm the step's intent actually
        # landed → re-pick with feedback if not, bounded by ANNEAL_AUTHOR_VERIFY_TURNS (default 2). This
        # catches a mechanically-successful-but-WRONG act (clicking a tab instead of filling the field)
        # that the old single-pass index-pick froze green. Destructive acts and navigations stay
        # SINGLE-PASS: we never re-fire an irreversible commit, and a goto is verified by its own landing
        # check below.
        rec.events.clear()
        url_before = page.url
        pick = complete_json(ACT_PICK_SYSTEM, f'Step: "{step["text"]}"\n\nINVENTORY:\n{inv_text}',
                             **({"model": model} if model else {}))
        verb = pick.get("verb")
        value = pick.get("value")
        scaffold_flagged = bool(step.get("destructive"))
        if scaffold_flagged:
            destructive_intent = True                    # the flow declares an irreversible action here

        if verb == "goto":
            # A navigation step must reach a CONCRETE, observed destination. Resolve from the LLM's value
            # if it named a path, else recover the intended section by matching the step to an observed
            # route (_match_route). CONTEXT-GROUNDING GAP (general, all targets): never fall back to the
            # app root — a platform that redirects the root to a default section would silently derail
            # EVERY later step onto the wrong page. And a goto with NO observed route is ACT_PICK PUNTING
            # because it could not see the control — that is a control-not-found, not a navigation, so we
            # fall through to the in-page seek path (a chance to scroll/re-perceive) instead of fabricating
            # a nav. Only a concrete observed section is treated as a real navigation here.
            matched = _match_route(step["text"], observed_paths)
            nav_path = value if (value and _norm_path(value) != "/") else matched
            if nav_path:
                if _norm_path(nav_path) not in observed_paths:  # B3 SAFETY: never a fabricated path
                    raise AuthoringIncomplete(
                        f"step {step['text']!r}: ACT_PICK chose goto {nav_path!r} — a path never observed "
                        f"on the app (an href it inferred from the step's wording, not one it saw). Failing "
                        f"honestly rather than fabricating a navigation. Observed: {sorted(observed_paths)}")
                await page.goto(nav_path, wait_until="domcontentloaded", timeout=30000)
                # CONTEXT VERIFY (general): confirm the nav reached the intended section. If the app
                # redirected away, the flow is NOT where it must be — record it rather than silently run
                # later steps on the wrong page.
                if _norm_path(urlsplit(page.url).path) != _norm_path(nav_path):
                    _record_ungrounded(step, f"navigation to {nav_path!r} landed on "
                                       f"{urlsplit(page.url).path!r} (redirected away from the intended "
                                       f"section)", scaffold_flagged, skipped_steps, test_id)
                    continue
                bundle_steps.append({"id": f"s{sid}", "intent": step["text"], "action": "goto",
                                     "arg": nav_path, "postcondition": {
                                         "all": [{"kind": "url", "match": ".*" + re.escape(nav_path)}],
                                         "budget_ms": 10000}})
                sid += 1
                continue
            # else: a punted goto with no observed section → fall through to in-page seek/grounding below
            #       (verb=goto is not _groundable, so the seek block re-perceives and re-picks the control).

        # in-page act: the bounded self-verify loop. `committed` marks a verified (or destructive
        # single-pass) attempt worth freezing; `skip_reason` records a grounding/resolution/verify
        # failure to RC4-record ONCE after the loop; `post_state` carries the winning act's observed
        # effect for the oracle seam.
        committed = False
        skip_reason = None
        skip_kind = "ungrounded"     # a failed intended interaction unless a site marks it a safe over-plan
        feedback = None
        post_state = None
        act_toggle_state = None      # post-act checked-state when THIS act is a click on a genuine toggle
        max_turns = 1 if scaffold_flagged else _verify_turns()
        for _turn in range(max_turns):
            if _turn > 0:
                # A prior attempt did NOT achieve the step: RE-PERCEIVE the (now-changed) page and RE-PICK
                # with feedback (§5.1). Fresh inventory so the correction grounds in the current controls.
                await await_settled(page)
                raw = await snapshot_interactive(page)
                inv = rank_inventory(raw, step["text"], k=INVENTORY_K)
                inv_text = inventory_text(inv)
                observed_paths |= {_norm_path(e["href"]) for e in raw if e.get("href")}
                rec.events.clear()
                url_before = page.url
                pick = complete_json(
                    ACT_PICK_SYSTEM,
                    f'Step: "{step["text"]}"\n\nINVENTORY:\n{inv_text}\n\nA PREVIOUS ATTEMPT DID NOT '
                    f'ACHIEVE THIS STEP: {feedback}\nPick a DIFFERENT element/action that performs it.',
                    **({"model": model} if model else {}))
                verb, value = pick.get("verb"), pick.get("value")

            # ACT_PICK could not ground this in-page act in the current view. Before conceding, ACTIVELY
            # SEEK (RC3): scroll + reveal collapsed sections and re-perceive/re-pick — the control may be
            # below the perception window or inside an accordion. Only if seeking also fails do we concede.
            if not _groundable(pick, inv):
                pick, inv = await _seek_target(page, step, inventory_text, model)
                if pick is not None:
                    verb, value = pick["verb"], pick.get("value")
                    rec.events.clear()                   # drop any network a seek scroll may have triggered,
                    #                                      so only THIS act's requests shape the postcondition
            # RC4: NEVER a silent skip. An ungroundable pick is recorded ONCE after the loop (auditable,
            # surfaced on freeze) and FAILS LOUD when it is the flow's declared IRREVERSIBLE action.
            if pick is None or not _groundable(pick, inv):
                skip_reason = (f"ACT_PICK could not ground it after active seeking "
                               f"(verb={verb!r}, {len(inv)} in inventory)")
                break
            ref = pick["ref"]
            desc = inv[ref]
            # SAVE-vs-STATE-TRANSITION (general, target-agnostic). A "save/persist the current form" step
            # on an auto-persisting form has no explicit save control, so ACT_PICK grounds it to the
            # page's primary action instead — and when that action is a LIFECYCLE TRANSITION (start /
            # launch / activate / publish / enable / go-live), clicking it changes the entity's STATE.
            # That silently breaks a later step which needs the original state — e.g. a delete many apps
            # FORBID on an active entity (the delete then finds no confirm dialog, mis-grounds, and the
            # flow reds). A "save" is a non-destructive over-plan on such a form, so RECORD-and-DROP it
            # (RC4) rather than execute a state change the step never asked for. Gated tightly: a plain
            # click whose intent carries a persist word but NO transition word, grounded to a control
            # LABELLED with a transition word — so a flow that explicitly says "start/activate/publish"
            # still acts, and a real "Save" button (labelled "Save") is never dropped.
            if verb == "click" and not scaffold_flagged:
                _intent_l = (step["text"] or "").lower()
                _label_l = ((desc.get("text") or desc.get("name") or "")).lower()
                _PERSIST = ("save", "persist")
                _TRANSITION = ("start", "launch", "activate", "publish", "go live", "enable", "resume")
                if (any(w in _intent_l for w in _PERSIST)
                        and any(t in _label_l for t in _TRANSITION)
                        and not any(t in _intent_l for t in _TRANSITION)):
                    skip_reason = (f"a 'save/persist' step grounded to a state-transition control "
                                   f"(labelled {_label_l!r}) — on an auto-persisting form there is no "
                                   f"explicit save and clicking it would change entity state; dropped as "
                                   f"a non-destructive over-plan")
                    skip_kind = "over_plan"      # deliberate, safe reduction — not a hollow-green drop
                    break
            # A value picker (combobox / listbox / native <select>) is chosen by OPENING it and picking
            # the option by visible text (actions._select_option) — never a bare click on the trigger or
            # on a non-unique <option>. Coerce by ROLE in code, NOT via a shared ACT_PICK prompt rule
            # (which the LLM over-applied to radios/menus and rippled P3/P5). Role-gated → radio/toggle/
            # menu/button flows are untouched; golden CRUD (no combobox) is unaffected.
            if verb in ("click", "select") and (
                    desc.get("role") in ("combobox", "listbox") or desc.get("tag") == "select"):
                verb = "select"                          # only a click/select is a pick; a fill on an
                #                                          editable/type-to-filter combobox stays a fill
            # A select MUST carry the option's text: a coerced click has value=null (clicks carry none),
            # and an empty value must never reach _select_option — `name=""` substring-matches EVERY
            # option, so it silently commits the first/default one (screenshot-proven: the wrong default
            # then fails the flow downstream). Recover it deterministically from the step's own quoted
            # literal (the scaffold pins names in quotes); a select still without a value is ungroundable.
            if verb == "select" and not (value or "").strip():
                m = (re.search(r'"([^"]+)"', step["text"]) or re.search(r'“([^”]+)”', step["text"])
                     or re.search(r"'([^']+)'", step["text"]) or re.search(r'‘([^’]+)’', step["text"]))
                value = m.group(1).strip() if m else None
                if not value:
                    skip_reason = ("select without an option value (ACT_PICK returned none and the "
                                   "step text names no quoted option)")
                    break
            target = _target_from_descriptor(desc)
            # P1 (A2 row-relative, item-keyed — code-level, cannot ripple): an icon-only row-action
            # control (a "…" / kebab menu button) shares its role+name with the identical control in
            # every other row, and rowFor's first-line label can miss the row's true identity (a leading
            # badge / checkbox / date). When such a control sits in a detected row, scope the target to
            # the row bearing the flow's OWN created item_name — the definitive row identifier the flow
            # just created and searched for. Gated to icon-only controls (no own visible text), so a
            # text-labelled control is never mis-scoped; item-keyed, so a flow not acting on it is safe.
            if (desc.get("row") or {}).get("label") and item_name and not desc.get("text"):
                target["within"] = {"row_label": item_name}
            if _DEBUG:
                print(f"[author] act {step['text']!r} turn={_turn} verb={verb} ref={ref} desc="
                      f"testid={desc.get('testid')} role={desc.get('role')} text={desc.get('text')!r} "
                      f"row={desc.get('row')} -> target={target}", flush=True)
            # FIX 2 (validate-before-commit — the act twin of _ground_scope for asserts): a row-scoped act
            # is DETERMINISTIC only when its scope isolates EXACTLY one row. 0 rows (the target row has
            # not rendered yet — a search/filter still settling) gets one bounded settle+recheck; a
            # persistent 0, or any >1 (ambiguous among stale/sibling rows), means we must NOT guess.
            within = target.get("within")
            n_rows = await _row_scope_count(page, within)
            if n_rows == 0:
                await page.wait_for_timeout(_SEEK_SETTLE_MS)   # let a search/filter re-render settle, then re-look
                n_rows = await _row_scope_count(page, within)
            try:
                if n_rows is not None and n_rows != 1:
                    raise LocatorUnresolved(f"row scope {within!r} matched {n_rows} row(s), need exactly 1")
                # FIX 1: resolution is the SECOND grounding gate (resolve() enforces count()==1, ANTI
                # D-8). A miss here — the picked control re-rendered away, or its role+text locator is
                # ambiguous among stale rows — gets the SAME RC4 discipline as an ungrounded pick.
                locator, _ = await resolve(page, derive_rungs(_target_ns(target)), 10000, _scope_ns(target))
            except (LocatorUnresolved, LocatorAmbiguous) as e:
                skip_reason = f"the picked control did not resolve to a unique live element ({e})"
                break
            if scaffold_flagged:
                # author-lane D15 (§6.4, "binds all lanes"): validate the scaffold's destructive guess is
                # on-token BEFORE acting. A token-less precursor the scaffold mis-flagged (a menu-open)
                # must not abort authoring — the mutation signal below binds the real control for guard.
                try:
                    await guard(locator, name_token)
                except PolicyStop:
                    pass

            if verb == "fill":
                await locator.first.fill(value or "")
            elif verb == "select":
                await actions._select_option(page, locator, value or "")
            elif verb == "hover":
                await locator.first.hover()
            elif verb == "keypress":
                await locator.first.press(value or "Enter")
            else:
                await actions.robust_click(locator)
            # settle instead of a flat 400ms: an act that triggers a client route change / async list
            # refresh / modal open needs the DOM (and any url change) to land before we synthesize the
            # postcondition and before the next step perceives. No-op once the page is already stable.
            await await_settled(page)
            act_net = [e for e in rec.events]    # provisional snapshot (fast case). A committing mutation
            #   can respond, and its client route change land, a beat LATE — after await_settled returns
            #   in the post-click quiet gap (observed live: two create POSTs ~3.7s apart, the route change
            #   trailing them). So for a COMMITTING VERB, wait (bounded, network-idle-aware) for the
            #   transition, then RE-SNAPSHOT act_net — otherwise a slow create is misread as non-committing
            #   (act_net still empty) and wrongly enters the verify/re-pick loop, or the next step
            #   perceives the STILL-MOUNTED prior view (its target absent). await_transition returns
            #   immediately on a URL change and early-exits on sustained network idle, so an in-place
            #   commit (a delete removing a row, a menu open) does NOT pay the whole window.
            navigated = False
            if verb in _COMMIT_VERBS:
                navigated = await await_transition(page, url_before)
                if navigated:
                    await await_settled(page)
                act_net = [e for e in rec.events]    # re-snapshot: a slow commit POST landed during the wait
            # toggle EFFECT (GAP 2 close-out): if THIS act is a click on a genuine toggle/switch/radio,
            # capture its post-act checked-state for the effect oracle. Strict detection (never data-state),
            # so a tab/accordion/popover click yields None and gets no `checked` oracle. Read on the winning
            # attempt; a non-toggle click leaves it None.
            act_toggle_state = None
            if verb == "click":
                try:
                    act_toggle_state = await locator.first.evaluate(_TOGGLE_STATE_JS)
                except Exception:
                    act_toggle_state = None
            if _DEBUG:
                print(f"[author] post-act s{sid} turn={_turn} {verb} -> url={page.url} "
                      f"(navigated={navigated}, net={len(act_net)})", flush=True)
                if os.getenv("ANNEAL_AUTHOR_SHOTDIR"):
                    try:
                        await page.screenshot(path=f"{os.environ['ANNEAL_AUTHOR_SHOTDIR']}/act_{sid}_{_turn}_{verb}.png")
                    except Exception:
                        pass

            # ── POST-ACT SELF-VERIFY (§5.1): did THIS act achieve the step's intent? ──
            # SINGLE-PASS (no verify, no re-fire) when the act already did something MATERIAL that must
            # not be repeated: a destructive act (never re-fire an irreversible commit), any act that
            # COMMITTED a mutation (a non-GET fired — `act_net`), OR any act that NAVIGATED (URL changed).
            # Re-firing a committed/navigating act risks a DUPLICATE mutation, and its transition makes an
            # immediate re-perception judge the STALE prior view → a false-negative that re-picks the
            # wrong control (observed live: a slow create-submit was re-picked to a state-transition
            # button). Gating on `act_net OR navigated` (not act_net alone) closes the timing hole where a
            # slow commit POST hadn't been captured yet. Only a NON-mutating, NON-navigating act (an inert
            # click, a fill, a menu/tab open) is safe to verify-and-re-pick — and that IS the witness
            # class (the concurrency mis-ground is an inert tab/info click; its target is a fill), so the
            # self-correction the loop was built for is preserved.
            if scaffold_flagged or (verb in _COMMIT_VERBS and act_net) or navigated:
                committed = True
                break
            satisfied, post_state, feedback = await _verify_act(
                page, step, verb, value, desc, locator, url_before, model)
            if satisfied:
                committed = True
                break
            post_state = None                            # a rejected attempt yields no oracle post-state
            if _turn + 1 >= max_turns:
                skip_reason = (f"the act did not achieve the step's intent after {max_turns} "
                               f"attempt(s): {feedback}")
                break
            # else: loop → re-perceive + re-pick with `feedback`
        if not committed:
            _record_ungrounded(step, skip_reason or "the act could not be completed",
                               scaffold_flagged, skipped_steps, test_id, kind=skip_kind)
            continue

        # ── the verified (or destructive single-pass) act → step + postconditions (synthesised ONCE, on
        #    the winning attempt) ──
        step_obj = {"id": f"s{sid}", "intent": step["text"], "action": verb, "target": target}
        if value is not None and verb in ("fill", "select", "keypress"):
            step_obj["arg"] = value

        # ── postcondition synthesis (§5.2): network (arm-before) + url change; effect oracle ──
        # Only a COMMIT verb may CLAIM a captured mutation (see `_COMMIT_VERBS`) — otherwise a fill
        # inherits a network postcondition it cannot reproduce on replay (the mutation already fired) and,
        # worse, the destructive flag on a token-less field. On an optimistic-UI app the commit's response
        # can arrive a beat LATE, landing in the NEXT step's capture window (observed live: a delete's
        # DELETE response arriving while the following 'search' fill runs). `act_net` is THIS act's
        # snapshot (captured before the transition wait), so a trailing auto-save cannot displace it.
        conds: list[dict] = []
        net = act_net
        network_log.extend(net)
        if net and verb in _COMMIT_VERBS:
            mutating = True
            e = net[-1]
            conds.append({"kind": "network", "method": e["method"], "path": _templatize_path(e["path"]),
                          "status": _status_class(e["status"])})
        if page.url != url_before:
            conds.append({"kind": "url", "match": _url_regex(urlsplit(page.url).path)})
        # EFFECT oracle (Prong A seam), GROUNDED in the self-verify's observed post-state (§6): `post_state`
        # carries what a fill actually redisplayed (exact-match on replay; None when the app doesn't
        # redisplay → no value_is that would red every replay, D26). None post_state (select/click) → the
        # seam falls back to its structural logic (e.g. a select's option-visible oracle).
        # a click's effect is the observed toggle checked-state (act_toggle_state); a fill/select feeds
        # its own post_state. Non-toggle clicks leave act_toggle_state None → no `checked` oracle.
        eff_post = {"checked": act_toggle_state} if (verb == "click" and act_toggle_state is not None) else post_state
        eff = synthesize_effect_oracle(verb, value, target, post_state=eff_post)
        if eff is not None:
            conds.append(eff)

        # ── identify the irreversible destructive control (§6.4/D15) ──
        # The step that ACTUALLY performs the destructive action is a COMMIT interaction that fires a
        # MUTATION — a non-GET request (`_NetRec` records ONLY non-GET, so a non-empty `net` == a
        # mutation fired). A menu-open, a type-to-confirm fill that precedes it, or a search fill that
        # merely CAPTURED the delete's late-arriving response (async/optimistic UI) must never claim the
        # flag — hence the same commit-verb gate as the network postcondition above. Verb- AND
        # target-agnostic (a precursor / trailing field can no longer steal the flag).
        if destructive_intent:
            if net and verb in _COMMIT_VERBS:
                destructive_step = step_obj              # committed the mutation → the destructive control
            if scaffold_flagged:
                flagged_act_step = step_obj              # the scaffold's own declared destructive act
            last_act_step = step_obj                     # fail-closed fallback if no mutation is observed
        if conds:
            step_obj["postcondition"] = {"all": conds, "budget_ms": 15000}
        bundle_steps.append(step_obj)
        sid += 1

    page.remove_listener("response", rec.on_response)

    if destructive_intent:
        # bind the flag to the terminal irreversible action (the mutating act); when no mutation was
        # observed (an app that deletes via GET / a beacon / an off-host API is invisible to _NetRec),
        # fall back to the SCAFFOLD'S OWN flagged act — the step the flow declared destructive — before
        # the last-act fail-closed catch-all. The last act of a CRUD flow is typically a token-less
        # verify (a search/filter re-run), so binding there PolicyStops every replay on a control that
        # deletes nothing; the flagged act is the confirm control itself (token-bearing → guard passes).
        # Still fail-closed: some act always carries the flag while the intent is active.
        chosen = destructive_step or flagged_act_step or last_act_step
        if chosen is not None:
            chosen["destructive"] = True
            mutating = True

    bundle = {
        "schema_version": 1, "project_id": project_id, "test_id": test_id, "version": 1,
        "state": "draft", "persona": persona, "params": {},
        "lineage": {"parent": None, "reason": "authored", "ruling": None, "incident": None,
                    "triggered_by": "hot_author", "verified_against": target_key},
        "intent": {"sources": [{"kind": "nl", "verbatim": nl}], "descriptor": title},
        "policy": {"exclusive": False, "mutating": mutating},
        "steps": bundle_steps,
    }
    bundle = _volatile_ize(bundle, name_token)
    return AuthorResult(bundle=bundle, network_log=network_log, territory=territory,
                        skipped_steps=skipped_steps)


def _volatile_ize(obj, token: str):
    """Replace the concrete authoring token with the {{run.token}} placeholder everywhere (B1/D5)."""
    if isinstance(obj, str):
        return obj.replace(token, "{{run.token}}")
    if isinstance(obj, dict):
        return {k: _volatile_ize(v, token) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_volatile_ize(v, token) for v in obj]
    return obj
