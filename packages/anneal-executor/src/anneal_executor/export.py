"""`anneal export` (D31, §5.5): StepPlan → a standalone, runnable Playwright-Python file.

A verbatim prelude of the §6.2 `resolve()`/`wait_hidden()` functions, then one call per PlanStep.
Because both backends (interpreter + export) read the same StepPlan, the script is a faithful
transcript of cold replay — for reading and hand-running, never in the execution path. The resolver
is embedded VERBATIM via `inspect.getsource` (the anneal_core exception import is swapped for inlined
local classes so the file is self-contained).
"""
from __future__ import annotations

import inspect

from . import actions as _actions_mod
from . import resolve as _resolve_mod
from . import waits as _waits_mod

_INLINE_EXC = """\
class LocatorUnresolved(Exception): pass
class LocatorAmbiguous(Exception): pass
class PostconditionUnmet(Exception): pass
"""

_HELPER_HEAD = '''\
import re
from urllib.parse import urlsplit


class _Rung:
    __slots__ = ("kind", "value", "name", "priority")
    def __init__(self, kind, value, name=None):
        self.kind, self.value, self.name, self.priority = kind, value, name, 0


class _Scope:
    def __init__(self, testid=None, role=None, row_label=None):
        self.testid, self.role, self.row_label = testid, role, row_label


def _net(method, pattern, status_class):
    lo = int(status_class[0]) * 100
    def _pred(response):
        try:
            if response.request.method != method:
                return False
            if not re.fullmatch(pattern, urlsplit(response.url).path):
                return False
            return lo <= response.status < lo + 100
        except Exception:
            return False
    return _pred


_CAP = re.compile(r"\\{\\{capture\\.([a-z0-9_]+)\\}\\}")
def _subcap(s, caps):
    if not s or "{{capture." not in s:
        return s
    return _CAP.sub(lambda m: str(caps.get(m.group(1), m.group(0))), s)
'''


def _strip_module_imports(src: str) -> str:
    """Drop the vendored module's own package imports (they don't exist in the standalone file) and
    hoist away `from __future__` (it must be the first statement — an embedded mid-file future import
    is a SyntaxError). The functions we vendor don't reference those stripped names."""
    return "\n".join(
        ln for ln in src.splitlines()
        if not ln.startswith(("from anneal_core", "from .resolve", "from anneal_executor",
                              "from __future__"))
    )


def _actions_helpers() -> str:
    """robust_click / _pick_option / _type_to_filter / _select_option + _OPTION vendored VERBATIM from
    actions.py, so the exported combobox choreography can never skew from what the interpreter runs
    (§4 finding)."""
    parts = [f"_OPTION = {_actions_mod._OPTION!r}", f"_EDITABLE_SEL = {_actions_mod._EDITABLE_SEL!r}"]
    for fn in (_actions_mod.robust_click, _actions_mod._pick_option, _actions_mod._type_to_filter,
               _actions_mod._select_option, _actions_mod._editable_leaf, _actions_mod._type_text,
               _actions_mod.fill_value):
        parts.append(_strip_module_imports(inspect.getsource(fn)))
    return "\n\n\n".join(parts)


def _resolver_source() -> str:
    """The §6.2 resolver, vendored verbatim (minus package/future imports — see _strip_module_imports)."""
    return _strip_module_imports(inspect.getsource(_resolve_mod))


def _waits_helpers() -> str:
    """The state-predicate evaluator (checked/attribute/text/count) vendored VERBATIM from waits.py, plus
    a `_Spec` shim so the emitted script can call the SAME eval_state the interpreter runs (no skew).
    Placed AFTER the resolver source so _scope_root/_locator_for/resolve/time/asyncio are in scope."""
    parts = [
        "class _Spec:\n"
        "    def __init__(self, kind, rungs, scope=None, equals=None, attr_name=None, contains=False,"
        " count=None, checked=None):\n"
        "        self.kind, self.rungs, self.scope, self.equals = kind, rungs, scope, equals\n"
        "        self.attr_name, self.contains, self.count, self.checked = attr_name, contains, count, checked",
        f"_CHECKED_JS = {_waits_mod._CHECKED_JS!r}",
    ]
    for fn in (_waits_mod._norm, _waits_mod._eval_single, _waits_mod.eval_state):
        parts.append(_strip_module_imports(inspect.getsource(fn)))
    return "\n\n\n".join(parts)


