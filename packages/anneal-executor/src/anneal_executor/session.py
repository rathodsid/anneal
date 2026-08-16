"""Session primitives — sign-in detection + seeded-session probe (D14, §9).

A sign-in page where an authenticated page is expected means the session expired — never type
credentials as a "step" (D6, the 17.8-minute credential-discovery trap). Sessions are seeded and
reused via storage-state (injected at context creation in evidence.recording); expiry PARKS, never
retries.
"""
from __future__ import annotations

import re

from .settle import await_settled

# a sign-in URL (closed-vocab across the common conventions)
_LOGIN_URL = re.compile(r"/(sign[-_]?in|log[-_]?in|auth/login)(/|$|\?|#)", re.I)


async def _has_password_field(page) -> bool:
    try:
        return await page.locator("input[type=password]:visible").count() > 0
    except Exception:
        return False


async def is_login_page(page) -> bool:
    """True when the current page is a sign-in page (URL pattern OR a visible password field, A5)."""
    if _LOGIN_URL.search(page.url or ""):
        return True
    return await _has_password_field(page)


async def probe_session(page, base_url: str, probe_path: str = "/") -> str:
    """Load the target and classify the pre-suite state (§9/§10 probe). Returns one of:
    'valid' | 'expired' | 'env_capacity' | 'env_infra' | 'unknown'.

    Deterministic + zero LLM: no classification, no agent spend. Infra/capacity are matched from the
    response status BEFORE auth (§7 ordering rule) so a degraded environment parks the suite distinctly
    (never misread as an auth or product failure), before any run_task is claimed (HLD §8).
    """
    try:
        resp = await page.goto(base_url.rstrip("/") + probe_path, wait_until="domcontentloaded",
                               timeout=30000)
    except Exception:
        # a hard navigation failure (net::ERR / origin timeout) is infra, not auth (§7 row 10)
        return "env_infra"
    status = resp.status if resp is not None else 0
    if status == 429:                      # §7 row 11 — shared-quota exhaustion, park + banner
        return "env_capacity"
    if status >= 500:                      # §7 row 10 — origin/infra failure
        return "env_infra"
    # SPA-readiness (A5): at domcontentloaded an authenticated shell and a sign-in wall BOTH look
    # empty (no password field rendered, redirect to /sign-in not yet fired), so classifying here races
    # hydration and an EXPIRED session reads as 'valid'. Settle first, then classify on the hydrated DOM.
    await await_settled(page)
    return "expired" if await is_login_page(page) else "valid"
