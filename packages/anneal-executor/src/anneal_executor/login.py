"""Provider-keyed login strategies (design §3.4). Deterministic, zero-LLM, pure Playwright —
legal in the cold lane's dep graph (imports only anneal_executor.session + playwright; never
anneal_llm, D31).

Selectors are provider CLOSED-VOCABULARY (generic email/user/login + submit heuristics and the
fixed IdP/captcha/OTP signal lists) — never per-target selectors, so the target-agnostic audit
stays green. `kind` picks a strategy from STRATEGIES.

Entry point `login(page, auth_cfg, creds, base_url) -> LoginOutcome` returns one honest result
type. Navigation + the pre-submit hard-auth check are hoisted here (out of the strategies) so a captcha/OTP target defers to a human with ZERO credential
submissions (T12/T13); the strategies operate the form on the page login() already navigated to.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from anneal_executor.session import is_login_page


@dataclass(frozen=True)
class LoginOutcome:
    """ok | hard_auth(reason) | failed(reason) — the one honest result type (§3.4)."""
    status: str                       # "ok" | "hard_auth" | "failed"
    reason: str | None = None

    @classmethod
    def ok(cls) -> "LoginOutcome":
        return cls("ok")

    @classmethod
    def hard_auth(cls, reason: str) -> "LoginOutcome":
        return cls("hard_auth", reason)

    @classmethod
    def failed(cls, reason: str) -> "LoginOutcome":
        return cls("failed", reason)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def needs_human(self) -> bool:
        """Only a real hard-auth signal (captcha/OTP/SSO) defers to a human (G5)."""
        return self.status == "hard_auth"


# ── closed-vocabulary field/submit detection ──────────────────────────────────────────────────
# Username/email field across the common conventions (type, name/id, placeholder, aria-label). Falls
# back to a plain visible text input when nothing keyed matches (e.g. a `type=text` user field).
_USER_SEL = ("input[type=email], input[autocomplete=username], "
             "input[name*=email i], input[name*=user i], input[name*=login i], "
             "input[id*=email i], input[id*=user i], input[id*=login i], "
             "input[placeholder*=user i], input[placeholder*=email i], "
             "input[aria-label*=user i], input[aria-label*=email i]")
_USER_FALLBACK = "input[type=text]:visible, input:not([type]):visible"

# Submit control — many apps use <input type=submit> or a [role=button], not <button>.
_SUBMIT_SEL = ("input[type=submit]:visible", "button[type=submit]:visible",
               "button:has-text('Sign in'):visible", "button:has-text('Log in'):visible",
               "button:has-text('Login'):visible", "button:has-text('Continue'):visible",
               "[role=button]:has-text('Sign in'):visible", "[role=button]:has-text('Log in'):visible")


async def _fill_verified(locator, value: str) -> bool:
    """Fill a field and VERIFY the value landed (a plain `fill` can silently no-op on some inputs).
    Retries via focus + type before giving up (§3.4)."""
    try:
        await locator.fill(value)
        if (await locator.input_value() or "") == value:
            return True
    except Exception:
        pass
    try:
        await locator.click(timeout=2000)
        await locator.press("Control+A")
        await locator.type(value, delay=20)
        return (await locator.input_value() or "") == value
    except Exception:
        return False


async def _clerk_continue(page) -> None:
    """Click the EXACT 'Continue' (the email/password submit) — NEVER 'Continue with Google' (a
    `has-text('Continue')` match grabs the SSO button and hijacks login into Google OAuth). Falls
    back to the Clerk primary button, then to submitting the password field."""
    try:
        await page.get_by_role("button", name="Continue", exact=True).click(timeout=4000)
        return
    except Exception:
        pass
    for sel in ("button.cl-formButtonPrimary:visible", "button[type=submit]:visible"):
        try:
            await page.locator(sel).first.click(timeout=3000)
            return
        except Exception:
            continue
    try:
        await page.locator("input[type=password]:visible").first.press("Enter")
    except Exception:
        pass


async def _clerk_login(page, creds) -> None:
    """Clerk auth — robust across single-step (identifier + password already visible) and two-step
    (email → Continue → password). Uses the EXACT 'Continue' so it never falls into Google SSO."""
    user = page.locator("#identifier-field, input[name=identifier]").first
    if await user.count() == 0:                       # other Clerk skins — generic closed-vocab
        user = page.locator(_USER_SEL).first
    await user.wait_for(state="visible", timeout=15000)
    await _fill_verified(user, creds.username)
    pwd = page.locator("input[type=password]:visible")
    if await pwd.count() == 0:                         # two-step: reveal the password field first
        await _clerk_continue(page)
        await page.wait_for_timeout(1800)
        pwd = page.locator("input[type=password]:visible")
    await pwd.first.wait_for(state="visible", timeout=10000)
    await _fill_verified(pwd.first, creds.password)
    await _clerk_continue(page)
    await page.wait_for_timeout(4000)  # Clerk leaves a #/redirect fragment briefly


async def _form_login(page, creds) -> None:
    """Generic form login — closed-vocab field detection (no per-target selectors). Handles single-
    step and two-step (email → continue → password). Simple form login only; MFA/SSO/bot-detection
    are diagnosed by hard_auth_signal, not forced here."""
    user = page.locator(_USER_SEL).first
    if await user.count() == 0:                        # no keyed field — plain text input fallback
        user = page.locator(_USER_FALLBACK).first
    try:
        await user.wait_for(state="visible", timeout=8000)
    except Exception:
        return
    await _fill_verified(user, creds.username)
    pwd = page.locator("input[type=password]:visible")
    if await pwd.count() == 0:                          # two-step: reveal the password field
        for sel in ("button:has-text('Continue')", "button:has-text('Next')",
                    "button[type=submit]", "input[type=submit]"):
            try:
                await page.locator(f"{sel}:visible").first.click(timeout=4000)
                await page.wait_for_timeout(1800)
                break
            except Exception:
                continue
        pwd = page.locator("input[type=password]:visible")
    try:
        await pwd.first.wait_for(state="visible", timeout=6000)
    except Exception:
        return
    await _fill_verified(pwd.first, creds.password)
    for sel in _SUBMIT_SEL:
        try:
            await page.locator(sel).first.click(timeout=4000)
            break
        except Exception:
            continue
    await page.wait_for_timeout(4000)  # post-submit settle


async def _no_auth(page, creds) -> None:
    """No-auth target (e.g. a local fixture) — nothing to do."""
    return None


async def hard_auth_signal(page, base_url: str) -> str | None:
    """Return an honest reason ONLY when a real hard-auth signal is present (IdP redirect, captcha, or
    an OTP/2FA field) — so we never cry 'MFA/SSO' when we simply couldn't operate the form (§3.4, G5).
    Provider closed-vocab: a fixed IdP host list, captcha iframes (by provider src OR title/class, so a
    hermetic captcha is still caught), and OTP/2FA field conventions."""
    try:
        cur, base = urlparse(page.url or ""), urlparse(base_url)
        idp = ("accounts.google.com", "login.microsoftonline.com", "okta.com", "auth0.com",
               "onelogin.com", "login.salesforce.com", "github.com/login")
        if cur.netloc and base.netloc and cur.netloc != base.netloc \
                and any(h in (cur.netloc + cur.path) for h in idp):
            return f"redirected to an identity provider ({cur.netloc}) — SSO sign-in needs you"
    except Exception:
        pass
    try:
        # A captcha CHALLENGE the user must solve → defer (G5). Deliberately distinguished from PASSIVE,
        # invisible bot-scoring (reCAPTCHA v3 badge / invisible reCAPTCHA), which does NOT block an
        # automated sign-in — deferring on that would falsely park every app that merely embeds a v3
        # score badge (a normal login still completes). So match ONLY genuine challenges: hCaptcha,
        # Cloudflare Turnstile, the reCAPTCHA v2 image-challenge frame, or a VISIBLE reCAPTCHA v2
        # checkbox widget. The v3 badge (.grecaptcha-badge / class google-recaptcha-v3) is NOT matched.
        if await page.locator('iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
                              'iframe[src*="challenges.cloudflare.com"], '
                              'iframe[src*="recaptcha/api2/bframe"], '
                              'div.g-recaptcha:visible').count() > 0:
            return "a captcha / bot-check challenge is present — sign-in needs you"
    except Exception:
        pass
    try:
        # NOTE: attribute values are QUOTED — '2fa' begins with a digit, an INVALID unquoted CSS value
        # that throws in Playwright's parser and would silently sink the whole OTP branch (an
        # unquoted form is tolerated by the sync API; the async parser is not).
        if await page.locator('input[autocomplete="one-time-code"], input[name*="otp" i], '
                              'input[name*="2fa" i], input[id*="otp" i], '
                              'input[placeholder*="verification" i], input[placeholder*="code" i]').count() > 0:
            return "a one-time-code (OTP) / 2FA step is present — sign-in needs you"
    except Exception:
        pass
    return None


STRATEGIES = {                     # the four supported login kinds
    "clerk": _clerk_login,
    "form": _form_login,
    "auto": _form_login,           # "auto" = form heuristics (design §3.1)
    "none": _no_auth,
}

_DEFAULT_SIGNIN = {"clerk": "/signin", "form": "/", "auto": "/", "none": "/"}


async def login(page, auth_cfg: dict, creds, *, base_url: str) -> LoginOutcome:
    """Log in per the target's auth block (§3.1). One honest attempt; no retry loop (§3.5). `kind:none`
    is a no-op that NEVER navigates (T15). For a real strategy: navigate to the sign-in page, diagnose
    a hard-auth signal BEFORE submitting any credentials (captcha/OTP/SSO → hard_auth, zero attempts),
    then operate the form; end off a login page ⇒ ok, else diagnose (hard_auth) or fail honestly."""
    kind = (auth_cfg or {}).get("kind", "auto")
    if kind == "none":
        return LoginOutcome.ok()                       # no navigation, no submit (T15)
    strategy = STRATEGIES.get(kind, _form_login)
    signin = (auth_cfg or {}).get("signin_path") or _DEFAULT_SIGNIN.get(kind, "/")
    url = base_url.rstrip("/") + signin
    # A navigation failure here is an INFRA fault, not a failed-auth outcome — let it propagate loud
    # (ground rule 3) rather than smudging the infra/auth boundary into a park (probe already proved
    # the target reachable moments earlier).
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(800)

    # diagnose hard-auth BEFORE any credential submission (T12/T13: zero attempts on captcha/OTP)
    reason = await hard_auth_signal(page, base_url)
    if reason:
        return LoginOutcome.hard_auth(reason)

    try:
        await strategy(page, creds)
    except Exception:
        pass                                           # evaluate the page the strategy left us on
    await page.wait_for_timeout(800)
    if not await is_login_page(page):
        return LoginOutcome.ok()
    reason = await hard_auth_signal(page, base_url)    # a hard signal may appear post-submit (2FA)
    if reason:
        return LoginOutcome.hard_auth(reason)
    return LoginOutcome.failed("couldn't complete the sign-in form automatically — the fields/submit "
                               "weren't found or the credentials were rejected")
