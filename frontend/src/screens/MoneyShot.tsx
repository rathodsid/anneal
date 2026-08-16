// Incident money-shot — Direction B (triage cockpit): rule a drift feature-vs-bug per member.
// Wired to GET /incidents/{id} + the per-member red/verify run steps & artifacts, POST /incidents/{id}/ruling.
//
// Ruling shape: collect each member's ✓feature / ✕bug CLIENT-SIDE, then submit
// ONE atomic POST {members:{test_id:...}, ruling_note}. 409 if already ruled. Evidence bytes are real
// (/artifacts). The pixel diff overlay/onion/slider modes stay target-state (kept as .stub); the
// side-by-side screenshot + step delta is wired for real.
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { artifactUrl, PROJECT } from "../lib/config";
import { Pill } from "../lib/verdict";

type IncidentDetail = components["schemas"]["IncidentDetail"];
type IncidentMember = components["schemas"]["IncidentMember"];
type Incident = components["schemas"]["Incident"];
type StepEvent = components["schemas"]["StepEvent"];

// ── Incidents list (/incidents) ───────────────────────────────────────────
export function IncidentsList() {
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  useEffect(() => {
    api
      .GET("/projects/{project}/incidents", { params: { path: { project: PROJECT } } })
      .then(({ data }) => setIncidents(data ?? []));
  }, []);
  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <b>Incidents</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <h1 style={{ margin: "0 0 12px", fontSize: 20 }}>Incidents</h1>
        {incidents === null ? (
          <div className="async">loading…</div>
        ) : incidents.length === 0 ? (
          <div className="ms-empty">No open incidents — nothing to rule.</div>
        ) : (
          <div className="rtable">
            <div className="rhead" style={{ gridTemplateColumns: "150px 1fr 160px 120px" }}>
              <span>state</span>
              <span>change</span>
              <span>surface</span>
              <span>class</span>
            </div>
            {incidents.map((i) => (
              <Link
                key={i.id}
                to={`/incidents/${i.id}`}
                className="rrow"
                style={{ gridTemplateColumns: "150px 1fr 160px 120px", textDecoration: "none" }}
              >
                <span>
                  <Pill verdict={i.ruling ? "green" : "red"} />
                </span>
                <span className="tk">
                  <span className="k">{i.change_brief?.slice(0, 70) ?? i.klass}</span>
                </span>
                <span className="env">{i.ck_surface}</span>
                <span className="trig">{i.ck_error_class}</span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// pair the two-row-per-step events → terminal row per step_index, in order.
function pairSteps(raw: StepEvent[]): StepEvent[] {
  const byIdx = new Map<number, StepEvent>();
  for (const e of raw) byIdx.set(e.step_index, e);
  return [...byIdx.values()].sort((a, b) => a.step_index - b.step_index);
}

type DiffRow = { kind: "keep" | "add" | "rm"; label: string };

// ── Money-shot (/incidents/:id) ───────────────────────────────────────────
export function MoneyShot() {
  const { id = "" } = useParams();
  const [inc, setInc] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sel, setSel] = useState(0);
  const [rulings, setRulings] = useState<Record<string, "feature" | "bug">>({});
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitMsg, setSubmitMsg] = useState<string | null>(null);
  const [diff, setDiff] = useState<Record<string, DiffRow[]>>({});

  async function load() {
    const { data, error } = await api.GET("/incidents/{incident_id}", { params: { path: { incident_id: id } } });
    if (error) setError(JSON.stringify(error));
    else setInc(data ?? null);
  }
  useEffect(() => {
    setInc(null);
    setError(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const members = inc?.members ?? [];
  const member: IncidentMember | undefined = members[sel];

  // lazy-load the step diff for the selected member (red vs verify run intents).
  useEffect(() => {
    if (!member || diff[member.test_id]) return;
    const red = member.red_run_id;
    const verify = member.verify_run_id;
    (async () => {
      const [rr, vr] = await Promise.all([
        red ? api.GET("/runs/{run_result_id}/steps", { params: { path: { run_result_id: red } } }) : Promise.resolve({ data: [] as StepEvent[] }),
        verify ? api.GET("/runs/{run_result_id}/steps", { params: { path: { run_result_id: verify } } }) : Promise.resolve({ data: [] as StepEvent[] }),
      ]);
      const redSteps = pairSteps((rr.data as StepEvent[]) ?? []);
      const verifySteps = pairSteps((vr.data as StepEvent[]) ?? []);
      const redIntents = new Set(redSteps.map((s) => s.intent || s.action));
      const verifyIntents = new Set(verifySteps.map((s) => s.intent || s.action));
      const rows: DiffRow[] = [];
      for (const s of verifySteps) {
        const label = s.intent || s.action;
        rows.push({ kind: redIntents.has(label) ? "keep" : "add", label });
      }
      for (const s of redSteps) {
        const label = s.intent || s.action;
        if (!verifyIntents.has(label)) rows.push({ kind: "rm", label });
      }
      setDiff((d) => ({ ...d, [member.test_id]: rows }));
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [member?.test_id]);

  const alreadyRuled = !!inc?.ruling;
  const ruledCount = Object.keys(rulings).length;

  function setRule(testId: string, r: "feature" | "bug") {
    setRulings((prev) => ({ ...prev, [testId]: r }));
  }
  function ruleAllFeature() {
    setRulings((prev) => {
      const next = { ...prev };
      for (const m of members) if (m.verify_run_id && !next[m.test_id]) next[m.test_id] = "feature";
      return next;
    });
  }
  async function submit() {
    if (!ruledCount || submitting) return;
    setSubmitting(true);
    setSubmitMsg(null);
    const { data, error, response } = await api.POST("/incidents/{incident_id}/ruling", {
      params: { path: { incident_id: id } },
      body: { members: rulings, ruling_note: note || null, ruled_by: "web" },
    });
    setSubmitting(false);
    if (response?.status === 409) {
      setSubmitMsg("This incident was already ruled (409). Reloading its state.");
      load();
      return;
    }
    if (error || !data) {
      setSubmitMsg(`Ruling failed: ${JSON.stringify(error)}`);
      return;
    }
    setSubmitMsg(`Ruled ${data.ruling} — ${data.affected.length} member(s) applied.`);
    load(); // reflect the now-set incident ruling
  }

  if (error) return frame(<div className="async err">Couldn't load incident: {error}</div>, id, null);
  if (!inc) return frame(<div className="async">loading incident…</div>, id, null);

  const stateLabel = alreadyRuled ? `ruled · ${inc.ruling}` : "open · needs ruling";

  return frame(
    <div className="cockpit">
      {/* left — member list */}
      <div className="b-list">
        <div className="b-list-h">
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span className="chip mono">{inc.ck_error_class}</span>
            <span className="chip mono">{inc.ck_surface}</span>
          </div>
          <div className="brief">{inc.change_brief}</div>
          <div className="b-progress">
            <span>ruled</span>
            <span className="bar">
              <i style={{ width: `${members.length ? (ruledCount / members.length) * 100 : 0}%` }} />
            </span>
            <span>
              {ruledCount} / {members.length}
            </span>
          </div>
        </div>
        {!alreadyRuled ? (
          <div className="b-bulk">
            <button className="btn sm feat" onClick={ruleAllFeature}>
              Rule all remaining Feature
            </button>
            <span className="chip mono" style={{ marginLeft: "auto", border: 0, color: "var(--muted)" }}>
              {members.length} tests
            </span>
          </div>
        ) : null}
        <div className="b-members">
          {members.map((m, i) => {
            const verdict = m.verify_run_id ? "provisional" : "red";
            const r = alreadyRuled ? m.ruling : rulings[m.test_id];
            return (
              <div
                key={m.test_id}
                className={`member ${i === sel ? "sel" : ""} ${r ? "ruled" : ""}`}
                role="button"
                tabIndex={0}
                onClick={() => setSel(i)}
              >
                <span className={`pill ${verdict === "red" ? "v-red" : "v-prov"}`} style={{ flex: "none" }}>
                  <span className="g">{verdict === "red" ? "✕" : <span className="g-prov" />}</span>
                </span>
                <div className="mbody">
                  <div className="mnm">{m.test_key}</div>
                  <div className="msub">
                    v{m.head_version}
                    {m.applied_version ? ` → v${m.applied_version}` : ""}
                    {inc.representative_test_id === m.test_id ? " · representative" : ""}
                  </div>
                  {r ? (
                    <div className={`mrule ${r === "feature" ? "feat" : "bug"}`}>
                      {r === "feature" ? "✓ ruled feature" : "✕ ruled bug"}
                    </div>
                  ) : null}
                </div>
                {!alreadyRuled ? (
                  <div className="macts" onClick={(e) => e.stopPropagation()}>
                    <button
                      className={`iconbtn feat ${r === "feature" ? "on" : ""}`}
                      title="feature"
                      onClick={() => setRule(m.test_id, "feature")}
                    >
                      ✓
                    </button>
                    <button
                      className={`iconbtn bug ${r === "bug" ? "on" : ""}`}
                      title="bug"
                      onClick={() => setRule(m.test_id, "bug")}
                    >
                      ✕
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>

      {/* right — selected member detail */}
      <div className="b-detail">
        {member ? (
          <MemberDetail
            member={member}
            incidentId={id}
            diff={diff[member.test_id]}
            ruled={alreadyRuled ? member.ruling ?? null : rulings[member.test_id] ?? null}
          />
        ) : (
          <div className="ms-empty">This incident has no members.</div>
        )}
      </div>

      {/* footer — ruling submit */}
      <div className="b-foot">
        {alreadyRuled ? (
          <div className="impact">
            Incident ruled <b>{inc.ruling}</b>
            {inc.ruling_note ? ` — "${inc.ruling_note}"` : ""}. <Link to="/runs">back to runs →</Link>
          </div>
        ) : (
          <>
            <div className="note">
              <input
                placeholder="ruling note — the 'why', read by the next agent when this surface drifts again"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </div>
            <button className="btn primary" disabled={!ruledCount || submitting} onClick={submit}>
              {submitting ? "Submitting…" : `Submit ruling (${ruledCount}/${members.length})`}
            </button>
            <div className="impact">
              {submitMsg ?? `${Object.values(rulings).filter((r) => r === "feature").length} feature · ${Object.values(rulings).filter((r) => r === "bug").length} bug`}
            </div>
          </>
        )}
      </div>
    </div>,
    id,
    stateLabel,
  );
}

function MemberDetail({
  member,
  incidentId: _incidentId,
  diff,
  ruled,
}: {
  member: IncidentMember;
  incidentId: string;
  diff: DiffRow[] | undefined;
  ruled: "feature" | "bug" | null;
}) {
  const verdict = member.verify_run_id ? "provisional" : "red";
  const red = member.red_run_id;
  const verify = member.verify_run_id;
  const changed = useMemo(() => (diff ? diff.filter((d) => d.kind !== "keep").length : 0), [diff]);
  return (
    <>
      <div className="dh">
        <Pill verdict={verdict} />
        <div style={{ flex: 1 }}>
          <div className="nm">{member.test_key}</div>
          <div className="sub">
            head v{member.head_version}
            {member.applied_version ? ` · re-authored → v${member.applied_version}` : " · could not re-author"}
            {member.quarantined ? " · quarantined" : ""}
          </div>
        </div>
        {ruled ? (
          <span className={`pill ${ruled === "feature" ? "v-green" : "v-red"}`}>
            <span className="g">{ruled === "feature" ? "✓" : "✕"}</span>ruled {ruled}
          </span>
        ) : null}
      </div>

      <div className="b-ev">
        <span className="lab">RED RUN</span>
        <EvLink runId={red} kind="trace" label="trace" />
        <EvLink runId={red} kind="video" label="video" />
        <EvLink runId={red} kind="screenshot" label="screenshot" />
        <span className="lab" style={{ marginLeft: 8 }}>VERIFY RUN</span>
        <EvLink runId={verify} kind="trace" label="trace" />
        <EvLink runId={verify} kind="video" label="video" />
        <EvLink runId={verify} kind="screenshot" label="screenshot" />
      </div>

      <div className="diffwrap">
        <div className="diffbar">
          <div className="modes" role="tablist">
            <button aria-selected={true}>side-by-side</button>
            <button aria-selected={false} disabled>slider</button>
            <button aria-selected={false} disabled>onion</button>
            <button aria-selected={false} disabled>difference</button>
          </div>
          <span className="stub">◔ overlay/onion/slider diff modes are target-state</span>
          <span className="pct">{changed ? `▲ ${changed} step(s) changed` : ""}</span>
        </div>
        <div className="diffbody">
          <div>
            <div className="diffcap">◱ BEFORE · red run</div>
            <Frame runId={red} />
          </div>
          <div>
            <div className="diffcap">◲ AFTER · {verify ? "verify run" : "no re-author"}</div>
            <Frame runId={verify} />
          </div>
        </div>
        <div className="steps-delta">
          {diff === undefined ? (
            <div className="sd-row keep">loading step diff…</div>
          ) : diff.length ? (
            diff.map((d, i) => (
              <div key={i} className={`sd-row ${d.kind}`}>
                <span className="mk">{d.kind === "add" ? "＋" : d.kind === "rm" ? "✕" : "·"}</span>
                {d.label}
              </div>
            ))
          ) : (
            <div className="sd-row keep">no step-level differences detected</div>
          )}
        </div>
      </div>

      {!verify ? (
        <div className="bugcard">
          <b style={{ color: "var(--v-red)" }}>⚠ Could not re-author.</b> No verify run was produced — the
          intended flow matched on no rung after the change, so it may no longer be achievable. Ruling{" "}
          <b>bug</b> rolls back and files a repro carrying this test as coverage.
        </div>
      ) : null}
    </>
  );
}

function EvLink({ runId, kind, label }: { runId?: string | null; kind: "trace" | "video" | "screenshot"; label: string }) {
  if (!runId) return <span className="evlink"><span className="no">○</span> {label}</span>;
  return (
    <a className="evlink" href={artifactUrl(runId, kind)} target="_blank" rel="noreferrer">
      <span className="ok">●</span> {label}
    </a>
  );
}

function Frame({ runId }: { runId?: string | null }) {
  const [err, setErr] = useState(false);
  if (!runId || err) {
    return (
      <div className="framed">
        <div className="none">{runId ? "no screenshot" : "no run"}</div>
      </div>
    );
  }
  return (
    <div className="framed">
      <img src={artifactUrl(runId, "screenshot")} alt="run screenshot" onError={() => setErr(true)} />
    </div>
  );
}

function frame(body: React.ReactNode, id: string, stateLabel: string | null) {
  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <Link to="/incidents">Incidents</Link> <span className="sep">/</span> <b>{id.slice(0, 8)}</b>
          {stateLabel ? (
            <span className={`pill ${stateLabel.startsWith("open") ? "v-red" : "v-green"}`} style={{ marginLeft: 6 }}>
              <span className="g">{stateLabel.startsWith("open") ? "✕" : "✓"}</span>
              {stateLabel}
            </span>
          ) : null}
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      {body}
    </div>
  );
}
