// Author a test (Surface 1) — NL → D26 interview → hot-lane authoring → calibrated frozen test.
// Wired to the two-shot POST /projects/{p}/tests?test_key=&target_id=
// (returns needs_answers{questions[]} OR frozen{test_id,version,calibration,steps,bundle}).
//
// Authoring is SYNCHRONOUS (the POST blocks until frozen), so the "Authoring" step is a real
// in-flight state — the prototype's live per-phase stream is target-state (marked .stub). Freeze-review
// (inspect/scope the proposed expectation before freeze) is B6-cut → kept as .stub.
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { LabelEditor, useProjectLabels } from "../components/labels";
import { PROJECT, pickDefaultTarget } from "../lib/config";

type Target = components["schemas"]["Target"];
type AuthorFrozen = components["schemas"]["AuthorFrozen"];
type TerritoryNotes = components["schemas"]["TerritoryNotes"];
type Suite = components["schemas"]["Suite"];

const NEW_SUITE = "__new__";
type SuiteAttach = { status: "idle" | "pending" | "done" | "error"; name?: string; error?: string };

type Step = { module?: { ref: string; version: number }; action?: string; arg?: string; intent?: string; id?: string };

const DEFAULT_NL =
  "Log in as a member, create a campaign called 'QA smoke test', set its start and end dates, choose an agent, confirm it appears in the campaigns list, then delete it to clean up.";

const STAGES = ["compose", "authoring", "interview", "frozen"] as const;
type Stage = (typeof STAGES)[number];
const STEPPER: { key: string; label: string }[] = [
  { key: "compose", label: "Compose" },
  { key: "interview", label: "Interview" },
  { key: "authoring", label: "Authoring" },
  { key: "frozen", label: "Frozen" },
];
const PHASES = ["Recon → TerritoryNotes", "Author the flow (perceive → decide → act)", "Synthesize the oracle", "Calibrate (D7 falsifiability)", "Freeze v0"];

