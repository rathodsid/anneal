// Suite & run (Surface 2) — run a saved suite (or a change-driven selection) against a target, and
// create/edit suite membership. Wired to the real endpoints.
//
// STALE MARKERS removed: the prototype marks suite CRUD ("no route yet") and calls
// launch "synchronous". Both are now real — A2/A4 shipped GET/POST/PATCH suites, and B1 made launch
// async (202 + suite_run_id, poll /suite_runs/{id} to terminal). Genuinely still stubbed: session
// re-seed for a parked_auth suite (owned by the auth workstream, no endpoint) — kept as .stub.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { RunModal, pollSuiteRun as pollRun, type RunModalData } from "../components/RunModal";
import { PROJECT, pickDefaultTarget } from "../lib/config";
import { fmtDuration } from "../lib/format";
import { Pill } from "../lib/verdict";

type Suite = components["schemas"]["Suite"];
type Target = components["schemas"]["Target"];
type TestListItem = components["schemas"]["TestListItem"];
type RunListItem = components["schemas"]["RunListItem"];
type SelectionPick = components["schemas"]["SelectionPick"];


type RunModalState = RunModalData | null;

type FormState = { mode: "create" | "edit"; name: string; picked: Set<string> } | null;

export function Suite() {
  const nav = useNavigate();
  const { id: routeSuiteId } = useParams(); // /suites/:id — preselected when drilled from the index
  const [suites, setSuites] = useState<Suite[] | null>(null);
  const [suiteId, setSuiteId] = useState<string>(routeSuiteId ?? "");
  const [targets, setTargets] = useState<Target[]>([]);
  const [targetId, setTargetId] = useState<string>("");
  const [trigger, setTrigger] = useState<"manual" | "api" | "deploy_hook">("manual");
  const [tests, setTests] = useState<TestListItem[]>([]);
  const [latest, setLatest] = useState<Map<string, RunListItem>>(new Map());
  const [tab, setTab] = useState<"members" | "select">("members");

  const [change, setChange] = useState("");
  const [picks, setPicks] = useState<SelectionPick[] | null>(null);
  const [selecting, setSelecting] = useState(false);

  const [runModal, setRunModal] = useState<RunModalState>(null);
  const [form, setForm] = useState<FormState>(null);

  const keyOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const t of tests) m.set(t.id, t.test_key);
    return (testId: string) => m.get(testId) ?? testId.slice(0, 8);
  }, [tests]);

  async function refreshSuites() {
    const { data } = await api.GET("/projects/{project}/suites", { params: { path: { project: PROJECT } } });
    if (data) {
      const sorted = [...data].sort((a, b) => a.name.localeCompare(b.name));
      setSuites(sorted);
      setSuiteId((cur) => cur || routeSuiteId || (sorted[0]?.id ?? ""));
    }
  }

  useEffect(() => {
    refreshSuites();
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) {
        setTargets(data);
        setTargetId(pickDefaultTarget(data));
      }
    });
    api.GET("/projects/{project}/tests", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) setTests(data);
    });
    api
      .GET("/projects/{project}/runs", { params: { path: { project: PROJECT }, query: { limit: 200 } } })
      .then(({ data }) => {
        if (!data) return;
        const m = new Map<string, RunListItem>();
        for (const r of data) if (!m.has(r.test_id)) m.set(r.test_id, r); // newest-first → first wins
        setLatest(m);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const suite = suites?.find((s) => s.id === suiteId) ?? null;
  const members = suite?.member_test_ids ?? [];

  // ── launch (async 202 → poll) ────────────────────────────────────────────
  async function pollSuiteRun(suiteRunId: string) {
    await pollRun(suiteRunId, (sr) => setRunModal((m) => (m ? { ...m, suiteRun: sr } : m)));
    setRunModal((m) => (m ? { ...m, launching: false } : m));
  }

  async function launchSuite() {
    if (!suite || !targetId) return;
    setRunModal({
      title: `Suite run — ${suite.name}`,
      memberKeys: members.map(keyOf),
      suiteRun: null,
      launching: true,
    });
    const { data, error, response } = await api.POST("/projects/{project}/suites/{suite_id}/runs", {
      params: { path: { project: PROJECT, suite_id: suite.id }, query: { target_id: targetId, trigger } },
    });
    if (response?.status === 409) {
      const activeId = (error as { detail?: { active_suite_run_id?: string } } | undefined)?.detail
        ?.active_suite_run_id;
      setRunModal((m) => (m ? { ...m, launching: false, busyActiveId: activeId ?? "unknown" } : m));
      return;
    }
    if (error || !data) {
      setRunModal((m) => (m ? { ...m, launching: false, error: JSON.stringify(error) } : m));
      return;
    }
    pollSuiteRun(data.suite_run_id);
  }

  // ── change-driven selection ──────────────────────────────────────────────
  async function previewSelection() {
    if (!targetId) return;
    setSelecting(true);
    setPicks(null);
    const { data } = await api.POST("/projects/{project}/runs/select", {
      params: { path: { project: PROJECT }, query: { target_id: targetId } },
      body: { change, launch: false },
    });
    setSelecting(false);
    if (data) setPicks(data.selection);
  }

  async function runSelection() {
    if (!targetId || !picks) return;
    const selectedKeys = picks.filter((p) => p.selected).map((p) => p.test_key);
    setRunModal({ title: "Selection run", memberKeys: selectedKeys, suiteRun: null, launching: true });
    const { data, error, response } = await api.POST("/projects/{project}/runs/select", {
      params: { path: { project: PROJECT }, query: { target_id: targetId } },
      body: { change, launch: true },
    });
    if (response?.status === 409) {
      setRunModal((m) => (m ? { ...m, launching: false, busyActiveId: "unknown" } : m));
      return;
    }
    if (error || !data?.run) {
      setRunModal((m) => (m ? { ...m, launching: false, error: "launch returned no run" } : m));
      return;
    }
    pollSuiteRun(data.run.suite_run_id);
  }

  // ── create / edit membership ─────────────────────────────────────────────
  async function saveForm() {
    if (!form) return;
    const member_test_ids = [...form.picked];
    if (form.mode === "create") {
      await api.POST("/projects/{project}/suites", {
        params: { path: { project: PROJECT } },
        body: { name: form.name, member_test_ids },
      });
    } else {
      await api.PATCH("/suites/{suite_id}", {
        params: { path: { suite_id: suiteId } },
        body: { name: form.name, member_test_ids },
      });
    }
    setForm(null);
    await refreshSuites();
  }

  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <Link to="/suites">Suites</Link> <span className="sep">/</span> <b>{suite?.name ?? "…"}</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>

      <div className="content narrow">
        <div className="suite-hd">
          <div>
            <h1>{suite?.name ?? "Suites"}</h1>
            <div className="sub">Run a saved suite, or select tests by what changed (§11), against a target.</div>
            <div className="meta">
              <span className="selctl">
                <span className="lab">suite</span>
                <select value={suiteId} onChange={(e) => { setSuiteId(e.target.value); setPicks(null); }}>
                  {(suites ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </span>
              <span className="chip">{members.length} tests</span>
              <span className="chip">saved suite</span>
            </div>
          </div>
          <div className="runbar">
            <button className="btn" onClick={() => setForm({ mode: "create", name: "", picked: new Set() })}>
              ＋ New suite
            </button>
            <button
              className="btn"
              disabled={!suite}
              onClick={() => setForm({ mode: "edit", name: suite!.name, picked: new Set(members) })}
            >
              ⚙ Edit membership
            </button>
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
            <span className="selctl">
              <span className="lab">trigger</span>
              <select value={trigger} onChange={(e) => setTrigger(e.target.value as typeof trigger)}>
                <option value="manual">manual</option>
                <option value="api">api</option>
                <option value="deploy_hook">deploy_hook</option>
              </select>
            </span>
            <button className="btn primary" disabled={!suite || !targetId} onClick={launchSuite}>
              ▶ Run suite
            </button>
          </div>
        </div>

        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "members"} onClick={() => setTab("members")}>
            Members <span className="b">{members.length}</span>
          </button>
          <button role="tab" aria-selected={tab === "select"} onClick={() => setTab("select")}>
            Select by change
          </button>
        </div>

        {tab === "members" ? (
          <section>
            <div className="mtable">
              <div className="mhead">
                <span>test</span>
                <span>test id</span>
                <span>last verdict</span>
                <span>duration</span>
              </div>
              {members.map((tid) => {
                const last = latest.get(tid);
                return (
                  <div className="mrow" key={tid} onClick={() => nav(`/tests/${tid}`)} title={`open ${keyOf(tid)} in the test viewer`}>
                    <span className="k">{keyOf(tid)}</span>
                    <span className="cov">
                      <span className="s">{tid.slice(0, 8)}</span>
                    </span>
                    <span>{last ? <Pill verdict={last.verdict} /> : <span className="last">no runs yet</span>}</span>
                    <span className="last">{last ? fmtDuration(last.duration_ms) : "—"}</span>
                  </div>
                );
              })}
              {members.length === 0 ? <div className="mrow"><span className="last">empty suite</span></div> : null}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12 }}>
              <span className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>
                launch runs all {members.length} members (subset-launch is target-state)
              </span>
              <button
                className="btn primary sm"
                style={{ marginLeft: "auto" }}
                disabled={!suite || !targetId}
                onClick={launchSuite}
              >
                ▶ Run suite
              </button>
            </div>
          </section>
        ) : (
          <section>
            <div className="selbox">
              <label>describe what changed — Anneal scores every test against it (§11)</label>
              <textarea
                value={change}
                onChange={(e) => setChange(e.target.value)}
                placeholder="Describe what changed in the app, in plain English — the selector picks the affected tests."
              />
              <div className="row">
                <button className="btn primary sm" onClick={previewSelection} disabled={selecting || !targetId}>
                  {selecting ? "Scoring…" : "Preview selection"}
                </button>
                <span className="hint">
                  deterministic · no LLM in the gate path · semantic 0.6 + structural 0.4 · threshold 0.45
                </span>
              </div>
            </div>
            {picks ? <SelectionResult picks={picks} onRun={runSelection} disabled={!targetId} /> : null}
          </section>
        )}
      </div>

      {runModal ? (
        <RunModal state={runModal} onClose={() => setRunModal(null)} onOpenRuns={() => nav("/runs")} />
      ) : null}
      {form ? (
        <SuiteFormModal
          form={form}
          tests={tests}
          onChangeName={(name) => setForm({ ...form, name })}
          onTogglePick={(id, on) => {
            const picked = new Set(form.picked);
            if (on) picked.add(id);
            else picked.delete(id);
            setForm({ ...form, picked });
          }}
          onClose={() => setForm(null)}
          onSave={saveForm}
        />
      ) : null}
    </div>
  );
}

