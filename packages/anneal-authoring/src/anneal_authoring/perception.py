"""Observe the LIVE rendered page's interactive elements.

This is observation of the running app (the same thing a human sees) — NOT source. It feeds the
NL→plan loop so the LLM picks targets that actually exist on screen (closed-vocab over what's rendered:
the LLM chooses a `ref` from this inventory, it never free-generates a selector — B3/anti-hallucination).
"""
from __future__ import annotations

_JS = r"""() => {
  function anchorFor(el) {
    const own = (el.innerText || el.value || '').trim();
    const named = el.getAttribute('aria-label') || el.getAttribute('data-testid') || el.getAttribute('placeholder');
    if (own || named) return null;
    let a = el.parentElement;
    while (a && (a.innerText || '').trim().length === 0) a = a.parentElement;
    if (!a) return null;
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const peers = [...a.querySelectorAll('[role="' + role + '"], ' + role)];
    const nth = Math.max(0, peers.indexOf(el));
    return {
      anchor_text: (a.innerText || '').trim().split('\n')[0].slice(0, 80),
      container_tag: a.tagName.toLowerCase(),
      role: role,
      nth: nth,
    };
  }

  // nearest enclosing LANDMARK (ARIA region semantics — a web standard, never an app class/noun): the
  // same entity rendered in two regions (a nav entry AND a content card) is unique WITHIN its region.
  const LANDMARK_SEL = 'nav,main,aside,dialog,[role=navigation],[role=main],[role=complementary],[role=dialog],[role=region],[role=banner],[role=contentinfo]';
  const LANDMARK_ROLE = {nav: 'navigation', main: 'main', aside: 'complementary', dialog: 'dialog'};
  function landmarkFor(el) {
    const lm = el.closest(LANDMARK_SEL);
    if (!lm) return null;
    return lm.getAttribute('role') || LANDMARK_ROLE[lm.tagName.toLowerCase()] || null;
  }

  const ROW_SEL = 'tr,[role=row],li';
  function rowFor(el) {
    let r = el.closest(ROW_SEL);
    if (!r) return null;
    const txt = (r.innerText || '').trim();
    if (!txt) return null;
    const label = txt.split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
    if (!label || label.length > 80) return null;
    return { container_tag: r.tagName.toLowerCase(), label: label };
  }

  const BASE_SEL = 'a,button,input,textarea,select,[role=button],[role=combobox],[role=option],[role=link],[role=tab],[role=menuitem],[role=switch],[role=checkbox],[role=radio]';

  function clickableTestidLeaf(el) {
    if (el.matches(BASE_SEL)) return false;
    if (!el.getAttribute('data-testid')) return false;
    if (el.querySelector(BASE_SEL)) return false;
    let cs; try { cs = getComputedStyle(el); } catch (e) { return false; }
    return cs && cs.cursor === 'pointer';
  }

  function clickableRow(el) {
    if (el.matches(BASE_SEL)) return false;
    const tag = el.tagName.toLowerCase();
    if (!(tag === 'div' || tag === 'li' || tag === 'tr' || el.getAttribute('role') === 'row')) return false;
    let cs; try { cs = getComputedStyle(el); } catch (e) { return false; }
    if (!cs || cs.cursor !== 'pointer') return false;
    const label = (el.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
    if (!label || label.length > 60) return false;
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      let acs; try { acs = getComputedStyle(a); } catch (e) { break; }
      if (acs && acs.cursor === 'pointer') return false;
    }
    return true;
  }

  function labelledByText(el) {
    // resolve aria-labelledby id refs to their text — a standard accessible-name source many React
    // forms use in place of a <label for>, so a control that looks unnamed still yields a role+name rung
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (!ids.length) return null;
    const t = ids.map(id => { const n = document.getElementById(id); return n ? (n.innerText || '').trim() : ''; })
                 .filter(Boolean).join(' ').trim();
    return t || null;
  }

  function describe(el, idx) {
    const r = el.getBoundingClientRect();
    const txt = (el.innerText || el.value || '').trim().slice(0, 60);
    return {
      ref: idx,
      tag: el.tagName.toLowerCase(),
      testid: el.getAttribute('data-testid') || null,
      role: el.getAttribute('role') || null,
      type: el.getAttribute('type') || null,
      // Is this an actionable LEAF (a semantic/ARIA control, or a clickable testid-bearing leaf an app
      // ships instead of a <button> — e.g. a <div data-testid> menu item/card), as opposed to a
      // row/container that merely groups cells for scope? Emitted so the RANKER's control-first tiebreak
      // uses perception's OWN classification instead of re-deriving actionability from role/tag alone
      // (which misses testid-leaf controls). clickableRow / data rows are containers -> leaf=false.
      leaf: el.matches(BASE_SEL) || clickableTestidLeaf(el),
      // accessible name, richest source first: aria-label → associated <label> → aria-labelledby →
      // title. The extra sources only fire when the earlier ones are empty, so a control that already
      // had a name is unchanged; a previously-nameless live control gains one → D28 builds role+name
      // instead of a bare, non-unique `role` rung (PL-3).
      name: el.getAttribute('aria-label')
            || (el.labels && el.labels[0] && el.labels[0].innerText.trim())
            || labelledByText(el)
            || el.getAttribute('title')
            || null,
      placeholder: el.getAttribute('placeholder') || null,
      href: el.getAttribute('href') || null,
      text: txt || null,
      bbox: {x: r.x, y: r.y, w: r.width, h: r.height},
      anchor: anchorFor(el),
      row: rowFor(el),
      landmark: landmarkFor(el),
    };
  }

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return false;
    if (r.bottom < 0 || r.top > (window.innerHeight + 6000)) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.opacity === '0' || cs.pointerEvents === 'none') return false;
    return true;
  }

  function collect(root, seen, out, cap, rowsOnly) {
    for (const el of root.querySelectorAll(BASE_SEL + ',[data-testid],tr,li,[role=row]')) {
      if (out.length >= cap) break;
      if (seen.has(el)) continue;
      if (!el.matches(BASE_SEL) && !clickableTestidLeaf(el) && !clickableRow(el)) continue;
      if (!visible(el)) continue;
      const isRow = !!rowFor(el);
      if (isRow !== rowsOnly) continue;
      seen.add(el);
      out.push(describe(el, out.length));
    }
  }

  const CAP = 400;   // describe ALL visible candidates — relevance ranking + the LLM-facing budget
  //                    are applied in Python (rank_inventory); a low DOM-order cap here was the
  //                    primary blindness (targets past #70 on a dense page were never described).
  const out = [];
  const seen = new Set();
  const OVERLAY_SEL = '[role=dialog],[role=menu],[role=listbox],[data-radix-popper-content-wrapper],[data-state=open]';
  const overlays = [...document.querySelectorAll(OVERLAY_SEL)].filter(visible);
  const top = overlays.length ? overlays[overlays.length - 1] : null;
  if (top) collect(top, seen, out, CAP, false);
  collect(document, seen, out, CAP, false);
  if (top) collect(top, seen, out, CAP, true);
  collect(document, seen, out, CAP, true);
  return out;
}"""


