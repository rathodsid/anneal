"""SPA-readiness gate (target-agnostic) — the one primitive that makes perceive/classify tolerant of
a client-rendered app.

`page.goto(..., wait_until="domcontentloaded")` resolves on the initial HTML document. For a hydrating
SPA (React/Vue + client-side auth/routing) that document is a near-empty shell: the interactive DOM
appears — and a client-side redirect may change the URL — hundreds of ms to several seconds later
(observed on a real target: 0 → 18 → 22 → 226 controls over ~1.3s, with a "/"→"/app" redirect
mid-ramp). Anything that snapshots or classifies at domcontentloaded races that ramp and reads an
empty/partial page.

`await_settled` combines two signals, bounded by a deadline:

  1. **network quiescence (bounded `networkidle`)** — the primary signal. A hydrating app renders in
     stages: shell → nav → then it *fetches* the heavy content (lists, tables) over the network. A DOM
     count can plateau between stages (observed: 22 controls for ~300ms, then 226 once the row fetch
     lands), so a count-only gate fires early on a partial page. Waiting for the network to go idle
     first covers those async data loads. It is BOUNDED: an SPA that holds a websocket / polls analytics
     never idles, so `networkidle` is capped and we fall through — it is an accelerator, never load-
     bearing, and can't hang the caller.
  2. **interactive-count stability** — the confirmation. After the network settles (or the cap
     elapses), poll the observed interactive-element count until it is non-zero AND unchanged across
     consecutive samples with the URL stable, so an in-flight client redirect resets the wait.

Design choices:
  * Readiness is inferred from the DOM the engine will actually read plus real network state — no
    fixed sleeps, no app specifics.
  * Fast no-op on a server-rendered page: `networkidle` fires immediately (no XHR) and the count is
    already stable → returns after the confirming polls, negligible cost.
  * NEVER raises. On a page that genuinely never settles it returns at the deadline with whatever is
    there; the caller's own budget/oracle still governs the verdict. Readiness is best-effort.

Target-agnostic (Rule 2): counts observed interactive structure only — no routes, testids, or domain
nouns. Lives in anneal-executor and imports no LLM (D31), so both cold replay and the warm/hot lanes
(which depend on the executor) can share it.
"""
from __future__ import annotations

import asyncio
import time

# Broad "interactive-ish" surface — the elements a user (and the perceiver) can act on. Deliberately a
# superset of any one lane's inventory: this is a readiness proxy, not the perception vocabulary.
_COUNT_JS = r"""() => {
  const SEL = 'a,button,input,textarea,select,[role=button],[role=combobox],[role=option],'
    + '[role=link],[role=tab],[role=menuitem],[role=switch],[role=checkbox],[role=radio],[data-testid]';
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) continue;
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (!cs || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    n++;
  }
  return n;
}"""


async def _interactive_count(page) -> int:
    try:
        return await page.evaluate(_COUNT_JS)
    except Exception:
        # a navigation in flight / execution context torn down → treat as not-yet-ready, keep polling
        return -1


async def await_settled(page, *, deadline_ms: int = 10000, networkidle_ms: int = 7000,
                        stable_ticks: int = 2, poll_ms: int = 100, min_count: int = 1) -> None:
    """Block until the app has fetched-and-rendered its interactive DOM, or `deadline_ms` elapses.

    Phase 1: wait (bounded by `networkidle_ms`, and never past the overall deadline) for the network to
    go idle — catches staged/async content loads a DOM-count plateau would miss. Times out silently on
    an SPA that never idles. Phase 2: poll until the visible-interactive count is >= `min_count` and
    unchanged across `stable_ticks` consecutive polls with `page.url` stable (an in-flight client
    redirect resets the wait). Best-effort: never raises, returns at the deadline regardless.
    """
    deadline = time.monotonic() + deadline_ms / 1000

    # Phase 1 — bounded network quiescence (accelerator, not load-bearing).
    ni_budget = min(networkidle_ms, max(0, int((deadline - time.monotonic()) * 1000)))
    if ni_budget > 0:
        try:
            await page.wait_for_load_state("networkidle", timeout=ni_budget)
        except Exception:
            pass          # perpetual-socket / polling SPA never idles → fall through to count-stability

    # Phase 2 — confirm the interactive DOM is present and stable.
    last_count, last_url, stable = -1, None, 0
    while time.monotonic() < deadline:
        count = await _interactive_count(page)
        url = page.url
        if count >= min_count and count == last_count and url == last_url:
            stable += 1
            if stable >= stable_ticks:
                return
        else:
            stable = 0
        last_count, last_url = count, url
        await asyncio.sleep(poll_ms / 1000)


async def await_transition(page, prev_url: str, *, window_ms: int = 9000, poll_ms: int = 100,
                           idle_grace_ms: int = 1500) -> bool:
    """Wait (bounded) for a client-side view TRANSITION — a URL change away from `prev_url`.

    A committing action that fires a mutation navigates to a new view a beat AFTER the mutation, not
    synchronously with it — and on a real target that beat is VARIABLE: the commit may fire as several
    POSTs seconds apart (observed live: two create POSTs ~3.7s apart), with the client route change
    trailing them. Between the click and the route change the PRIOR view is still mounted, so
    `await_settled` alone can return on the stale view and the next step then perceives/acts on the OLD
    page (a fill landing on a stale field, or the destination's control simply absent). Poll for the URL
    to change so the caller can gate the next step on the destination.

    Returns True as soon as the URL changes. Returns False when NO transition is coming — detected early
    via NETWORK IDLE (an in-flight-request counter): once the page has been quiet for `idle_grace_ms`
    with the URL unchanged, no navigation is pending, so an in-place commit (a delete that removes a row,
    a menu open, a toast) falls through cheaply instead of paying the whole window. `window_ms` is the
    hard ceiling for a slow-but-certain navigation whose late POSTs keep the network busy (idle never
    trips). Never raises. Target-agnostic — URL + network-activity only, no route/testid/domain noun."""
    inflight = {"n": 0}

    def _req(_):
        inflight["n"] += 1

    def _done(_):
        inflight["n"] = max(0, inflight["n"] - 1)

    page.on("request", _req)
    page.on("requestfinished", _done)
    page.on("requestfailed", _done)
    try:
        deadline = time.monotonic() + window_ms / 1000
        idle_since = None
        while time.monotonic() < deadline:
            if page.url != prev_url:
                return True
            if inflight["n"] <= 0:                        # network quiet → maybe no transition coming
                if idle_since is None:
                    idle_since = time.monotonic()
                elif (time.monotonic() - idle_since) * 1000 >= idle_grace_ms:
                    return False                          # sustained idle, URL unchanged → no nav pending
            else:
                idle_since = None                         # a late POST is in flight → keep waiting for it
            await asyncio.sleep(poll_ms / 1000)
        return False
    finally:
        page.remove_listener("request", _req)
        page.remove_listener("requestfinished", _done)
        page.remove_listener("requestfailed", _done)
