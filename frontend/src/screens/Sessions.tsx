// Sessions (Target) — the seeded target-app logins Anneal replays tests through. Wired to GET /projects/{p}/sessions.
//
// The read is CREDENTIAL-keyed (SessionHealth: persona/state/seeded/last_validated) — there is NO
// target dimension in the read, so the prototype's per-target grouping is target-state and is not
// reproduced. Session health READ is real now (A2). Re-seed / validate-now have NO endpoint (owned
// by the auth workstream) → genuine .stub. A degraded session is the §9 parked_auth recovery case.
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { PROJECT } from "../lib/config";
import { fmtAbs, relTime } from "../lib/format";

type SessionHealth = components["schemas"]["SessionHealth"];

function statePill(state: string) {
  return state === "active" ? (
    <span className="pill v-green"><span className="g">●</span>Active</span>
  ) : state === "expired" ? (
    <span className="pill v-park"><span className="g">⏸</span>expired</span>
  ) : (
    <span className="pill v-park"><span className="g">○</span>{state}</span>
  );
}

export function Sessions() {
  const [sessions, setSessions] = useState<SessionHealth[] | null>(null);
  useEffect(() => {
    api
      .GET("/projects/{project}/sessions", { params: { path: { project: PROJECT } } })
      .then(({ data }) => setSessions(data ?? []));
  }, []);

  const expired = (sessions ?? []).filter((s) => s.state === "expired");

  return (
    <div className="main sess">
      <header className="topbar">
        <nav className="crumbs">
          Target <span className="sep">/</span> <b>Sessions</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <div className="sess-hd">
          <div>
            <h1>Sessions</h1>
            <div className="sub">
              The seeded target-app logins Anneal replays tests through — one per persona. The §9 probe
              gate validates a session before a run claims a test; a degraded one <b>parks</b> the run
              (<span className="mono">AUTH_EXPIRED</span>) and never counts as a failure. Re-seed to restore it.
            </div>
          </div>
          <div style={{ marginLeft: "auto" }}>
            <span className="stub">◔ per-target session keying is target-state (read is credential-keyed today)</span>
          </div>
        </div>

        {expired.length > 0 ? (
          <div className="parkbanner">
            <h4>
              <span className="pill v-park"><span className="g">⏸</span> parked · auth</span>
              The <b>{expired.map((s) => s.persona).join(", ")}</b> session{expired.length > 1 ? "s" : ""} expired
            </h4>
            <p>
              A degraded session's cookies no longer authenticate — the probe gate parks any run that would
              use it (<span className="mono">AUTH_EXPIRED</span>), rather than counting it as a red (§9). Re-seed
              to unblock those runs.
            </p>
            <div className="acts">
              <button className="btn primary sm" disabled>⚿ Re-seed session</button>
              <button className="btn sm" disabled>Validate now</button>
              <span className="stub">◔ re-seed / validate have no endpoint yet (auth workstream)</span>
            </div>
          </div>
        ) : null}

        {sessions === null ? (
          <div className="async">loading…</div>
        ) : sessions.length === 0 ? (
          <div className="tgt-empty">No seeded sessions for this project.</div>
        ) : (
          <div className="sgrid">
            {sessions.map((s) => (
              <div className={`scard ${s.state === "expired" ? "exp" : ""}`} key={s.credential_id}>
                <div className="top">
                  <span className="persona">{s.persona}</span>
                  <span className="chip">{s.credential_id.slice(0, 8)}</span>
                  <span className="state">{statePill(s.state)}</span>
                </div>
                <div className="kv">
                  <span className="k">last validated</span>
                  <span className="v" title={fmtAbs(s.last_validated)}>{relTime(s.last_validated)}</span>
                  <span className="k">seeded</span>
                  <span className="v" title={fmtAbs(s.seeded_at)}>{relTime(s.seeded_at)}</span>
                  <span className="k">storage state</span>
                  <span className="v">
                    <span className="dotline">
                      <span className={`d ${s.state === "active" ? "d-ok" : "d-no"}`} />
                      {s.state === "active" ? "valid auth cookies" : "no valid auth cookies"}
                    </span>
                  </span>
                </div>
                <div className="foot">
                  <button className={`btn sm ${s.state === "expired" ? "primary" : ""}`} disabled>
                    ⚿ Re-seed session
                  </button>
                  <button className="btn sm" disabled>Validate now</button>
                  <span className="stub" style={{ marginLeft: "auto" }}>◔ target-state</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
