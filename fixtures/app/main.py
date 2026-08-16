"""The hermetic golden app — a mock target with the EXACT testids/roles/routes the fixtures target.

Deterministic, no external network. M0 shipped the steady (un-drifted) app; **M3 adds a drift switch**
(HLD §6/§9): a runtime-toggleable "redesigned" variant, each drift independent so the heal vs
re-author paths are testable in isolation. Drift is process-global state flipped over HTTP by the
test — `POST /__drift {"testid":true,...}` sets it, `POST /__drift {}` resets. (A control endpoint,
not an env var, so a single running container can serve many drift combinations across a test run.)

The four drifts (LLD §12 M3 row; the drift toggles the ruled-out M0 note):
  - `testid`        rename a control's data-testid (primary rung gone; role/name survives) → ladder HEAL
  - `menutext`      rename a menu item's visible text (text rung gone; testid survives)     → ladder HEAL
  - `require_dates` Start/End become required; POST 422 when missing (enforced for real)     → RE-AUTHOR
  - `redesign`      modal create-form → full-page /campaigns/new                             → RE-AUTHOR

Steady behaviour (unchanged from M0):
  /login (input-username, input-password, btn-signin) → /campaigns
  /campaigns (campaigns-table[role=table], btn-add-campaign, create form, row menu, delete dialog)
  POST /api/v2/campaigns → 201 {id}; client-nav to /campaign-v2/{id};  DELETE → 200
The combobox picks BY VISIBLE TEXT; the delete dialog names the campaign so the D15 guard sees the token.
"""
from __future__ import annotations

import itertools

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()

# ── AUTH FIXTURES (auth/multi-platform, Phase A) ─────────────────────────────────────────────────
# Hermetic login pages so the engine's provider-keyed strategies (clerk|form|auto|none) have a
# deterministic gate to log into — no drift, no randomness, no external network. Each page sets the
# SAME session cookie (`fixture_session`) on a valid POST /api/login; /private serves protected
# content only when that cookie is valid, else it serves a page with a password field so the engine's
# `is_login_page` probe reads it as expired. Counters (`/__auth_stats`) let a test assert HOW MANY
# times a credential submission ran (reuse-first ⇒ 0; one soft re-login ⇒ 1). Creds are obviously fake.
_VALID_CREDS: dict[str, str] = {"fixture-user": "fixture-pass",
                                "fixture-user-b": "fixture-pass-b"}
_AUTH_STATS: dict[str, int] = {"login_attempts": 0, "logins_ok": 0, "sso_trap_hits": 0}


def _session_token(username: str) -> str:
    # Stateless token: no server-side session set (survives /__reset, so a storage_state saved by one
    # test can't be invalidated by another test's reset). A valid cookie value always starts "valid-".
    return f"valid-{username}"


def _is_valid_session(token: str | None) -> bool:
    return bool(token) and token.startswith("valid-")


def _auth_page(*, title: str, body: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>'
            f'<body>{body}<script>{_AUTH_SCRIPT}</script></body></html>')


# One JS submit path for every fixture login page: POST the collected creds to /api/login; on 200 the
# server has set the cookie, so navigate to /private (a non-login page ⇒ the engine reads success).
# On 401 show an inline error and STAY on the login page (a visible password field ⇒ is_login_page).
_AUTH_SCRIPT = """
async function postLogin(){
  const u = (document.querySelector('#username,#identifier-field,#email')||{}).value || '';
  const p = (document.querySelector('input[type=password]')||{}).value || '';
  const r = await fetch('/api/login', {method:'POST', headers:{'content-type':'application/json'},
                                       body: JSON.stringify({username:u, password:p})});
  if (r.status === 200) { window.location = '/private'; return; }
  const e = document.getElementById('login-error');
  if (e) { e.textContent = 'invalid credentials'; e.style.display='block'; }
}
// two-step reveal (generic + clerk): first Continue reveals the password field, second submits.
function reveal(){
  const w = document.getElementById('pw-wrap'); if (w) w.style.display='block';
  const s = document.getElementById('btn-submit'); if (s) s.style.display='inline';
  const c = document.getElementById('btn-continue'); if (c) c.style.display='none';
}
function clerkContinue(){
  const w = document.getElementById('pw-wrap');
  if (w && w.style.display === 'none') { w.style.display='block'; return; }  // step 1: reveal only
  postLogin();                                                               // step 2: submit
}
// fill-clear trap (T11): the FIRST input event blanks the field (mimics a controlled/React input that
// silently drops a programmatic `.fill()`), then disarms — so only the click+type retry path lands a
// value. `fill()` fires one input event (consumes the arm); subsequent `type()` keystrokes stick.
let _fillArmed = true;
function clearOnce(el){ if (_fillArmed) { _fillArmed = false; el.value = ''; } }
"""

