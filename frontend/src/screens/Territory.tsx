// Territory (Target) — what recon learned about a target (D25 TerritoryNotes). Wired to GET /targets/{id}/territory + GET /projects/{p}/targets.
//
// STALE MARKER removed: the prototype says "no read route yet — whole view target-state"; the read
// shipped in A2, so this renders REAL data. The notes are the raw recon shape author.py writes
// ({probe_path, routes[], widgets[], recon_sample, revealed[]}) — NOT the prototype's curated
// idioms/flows/terms prose (that was mock), so only the sections the real shape supports are shown.
// Add/edit a note is genuinely target-state (write-only path, no edit route) → kept as .stub.
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { PROJECT, pickDefaultTarget } from "../lib/config";
import { relTime } from "../lib/format";

type Target = components["schemas"]["Target"];
type TerritoryNotes = components["schemas"]["TerritoryNotes"];

export function Territory() {
  const [targets, setTargets] = useState<Target[]>([]);
  const [targetId, setTargetId] = useState("");
  const [notes, setNotes] = useState<TerritoryNotes | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "empty">("loading");
  const [q, setQ] = useState("");

  useEffect(() => {
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) {
        setTargets(data);
        setTargetId((cur) => cur || pickDefaultTarget(data));
      }
    });
  }, []);

  useEffect(() => {
    if (!targetId) return;
    setStatus("loading");
    setNotes(null);
    api.GET("/targets/{target_id}/territory", { params: { path: { target_id: targetId } } }).then(({ data, error }) => {
      if (error || !data) setStatus("empty");
      else {
        setNotes(data);
        setStatus("ok");
      }
    });
  }, [targetId]);

  const n = (notes?.notes ?? {}) as Record<string, unknown>;
  const routes = (n.routes as string[]) ?? [];
  const widgets = (n.widgets as string[]) ?? [];
  const revealed = (n.revealed as { opener?: string; kind?: string }[]) ?? [];
  const probePath = n.probe_path as string | undefined;
  const reconSample = n.recon_sample as string | undefined;

  const needle = q.trim().toLowerCase();
  const m = (s: string) => !needle || s.toLowerCase().includes(needle);
  const fRoutes = routes.filter(m);
  const fWidgets = widgets.filter(m);
  const fRevealed = revealed.filter((r) => m(`${r.opener} ${r.kind}`));
  const shownCount = fRoutes.length + fWidgets.length + fRevealed.length;

  return (
    <div className="main terr">
      <header className="topbar">
        <nav className="crumbs">
          Target <span className="sep">/</span> <b>Territory</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <div className="terr-hd">
          <div>
            <h1>Territory</h1>
            <div className="sub">
              What the engine learned about this target while authoring — its probe path, observed routes,
              widget roles, and a recon sample (D25 TerritoryNotes). Recon knowledge is reused to author and
              re-author faster.
            </div>
          </div>
          <div style={{ marginLeft: "auto" }}>
            <span className="selctl">
              <span className="lab">target</span>
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                {targets.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.key}
                  </option>
                ))}
              </select>
            </span>
          </div>
        </div>

        <div className="readbanner">
          <span className="t">read-only</span>
          <span>
            TerritoryNotes are read live (A2). <b>Adding / editing / teaching</b> the engine a note directly is
            target-state — recon writes them at authoring time.
          </span>
          <span className="stub" style={{ marginLeft: "auto" }}>◔ add/edit is target-state</span>
        </div>

        <div className="toolbar">
          <label className="search">
            <span className="i">⌕</span>
            <input placeholder="Search routes, widgets, revealed controls…" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <button className="btn sm" disabled>
            ＋ Add note <span className="stub">◔ target-state</span>
          </button>
          {notes ? (
            <span className="count-note">
              v{notes.version} · authored by {notes.authored_by} · {relTime(notes.created_at)}
              {notes.stale ? " · stale" : ""}
            </span>
          ) : null}
        </div>

        {status === "loading" ? (
          <div className="async">loading…</div>
        ) : status === "empty" ? (
          <div className="tgt-empty">No territory notes for this target yet — recon writes them at authoring time.</div>
        ) : (
          <>
            {fRoutes.length ? (
              <div className="sec">
                <div className="sec-h">
                  <span className="ico">◈</span>
                  <span className="name">Routes &amp; surfaces</span>
                  <span className="b">{fRoutes.length}</span>
                </div>
                <div className="notes">
                  {fRoutes.map((r) => (
                    <div className="note" key={r}>
                      <div className="term">{r}</div>
                      <div className="body">
                        {r === probePath ? "Probe path — the authenticated landing surface the §9 gate checks." : "Observed route."}
                        <div className="prov">
                          <span className="provchip">◔ observed · recon</span>
                          {r === probePath ? <span className="provchip">probe_path</span> : null}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {fWidgets.length ? (
              <div className="sec">
                <div className="sec-h">
                  <span className="ico">❖</span>
                  <span className="name">Widget idioms — observed roles</span>
                  <span className="b">{fWidgets.length}</span>
                </div>
                <div className="notes">
                  <div className="note">
                    <div className="term">ARIA roles</div>
                    <div className="body">
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {fWidgets.map((w) => (
                          <span className="chip" key={w}>{w}</span>
                        ))}
                      </div>
                      <div className="prov"><span className="provchip">◔ observed · recon</span></div>
                    </div>
                  </div>
                </div>
              </div>
            ) : null}

            {fRevealed.length ? (
              <div className="sec">
                <div className="sec-h">
                  <span className="ico">⇥</span>
                  <span className="name">Revealed controls</span>
                  <span className="b">{fRevealed.length}</span>
                </div>
                <div className="notes">
                  {fRevealed.map((r, i) => (
                    <div className="note" key={i}>
                      <div className="term">{r.opener}</div>
                      <div className="body">
                        Opens a <b>{r.kind}</b> — controls behind it are acted on after opening this first.
                        <div className="prov"><span className="provchip">◔ learned at authoring</span></div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {reconSample && !needle ? (
              <div className="sec">
                <div className="sec-h">
                  <span className="ico">¶</span>
                  <span className="name">Recon sample</span>
                </div>
                <div className="recon">{reconSample}</div>
              </div>
            ) : null}

            {shownCount === 0 && needle ? <div className="tgt-empty">No notes match your search.</div> : null}
            <div className="count-note" style={{ marginTop: 4 }}>{shownCount} entries</div>
          </>
        )}
      </div>
    </div>
  );
}
