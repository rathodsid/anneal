// Targets & credentials (Target) — how Anneal logs into the apps it tests.
//
// ALMOST ENTIRELY TARGET-STATE. Wired REAL: GET /projects/{p}/targets (with profile.auth) and
// GET /projects/{p}/sessions (CREDENTIAL-keyed — no target dimension; the per-(credential×target)
// keying is target-state). Everything else has NO endpoint and is .stub: Connect-a-platform
// (POST /targets), the credential-resolution chain + encryption state, re-seed / validate-now, and
// the soft-relogin outcome — all owned by the auth workstream (MULTI-PLATFORM-AUTH.md, B5 cut).
// Rules held: never show a secret (resolution layer only; password inputs write-only), NO selectors
// anywhere (provider kind + paths only), hard-auth = park-needs-human (parked family, not red).
import { useEffect, useState, type ChangeEvent } from "react";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { PROJECT, pickDefaultTarget } from "../lib/config";
import { relTime } from "../lib/format";

type Target = components["schemas"]["Target"];
type SessionHealth = components["schemas"]["SessionHealth"];
type AuthCfg = { kind?: string; creds_ref?: string; signin_path?: string; verify_paths?: string[] };

const KIND: Record<string, [string, string]> = {
  clerk: ["clerk", "k-clerk"],
  form: ["form", "k-form"],
  auto: ["auto (form)", "k-form"],
  none: ["no auth", "k-none"],
};
const KIND_NOTE: Record<string, string> = {
  clerk: 'exact-"Continue", SSO-hijack guard',
  form: "generic email/user/password vocab",
  auto: "form heuristics",
  none: "no-op",
};