_ids = itertools.count(1000)
CAMPAIGNS: dict[str, dict] = {"seed-1": {"name": "Welcome Campaign", "agent": "Aria",
                                         "start": "2026-01-01", "end": "2026-02-01"}}

# ── M3 drift state (process-global; flipped over HTTP by the test) ───────────────────────────────
DRIFT: dict[str, bool] = {"testid": False, "menutext": False, "require_dates": False, "redesign": False,
                          # `reveal_create`: Add opens the create form in a click-to-reveal PORTAL OVERLAY
                          # (position:fixed, NO aria role) — mirrors a real React-portal modal that carries
                          # no ARIA disclosure. Exercises recon-time behavioral progressive disclosure.
                          "reveal_create": False,
                          # `mutation_trap` (test-only): adds a create-verb opener whose click fires a
                          # non-GET write — proves the reveal probe BLOCKS the mutation (never authored).
                          "mutation_trap": False}


def _rows_html() -> str:
    out = []
    menu_testid = "btn-row-menu"
    for cid, c in CAMPAIGNS.items():
        out.append(
            f'<tr role="row" data-id="{cid}" data-name="{c["name"]}">'
            f'<td>{c["name"]}</td><td>{c["agent"]}</td>'
            f'<td><button data-testid="{menu_testid}" aria-label="Row menu" '
            f'onclick="openMenu(this)">&#8943;</button></td></tr>')
    return "\n".join(out)


def _add_button_html() -> str:
    # `testid` drift: the primary rung (data-testid) is renamed, but role=button + aria-label survive,
    # so the frozen ladder heals to a fallback rung (→ auto re-freeze incident, D28).
    testid = "btn-new-campaign" if DRIFT["testid"] else "btn-add-campaign"
    # `redesign` drift: Add navigates to a full create PAGE instead of revealing the inline modal form.
    # `reveal_create` drift: Add opens the form in a fixed portal overlay (a plain button, no ARIA).
    if DRIFT["redesign"]:
        onclick = "location='/campaigns/new'"
    elif DRIFT["reveal_create"]:
        onclick = "openPortal()"
    else:
        onclick = "showForm()"
    return (f'<button data-testid="{testid}" aria-label="Add Campaign" '
            f'onclick="{onclick}">Add Campaign</button>')


def _delete_menuitem_html() -> str:
    # `menutext` drift: the visible text (the primary text/name rung) changes, but data-testid survives,
    # so the frozen ladder heals to the testid fallback rung (→ auto re-freeze incident, D28).
    label = "Remove" if DRIFT["menutext"] else "Delete"
    return f'<div role="menuitem" data-testid="menuitem-delete" onclick="openDialog()">{label}</div>'


def _create_form_html(*, standalone: bool) -> str:
    # `require_dates` drift: Start Date becomes a NEW required field (carries `required`+`aria-required`
    # and a visible "*") so a live recon/re-author perceives the new obligation; the POST enforces it
    # for real (422 below) — a permissive app would make "new required field → behavior change" vacuous.
    req = ' required aria-required="true"' if DRIFT["require_dates"] else ""
    star = ' <span aria-hidden="true">*</span>' if DRIFT["require_dates"] else ""
    display = "" if standalone else ' style="display:none"'
    wrap_open, wrap_close = ("", "") if standalone else (f'<div id="create-form"{display}>', "</div>")
    hint = ('<p data-testid="form-hint">Start Date is now required.</p>'
            if DRIFT["require_dates"] else "")
    return f"""{wrap_open}
  {hint}
  <input data-testid="input-campaign-name" aria-label="Campaign name" placeholder="Campaign name"><br>
  <label>Start Date{star} <input type="date" data-testid="input-start-date" aria-label="Start Date"{req}></label><br>
  <label>End Date <input type="date" data-testid="input-end-date" aria-label="End Date"></label><br>
  <div style="position:relative;display:inline-block">
    <div role="combobox" data-testid="select-campaign-agent" aria-label="Agent" tabindex="0"
         onclick="toggleOptions()"><span id="agent-label">Select agent&hellip;</span></div>
    <ul role="listbox" id="agent-options" style="display:none;list-style:none;margin:0;padding:0;border:1px solid #333">
      <li role="option" onclick="pickAgent('Aria')">Aria</li>
      <li role="option" onclick="pickAgent('Nova')">Nova</li>
      <li role="option" onclick="pickAgent('Orion')">Orion</li>
    </ul>
  </div><br>
  <p id="form-error" style="color:#b00;display:none" role="alert"></p>
  <button data-testid="{'btn-submit-campaign' if DRIFT['testid'] else 'btn-campaign-create-submit'}" aria-label="Create" onclick="submitForm()">Create</button>
{wrap_close}"""