async def snapshot_interactive(page) -> list[dict]:
    try:
        return await page.evaluate(_JS)
    except Exception:
        return []


# Visible STATIC text an oracle may key on — headings and testid-bearing text leaves — which the
# interactive inventory never lists (nothing to click). Same descriptor shape as `describe`, so the
# ranker, the picker and `_target_from_descriptor` treat them like any observed element.
_ANCHORS_JS = r"""() => {
  const SEL = 'h1,h2,h3,h4,h5,h6,[role=heading],[data-testid]';
  const CTRL = 'a,button,input,textarea,select,[role=button],[role=link],[role=combobox],[role=option],[role=tab],[role=menuitem]';
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (el.matches(CTRL) || el.closest(CTRL) || el.querySelector(CTRL)) continue;   // controls: the interactive inventory's
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const txt = (el.innerText || '').trim().slice(0, 80);
    if (!txt) continue;
    const heading = /^h[1-6]$/i.test(el.tagName) || el.getAttribute('role') === 'heading';
    out.push({ref: out.length, tag: el.tagName.toLowerCase(), testid: el.getAttribute('data-testid') || null,
              role: heading ? 'heading' : null, type: null, leaf: false, name: null, placeholder: null,
              href: null, text: txt, row: null, landmark: null});
    if (out.length >= 80) break;
  }
  return out;
}"""