def _capwrap(v) -> str:
    """Emit a value literal, wrapping it in a runtime `_subcap(..., _caps)` ONLY when it carries a
    {{capture.*}} marker. A capture-free value emits as a plain repr — so a plan with no captures
    exports byte-identically to before (the golden 'anneal export matches' check is unaffected)."""
    if isinstance(v, str) and "{{capture." in v:
        return f"_subcap({v!r}, _caps)"
    return repr(v)


def _capture_lines(s) -> list[str]:
    """Emit the post-step capture extraction (§ GAP-4), mirroring interpreter._do_capture."""
    if not getattr(s, "capture", None):
        return []
    cap = s.capture
    out = []
    if cap.source == "text":
        out.append(f"    _cl, _ = await resolve(page, {_rungs_lit(s.rungs)}, {s.budget_ms}, {_scope_lit(s.scope)})")
        out.append("    _src = (await _cl.inner_text()) or ''")
    else:
        out.append("    _src = page.url")
    out.append(f"    _m = re.search({cap.pattern!r}, _src)")
    out.append(f"    assert _m, 'capture {cap.name} did not match'")
    out.append(f"    _caps[{cap.name!r}] = _m.group(1) if _m.groups() else _m.group(0)")
    return out


def _rung_lit(r) -> str:
    return f"_Rung({r.kind!r}, {_capwrap(r.value)}, {_capwrap(r.name)})"


def _rungs_lit(rungs) -> str:
    return "[" + ", ".join(_rung_lit(r) for r in rungs) + "]"


def _scope_lit(sc) -> str:
    if sc is None:
        return "None"
    return f"_Scope(testid={_capwrap(sc.testid)}, role={_capwrap(sc.role)}, row_label={_capwrap(sc.row_label)})"


_VERB_LINE = {
    "click": "await robust_click(_loc)",
    "fill": "await fill_value(page, _loc, {arg})",
    "select": "await _select_option(page, _loc, {arg})",
    "keypress": "await _loc.first.press({arg})",
    "hover": "await _loc.first.hover()",
    "upload": "await _loc.first.set_input_files({arg})",
}


