"""Per-verb implementations — one impl per verb. No separate assert clause: the
`assert` verb dispatches through `assert_` here just like every other action (§6.1).

`robust_click` and the combobox choreography (`_pick_option`) encode two hard-won rules:
a click TimeoutError is an actionability problem (scroll/overlay), not an
app limit; a combobox click that ran ≠ a selection that stuck, so options are picked BY VISIBLE TEXT.
"""
from __future__ import annotations

from anneal_core.errors import PostconditionUnmet

from .resolve import resolve, wait_hidden
from .settle import await_settled
from .waits import eval_state

# :visible-scoped — a closed dropdown leaves its options in the DOM (hidden); without :visible a
# `.first` would match a stale hidden option and time out (render.py note, A8).
_OPTION = "[role=option]:visible, [cmdk-item]:visible, [data-testid=select-item]:visible"


async def robust_click(locator) -> None:
    """A click TimeoutError is almost always actionability (scroll container, overlay, pointer
    interception, or a text/icon leaf inside the control that carries the handler) — not an app limit.
    Scroll → click → then a BOUNDED synthetic click on the nearest clickable ancestor. The fallback
    runs no actionability wait, so a still-animating or transiently-covered target can't hang (the old
    unbounded `dispatch_event` blocked the full 30s default), and the ancestor hop lets a click that
    resolved to a leaf label reach the button/menuitem/link that actually handles it."""
    el = locator.first
    try:
        await el.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    try:
        await el.click(timeout=8000)
    except Exception:
        await el.evaluate(
            "e => (e.closest('button,[role=button],[role=menuitem],[role=option],a,[data-testid]') || e).click()",
            timeout=4000)


async def _pick_option(page, value: str):
    """Choose the dropdown option for `value` tolerantly: exact substring first, else the option
    whose first-line label is a case-insensitive substring of `value` (or vice-versa), best overlap
    wins. No blind first-pick (A4)."""
    opts = page.locator(_OPTION)
    exact = opts.filter(has_text=value)
    if await exact.count() > 0:
        return exact.first
    vlow = value.lower().strip()
    best, best_len = None, 0
    for i in range(await opts.count()):
        o = opts.nth(i)
        try:
            label = ((await o.inner_text()) or "").strip().split("\n")[0].strip().lower()
        except Exception:
            continue
        if label and (label in vlow or vlow in label) and len(label) > best_len:
            best, best_len = o, len(label)
    return best                                          # None when nothing matched (caller decides)


async def _type_to_filter(page, value: str) -> None:
    """Narrow a type-to-filter / searchable combobox to `value` before picking. A large or virtualized
    option list does NOT render every option up front (only the ones in view), so an option that needs
    typing/scrolling to appear can't be picked from the initial set — the combobox then commits a wrong
    default. Type the requested value into the popup's search input (or the focused control) to filter.
    General combobox behaviour, deterministic, no app-specific literal. Failures are swallowed — the pick
    still runs best-effort."""
    for sel in ("[cmdk-input]", "[role=combobox] input:visible", "input[role=combobox]:visible",
                "[role=listbox] input:visible", "[role=dialog] input:visible", "input:focus"):
        try:
            box = page.locator(sel)
            if await box.count() > 0 and await box.first.is_visible():
                await box.first.fill(value)
                await page.wait_for_timeout(350)
                return
        except Exception:
            continue
    try:
        await page.keyboard.type(value)                          # the open popup usually holds focus
        await page.wait_for_timeout(350)
    except Exception:
        pass


async def _select_option(page, locator, value: str) -> None:
    """Open a combobox and select the option for `value`, robust to type-to-filter / virtualized lists.
    A large picker renders only the in-view options, so the requested one may be absent until typed, and
    a missed pick lets the combobox commit a WRONG default (breaking every later step — e.g. a create
    that then fails validation). So: open → type to filter → pick by exact role+name (tolerant fallback)
    → VERIFY the trigger now shows the requested value, retry once if not. Target-agnostic; the query is
    the option text the flow asked for, never an app noun.

    A BLANK `value` is refused loudly: `get_by_role("option", name="")` substring-matches EVERY option,
    so an empty value silently commits the first/default option (a blind first-pick, forbidden by A4) and
    the `"" in shown` verify rubber-stamps it. There is no requested option to select — fail the step."""
    value = (value or "").strip()
    if not value:
        raise ValueError("select needs the requested option's text; an empty value would match every "
                         "option and silently commit the first/default one — refusing to blind-pick")
    for _ in range(2):
        await robust_click(locator)                              # open the combobox
        try:
            await page.locator(_OPTION).first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        await _type_to_filter(page, value)                       # ALWAYS narrow (a virtualized list may
        #                                                          not render the target until typed)
        opt = page.get_by_role("option", name=value)             # exact accessible-name match first
        if await opt.count() == 0:
            opt = await _pick_option(page, value)                # tolerant fallback (substring / overlap)
        try:
            if opt is not None and await opt.count() > 0:
                await opt.first.click(timeout=5000)              # PLAIN click (the proven path) — a
                #     robust_click ancestor-hop can land on the wrong option's row and commit a default
                await page.wait_for_timeout(250)
        except Exception:
            pass
        # VERIFY the selection committed — a trigger still showing its placeholder/old value means the
        # pick missed and the combobox would silently keep a wrong default. Retry once; only then give up.
        try:
            shown = ((await locator.first.inner_text()) or "").strip().lower()
        except Exception:
            return                                               # unreadable trigger → don't loop blindly
        if value.strip().lower() in shown:
            return


