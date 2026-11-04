// Test viewer — one test: intent, flow + oracle, D7 calibration, version lineage, run history,
// changelog. Wired to GET /tests/{id} (+ /versions,
// /versions/{v} for the bundle+calibration, /golden), GET /projects/{p}/runs?test_id=, and
// /projects/{p}/changelog (filtered client-side by test_id — no test_id param yet).
//
// Real vs stubbed: golden reference is REAL now (B4) — shown when present, honest
// empty state when 404 (empty slot is normal). Genuinely stubbed: Re-baseline (B5 cut) and version
// rollback (no route) — kept as .stub. The golden fixture has one frozen v1, null calibration, no
// golden and no changelog, so those sections render honest empty states rather than mock content.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { Shell } from "../components/Shell";
import { LabelEditor, useProjectLabels } from "../components/labels";
import { API_BASE, PROJECT } from "../lib/config";
import { fmtDuration, relTime } from "../lib/format";
import { Pill } from "../lib/verdict";

type TestDetail = components["schemas"]["TestDetail"];
type TestVersion = components["schemas"]["TestVersion"];
type TestVersionListItem = components["schemas"]["TestVersionListItem"];
type Golden = components["schemas"]["Golden"];
type RunListItem = components["schemas"]["RunListItem"];
type ChangelogEntry = components["schemas"]["ChangelogEntry"];
type Target = components["schemas"]["Target"];

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
type RunState = { state: "running" | "busy" | "error" } | { state: "done"; runId?: string; verdict?: string } | null;

type Tab = "overview" | "flow" | "versions" | "runs" | "changelog";
const PASS = new Set(["green", "green_with_heals", "provisional"]);
const SPARK_H: Record<string, number> = { green: 20, green_with_heals: 24, provisional: 28, red: 34, parked_auth: 30, parked_env: 30 };
const SPARK_C: Record<string, string> = { green: "sp-green", green_with_heals: "sp-heals", provisional: "sp-prov", red: "sp-red", parked_auth: "sp-park", parked_env: "sp-park" };

// a bundle step is either a module ref or an atomic action (loose shapes — bundle is untyped jsonb)
type Step = {
  module?: { ref: string; version: number };
  id?: string;
  action?: string;
  arg?: string;
  intent?: string;
  target?: { selector?: string; strategy?: string; fallbacks?: Record<string, unknown> };
  postcondition?: { all?: { kind?: string; state?: string; target?: { selector?: string } }[]; budget_ms?: number };
};