export function Author() {
  const nav = useNavigate();
  const [stage, setStage] = useState<Stage>("compose");
  const [nl, setNl] = useState(DEFAULT_NL);
  const [testKey, setTestKey] = useState("qa-smoke-campaign");
  const [targets, setTargets] = useState<Target[]>([]);
  const [targetId, setTargetId] = useState("");
  const [questions, setQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [frozen, setFrozen] = useState<AuthorFrozen | null>(null);
  const [territory, setTerritory] = useState<TerritoryNotes | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [suites, setSuites] = useState<Suite[]>([]);
  const [suiteChoice, setSuiteChoice] = useState(""); // "" none · suite.id · NEW_SUITE
  const [newSuiteName, setNewSuiteName] = useState("");
  const [suiteAttach, setSuiteAttach] = useState<SuiteAttach>({ status: "idle" });
  const [labels, setLabels] = useState<string[]>([]);
  const projectLabels = useProjectLabels(0);

  useEffect(() => {
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) {
        setTargets(data);
        setTargetId((cur) => cur || pickDefaultTarget(data));
      }
    });
    api.GET("/projects/{project}/suites", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) setSuites(data);
    });
  }, []);

  // Optionally file the freshly frozen test into a suite — an existing one (append, no clobber) or a
  // new named suite. Non-fatal: the test is already frozen regardless, so a failure here is surfaced
  // beside the freeze banner, never as an authoring error.
  async function attachToSuite(testId: string) {
    if (!suiteChoice) return;
    setSuiteAttach({ status: "pending" });
    try {
      if (suiteChoice === NEW_SUITE) {
        const name = newSuiteName.trim();
        const existing = suites.find((s) => s.name === name);
        if (existing) await appendMember(existing, testId);
        else {
          const { error } = await api.POST("/projects/{project}/suites", {
            params: { path: { project: PROJECT } },
            body: { name, member_test_ids: [testId] },
          });
          if (error) throw new Error(JSON.stringify(error));
        }
        setSuiteAttach({ status: "done", name });
      } else {
        const s = suites.find((x) => x.id === suiteChoice);
        if (!s) throw new Error("suite not found");
        await appendMember(s, testId);
        setSuiteAttach({ status: "done", name: s.name });
      }
    } catch (e) {
      setSuiteAttach({ status: "error", error: String(e) });
    }
  }

  async function appendMember(s: Suite, testId: string) {
    // PATCH replaces the whole array, so append to the current membership (deduped).
    const ids = [...new Set([...(s.member_test_ids ?? []), testId])];
    const { error } = await api.PATCH("/suites/{suite_id}", {
      params: { path: { suite_id: s.id } },
      body: { member_test_ids: ids },
    });
    if (error) throw new Error(JSON.stringify(error));
  }

  const answered = questions.every((q) => (answers[q] ?? "").trim());

  async function author(mergedAnswers?: Record<string, string>) {
    if (!targetId) return;
    const prev = stage;
    setStage("authoring");
    setError(null);
    const { data, error } = await api.POST("/projects/{project}/tests", {
      params: {
        path: { project: PROJECT },
        // labels are OPTIONAL and caller-supplied: the author prompts never assign them, and a missing
        // label never blocks a freeze (D26 blocks on unanswered interview questions, not on metadata)
        query: { test_key: testKey, target_id: targetId, ...(labels.length ? { labels } : {}) },
      },
      // persona picker removed from the UI — it was a hardcoded member/admin stub disconnected from the
      // real (project, persona) credentials. Send the engine default; the seeded credential is member.
      body: { nl, persona: "member", ...(mergedAnswers ? { answers: mergedAnswers } : {}) },
    });
    if (error || !data) {
      setError(typeof error === "string" ? error : JSON.stringify(error));
      setStage(prev === "compose" ? "compose" : "interview");
      return;
    }
    if (data.status === "needs_answers") {
      setQuestions(data.questions);
      setAnswers((a) => {
        const next = { ...a };
        for (const q of data.questions) if (!(q in next)) next[q] = "";
        return next;
      });
      setStage("interview");
    } else if (data.status === "error") {
      // streamed authoring delivers failure in-band (the 200 status line is committed before the
      // outcome is known) — surface it the same as a transport error.
      setError(data.detail);
      setStage(prev === "compose" ? "compose" : "interview");
    } else {
      setFrozen(data);
      setStage("frozen");
      attachToSuite(data.test_id);
      api
        .GET("/targets/{target_id}/territory", { params: { path: { target_id: targetId } } })
        .then(({ data: t }) => setTerritory(t ?? null));
    }
  }

  const curIdx = STEPPER.findIndex((s) => s.key === (stage === "authoring" ? "authoring" : stage));

  return (
    <div className="main auth">
      <header className="topbar">
        <nav className="crumbs">
          Author <span className="sep">/</span> <b>new test</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <h1>Author a test</h1>
        <p className="lede">
          Describe the flow in plain language. Anneal drives a real browser to author it, asks only the
          questions that change what the test asserts, proves the assertion can fail, then freezes it.
        </p>

        <div className="stepper">
          {STEPPER.map((s, i) => (
            <span key={s.key} style={{ display: "contents" }}>
              <span className={`s ${STEPPER[curIdx]?.key === s.key ? "active" : ""} ${i < curIdx ? "done" : ""}`}>
                <span className="dot">{i < curIdx ? "✓" : i + 1}</span> {s.label}
              </span>
              {i < STEPPER.length - 1 ? <span className="sep" /> : null}
            </span>
          ))}
        </div>

        {stage === "compose" ? (
          <div className="card">
            <div className="field">
              <label>flow — plain language</label>
              <textarea value={nl} onChange={(e) => setNl(e.target.value)} />
              <div className="hintline">skip selectors · the author resolves controls live and heals when the DOM changes</div>
            </div>
            <div className="grid2">
              <div className="field">
                <label>test key</label>
                <input value={testKey} onChange={(e) => setTestKey(e.target.value)} />
              </div>
              <div className="field">
                <label>target</label>
                <select value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>{t.key}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>suite — optional</label>
                <select value={suiteChoice} onChange={(e) => setSuiteChoice(e.target.value)}>
                  <option value="">— none —</option>
                  {suites.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                  <option value={NEW_SUITE}>＋ new suite…</option>
                </select>
                {suiteChoice === NEW_SUITE ? (
                  <input
                    style={{ marginTop: 6 }}
                    placeholder="new suite name"
                    value={newSuiteName}
                    onChange={(e) => setNewSuiteName(e.target.value)}
                  />
                ) : null}
                <div className="hintline">the frozen test is filed into this suite · leave as none to skip</div>
              </div>
            </div>
            <div className="field labelfield">
              <label>labels — optional</label>
              <div className="chipfield">
                <LabelEditor value={labels} options={projectLabels} onChange={setLabels} />
              </div>
              <div className="hintline">the frozen test carries these · a test can freeze with none</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 4 }}>
              <span className="hintline">authoring drives a real browser + the model — this can take a minute.</span>
              <button
                className="btn primary"
                style={{ marginLeft: "auto" }}
                disabled={!nl.trim() || !testKey.trim() || !targetId || (suiteChoice === NEW_SUITE && !newSuiteName.trim())}
                onClick={() => author()}
              >
                Author test →
              </button>
            </div>
            {error ? <div className="err">Authoring failed: {error}</div> : null}
          </div>
        ) : null}

        {stage === "authoring" ? (
          <div className="card">
            <div className="livehdr" style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 6 }}>
              <span className="pill v-hot"><span className="g">◐</span> hot lane</span>
              <span className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>driving a real browser + model — authoring synchronously…</span>
            </div>
            <div>
              {PHASES.map((p, i) => (
                <div className={`phase ${i === 0 ? "run" : "pending"}`} key={p}>
                  <span className="pg">{i === 0 ? "◐" : i + 1}</span>
                  <div className="pb"><div className="pt">{p}</div></div>
                </div>
              ))}
            </div>
            <div className="stub" style={{ marginTop: 10 }}>◔ live per-phase streaming is target-state — the POST returns when the freeze completes</div>
          </div>
        ) : null}

        {stage === "interview" ? (
          <>
            <div className="blocked-note">◑ needs answers — the freeze is <b style={{ margin: "0 3px" }}>blocked</b> until these are resolved (never guessed · D26)</div>
            {questions.map((q, i) => (
              <div className="qcard" key={i}>
                <div className="qh"><span className="pill v-hot"><span className="g">?</span> blocking · changes the assertion</span></div>
                <div className="q">{q}</div>
                <input
                  placeholder="your answer…"
                  value={answers[q] ?? ""}
                  onChange={(e) => setAnswers((a) => ({ ...a, [q]: e.target.value }))}
                />
              </div>
            ))}
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <button className="btn sm" onClick={() => setStage("compose")}>← Edit flow</button>
              <button className="btn primary" style={{ marginLeft: "auto" }} disabled={!answered} onClick={() => author(answers)}>
                Continue authoring →
              </button>
            </div>
            {error ? <div className="err">Authoring failed: {error}</div> : null}
          </>
        ) : null}

        {stage === "frozen" && frozen ? <Frozen frozen={frozen} testKey={testKey} territory={territory} suiteAttach={suiteAttach} onReset={() => { setStage("compose"); setFrozen(null); setQuestions([]); setAnswers({}); setSuiteAttach({ status: "idle" }); }} nav={nav} /> : null}
      </div>
    </div>
  );
}