def _step_lines(s) -> list[str]:
    out = [f"    # [{s.index}] {s.step_id}: {s.intent}"]
    if s.action == "goto":
        out.append(f"    await page.goto({_capwrap(s.arg)}, wait_until='domcontentloaded', timeout=30000)")
        out.extend(_capture_lines(s))
        return out

    if s.action == "assert":
        spec = s.assert_
        rl, sl = _rungs_lit(spec.rungs), _scope_lit(spec.scope)
        if spec.kind == "element" and spec.state == "hidden":
            out.append(f"    await wait_hidden(page, {rl}, {s.budget_ms}, {sl})")
        elif spec.kind == "element" and spec.state == "visible":
            out.append(f"    _loc, _ = await resolve(page, {rl}, {s.budget_ms}, {sl})")
            out.append(f"    await _loc.wait_for(state='visible', timeout={s.budget_ms})")
        elif spec.kind == "value_is":
            neg = "not " if spec.negative else ""
            out.append(f"    _loc, _ = await resolve(page, {rl}, {s.budget_ms}, {sl})")
            out.append(f"    assert {neg}((await _loc.input_value()) == {_capwrap(spec.equals)})")
        else:  # checked | attribute | text | count — the shared eval_state
            out.append(f"    _spec = _Spec({spec.kind!r}, {rl}, {sl}, equals={_capwrap(spec.equals)}, "
                       f"attr_name={spec.attr_name!r}, contains={spec.contains!r}, count={spec.count!r}, "
                       f"checked={spec.checked!r})")
            out.append(f"    _ok, _ = await eval_state(page, _spec, {s.budget_ms})")
            out.append("    assert _ok")
        out.extend(_capture_lines(s))
        return out

    out.append(f"    _loc, _ = await resolve(page, {_rungs_lit(s.rungs)}, {s.budget_ms}, {_scope_lit(s.scope)})")
    if s.destructive:
        out.append("    # destructive (D15): the interpreter guards that the enclosing entity carries the run token")
    verb = _VERB_LINE[s.action].format(arg=_capwrap(s.arg))
    if len(s.arm_before) == 1:
        e = s.arm_before[0]
        out.append(f"    async with page.expect_response(_net({e.method!r}, {e.path_pattern!r}, {e.status_class!r}), timeout={e.budget_ms}):")
        out.append(f"        {verb}")
    else:
        out.append(f"    {verb}")
        for e in s.arm_before:
            out.append(f"    # network expectation (arm-before): {e.method} {e.path_pattern} {e.status_class}")
    for w in s.waits:
        rl, sl = _rungs_lit(w.rungs), _scope_lit(w.scope)
        if w.kind == "url":
            out.append(f"    await page.wait_for_url(re.compile({_capwrap(w.match)}), timeout={w.budget_ms})")
        elif w.kind == "element" and w.state == "visible":
            out.append(f"    _w, _ = await resolve(page, {rl}, {w.budget_ms}, {sl})")
            out.append(f"    await _w.wait_for(state='visible', timeout={w.budget_ms})")
        elif w.kind == "element" and w.state == "hidden":
            out.append(f"    await wait_hidden(page, {rl}, {w.budget_ms}, {sl})")
        elif w.kind == "value_is":
            out.append(f"    _w, _ = await resolve(page, {rl}, {w.budget_ms}, {sl})")
            out.append(f"    assert (await _w.input_value()) == {_capwrap(w.equals)}")
        elif w.kind in ("checked", "attribute", "text", "count"):
            out.append(f"    _spec = _Spec({w.kind!r}, {rl}, {sl}, equals={_capwrap(w.equals)}, "
                       f"attr_name={w.attr_name!r}, contains={w.contains!r}, count={w.count!r}, "
                       f"checked={w.checked!r})")
            out.append(f"    _wok, _ = await eval_state(page, _spec, {w.budget_ms})")
            out.append("    assert _wok")
    out.extend(_capture_lines(s))
    return out


_HEADER = '''\
"""AUTOGENERATED by `anneal export` — a faithful transcript of cold replay (LLD §5.5).

Standalone Playwright-Python. The resolver below is the SAME §6.2 code the interpreter runs
(vendored verbatim). Set ANNEAL_BASE_URL and run:  python this_script.py
Test: {bundle_ref}
"""
import asyncio
import os

from playwright.async_api import async_playwright
'''


def to_script(plan) -> str:
    parts = [
        _HEADER.format(bundle_ref=plan.bundle_ref),
        "",
        _INLINE_EXC,
        _HELPER_HEAD,
        _actions_helpers(),
        "",
        _resolver_source(),
        "",
        _waits_helpers(),
        "",
        "async def _flow(page):",
    ]
    body: list[str] = ["    _caps = {}  # § GAP-4 runtime captures ({{capture.*}})"]
    for s in plan.steps:
        body.extend(_step_lines(s))
    parts.append("\n".join(body))
    parts.append("""

async def main():
    base = os.environ.get("ANNEAL_BASE_URL", "http://localhost:8000")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(base_url=base, viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        await _flow(page)
        await ctx.close()
        await browser.close()
    print("anneal export replay: OK")


if __name__ == "__main__":
    asyncio.run(main())
""")
    return "\n".join(parts)