export function TargetAuth() {
  const [targets, setTargets] = useState<Target[]>([]);
  const [sessions, setSessions] = useState<SessionHealth[]>([]);
  const [sel, setSel] = useState<string>("");
  const [connectOpen, setConnectOpen] = useState(false);
  // Connect-a-platform form — WIRED to POST /projects/{p}/targets (the onboarding endpoint is built).
  const [form, setForm] = useState({ key: "", base_url: "", kind: "auto", creds_ref: "",
    signin_path: "", persona: "member", username: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: string) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  function load() {
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) { setTargets(data); setSel((s) => s || pickDefaultTarget(data)); }
    });
    api.GET("/projects/{project}/sessions", { params: { path: { project: PROJECT } } }).then(({ data }) => setSessions(data ?? []));
  }
  useEffect(load, []);

  async function submitConnect() {
    if (!form.key || !form.base_url) { setErr("key and base_url are required"); return; }
    setBusy(true); setErr(null);
    const auth: AuthCfg = { kind: form.kind };
    if (form.creds_ref) auth.creds_ref = form.creds_ref;
    if (form.signin_path) auth.signin_path = form.signin_path;
    const { data, error } = await api.POST("/projects/{project}/targets", {
      params: { path: { project: PROJECT } },
      body: { key: form.key, base_url: form.base_url, auth, persona: form.persona,
        timezone: "UTC", // explicit server default — regenerated schema marks defaulted fields required
        ...(form.username ? { username: form.username } : {}),
        ...(form.password ? { password: form.password } : {}) },
    });
    setBusy(false);
    if (error) { setErr(typeof error === "string" ? error : JSON.stringify(error)); return; }
    setConnectOpen(false);
    setForm({ key: "", base_url: "", kind: "auto", creds_ref: "", signin_path: "", persona: "member", username: "", password: "" });
    if (data?.target_id) setSel(data.target_id);
    load();
  }

  const target = targets.find((t) => t.id === sel);
  const auth = ((target?.profile ?? {}) as Record<string, unknown>).auth as AuthCfg | undefined;
  const kind = auth?.kind ?? "none";
  const isNone = kind === "none";
  const canonical = !!((target?.profile ?? {}) as Record<string, unknown>).canonical;

  return (
    <div className="main tauth">
      <header className="topbar">
        <nav className="crumbs">
          Target <span className="sep">/</span> <b>Targets &amp; credentials</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <div className="hdrow">
          <div>
            <h1>Targets &amp; credentials</h1>
            <p className="lede">
              How Anneal logs into the apps it tests — onboard a platform once, then it reuses the session and
              only re-logs in on expiry.
            </p>
          </div>
          <button className="btn primary" onClick={() => setConnectOpen(true)}>
            + Connect a platform
          </button>
        </div>

        <div className="designbanner">
          <span>ⓘ <b>Target-app auth</b> — not operator/console login (that stays single-operator this cycle).</span>
          <span className="stub" style={{ marginLeft: "auto" }}>◔ designed, not built — every mutation here is target-state (MULTI-PLATFORM-AUTH.md)</span>
        </div>

        <div className="grid">
          <div className="tlist">
            {targets.map((t) => {
              const a = ((t.profile ?? {}) as Record<string, unknown>).auth as AuthCfg | undefined;
              const k = KIND[a?.kind ?? "none"];
              const canon = !!((t.profile ?? {}) as Record<string, unknown>).canonical;
              return (
                <button key={t.id} className={`tcard ${t.id === sel ? "sel" : ""}`} onClick={() => setSel(t.id)}>
                  <div className="t1">
                    <span className="k">{t.key}</span>
                    {canon ? <span className="chip" style={{ color: "var(--v-green)" }}>canonical</span> : null}
                  </div>
                  <div className="u">{t.base_url}</div>
                  <div className="t2">
                    <span className={`pill ${k[1]}`}>{k[0]}</span>
                  </div>
                </button>
              );
            })}
          </div>

          <div className="detail">
            {!target ? (
              <div className="card"><div className="sub">Select a target.</div></div>
            ) : (
              <>
                <div className="card">
                  <div className="dhead">
                    <h3>{target.key}</h3>
                    <span className={`pill ${KIND[kind][1]}`}>{KIND[kind][0]}</span>
                    {canonical ? <span className="chip" style={{ color: "var(--v-green)" }}>canonical</span> : null}
                  </div>
                  <div className="sub mono">{target.base_url}</div>
                </div>

                <div className="card">
                  <h5 className="sec">Auth config <span className="chip">target.profile.auth</span></h5>
                  <div className="kv">
                    <span className="k">kind</span>
                    <span className="v">
                      <span className={`pill ${KIND[kind][1]}`}>{KIND[kind][0]}</span>{" "}
                      <span style={{ color: "var(--muted)" }}>{KIND_NOTE[kind]}</span>
                    </span>
                    {isNone ? null : (
                      <>
                        <span className="k">creds_ref</span>
                        <span className="v">{auth?.creds_ref ?? "—"}</span>
                        <span className="k">signin_path</span>
                        <span className="v">{auth?.signin_path ?? <span style={{ color: "var(--faint)" }}>default per kind</span>}</span>
                        <span className="k">verify_paths</span>
                        <span className="v">{auth?.verify_paths?.length ? auth.verify_paths.join(", ") : <span style={{ color: "var(--faint)" }}>—</span>}</span>
                      </>
                    )}
                  </div>
                  <div className="stub" style={{ marginTop: 12 }}>
                    ◔ selectors never appear here — "{kind}" is a provider strategy in engine code (login.py), not a target · edit = target-state
                  </div>
                </div>

                <div className="card">
                  <h5 className="sec">Credential resolution {isNone ? null : <span className="chip">layered chain</span>}</h5>
                  {isNone ? (
                    <div className="layer">
                      <span className="n">—</span>
                      <div className="lbody"><div className="lt">no credentials needed</div><div className="ls">kind: none — the golden app runs with no login</div></div>
                    </div>
                  ) : (
                    <>
                      <div className="chain">
                        {[
                          ["1", "DB · credential.secret_enc", "Fernet-decrypted at use · (project, persona)"],
                          ["2", "env · ANNEAL_<REF>_USERNAME / _PASSWORD", "per-ref override"],
                          ["3", "env · ANNEAL_UI_USERNAME / _PASSWORD", "generic fallback"],
                        ].map(([n, t, s]) => (
                          <div className="layer" key={n}>
                            <span className="n">{n}</span>
                            <div className="lbody"><div className="lt">{t}</div><div className="ls">{s}</div></div>
                          </div>
                        ))}
                      </div>
                      <div className="stub" style={{ marginTop: 11 }}>
                        ◔ which layer resolves + encryption state are decided at run time (no read endpoint) — target-state · the secret is never displayed or returned
                      </div>
                    </>
                  )}
                </div>

                {isNone ? null : (
                  <div className="card">
                    <h5 className="sec">Sessions <span className="chip">credential-keyed today</span></h5>
                    {sessions.length ? (
                      <div className="kv">
                        {sessions.map((s) => (
                          <span style={{ display: "contents" }} key={s.credential_id}>
                            <span className="k">{s.persona}</span>
                            <span className="v">
                              <span className={`sdot ${s.state === "active" ? "valid" : s.state === "expired" ? "expired" : "none"}`}>
                                <span className="d" />
                                {s.state === "active" ? "valid · reuse-first" : s.state}
                              </span>
                              {"  "}· validated {relTime(s.last_validated)}
                            </span>
                          </span>
                        ))}
                      </div>
                    ) : (
                      <div className="sub">No sessions seeded for this project.</div>
                    )}
                    <div className="acts">
                      <button className="btn sm" disabled>⚿ Re-seed session</button>
                      <button className="btn sm" disabled>Validate now</button>
                      <span className="stub">◔ per-(credential×target) keying, re-seed &amp; validate are target-state (auth workstream)</span>
                    </div>
                  </div>
                )}

                <div className="card">
                  <h5 className="sec">Last login · soft re-login</h5>
                  <div className="outcome human">
                    <b>reuse-first (G4)</b> — a healthy session injects storage_state at context creation, so the engine
                    never types credentials per run. On expiry it soft-re-logins <b>once</b> at the pre-suite gate
                    (ok → refresh &amp; run · failed → park with the verbatim reason · hard-auth → park needs-human).
                    <div className="stub" style={{ marginTop: 10 }}>◔ soft-relogin outcome is carried on the park payload — target-state (no read endpoint)</div>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {connectOpen ? (
        <div className="scrim" onClick={(e) => e.target === e.currentTarget && setConnectOpen(false)}>
          <div className="modal">
            <div className="modal-h">
              <span className="tt">Connect a platform</span>
              <button className="x" onClick={() => setConnectOpen(false)}>×</button>
            </div>
            <div className="modal-b">
              <div className="field row2">
                <div><label>key</label><input placeholder="staging" value={form.key} onChange={set("key")} /></div>
                <div><label>base_url</label><input placeholder="https://app.example.com" value={form.base_url} onChange={set("base_url")} /></div>
              </div>
              <div className="field row2">
                <div><label>auth kind</label><select value={form.kind} onChange={set("kind")}><option value="auto">auto (form)</option><option value="clerk">clerk</option><option value="form">form</option><option value="none">none</option></select></div>
                <div><label>creds_ref</label><input placeholder="staging" value={form.creds_ref} onChange={set("creds_ref")} /><div className="hint">default: project id</div></div>
              </div>
              <div className="field row2">
                <div><label>signin_path (optional)</label><input placeholder="default per kind" value={form.signin_path} onChange={set("signin_path")} /></div>
                <div><label>persona</label><select value={form.persona} onChange={set("persona")}><option value="member">member</option><option value="admin">admin</option></select></div>
              </div>
              <div className="field row2">
                <div><label>username (optional)</label><input placeholder="leave blank if env covers it" value={form.username} onChange={set("username")} /></div>
                <div><label>password (optional)</label><input type="password" placeholder="•••••••• (write-only)" value={form.password} onChange={set("password")} /></div>
              </div>
              <div className="hint" style={{ margin: "-4px 0 14px" }}>
                Credentials are Fernet-encrypted and never read back. Leave blank to resolve from an{" "}
                <span className="val">ANNEAL_*</span> env layer. First login happens lazily on first run.
              </div>
              {err ? <div className="hint" style={{ color: "var(--v-red, #b00)", marginBottom: 10 }}>{err}</div> : null}
              <div style={{ display: "flex", gap: 9, alignItems: "center" }}>
                <button className="btn sm" style={{ marginLeft: "auto" }} onClick={() => setConnectOpen(false)}>Cancel</button>
                <button className="btn sm primary" disabled={busy || !form.key || !form.base_url} onClick={submitConnect}>{busy ? "Connecting…" : "Connect"}</button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