async def snapshot_text_anchors(page) -> list[dict]:
    """A 'verify X renders' oracle on a page with nothing to click must still pick from OBSERVED text
    (B3 for oracles), never invent it from the step wording — so the assert picker also sees the page's
    visible headings and testid-bearing text leaves."""
    try:
        return await page.evaluate(_ANCHORS_JS)
    except Exception:
        return []


import math
import re as _re

# LLM-facing inventory budget: how many (ranked) candidates ACT_PICK sees per step. Larger than the
# old flat cap (70) for headroom; ranking guarantees the step's target is near the top, not truncated.
INVENTORY_K = 120

# Generic action/filler words carry no target signal — dropping them keeps ranking focused on the
# nouns/identifiers a step names. STRICTLY generic web-interaction vocabulary (never domain nouns), so
# this stays target-agnostic. Kept deliberately small: only words that appear in almost any step.
_STOPWORDS = frozenset("""
a an the this that these those and or of to in on at into onto from for with by is are be it its
click open close set select choose pick type enter fill press check verify confirm ensure make sure
go navigate then next new any some all each its it up down back button link field input box menu
item option list row page tab section panel form dialog popup modal icon value text label name here
""".split())


def _tokens(s: str) -> set[str]:
    return {t for t in _re.split(r"[^a-z0-9]+", (s or "").lower()) if t and t not in _STOPWORDS}


# An act (fill/select/click/hover/keypress) operates on an INTERACTIVE CONTROL. A data container — a
# table row / list item that merely GROUPS cells — is in the inventory ONLY to provide row scope for a
# control nested inside it (A2 row-relative); it is never itself the act target. But its aggregated cell
# text lets it match any step naming a value the row DISPLAYS (a list row shows the very option value
# the step selects), so a pure text-overlap rank floods the top with rows and BURIES the real control —
# the act then grounds to a row and the control keeps its default (screenshot-proven: wrong value stuck).
# Rank genuine controls ahead of non-actionable containers so the step's target surfaces. Keyed on
# generic ARIA roles / element tags, never an app noun (target-agnostic).
_CONTROL_ROLES = frozenset({"button", "combobox", "listbox", "option", "link", "tab", "menuitem",
                            "switch", "checkbox", "radio", "textbox", "searchbox", "slider", "spinbutton"})
_CONTROL_TAGS = frozenset({"a", "button", "input", "textarea", "select"})


def _is_control(el: dict) -> bool:
    """True for an actionable LEAF (an act target), False for a data container (a row grouping cells,
    present only for scope). Uses perception's OWN leaf classification (`describe` emits `leaf` = a
    semantic/ARIA control OR a clickable testid-bearing leaf), so the ranker and the observer never
    disagree — the earlier role/tag-only test missed a `<div data-testid>` menu item/card an app ships
    instead of a `<button>`, demoting it below every real control. Falls back to the role/tag test for
    any descriptor lacking the flag (a non-perception caller / an older capture)."""
    leaf = el.get("leaf")
    if leaf is not None:
        return bool(leaf)
    return el.get("role") in _CONTROL_ROLES or el.get("tag") in _CONTROL_TAGS


def _score_fields(el: dict) -> list[tuple[int, str | None]]:
    """(weight, text) per identifying field, richest signal first. A control's OWN identity — its
    accessible name — carries the most; its testid/placeholder next; free text least. The `row`
    (the container it sits in) is deliberately weighted LOW (== free text): it is context for SCOPE,
    not the control's identity, so a row-action control does not inherit its whole row's descriptive
    text as strong relevance (that flooded N identical row controls above the one control a step names).
    A genuinely row-DISCRIMINATING step still surfaces the right row's control because IDF lifts a rare
    row-name token regardless of this weight."""
    row = (el.get("row") or {}).get("label")
    anchor = (el.get("anchor") or {}).get("anchor_text")
    return [
        (3, el.get("name")), (1, row), (2, anchor), (2, el.get("placeholder")),
        (2, (el.get("testid") or "").replace("-", " ").replace("_", " ")),
        (1, el.get("text")), (1, el.get("role")), (1, el.get("type")),
    ]


