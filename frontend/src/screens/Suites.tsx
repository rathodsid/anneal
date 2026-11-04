// Suites index (Surface: /suites) — every suite in the project. .
// A suite is a NAMED SET OF TESTS ({name, member_test_ids[]}); running one binds it to a target +
// trigger and yields a verdict. Name + member count are real (GET /suites); New-suite POSTs
// {name, member_test_ids} (A4); Run launches async (202 → poll /suite_runs/{id}, B1). The last-run
// column (verdict · when · trigger · incident) has NO source today — no suite_runs list, no enrichment
// on GET /suites (gap G1) — so it stays behind .stub. Saved-vs-ad-hoc is the G4 name heuristic
// (selection-run / ui-run ⇒ ad-hoc). Row → the suite detail (/suites/:id).
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { RunModal, pollSuiteRun } from "../components/RunModal";
import { Shell } from "../components/Shell";
import { PROJECT, pickDefaultTarget } from "../lib/config";

type Suite = components["schemas"]["Suite"];
type Target = components["schemas"]["Target"];
type TestListItem = components["schemas"]["TestListItem"];
type SuiteRunDetail = components["schemas"]["SuiteRunDetail"];

const isAdhoc = (name: string) => name === "selection-run" || name === "ui-run";

type Tab = "saved" | "adhoc" | "all";
type NewForm = { name: string; picked: Set<string> } | null;
type RunState = {
  suite: Suite;
  targetId: string;
  trigger: "manual" | "api" | "deploy_hook";
  suiteRun: SuiteRunDetail | null;
  launching: boolean;
  busyActiveId?: string | null;
  error?: string | null;
} | null;