function Frozen({ frozen, testKey, territory, suiteAttach, onReset, nav }: { frozen: AuthorFrozen; testKey: string; territory: TerritoryNotes | null; suiteAttach: SuiteAttach; onReset: () => void; nav: ReturnType<typeof useNavigate> }) {
  const bundle = (frozen.bundle ?? {}) as Record<string, unknown>;
  const steps = (bundle.steps as Step[] | undefined) ?? [];
  const notes = (territory?.notes ?? {}) as Record<string, unknown>;
  const routes = (notes.routes as string[]) ?? [];
  const widgets = (notes.widgets as string[]) ?? [];
  return (
    <>
      <div className="fbanner">
        <span className="pill v-green"><span className="g">✓</span> frozen{frozen.calibration ? " · calibrated" : ""}</span>
        <span className="big">{testKey} · v{frozen.version}</span>
        <span className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>{frozen.steps} steps</span>
        {suiteAttach.status === "pending" ? <span className="pill">◐ adding to suite…</span> : null}
        {suiteAttach.status === "done" ? <span className="pill v-green">✓ added to suite “{suiteAttach.name}”</span> : null}
        {suiteAttach.status === "error" ? <span className="pill v-red" title={suiteAttach.error}>⚠ suite add failed</span> : null}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button className="btn sm" onClick={onReset}>Author another</button>
          <button className="btn sm primary" onClick={() => nav(`/tests/${frozen.test_id}`)}>Open test →</button>
        </div>
      </div>

      <div className="card">
        <h5 className="sec">Freeze review <span className="stub" style={{ marginLeft: 6 }}>target-state — today it freezes one-shot (B6)</span></h5>
        <div className="cap" style={{ marginBottom: 0 }}>
          Inspecting / scoping / dropping a proposed expectation before committing is target-state — the test froze one-shot.
        </div>
      </div>

      <div className="card">
        <h5 className="sec">Frozen flow — {steps.length} steps</h5>
        {steps.map((s, i) => {
          if (s.module) {
            return (
              <div className="fstep" key={i}>
                <span className="n">{i}</span>
                <div><span className="act mod">module</span>{s.module.ref} · v{s.module.version}</div>
              </div>
            );
          }
          const act = s.action ?? "";
          const cls = act === "assert" ? "assert" : act === "goto" ? "goto" : "";
          return (
            <div className="fstep" key={i}>
              <span className="n">{i}</span>
              <div><span className={`act ${cls}`}>{act}</span>{s.intent ?? s.id}</div>
            </div>
          );
        })}
      </div>

      {frozen.calibration ? (
        <div className="card" style={{ borderColor: "color-mix(in srgb,var(--v-green) 30%,var(--line))", background: "color-mix(in srgb,var(--v-green-bg) 35%,var(--panel))" }}>
          <h3>✓ Calibrated (D7) — the oracle was proven falsifiable</h3>
          <div className="cap" style={{ marginBottom: 0 }}>
            A green replay only means something if the assertion could have failed. Proof recorded at freeze:
          </div>
          <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap", margin: "10px 0 0" }}>{JSON.stringify(frozen.calibration, null, 2)}</pre>
        </div>
      ) : null}

      {routes.length || widgets.length ? (
        <div className="card">
          <h5 className="sec">Learned about the target — TerritoryNotes (D25)</h5>
          <div className="stepchips">
            {routes.length ? <span className="sc">routes: {routes.join(", ")}</span> : null}
            {widgets.length ? <span className="sc">widgets: {widgets.join(", ")}</span> : null}
          </div>
          <div className="review-note">recon is persisted so future authoring &amp; selection reuse it · <Link to="/territory">open Territory →</Link></div>
        </div>
      ) : null}
    </>
  );
}