_SCRIPT = """
let selectedAgent = "";
let menuId = "", menuName = "";
function showForm(){ const f=document.getElementById('create-form'); if(f) f.style.display='block'; }
function openPortal(){ const p=document.getElementById('create-portal'); if(p) p.style.display='block'; }
function addNote(){ fetch('/api/v2/campaigns', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({name:'trap-note'})}); }
function toggleOptions(){ const o=document.getElementById('agent-options'); o.style.display = (o.style.display==='none'?'block':'none'); }
function pickAgent(name){ selectedAgent=name; document.getElementById('agent-label').textContent=name; document.getElementById('agent-options').style.display='none'; }
async function submitForm(){
  const body = {
    name: document.querySelector('[data-testid=input-campaign-name]').value,
    start: document.querySelector('[data-testid=input-start-date]').value,
    end: document.querySelector('[data-testid=input-end-date]').value,
    agent: selectedAgent,
  };
  const r = await fetch('/api/v2/campaigns', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
  if (r.status === 422) {                       // required-field enforcement (require_dates drift)
    const j = await r.json();
    const e = document.getElementById('form-error');
    if (e) { e.textContent = j.error || 'Missing required fields'; e.style.display='block'; }
    return;                                     // no navigation — the frozen url postcondition reds
  }
  const j = await r.json();
  window.location = '/campaign-v2/' + j.id;
}
function openMenu(btn){
  const tr = btn.closest('tr');
  menuId = tr.getAttribute('data-id'); menuName = tr.getAttribute('data-name');
  document.getElementById('row-menu').style.display='block';
}
function openDialog(){
  document.getElementById('row-menu').style.display='none';
  document.getElementById('confirm-text').textContent = 'Delete ' + menuName + '?';
  document.getElementById('confirm-dialog').style.display='block';
}
function closeDialog(){ document.getElementById('confirm-dialog').style.display='none'; }
async function confirmDelete(){
  await fetch('/api/v2/campaigns/' + menuId, {method:'DELETE'});
  const tr = document.querySelector('tr[data-id="'+menuId+'"]');
  if (tr) tr.remove();
  closeDialog();
}
"""

_STYLE = """
  #row-menu,#confirm-dialog{position:fixed;top:80px;left:80px;border:1px solid #333;background:#fff;padding:8px;z-index:10}
  [role=option]{padding:4px;cursor:pointer}
  table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}
"""

_LOGIN = """<!doctype html><html><head><meta charset="utf-8"><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
<label>Username <input data-testid="input-username" aria-label="Username"></label><br>
<label>Password <input data-testid="input-password" aria-label="Password" type="password"></label><br>
<button data-testid="btn-signin" aria-label="Sign in" onclick="location='/campaigns'">Sign in</button>
</body></html>"""


def _campaigns_html() -> str:
    # default: inline click-to-reveal (#create-form, display:none → showForm). redesign: form on a
    # separate page. reveal_create: the SAME form inside a fixed PORTAL overlay (no ARIA role), hidden
    # until openPortal(). Exactly one form surface is present per mode; M2/M3 use the inline default.
    if DRIFT["redesign"]:
        form_section = ""
    elif DRIFT["reveal_create"]:
        form_section = (f'<div id="create-portal" data-testid="create-portal" '
                        f'style="display:none;position:fixed;inset:0;z-index:50;background:#fff;'
                        f'padding:16px;overflow:auto">{_create_form_html(standalone=True)}</div>')
    else:
        form_section = _create_form_html(standalone=False)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Campaigns</title>