// bold the authored reason's signal phrases, matching the prototype's .reason b emphasis.
function boldReason(reason: string) {
  return reason.split(/(coverage mentions|touches changed surface\(s\))/g).map((part, i) =>
    part === "coverage mentions" || part === "touches changed surface(s)" ? (
      <b key={i}>{part}</b>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

function SelectionResult({ picks, onRun, disabled }: { picks: SelectionPick[]; onRun: () => void; disabled: boolean }) {
  const sel = picks.filter((p) => p.selected).length;
  return (
    <>
      <div className="selsum">
        <span className="big">
          {sel} of {picks.length} selected
        </span>
        <span>· threshold 0.45</span>
        <span>· {picks.filter((p) => p.cold_start).length} cold-start safety net</span>
        <span className="form">score = 0.6·semantic + 0.4·structural</span>
      </div>
      {picks.map((p) => {
        const cls = p.cold_start ? "cold" : p.selected ? "" : "skipped";
        return (
          <div className={`prow ${cls}`} key={p.test_id}>
            <div className="top">
              {p.cold_start ? (
                <span className="selchip cold">◇ cold-start</span>
              ) : p.selected ? (
                <span className="selchip sel">✓ selected</span>
              ) : (
                <span className="selchip skip">– skipped</span>
              )}
              <span className="k">{p.test_key}</span>
            </div>
            <div />
            <div className="score">
              <div className="scorebar">
                <i style={{ width: `${Math.round(p.score * 100)}%` }} />
                <span className="th" title="threshold 0.45" />
              </div>
              <span className="num">{p.score.toFixed(2)}</span>
            </div>
            <div className="reason">{boldReason(p.reason)}</div>
          </div>
        );
      })}
      <div style={{ display: "flex", marginTop: 12 }}>
        <button className="btn primary sm" style={{ marginLeft: "auto" }} onClick={onRun} disabled={disabled}>
          ▶ Run selected ({sel}) as selection-run
        </button>
      </div>
    </>
  );
}

function SuiteFormModal({
  form,
  tests,
  onChangeName,
  onTogglePick,
  onClose,
  onSave,
}: {
  form: NonNullable<FormState>;
  tests: TestListItem[];
  onChangeName: (name: string) => void;
  onTogglePick: (id: string, on: boolean) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  return (
    <div className="scrim" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-h">
          <span className="tt">{form.mode === "edit" ? `Edit suite · ${form.name}` : "New suite"}</span>
          <button className="x" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-b">
          <div className="ff">
            <label>suite name</label>
            <input
              type="text"
              value={form.name}
              placeholder="e.g. campaign-smoke"
              onChange={(e) => onChangeName(e.target.value)}
            />
          </div>
          <div className="ff" style={{ marginBottom: 8 }}>
            <label>member tests — optional · pick from the catalog{form.mode === "create" ? " or add them later" : ""}</label>
            {tests.length === 0 ? (
              <div className="catalog empty" style={{ padding: "10px 12px", color: "var(--muted)", fontSize: 12 }}>
                No tests in the catalog yet — a suite can start empty.
              </div>
            ) : (
              <div className="catalog">
                {tests.map((t) => (
                  <label className="crow" key={t.id}>
                    <input
                      type="checkbox"
                      checked={form.picked.has(t.id)}
                      onChange={(e) => onTogglePick(t.id, e.target.checked)}
                    />
                    <span className="k">{t.test_key}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
          <div className="ffbar">
            <span className="cnt">{form.picked.size} tests selected · empty is fine</span>
            <button className="btn sm" style={{ marginLeft: "auto" }} onClick={onClose}>
              Cancel
            </button>
            <button className="btn primary sm" disabled={!form.name.trim()} onClick={onSave}>
              {form.mode === "edit" ? "Save changes" : "Create suite"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