export function TestViewer() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [test, setTest] = useState<TestDetail | null>(null);
  const [head, setHead] = useState<TestVersion | null>(null);
  const [versions, setVersions] = useState<TestVersionListItem[]>([]);
  const [golden, setGolden] = useState<Golden | null>(null);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [changelog, setChangelog] = useState<ChangelogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [targets, setTargets] = useState<Target[]>([]);
  const [run, setRun] = useState<RunState>(null);
  const [savingLabels, setSavingLabels] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);
  const [labelBump, setLabelBump] = useState(0);
  const projectLabels = useProjectLabels(labelBump);

  /** Optimistic: paint the new set, PATCH, revert on failure (the server is the arbiter of shape). */
  async function saveLabels(next: string[]) {
    const prev = test;
    if (!prev) return;
    setLabelError(null);
    setSavingLabels(true);
    setTest({ ...prev, labels: next });
    const { data, error: err } = await api.PATCH("/tests/{test_id}", {
      params: { path: { test_id: id } },
      body: { labels: next },
    });
    setSavingLabels(false);
    if (data) {
      setTest(data);
      setLabelBump((b) => b + 1);              // counts changed → refresh the typeahead
    } else {
      setTest(prev);
      setLabelError(typeof err === "string" ? err : "could not save labels");
    }
  }

  useEffect(() => {
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => data && setTargets(data));
  }, []);

  useEffect(() => {
    let live = true;
    setTest(null);
    setError(null);
    setRun(null);
    (async () => {
      const t = await api.GET("/tests/{test_id}", { params: { path: { test_id: id } } });
      if (!live) return;
      if (t.error || !t.data) {
        setError(JSON.stringify(t.error));
        return;
      }
      setTest(t.data);
      const [hv, vs, gd, rr, cl] = await Promise.all([
        api.GET("/tests/{test_id}/versions/{version}", {
          params: { path: { test_id: id, version: t.data.head_version } },
        }),
        api.GET("/tests/{test_id}/versions", { params: { path: { test_id: id } } }),
        api.GET("/tests/{test_id}/golden", { params: { path: { test_id: id } } }),
        api.GET("/projects/{project}/runs", { params: { path: { project: PROJECT }, query: { test_id: id, limit: 12 } } }),
        api.GET("/projects/{project}/changelog", { params: { path: { project: PROJECT } } }),
      ]);
      if (!live) return;
      setHead(hv.data ?? null);
      setVersions(vs.data ?? []);
      setGolden(gd.data ?? null); // 404 → data undefined → null (empty slot is normal)
      setRuns(rr.data ?? []);
      setChangelog((cl.data ?? []).filter((c) => c.test_id === id));
    })();
    return () => {
      live = false;
    };
  }, [id]);

  const bundle = (head?.bundle ?? {}) as Record<string, unknown>;
  const intent = bundle.intent as { descriptor?: string; sources?: { kind?: string; verbatim?: string }[] } | undefined;
  const policy = bundle.policy as { mutating?: boolean; exclusive?: boolean } | undefined;
  const steps = (bundle.steps as Step[] | undefined) ?? [];
  const persona = bundle.persona as string | undefined;
  const nlSource = intent?.sources?.find((s) => s.kind === "nl")?.verbatim;
  const surfaces = useMemo(
    () => [...new Set(steps.filter((s) => s.action === "goto" && s.arg?.startsWith("/")).map((s) => s.arg!))],
    [steps],
  );
  const postconds = useMemo(
    () => steps.flatMap((s) => (s.postcondition?.all ?? []).map((p) => ({ ...p, stepIntent: s.intent }))),
    [steps],
  );

  const passRate = runs.length ? Math.round((runs.filter((r) => PASS.has(r.verdict)).length / runs.length) * 100) : null;
  const healCount = runs.filter((r) => r.verdict === "green_with_heals").length;

  // Run this test (async, B1): POST → 202 {suite_run_id} → poll /suite_runs/{id} to terminal.
  // Runs against the test's canonical target (verified_against), falling back to the first non-expired.
  async function runTest() {
    const tgt =
      targets.find((t) => t.key === head?.verified_against) ??
      targets.find((t) => !t.key.includes("expired")) ??
      targets[0];
    if (!tgt) {
      setRun({ state: "error" });
      return;
    }
    setRun({ state: "running" });
    const { data, error, response } = await api.POST("/projects/{project}/tests/{test_id}/run", {
      params: { path: { project: PROJECT, test_id: id }, query: { target_id: tgt.id } },
    });
    if (response?.status === 409) {
      setRun({ state: "busy" });
      return;
    }
    if (error || !data) {
      setRun({ state: "error" });
      return;
    }
    for (let i = 0; i < 600; i++) {
      const sr = await api.GET("/suite_runs/{suite_run_id}", { params: { path: { suite_run_id: data.suite_run_id } } });
      const d = sr.data;
      if (d && (d.state === "done" || d.state === "parked")) {
        setRun({ state: "done", runId: d.results?.[0]?.id, verdict: d.verdict ?? d.results?.[0]?.verdict });
        return;
      }
      await sleep(500);
    }
    setRun({ state: "error" });
  }

  if (error) return frame(<div className="async err">Couldn't load test: {error}</div>, "…");
  if (!test) return frame(<div className="async">loading test…</div>, "…");

  return frame(
    <div className="content">
      <div className="thead">
        <div>
          <h1>{test.test_key}</h1>
          <div className="meta">
            <span className="chip">head v{test.head_version}</span>
            {head ? <span className="chip">{head.state}</span> : null}
            {head?.ruling ? <span className="chip">{head.ruling}</span> : null}
            {head?.verified_against ? <span className="chip">canonical · {head.verified_against}</span> : null}
            {persona ? <span className="chip">persona {persona}</span> : null}
            {policy?.mutating ? <span className="chip">mutating</span> : null}
            {policy?.exclusive ? <span className="chip">exclusive</span> : null}
            {test.quarantined ? <span className="chip" style={{ color: "var(--v-red)" }}>quarantined</span> : null}
            {golden ? <span className="chip" style={{ color: "var(--v-heals)" }}>◉ golden</span> : null}
          </div>
          {/* Labels sit BELOW the chip run, not inside it: those chips describe the frozen artifact
              (version, state, canonical target, policy) and are read-only; labels are catalog metadata
              you edit. Each edit is one PATCH — it writes a changelog row, never a version. */}
          <div className="labelrow">
            <span className="k">labels</span>
            <LabelEditor
              value={test.labels}
              options={projectLabels}
              busy={savingLabels}
              onChange={saveLabels}
            />
            {labelError ? <span className="lerr">{labelError}</span> : null}
          </div>
        </div>
        <div className="acts">
          <a
            className="btn"
            href={`${API_BASE}/export/${id}/${test.head_version}`}
            target="_blank"
            rel="noreferrer"
          >
            ⤓ Export script
          </a>
          <button className="btn" disabled>
            ↻ Re-baseline <span className="stub" style={{ marginLeft: 4 }}>target-state</span>
          </button>
          <button className="btn primary" onClick={runTest} disabled={run?.state === "running" || targets.length === 0}>
            ▶ {run?.state === "running" ? "Running…" : "Run"}
          </button>
          {run?.state === "busy" ? (
            <span className="chip" style={{ color: "var(--v-park)" }}>⏸ target busy</span>
          ) : run?.state === "error" ? (
            <span className="chip" style={{ color: "var(--v-red)" }}>run failed</span>
          ) : run?.state === "done" ? (
            <span className="chip" style={{ display: "inline-flex", gap: 6 }}>
              <Pill verdict={run.verdict ?? "green"} />
              {run.runId ? <Link to={`/runs/${run.runId}`}>open run →</Link> : null}
            </span>
          ) : null}
        </div>
      </div>

      <div className="health">
        <div className="blk">
          <span className="k">last {runs.length} runs</span>
          <div className="spark">
            {[...runs].reverse().map((r) => (
              <i
                key={r.id}
                className={SPARK_C[r.verdict] ?? "sp-park"}
                style={{ height: SPARK_H[r.verdict] ?? 20 }}
                title={`${r.verdict} · v${r.test_version} · ${r.environment} · ${relTime(r.started_at)}`}
              />
            ))}
            {runs.length === 0 ? <span className="tv-empty">no runs yet</span> : null}
          </div>
        </div>
        <div className="blk">
          <span className="k">pass rate</span>
          <span className="v">
            {passRate == null ? "—" : `${passRate}%`}
            {runs.length ? <small> · {runs.filter((r) => PASS.has(r.verdict)).length}/{runs.length}</small> : null}
          </span>
        </div>
        <div className="blk">
          <span className="k">self-healed</span>
          <span className="v">{healCount}<small>×</small></span>
        </div>
        <div className="blk" style={{ marginLeft: "auto" }}>
          <span className="k">calibration (D7)</span>
          <span className="v" style={{ fontSize: 13 }}>
            {head?.calibration ? (
              <span className="pill v-green" style={{ fontSize: 11 }}>
                <span className="g">✓</span> falsifiable
              </span>
            ) : (
              <span className="pill v-park" style={{ fontSize: 11 }}>
                <span className="g">○</span> not calibrated
              </span>
            )}
          </span>
        </div>
      </div>

      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "overview"} onClick={() => setTab("overview")}>
          Overview
        </button>
        <button role="tab" aria-selected={tab === "flow"} onClick={() => setTab("flow")}>
          Flow &amp; oracle <span className="b">{steps.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "versions"} onClick={() => setTab("versions")}>
          Versions <span className="b">{versions.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "runs"} onClick={() => setTab("runs")}>
          Run history <span className="b">{runs.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "changelog"} onClick={() => setTab("changelog")}>
          Changelog <span className="b">{changelog.length}</span>
        </button>
      </div>

      {/* OVERVIEW */}
      <section className={`pane ${tab === "overview" ? "on" : ""}`}>
        <div className="card golden">
          <div className="ghead">
            <h5 className="sec" style={{ margin: 0 }}>Golden reference — how this test should look</h5>
          </div>
          {golden ? (
            <>
              <div className="gvideo">
                <video src={`${API_BASE}/tests/${id}/golden/video`} controls playsInline />
              </div>
              <div className="gmeta">
                <span>source: {golden.source}</span>
                {golden.reflects_version != null ? <span>· reflects v{golden.reflects_version}</span> : null}
                <span>· {relTime(golden.created_at)}</span>
              </div>
            </>
          ) : (
            <div className="gvideo">
              <div className="poster">
                <div className="pmock">
                  no golden reference recorded
                  <br />
                  <span style={{ fontSize: 10, letterSpacing: ".04em" }}>
                    capture from the authoring run or upload one (B4)
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <h5 className="sec">Intent — what this test guarantees</h5>
          <p style={{ margin: "0 0 12px", fontSize: 13 }}>{intent?.descriptor ?? "—"}</p>
          {nlSource ? (
            <>
              <h5 className="sec">Authored from (source: nl)</h5>
              <div className="quote">"{nlSource}"</div>
            </>
          ) : null}
        </div>

        <div className="card">
          <h5 className="sec">Coverage — observed surfaces</h5>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 14 }}>
            {surfaces.length ? (
              surfaces.map((s) => <span key={s} className="surf">{s}</span>)
            ) : (
              <span className="tv-empty">no navigational surfaces in the bundle</span>
            )}
          </div>
          <div className="kv">
            <span className="k">policy</span>
            <span className="v">{policy?.mutating ? "mutating" : "read-only"} · {policy?.exclusive ? "exclusive" : "non-exclusive"}</span>
            <span className="k">persona</span>
            <span className="v">{persona ?? "—"}</span>
            <span className="k">canonical target</span>
            <span className="v">{head?.verified_against ?? "—"} <span className="mono" style={{ color: "var(--muted)", fontSize: 11 }}>(only runs here may heal/re-author)</span></span>
            <span className="k">head version</span>
            <span className="v">v{test.head_version} ({head?.state}{head?.ruling ? `, ${head.ruling}` : ""})</span>
          </div>
        </div>

        <div className="card oracle">
          {head?.calibration ? (
            <>
              <h3>✓ Calibrated — the oracle was proven falsifiable (D7)</h3>
              <div className="cap">A green replay only means something if the assertion could have failed.</div>
              <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap", margin: 0 }}>
                {JSON.stringify(head.calibration, null, 2)}
              </pre>
            </>
          ) : (
            <>
              <h3>Calibration (D7)</h3>
              <div className="tv-empty">
                This version has no calibration record — the falsifiability proof (negative control +
                network discriminativeness) isn't attached to this frozen fixture.
              </div>
            </>
          )}
        </div>
      </section>

      {/* FLOW & ORACLE */}
      <section className={`pane ${tab === "flow" ? "on" : ""}`}>
        <div className="card">
          <h5 className="sec">Frozen flow — {steps.length} steps (head v{test.head_version})</h5>
          {steps.map((s, i) => {
            if (s.module) {
              return (
                <div className="fstep" key={i}>
                  <span className="n">{i}</span>
                  <div className="body">
                    <div className="intent">
                      <span className="act mod">module</span>
                      {s.module.ref} · v{s.module.version}
                    </div>
                  </div>
                </div>
              );
            }
            const act = s.action ?? "";
            const actCls = act === "assert" ? "assert" : act === "goto" ? "goto" : "";
            const loc = s.target?.strategy
              ? `${s.target.strategy}=${s.target.selector ?? ""}`
              : s.arg
                ? s.arg
                : null;
            const fb = s.target?.fallbacks ? Object.entries(s.target.fallbacks).map(([k, v]) => `${k}=${v}`).join(" · ") : null;
            const post = s.postcondition?.all?.[0];
            return (
              <div className="fstep" key={i}>
                <span className="n">{i}</span>
                <div className="body">
                  <div className="intent">
                    <span className={`act ${actCls}`}>{act}</span>
                    {s.intent ?? s.id}
                  </div>
                  {loc ? (
                    <div className="loc">
                      ↳ {loc}
                      {fb ? <span className="fb"> · fallback {fb}</span> : null}
                    </div>
                  ) : null}
                  {post ? (
                    <div className="loc" style={{ color: "var(--v-green)" }}>
                      ✓ postcondition: {post.kind} {post.state ?? ""} {post.target?.selector ?? ""}
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
        <div className="card oracle">
          <h5 className="sec">Oracle — the postconditions this test asserts</h5>
          {postconds.length ? (
            postconds.map((p, i) => (
              <div className="pred" key={i}>
                <span className="kind">{p.kind}</span>
                {p.target?.selector ? <span className="val">{p.target.selector}</span> : null}
                {p.state ? <b>{p.state}</b> : null}
                <span style={{ marginLeft: "auto", color: "var(--muted)" }}>{p.stepIntent}</span>
              </div>
            ))
          ) : (
            <div className="tv-empty">no postconditions recorded</div>
          )}
        </div>
      </section>

      {/* VERSIONS */}
      <section className={`pane ${tab === "versions" ? "on" : ""}`}>
        <div className="card">
          <h5 className="sec">Version lineage — draft → calibrated → frozen; only frozen replays</h5>
          <div style={{ marginTop: 6 }}>
            {[...versions]
              .sort((a, b) => b.version - a.version)
              .map((v) => {
                const cls = v.ruling === "provisional" ? "prov" : v.reason?.includes("heal") ? "heal" : "";
                return (
                  <div className={`vitem ${cls}`} key={v.version}>
                    <span className="dot" />
                    <div className="vb">
                      <div className="vh">
                        <span className="vv">v{v.version}</span>
                        {v.ruling ? (
                          <span className={`pill ${v.ruling === "provisional" ? "v-prov" : "v-green"}`} style={{ fontSize: 10 }}>
                            <span className="g">{v.ruling === "provisional" ? <span className="g-prov" /> : "✓"}</span>
                            {v.ruling}
                          </span>
                        ) : null}
                        <span className="chip">{v.state}</span>
                        {v.version === test.head_version ? <span className="head">HEAD</span> : null}
                        {v.version !== test.head_version ? (
                          <button className="btn" style={{ padding: "2px 8px", fontSize: 11, marginLeft: "auto" }} disabled>
                            ↩ roll back <span className="stub" style={{ marginLeft: 4 }}>target-state</span>
                          </button>
                        ) : null}
                      </div>
                      <div className="vr">{v.reason}</div>
                      <div className="vmeta">
                        <span>reason: {v.reason}</span>
                        {v.triggered_by ? <span>triggered by: {v.triggered_by}</span> : null}
                        {v.parent_version != null ? <span>parent: v{v.parent_version}</span> : null}
                        {v.incident_id ? <span>incident: {v.incident_id.slice(0, 8)}</span> : null}
                      </div>
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      </section>

      {/* RUN HISTORY */}
      <section className={`pane ${tab === "runs" ? "on" : ""}`}>
        <div className="card" style={{ padding: "14px 16px 8px" }}>
          <h5 className="sec">Last {runs.length} runs</h5>
          <div className="hrtable">
            <div className="hrhead">
              <span>verdict</span>
              <span>ver</span>
              <span>environment</span>
              <span>class</span>
              <span>duration</span>
              <span>when</span>
            </div>
            {runs.map((r) => (
              <button key={r.id} className="hrrow" onClick={() => nav(`/runs/${r.id}`)}>
                <span>
                  <Pill verdict={r.verdict} />
                </span>
                <span className="ver">v{r.test_version}</span>
                <span className="env">
                  {r.environment}
                  {r.off_canonical ? <span className="off">off-canonical</span> : null}
                </span>
                <span className="ec">{r.error_class ?? ""}</span>
                <span className="env">{fmtDuration(r.duration_ms)}</span>
                <span className="when">{relTime(r.started_at)}</span>
              </button>
            ))}
            {runs.length === 0 ? <div className="tv-empty" style={{ padding: "12px 4px" }}>no runs recorded for this test</div> : null}
          </div>
        </div>
      </section>

      {/* CHANGELOG */}
      <section className={`pane ${tab === "changelog" ? "on" : ""}`}>
        <div className="card">
          <h5 className="sec">Changelog — append-only record for this test</h5>
          {changelog.length ? (
            changelog.map((c) => (
              <div className="clog" key={c.id}>
                <span className={`kind kind-${c.kind}`}>{c.kind}</span>
                <div>
                  <div className="cs">{c.summary}</div>
                  {c.ruling_note ? <div className="cs" style={{ fontStyle: "italic", color: "var(--ink-2)" }}>"{c.ruling_note}"</div> : null}
                  <div className="cw">{relTime(c.created_at)}</div>
                </div>
              </div>
            ))
          ) : (
            <div className="tv-empty">no changelog entries for this test yet</div>
          )}
        </div>
      </section>
    </div>,
    test.test_key,
  );
}

// shell wrapper so loading/error states keep the chrome.
function frame(body: React.ReactNode, title: string) {
  return (
    <Shell className="tv" crumbs={<><Link to="/runs">Tests</Link> <span className="sep">/</span> <b>{title}</b></>}>
      {body}
    </Shell>
  );
}