<style>{_STYLE}</style></head>
<body>
<nav data-testid="nav-authenticated">Campaigns</nav>
{_add_button_html()}
{'<button data-testid="btn-add-note" aria-label="Add Note" onclick="addNote()">Add Note</button>' if DRIFT["mutation_trap"] else ''}
{form_section}
<table role="table" data-testid="campaigns-table">
  <thead><tr role="row"><th>Name</th><th>Agent</th><th>Actions</th></tr></thead>
  <tbody id="rows">{_rows_html()}</tbody>
</table>

<div id="row-menu" role="menu" style="display:none">
  {_delete_menuitem_html()}
</div>

<div id="confirm-dialog" role="dialog" aria-label="Confirm delete" style="display:none">
  <p id="confirm-text"></p>
  <button aria-label="Delete" onclick="confirmDelete()">Delete</button>
  <button aria-label="Cancel" onclick="closeDialog()">Cancel</button>
</div>
<script>{_SCRIPT}</script>
</body></html>"""


def _create_page_html() -> str:
    # the `redesign` full-page create surface (behavior re-author target).
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>New Campaign</title>
<style>{_STYLE}</style></head>
<body>
<nav data-testid="nav-authenticated">New Campaign</nav>
<h1>Create a campaign</h1>
{_create_form_html(standalone=True)}
<a href="/campaigns">Cancel</a>
<script>{_SCRIPT}</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
def login():
    return _LOGIN


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns():
    return _campaigns_html()


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaigns_new():
    # only meaningful under the redesign drift; harmless (a valid create page) otherwise.
    return _create_page_html()


@app.get("/expired/campaigns", response_class=HTMLResponse)
def expired_campaigns():
    # simulate an expired session: a protected route bounces to sign-in (§9 probe target).
    return _LOGIN


@app.get("/capacity/campaigns")
def capacity_campaigns():
    # simulate a shared-quota exhaustion (§10 ENV_CAPACITY probe target): 429 + a limit string.
    return JSONResponse({"error": "rate limit exceeded — quota reached"}, status_code=429)


@app.get("/infra/campaigns")
def infra_campaigns():
    # simulate an origin/infra failure (§10 ENV_INFRA probe target): 503.
    return JSONResponse({"error": "service unavailable"}, status_code=503)


@app.get("/campaign-v2/{cid}", response_class=HTMLResponse)
def campaign_detail(cid: str):
    c = CAMPAIGNS.get(cid, {"name": "(new campaign)", "agent": ""})
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Campaign</title></head><body>'
            f'<nav data-testid="nav-authenticated">Campaign</nav>'
            f'<h1 data-testid="campaign-detail">{c["name"]}</h1>'
            f'<a href="/campaigns">Back to campaigns</a></body></html>')


@app.post("/api/v2/campaigns")
async def create_campaign(request: Request):
    body = await request.json()
    if DRIFT["require_dates"] and not body.get("start"):
        # enforced for real (the logged M3 note): a permissive app makes "new required field →
        # behavior change" vacuous. Missing Start Date → 422, no row created.
        return JSONResponse({"error": "Start Date is required"}, status_code=422)
    cid = f"c{next(_ids)}"
    CAMPAIGNS[cid] = {"name": body.get("name", ""), "agent": body.get("agent", ""),
                      "start": body.get("start", ""), "end": body.get("end", "")}
    return JSONResponse({"id": cid}, status_code=201)


@app.get("/api/v2/campaigns")
def list_campaigns():
    return CAMPAIGNS


@app.delete("/api/v2/campaigns/{cid}")
def delete_campaign(cid: str):
    CAMPAIGNS.pop(cid, None)
    return JSONResponse({"deleted": cid}, status_code=200)


# ── M3 drift control (test-only; process-global) ─────────────────────────────────────────────────
@app.get("/__drift")
def get_drift():
    return DRIFT


@app.post("/__drift")
async def set_drift(request: Request):
    body = await request.json()
    for k in DRIFT:
        DRIFT[k] = bool(body.get(k, False))     # POST {} resets all to False
    return DRIFT


@app.post("/__reset")
def reset():
    # test-only: restore the app to its initial hermetic state (seed row + no drift + zero auth stats).
    # The seed row is what row-scoped steps target, so it must be restorable after a test deletes it.
    CAMPAIGNS.clear()
    CAMPAIGNS["seed-1"] = {"name": "Welcome Campaign", "agent": "Aria",
                           "start": "2026-01-01", "end": "2026-02-01"}
    for k in DRIFT:
        DRIFT[k] = False
    for k in _AUTH_STATS:
        _AUTH_STATS[k] = 0
    return {"ok": True, "campaigns": len(CAMPAIGNS)}


# ── AUTH FIXTURE ROUTES (Phase A) ─────────────────────────────────────────────────────────────────
@app.get("/login-form", response_class=HTMLResponse)
def login_form():
    # single-step: username + password + submit. Generic closed-vocab field attrs so the `form`
    # strategy detects them (autocomplete=username, type=password, submit button).
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<label>Username <input id="username" name="username" type="text" autocomplete="username" aria-label="Username" placeholder="Username"></label><br>
<label>Password <input id="password" name="password" type="password" aria-label="Password" placeholder="Password"></label><br>
<button id="btn-submit" type="submit" aria-label="Sign in" onclick="postLogin()">Sign in</button>
<p id="login-error" role="alert" style="color:#b00;display:none"></p>""")