# ── verb dispatch (§6.1: actions.dispatch(step.action)(page, step, locator)) ─
async def do_goto(page, step, locator):
    await page.goto(step.arg, wait_until="domcontentloaded", timeout=30000)
    # SPA-readiness: domcontentloaded returns on an un-hydrated shell (and may precede a client-side
    # redirect). Settle before the goto's own url/element postcondition runs, so the wait — and the
    # NEXT step — perceive the real page, not an empty document. No-op on server-rendered targets.
    await await_settled(page)


async def do_click(page, step, locator):
    await robust_click(locator)


# The editable leaf inside a resolved target: an app commonly puts the step's testid on a WRAPPER around a
# rich editor (a CodeMirror/Lexical `[contenteditable]`), so the resolved locator is not itself typeable.
_EDITABLE_SEL = "[contenteditable=true], textarea, input"


async def _editable_leaf(root):
    """Return (node_to_type_into, is_rich). If the resolved element is itself an input/textarea/
    contentEditable, drive it directly; otherwise descend to the first editable node it WRAPS (the testid
    is often on a container of the editor). `is_rich` marks a contentEditable target — `.fill()` cannot
    dirty a rich editor, so it must be driven by real keystrokes. Generic DOM semantics, no app literal."""
    try:
        self_editable = await root.evaluate(
            "e => e.isContentEditable || e.tagName === 'INPUT' || e.tagName === 'TEXTAREA'")
    except Exception:
        self_editable = False
    target = root if self_editable else root.locator(_EDITABLE_SEL).first
    try:
        is_rich = await target.evaluate("e => !!e.isContentEditable")
    except Exception:
        is_rich = False
    return target, is_rich


async def _type_text(page, target, value: str) -> None:
    """Focus the editable node, select its existing content, and TYPE via real keystrokes — the event
    pipeline a rich editor (CodeMirror/Lexical) and keystroke-driven / masked inputs listen to, which a
    programmatic value-set (`.fill()`) bypasses (so it never dirties the framework's state). Mirrors the
    typing `_select_option` already relies on. `ControlOrMeta+a` selects all OS-agnostically."""
    await target.click()
    await target.press("ControlOrMeta+a")
    await target.press("Delete")
    await page.keyboard.type(value or "")


async def fill_value(page, locator, value: str) -> None:
    # A plain <input>/<textarea> takes the fast, reliable `.fill()` path (it fires the input event a
    # controlled React field needs) — unchanged, so existing input-based flows are unaffected. But a rich
    # contentEditable editor (CodeMirror/Lexical), or a wrapper the step's testid landed on, cannot be
    # driven by `.fill()`: it throws on a non-editable wrapper and does not dirty the editor even when it
    # doesn't throw. Route those — and any control `.fill()` refuses — through a real keystroke path.
    # Generic web semantics (contentEditable / input tag), no app- or platform-specific literal.
    el = locator.first
    target, is_rich = await _editable_leaf(el)
    if not is_rich:
        try:
            await el.fill(value)
            return
        except Exception:
            pass                                   # .fill() refused (masked / keystroke-only) → keystrokes
    await _type_text(page, target, value)


async def do_fill(page, step, locator):
    await fill_value(page, locator, step.arg)


async def do_select(page, step, locator):
    await _select_option(page, locator, step.arg)


async def do_keypress(page, step, locator):
    await locator.first.press(step.arg)


async def do_hover(page, step, locator):
    await locator.first.hover()


async def do_upload(page, step, locator):
    await locator.first.set_input_files(step.arg)


_DISPATCH = {
    "goto": do_goto, "click": do_click, "fill": do_fill, "select": do_select,
    "keypress": do_keypress, "hover": do_hover, "upload": do_upload,
}


def dispatch(action: str):
    return _DISPATCH[action]


async def assert_(page, spec, budget_ms, reload_on_miss):
    """The assert verb handler — dispatch by predicate kind (§2.5 / §6.2). Returns (ok, rung_used).
    `negative` is legal ONLY on value_is (CR-4 rejected it on a state predicate at compile), so the
    double negation is structurally unreachable here."""
    try:
        if spec.kind == "element" and spec.state == "hidden":
            return True, await wait_hidden(page, spec.rungs, budget_ms, spec.scope)          # 'gone'
        if spec.kind == "element" and spec.state == "visible":
            loc, rung = await resolve(page, spec.rungs, budget_ms, spec.scope)
            await loc.wait_for(state="visible", timeout=budget_ms)
            return True, rung
        if spec.kind == "value_is":
            loc, rung = await resolve(page, spec.rungs, budget_ms, spec.scope)
            raw = (await loc.input_value()) == spec.equals
            return (not raw if spec.negative else raw), rung
        if spec.kind in ("checked", "attribute", "text", "count"):
            return await eval_state(page, spec, budget_ms)                                 # C-1
    except (Exception,) as exc:  # noqa: BLE001
        # only reload-and-retry the ladder/postcondition misses; re-raise everything else
        from anneal_core.errors import LocatorAmbiguous, LocatorUnresolved
        if reload_on_miss and isinstance(exc, (LocatorUnresolved, LocatorAmbiguous, PostconditionUnmet)):
            await page.reload(wait_until="domcontentloaded")
            await await_settled(page)                 # let the SPA re-hydrate before the retry oracle
            return await assert_(page, spec, budget_ms, reload_on_miss=False)
        raise
    raise PostconditionUnmet(f"unhandled assert predicate kind {spec.kind!r}")