export function Suites() {
  const nav = useNavigate();
  const [suites, setSuites] = useState<Suite[] | null>(null);
  const [tests, setTests] = useState<TestListItem[]>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("saved");
  const [q, setQ] = useState("");
  const [form, setForm] = useState<NewForm>(null);
  const [run, setRun] = useState<RunState>(null);

  async function refreshSuites() {
    const { data, error } = await api.GET("/projects/{project}/suites", { params: { path: { project: PROJECT } } });
    if (error) { setError("failed to load suites"); return; }
    setSuites([...(data ?? [])].sort((a, b) => a.name.localeCompare(b.name)));
  }

  useEffect(() => {
    refreshSuites();
    api.GET("/projects/{project}/tests", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) setTests(data);
    });
    api.GET("/projects/{project}/targets", { params: { path: { project: PROJECT } } }).then(({ data }) => {
      if (data) setTargets(data);
    });
  }, []);

  const keyOf = useMemo(() => {
    const m = new Map(tests.map((t) => [t.id, t.test_key]));
    return (id: string) => m.get(id) ?? id.slice(0, 8);
  }, [tests]);

  async function launch() {
    if (!run?.targetId) return;
    const { suite, targetId, trigger } = run;
    setRun((m) => (m ? { ...m, launching: true, error: null, busyActiveId: null, suiteRun: null } : m));
    const { data, error, response } = await api.POST("/projects/{project}/suites/{suite_id}/runs", {
      params: { path: { project: PROJECT, suite_id: suite.id }, query: { target_id: targetId, trigger } },
    });
    if (response?.status === 409) {
      const activeId = (error as { detail?: { active_suite_run_id?: string } } | undefined)?.detail?.active_suite_run_id;
      setRun((m) => (m ? { ...m, launching: false, busyActiveId: activeId ?? "unknown" } : m));
      return;
    }
    if (error || !data) {
      setRun((m) => (m ? { ...m, launching: false, error: JSON.stringify(error) } : m));
      return;
    }
    await pollSuiteRun(data.suite_run_id, (sr) => setRun((m) => (m ? { ...m, suiteRun: sr } : m)));
    setRun((m) => (m ? { ...m, launching: false } : m));
  }

  const counts = useMemo(() => {
    const list = suites ?? [];
    const adhoc = list.filter((s) => isAdhoc(s.name)).length;
    return { saved: list.length - adhoc, adhoc, all: list.length };
  }, [suites]);

  if (error) return <Shell crumbs={<b>Suites</b>}><div className="async err">{error}</div></Shell>;
  if (!suites) return <Shell crumbs={<b>Suites</b>}><div className="async">Loading suites…</div></Shell>;

  const ql = q.trim().toLowerCase();
  const rows = suites.filter(
    (s) => (tab === "all" || (tab === "adhoc" ? isAdhoc(s.name) : !isAdhoc(s.name))) && s.name.toLowerCase().includes(ql),
  );

  async function createSuite() {
    if (!form) return;
    await api.POST("/projects/{project}/suites", {
      params: { path: { project: PROJECT } },
      body: { name: form.name.trim(), member_test_ids: [...form.picked] },
    });
    setForm(null);
    await refreshSuites();
  }

  return (
    <Shell crumbs={<b>Suites</b>}>
      <div className="content suites-scope">
        <div className="hdrow">
          <div>
            <h1>Suites</h1>
            <p className="lede">
              A suite is a <b>named set of tests</b>. Running one binds it to a target + trigger and yields a verdict.
            </p>
          </div>
          <button
            className="btn primary"
            onClick={() => setForm({ name: "", picked: new Set() })}
          >
            + New suite
          </button>
        </div>

        <div className="defnote">
          <b>suite</b> = <span className="val">{"{ name, member_test_ids[] }"}</span> — no target or schedule of its own.{" "}
          <b>saved</b> suites are yours; <b>selection-run</b> / <b>ui-run</b> are auto-created (change-driven
          selection · single-test run).
        </div>

        <div className="controls">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "saved"} onClick={() => setTab("saved")}>
              Saved <span className="b">{counts.saved}</span>
            </button>
            <button role="tab" aria-selected={tab === "adhoc"} onClick={() => setTab("adhoc")}>
              Ad-hoc <span className="b">{counts.adhoc}</span>
            </button>
            <button role="tab" aria-selected={tab === "all"} onClick={() => setTab("all")}>
              All <span className="b">{counts.all}</span>
            </button>
          </div>
          <div className="search">
            <span className="i">⌕</span>
            <input placeholder="Search suites by name…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <span className="stub" style={{ marginLeft: "auto" }}>
            ◔ last-run column is derived from suite_runs — list returns only name + members (gap G1)
          </span>
        </div>

        <div className="table">
          <div className="thead">
            <span>suite</span>
            <span>members</span>
            <span>last run</span>
            <span>when · trigger</span>
            <span />
          </div>
          {rows.length === 0 ? (
            <div className="empty">
              No {tab === "all" ? "" : tab + " "}suites{q ? ` match “${q}”` : ""}.
            </div>
          ) : (
            rows.map((s) => (
              <div className="srow" key={s.id} onClick={() => nav(`/suites/${s.id}`)}>
                <div>
                  <div className="nm">
                    {s.name}
                    {isAdhoc(s.name) ? <span className="stag">ad-hoc</span> : null}
                  </div>
                </div>
                <div className="members">
                  {s.member_test_ids?.length ?? 0} <span>test{(s.member_test_ids?.length ?? 0) === 1 ? "" : "s"}</span>
                </div>
                <div>
                  <span className="stub">◔ G1</span>
                </div>
                <div className="when">—</div>
                <div className="rowbtn">
                  <button
                    className="btn sm"
                    onClick={(e) => {
                      e.stopPropagation();
                      setRun({
                        suite: s,
                        targetId: pickDefaultTarget(targets),
                        trigger: "manual",
                        suiteRun: null,
                        launching: false,
                      });
                    }}
                  >
                    ▶ Run
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {form ? (
        <NewSuiteModal
          form={form}
          tests={tests}
          onChangeName={(name) => setForm({ ...form, name })}
          onTogglePick={(id, on) => {
            const picked = new Set(form.picked);
            on ? picked.add(id) : picked.delete(id);
            setForm({ ...form, picked });
          }}
          onClose={() => setForm(null)}
          onCreate={createSuite}
        />
      ) : null}

      {run ? (
        <RunModal
          state={{
            title: `Run suite — ${run.suite.name}`,
            memberKeys: (run.suite.member_test_ids ?? []).map(keyOf),
            suiteRun: run.suiteRun,
            launching: run.launching,
            busyActiveId: run.busyActiveId,
            error: run.error,
          }}
          bodyClass="suites-scope"
          controls={
            <div className="runctl">
              <span className="selctl">
                <span className="lab">target</span>
                <select
                  value={run.targetId}
                  disabled={run.launching}
                  onChange={(e) => setRun((m) => (m ? { ...m, targetId: e.target.value } : m))}
                >
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>{t.key}</option>
                  ))}
                </select>
              </span>
              <span className="selctl">
                <span className="lab">trigger</span>
                <select
                  value={run.trigger}
                  disabled={run.launching}
                  onChange={(e) => setRun((m) => (m ? { ...m, trigger: e.target.value as NonNullable<RunState>["trigger"] } : m))}
                >
                  <option value="manual">manual</option>
                  <option value="api">api</option>
                  <option value="deploy_hook">deploy_hook</option>
                </select>
              </span>
              <button
                className="btn primary sm"
                style={{ marginLeft: "auto" }}
                disabled={run.launching || !run.targetId}
                onClick={launch}
              >
                ▶ Launch {(run.suite.member_test_ids ?? []).length} test
                {(run.suite.member_test_ids ?? []).length === 1 ? "" : "s"}
              </button>
            </div>
          }
          onClose={() => setRun(null)}
          onOpenRuns={() => nav("/runs")}
        />
      ) : null}
    </Shell>
  );
}

function NewSuiteModal({
  form,
  tests,
  onChangeName,
  onTogglePick,
  onClose,
  onCreate,
}: {
  form: NonNullable<NewForm>;
  tests: TestListItem[];
  onChangeName: (name: string) => void;
  onTogglePick: (id: string, on: boolean) => void;
  onClose: () => void;
  onCreate: () => void;
}) {
  return (
    <div className="scrim" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-h">
          <span className="tt">New suite</span>
          <button className="x" onClick={onClose}>×</button>
        </div>
        <div className="modal-b">
          <div className="ff">
            <label>suite name</label>
            <input
              type="text"
              value={form.name}
              placeholder="e.g. billing-critical"
              onChange={(e) => onChangeName(e.target.value)}
            />
          </div>
          <div className="ff" style={{ marginBottom: 8 }}>
            <label>members — optional · pick tests now or add them later</label>
            {tests.length === 0 ? (
              <div className="catalog empty" style={{ padding: "10px 12px", color: "var(--muted)", fontSize: 12 }}>
                No tests in the catalog yet — create the suite now and file tests into it later (from Author or Edit membership).
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
            <span className="cnt">{form.picked.size} test{form.picked.size === 1 ? "" : "s"} selected · empty is fine</span>
            <button className="btn sm" style={{ marginLeft: "auto" }} onClick={onClose}>Cancel</button>
            <button className="btn primary sm" disabled={!form.name.trim()} onClick={onCreate}>
              Create suite
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