@app.get("/login-2step", response_class=HTMLResponse)
def login_2step():
    # two-step: email → Continue (reveals password) → submit. The password field is hidden until the
    # generic `Continue` reveal, exercising the `form` strategy's two-step branch.
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<label>Email <input id="email" name="email" type="email" autocomplete="username" aria-label="Email" placeholder="Email"></label><br>
<div id="pw-wrap" style="display:none">
  <label>Password <input id="password" name="password" type="password" aria-label="Password"></label><br>
</div>
<button id="btn-continue" type="button" aria-label="Continue" onclick="reveal()">Continue</button>
<button id="btn-submit" type="submit" aria-label="Sign in" style="display:none" onclick="postLogin()">Sign in</button>
<p id="login-error" role="alert" style="color:#b00;display:none"></p>""")


@app.get("/login-fillclear", response_class=HTMLResponse)
def login_fillclear():
    # single-step form whose username input DROPS the first programmatic fill (see clearOnce) — the
    # engine's _fill_verified must notice the value didn't land and retry via click+type (T11).
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<label>Username <input id="username" name="username" type="text" autocomplete="username" aria-label="Username" oninput="clearOnce(this)"></label><br>
<label>Password <input id="password" name="password" type="password" aria-label="Password"></label><br>
<button id="btn-submit" type="submit" aria-label="Sign in" onclick="postLogin()">Sign in</button>
<p id="login-error" role="alert" style="color:#b00;display:none"></p>""")


@app.get("/login-clerkish", response_class=HTMLResponse)
def login_clerkish():
    # Clerk-shaped: #identifier-field + #password-field, an EXACT "Continue" (reveals then submits) and
    # a DECOY "Continue with Google" that navigates to /sso-trap. The `clerk` strategy must click the
    # exact "Continue" (never the SSO decoy) — a `has-text('Continue')` match would hijack into SSO.
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<input id="identifier-field" name="identifier" type="text" aria-label="Email address" placeholder="Email address"><br>
<div id="pw-wrap" style="display:none">
  <input id="password-field" name="password" type="password" aria-label="Password"><br>
</div>
<button class="cl-formButtonPrimary" type="button" aria-label="Continue" onclick="clerkContinue()">Continue</button>
<button type="button" aria-label="Continue with Google" onclick="location.href='/sso-trap'">Continue with Google</button>
<p id="login-error" role="alert" style="color:#b00;display:none"></p>""")


@app.get("/login-captcha", response_class=HTMLResponse)
def login_captcha():
    # a password field guarded by a REAL captcha CHALLENGE (a visible reCAPTCHA v2 checkbox widget,
    # `.g-recaptcha`) — a HARD-auth signal. The engine must detect it and defer to a human WITHOUT
    # submitting credentials (zero attempts). Contrast /login-recaptcha-v3 (passive → NOT hard-auth).
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<label>Username <input id="username" type="text" autocomplete="username" aria-label="Username"></label><br>
<label>Password <input id="password" type="password" aria-label="Password"></label><br>
<div class="g-recaptcha" style="width:304px;height:78px;border:1px solid #ccc;padding:8px">
  I'm not a robot (reCAPTCHA v2 challenge)</div>
<button id="btn-submit" type="submit" aria-label="Sign in" onclick="postLogin()">Sign in</button>""")