def _score(el: dict, q: set[str], idf: dict[str, float] | None = None) -> float:
    """Weighted token overlap between the step query and an element's identifying fields, each matched
    token scaled by its inverse document frequency (`idf`) across the observed inventory. A token that
    occurs in almost every element (the page's ubiquitous domain noun — e.g. "agent" on an agents page)
    is near-zero signal and must not out-vote the token that actually discriminates the target; a rare
    token dominates. `idf=None` (no distribution supplied) falls back to uniform weight — identical to
    the original overlap count — so any direct caller is unchanged. Target-agnostic: the distribution is
    computed from THIS page's own tokens, never a hardcoded word list."""
    if not q:
        return 0.0
    total = 0.0
    for weight, raw in _score_fields(el):
        ftok = _tokens(raw if isinstance(raw, str) else "")
        for t in (q & ftok):
            total += weight * (idf.get(t, 1.0) if idf else 1.0)
    return total


def _idf(elements: list[dict]) -> dict[str, float]:
    """Inverse document frequency of each token across the inventory: idf(t) = log((N+1)/(df(t)+1)) + 1,
    where df(t) is the number of elements whose identifying fields contain t. A token in nearly every
    element (df→N) tends to ~1 (near-floor); a rare token is large. Computed from the SAME fields
    `_score` reads, so weighting is consistent. Target-agnostic — purely the observed distribution."""
    n = len(elements)
    df: dict[str, int] = {}
    for el in elements:
        toks: set[str] = set()
        for _, raw in _score_fields(el):
            toks |= _tokens(raw if isinstance(raw, str) else "")
        for t in toks:
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}


def rank_inventory(elements: list[dict], query: str, k: int = INVENTORY_K) -> list[dict]:
    """Order `elements` by relevance to `query` (the step's NL text) and keep the top `k`, re-assigning
    `ref` to the new 0..k-1 positions (callers index `inv[ref]`). A stable sort keeps DOM order among
    equal scores, so with an empty/zero-signal query this degrades to the original first-k-in-DOM-order
    behaviour — small pages (all elements fit) are unchanged. This is what stops a dense page's target
    from being truncated away: any element the step actually names sorts ahead of the noise.

    With a real query, actionable controls sort ahead of non-actionable data containers (see
    `_is_control`) BEFORE the text score — so a control the step must operate can't be buried under
    data rows that merely display the step's target value. The text score is IDF-weighted over the
    page's OWN token distribution, so N identical row-action controls matching only the ubiquitous
    domain noun cannot out-rank the one control matching the step's discriminating token. Zero-signal
    queries keep pure DOM order."""
    q = _tokens(query)
    if not q:
        ranked = sorted(enumerate(elements), key=lambda p: p[0])          # DOM order (unchanged)
    else:
        idf = _idf(elements)
        ranked = sorted(enumerate(elements),
                        key=lambda p: (0 if _is_control(p[1]) else 1, -_score(p[1], q, idf), p[0]))
    out = [dict(el, ref=i) for i, (_, el) in enumerate(ranked[:k])]
    return out


def inventory_text(elements: list[dict]) -> str:
    """A compact, closed-vocab rendering of the inventory for the LLM to pick a `ref` from."""
    lines = []
    for e in elements:
        parts = [f"#{e['ref']}", e["tag"]]
        if e.get("testid"):
            parts.append(f"testid={e['testid']}")
        if e.get("role"):
            parts.append(f"role={e['role']}")
        if e.get("type"):
            parts.append(f"type={e['type']}")
        if e.get("name"):
            parts.append(f"name={e['name']!r}")
        if e.get("placeholder"):
            parts.append(f"ph={e['placeholder']!r}")
        if e.get("text"):
            parts.append(f"text={e['text']!r}")
        if e.get("row"):
            parts.append(f"row={e['row']['label']!r}")
        anchor = e.get("anchor") or {}
        if anchor.get("anchor_text") and not e.get("name") and not e.get("text"):
            parts.append(f"near={anchor['anchor_text']!r}")   # nearby label for an otherwise-anonymous control
        if e.get("href"):
            parts.append(f"href={e['href']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)