@app.get("/login-recaptcha-v3", response_class=HTMLResponse)
def login_recaptcha_v3():
    # a normal login form carrying a PASSIVE reCAPTCHA v3 badge (invisible, score-based) — the shape
    # a real-world Clerk sign-in uses. This is NOT a hard-auth block: a normal sign-in still completes, so
    # the engine must IGNORE the badge and log in (it must not park needs_human). The `.grecaptcha-badge`
    # + `google-recaptcha-v3` class + the anchor iframe are exactly what the engine must NOT defer on.
    return _auth_page(title="Sign in", body="""
<h1>Sign in</h1>
<label>Username <input id="username" name="username" type="text" autocomplete="username" aria-label="Username" placeholder="Username"></label><br>
<label>Password <input id="password" name="password" type="password" aria-label="Password" placeholder="Password"></label><br>
<button id="btn-submit" type="submit" aria-label="Sign in" onclick="postLogin()">Sign in</button>
<p id="login-error" role="alert" style="color:#b00;display:none"></p>
<div class="grecaptcha-badge google-recaptcha-v3" style="position:fixed;bottom:14px;right:14px;width:70px;height:60px;overflow:hidden">
  <iframe title="reCAPTCHA" src="/__recaptcha_v3_anchor" width="256" height="60"></iframe>
</div>
<textarea class="g-recaptcha-response" style="display:none"></textarea>""")


@app.get("/__recaptcha_v3_anchor", response_class=HTMLResponse)
def recaptcha_v3_anchor():
    # hermetic stand-in for the invisible reCAPTCHA v3 anchor iframe body — no external network.
    return '<!doctype html><html><body>protected by reCAPTCHA</body></html>'


@app.get("/login-otp", response_class=HTMLResponse)
def login_otp():
    # a one-time-code / 2FA field — a HARD-auth signal (the engine defers to a human, no submit).
    return _auth_page(title="Verify", body="""
<h1>Two-step verification</h1>
<label>Password <input id="password" type="password" aria-label="Password"></label><br>
<label>Verification code <input name="otp" autocomplete="one-time-code" aria-label="Verification code" placeholder="123456"></label><br>
<button id="btn-submit" type="submit" aria-label="Verify" onclick="postLogin()">Verify</button>""")


@app.get("/__captcha_frame", response_class=HTMLResponse)
def captcha_frame():
    # hermetic stand-in for a recaptcha/hcaptcha iframe body — no external network.
    return '<!doctype html><html><body>captcha</body></html>'


@app.get("/sso-trap", response_class=HTMLResponse)
def sso_trap():
    # the SSO decoy destination: reaching it means the engine clicked "Continue with Google" instead
    # of the exact "Continue". Counted so a test can assert it was NEVER hit.
    _AUTH_STATS["sso_trap_hits"] += 1
    return _auth_page(title="SSO", body='<h1 data-testid="sso-trap">SSO redirect — wrong button</h1>')


@app.get("/private", response_class=HTMLResponse)
def private(request: Request):
    # protected area: valid session cookie ⇒ protected content (a non-login page); otherwise a page
    # WITH a visible password field so the engine's is_login_page probe reads the session as expired.
    if _is_valid_session(request.cookies.get("fixture_session")):
        return _auth_page(title="Private",
                          body='<nav data-testid="nav-authenticated">Private</nav>'
                               '<h1 data-testid="private-content">Members only</h1>')
    return _auth_page(title="Sign in",
                      body='<h1>Session expired — sign in</h1>'
                           '<input id="password" type="password" aria-label="Password">'
                           '<button type="submit" aria-label="Sign in" onclick="postLogin()">Sign in</button>')


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    _AUTH_STATS["login_attempts"] += 1
    u, p = body.get("username", ""), body.get("password", "")
    if p and _VALID_CREDS.get(u) == p:
        _AUTH_STATS["logins_ok"] += 1
        resp = JSONResponse({"ok": True})
        resp.set_cookie("fixture_session", _session_token(u), httponly=True, samesite="lax")
        return resp
    return JSONResponse({"ok": False, "error": "invalid credentials"}, status_code=401)


@app.get("/__auth_stats")
def auth_stats():
    # login_attempts / logins_ok / sso_trap_hits — reset per app start and by POST /__reset.
    return dict(_AUTH_STATS)
