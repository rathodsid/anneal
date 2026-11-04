// Tests catalog (Surface: /tests) — every test in the project, grouped by the suites it belongs to OR
// by the labels it carries. . The suite grouping is fully real: GET /tests
// × GET /suites reverse-mapped on suite.member_test_ids gives every group (a test repeats under each
// suite it's in), the ad-hoc suites, and the "In no suite" coverage-gap bucket (tests in no member
// list). test_key / head_version / quarantined / labels / verified_against are real fields. Last verdict
// + incident are DERIVED from a single GET /runs (newest-first, first-wins per test — the same trick
// Suite.tsx uses; not a stub). Coverage surfaces (G2) have no client source today (only in the head
// bundle) → kept behind .stub. Saved-vs-ad-hoc is the G4 name heuristic. Row → test-viewer.
//
// Grouping by LABEL is the same structure with a different key: a group per label + an "Unlabelled"
// bucket — neutral, NOT the amber coverage-gap treatment, because no-suite means no coverage while
// unlabelled does not. Each label group launches via POST /projects/{p}/labels/{label}/runs, whose
// target the API DERIVES from the members' verified_against: the button names it, and the API refuses
// with a split when the label spans environments. Launch progress is polled inline (there is no
// suite-run route; the same poll-to-terminal shape TestViewer uses for a single test).
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { Shell } from "../components/Shell";
import { LabelChips, useProjectLabels } from "../components/labels";
import { PROJECT } from "../lib/config";
import { fmtDuration } from "../lib/format";
import { Pill } from "../lib/verdict";

type Suite = components["schemas"]["Suite"];
type TestListItem = components["schemas"]["TestListItem"];
type RunListItem = components["schemas"]["RunListItem"];
type SuiteRunDetail = components["schemas"]["SuiteRunDetail"];

const NO_SUITE = "__nosuite__";
const UNLABELLED = "__unlabelled__";
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// G4 heuristic: the engine auto-creates suites literally named "selection-run" (change-driven
// selection), "ui-run" (a console one-off) and "label:<name>" (a label run) — none is a saved suite a
// human curates, so none should masquerade as one in the suite grouping.
const isAdhoc = (name: string) =>
  name === "selection-run" || name === "ui-run" || name.startsWith("label:");

type GroupData = { key: string; name: string; suiteId: string | null; adhoc: boolean; ids: string[] };
// what launching a label WOULD do, from the same fields the API derives from
type LaunchPlan = { targets: string[]; runnable: number; excluded: number };
type Launch =
  | { label: string; kind: "running" | "done"; sr: SuiteRunDetail | null }
  | { label: string; kind: "busy"; activeId?: string | null }
  | { label: string; kind: "spans"; split: { target_key: string | null; test_count: number }[] }
  | { label: string; kind: "error"; detail: string }
  | null;

export function Tests() {
  const nav = useNavigate();
  const [tests, setTests] = useState<TestListItem[] | null>(null);
  const [suites, setSuites] = useState<Suite[]>([]);
  const [latest, setLatest] = useState<Map<string, RunListItem>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [groupBy, setGroupBy] = useState<"suite" | "label">("suite");
  const [labelFilter, setLabelFilter] = useState<string | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [launch, setLaunch] = useState<Launch>(null);
  const projectLabels = useProjectLabels(tests);

  useEffect(() => {
    const p = { params: { path: { project: PROJECT } } } as const;
    Promise.all([
      api.GET("/projects/{project}/tests", p),
      api.GET("/projects/{project}/suites", p),
      api.GET("/projects/{project}/runs", { params: { path: { project: PROJECT }, query: { limit: 200 } } }),
    ])
      .then(([t, s, r]) => {
        if (t.error || !t.data) throw new Error("failed to load tests");
        setTests(t.data);
        setSuites(s.data ?? []);
        const m = new Map<string, RunListItem>();
        for (const run of r.data ?? []) if (!m.has(run.test_id)) m.set(run.test_id, run); // newest-first → first wins
        setLatest(m);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const byId = useMemo(() => new Map((tests ?? []).map((t) => [t.id, t])), [tests]);

  // how many suites each test belongs to (drives the "also in N suites" chip + the no-suite bucket).
  const memberCount = useMemo(() => {
    const c = new Map<string, number>();
    for (const s of suites) for (const id of s.member_test_ids ?? []) c.set(id, (c.get(id) ?? 0) + 1);
    return c;
  }, [suites]);

  // Suite order: saved first, then ad-hoc, alpha within; the no-suite bucket last. Labels are alpha
  // with the Unlabelled bucket last — same shape, different key.
  const groups: GroupData[] = useMemo(() => {
    if (groupBy === "label") {
      const byLabel = new Map<string, string[]>();
      const unlabelled: string[] = [];
      for (const t of tests ?? []) {
        if (!t.labels.length) unlabelled.push(t.id);
        for (const l of t.labels) byLabel.set(l, [...(byLabel.get(l) ?? []), t.id]);
      }
      const g: GroupData[] = [...byLabel.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([label, ids]) => ({ key: label, name: label, suiteId: null, adhoc: false, ids }));
      g.push({ key: UNLABELLED, name: UNLABELLED, suiteId: null, adhoc: false, ids: unlabelled });
      return g;
    }
    const ordered = [...suites].sort((a, b) => {
      const ka = isAdhoc(a.name) ? 1 : 0;
      const kb = isAdhoc(b.name) ? 1 : 0;
      return ka - kb || a.name.localeCompare(b.name);
    });
    const g: GroupData[] = ordered.map((s) => ({
      key: s.id,
      name: s.name,
      suiteId: s.id,
      adhoc: isAdhoc(s.name),
      ids: s.member_test_ids ?? [],
    }));
    const noSuite = (tests ?? []).filter((t) => !memberCount.get(t.id)).map((t) => t.id);
    g.push({ key: NO_SUITE, name: NO_SUITE, suiteId: null, adhoc: false, ids: noSuite });
    return g;
  }, [suites, tests, memberCount, groupBy]);

  const inNoSuiteCount = groups.find((g) => g.key === NO_SUITE)?.ids.length ?? 0;

  function planFor(label: string): LaunchPlan {
    const members = (tests ?? []).filter((t) => t.labels.includes(label));
    const runnable = members.filter((t) => !t.quarantined);
    return {
      targets: [...new Set(runnable.map((t) => t.verified_against ?? "—"))].sort(),
      runnable: runnable.length,
      excluded: members.length - runnable.length,
    };
  }

  async function runLabel(label: string) {
    setLaunch({ label, kind: "running", sr: null });
    const { data, error: err, response } = await api.POST("/projects/{project}/labels/{label}/runs", {
      params: { path: { project: PROJECT, label }, query: { trigger: "manual" } },
    });

    if (!data?.suite_run_id) {
      // the API refuses rather than guessing: 409 busy | 409 spans targets | 404 nothing carries it
      const detail = (err as { detail?: unknown } | undefined)?.detail;
      if (typeof detail === "object" && detail !== null && "error" in detail) {
        const d = detail as { error: string; active_suite_run_id?: string | null; split?: LaunchPlanSplit };
        if (d.error === "label spans targets") return setLaunch({ label, kind: "spans", split: d.split ?? [] });
        return setLaunch({ label, kind: "busy", activeId: d.active_suite_run_id });
      }
      return setLaunch({
        label,
        kind: "error",
        detail: typeof detail === "string" ? detail : `launch failed (HTTP ${response?.status})`,
      });
    }

    // poll to terminal in place — the console has no suite-run screen, and the interesting evidence is
    // the per-test verdicts, which link into the run inspector
    for (let i = 0; i < 900; i++) {
      const { data: sr } = await api.GET("/suite_runs/{suite_run_id}", {
        params: { path: { suite_run_id: data.suite_run_id } },
      });
      if (sr) setLaunch({ label, kind: sr.state === "done" || sr.state === "parked" ? "done" : "running", sr });
      if (sr && (sr.state === "done" || sr.state === "parked")) {
        // refresh the derived last-verdict column: leaving it at "never run" under a green launch note
        // would have the screen contradicting itself
        const { data: runs } = await api.GET("/projects/{project}/runs", {
          params: { path: { project: PROJECT }, query: { limit: 200 } },
        });
        const m = new Map<string, RunListItem>();
        for (const run of runs ?? []) if (!m.has(run.test_id)) m.set(run.test_id, run);
        setLatest(m);
        return;
      }
      await sleep(500);
    }
    setLaunch({ label, kind: "error", detail: "the run did not reach a terminal state" });
  }

  if (error) return <Shell crumbs={<b>Tests</b>}><div className="async err">{error}</div></Shell>;
  if (!tests) return <Shell crumbs={<b>Tests</b>}><div className="async">Loading tests…</div></Shell>;

  const ql = q.trim().toLowerCase();

  function toggle(name: string) {
    setClosed((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }
  const allClosed = groups.every((g) => closed.has(g.key));
  const collapseAll = () => setClosed(allClosed ? new Set() : new Set(groups.map((g) => g.key)));

  // resolve each group's rows against the search + label filter once, so we can render an empty state
  const matches = (t: TestListItem) =>
    t.test_key.toLowerCase().includes(ql) &&
    (labelFilter === null ||
      (labelFilter === UNLABELLED ? t.labels.length === 0 : t.labels.includes(labelFilter)));
  const resolved = groups.map((g) => ({
    g,
    rows: g.ids.map((id) => byId.get(id)).filter((t): t is TestListItem => !!t && matches(t)),
  }));
  const anyVisible = resolved.some((r) => r.rows.length > 0);
  const filterPlan = labelFilter && labelFilter !== UNLABELLED ? planFor(labelFilter) : null;

  return (
    <Shell
      crumbs={<b>Tests</b>}
      actions={
        <button className="kbtn" onClick={collapseAll}>
          {allClosed ? "expand all" : "collapse all"}
        </button>
      }
    >
      <div className="content tests-scope">
        <div className="hdrow">
          <div>
            <h1>Tests</h1>
            <p className="lede">
              Every test in the project. Group by the suites it belongs to, or by the labels it carries —
              a test can appear under several of either.
            </p>
          </div>
          <button className="btn primary" onClick={() => nav("/author")}>
            ✎ New test
          </button>
        </div>

        <div className="controls">
          <span className="stat">
            <b>{tests.length}</b> tests · <b>{suites.length}</b> suites · <b>{projectLabels.length}</b> labels
            {groupBy === "suite" ? <> · <b>{inNoSuiteCount}</b> in no suite</> : null}
          </span>
          <span className="seglab">group by</span>
          <span className="seg">
            <button className={groupBy === "suite" ? "on" : ""} onClick={() => setGroupBy("suite")}>
              suite
            </button>
            <button className={groupBy === "label" ? "on" : ""} onClick={() => setGroupBy("label")}>
              label
            </button>
          </span>
          <div className="search">
            <span className="i">⌕</span>
            <input placeholder="Search tests by key…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>

          {/* single-select, matching the API: the launch endpoint takes ONE label, so a multi-select
              would promise a run the backend cannot perform */}
          <span className="ddwrap">
            <button className="dd" onClick={() => setFilterOpen((o) => !o)}>
              {labelFilter === null ? (
                <span className="ph">all labels</span>
              ) : labelFilter === UNLABELLED ? (
                <span className="mono">unlabelled</span>
              ) : (
                <span className="lchip on"><span className="h">⌗</span>{labelFilter}</span>
              )}
              <span className="cv">▾</span>
            </button>
            {filterOpen ? (
              <div className="menu" onClick={() => setFilterOpen(false)}>
                <div className="mh">filter by label</div>
                {projectLabels.map((l) => (
                  <button
                    className={"mi" + (labelFilter === l.label ? " on" : "")}
                    key={l.label}
                    onClick={() => setLabelFilter(l.label)}
                  >
                    <span>⌗ {l.label}</span>
                    <span className="n">{l.test_count}</span>
                  </button>
                ))}
                {projectLabels.length ? <div className="sep" /> : null}
                <button
                  className={"mi" + (labelFilter === UNLABELLED ? " on" : "")}
                  onClick={() => setLabelFilter(UNLABELLED)}
                >
                  <span>unlabelled</span>
                </button>
                <button className="mi" onClick={() => setLabelFilter(null)}>
                  <span>clear filter</span>
                </button>
              </div>
            ) : null}
          </span>

          {/* the run action follows the filter, so you never have to switch grouping to launch */}
          {filterPlan ? (
            <button
              className="btn sm"
              disabled={filterPlan.runnable === 0}
              onClick={() => runLabel(labelFilter!)}
            >
              ▷ Run label ⌗{labelFilter}
              {filterPlan.runnable > 0 && filterPlan.targets.length === 1
                ? ` on ${filterPlan.targets[0]}`
                : ""}
              <span className="gc">({filterPlan.runnable})</span>
            </button>
          ) : null}
          <span className="stub" style={{ marginLeft: "auto" }}>
            ◔ coverage surfaces are derived — list returns test_key, head_version, quarantined (gap G2)
          </span>
        </div>

        {launch ? <LaunchNote launch={launch} onDismiss={() => setLaunch(null)} /> : null}

        {!anyVisible ? (
          <div className="empty">
            {tests.length === 0 ? "No tests in this project yet." : `No tests match ${ql ? `“${q}”` : "this filter"}.`}
          </div>
        ) : (
          resolved.map(({ g, rows }) => {
            if (!rows.length) return null;
            const isNo = g.key === NO_SUITE;
            const isUnlab = g.key === UNLABELLED;
            const isClosed = closed.has(g.key);
            const plan = groupBy === "label" && !isUnlab ? planFor(g.key) : null;
            return (
              <div
                key={g.key}
                className={`group ${isClosed ? "closed" : ""} ${isNo ? "nosuite" : ""} ${isUnlab ? "unlab" : ""}`}
              >
                <div className="ghead" onClick={() => toggle(g.key)}>
                  <span className="chev">{isClosed ? "▸" : "▾"}</span>
                  <span className="gname">{isNo ? "In no suite" : isUnlab ? "Unlabelled" : g.name}</span>
                  {g.adhoc ? <span className="gtag adhoc">ad-hoc</span> : null}
                  {isNo ? <span className="gtag gap">coverage gap</span> : null}
                  {plan ? <span className="gtag lbl">label</span> : null}
                  <span className="gc">
                    {rows.length}
                    {rows.length !== g.ids.length ? "/" + g.ids.length : ""} test{g.ids.length === 1 ? "" : "s"}
                    {plan && plan.excluded ? ` · ${plan.excluded} excluded` : ""}
                  </span>
                  <span className="gact">
                    {plan ? (
                      // one distinct canonical target → name it; several → say so, and let the API refuse
                      // NOT primary: every label group carries this action, and four primaries on one
                      // screen compete with each other and with "New test". Disabled when nothing is
                      // runnable (every member quarantined / never frozen) — offering a button whose
                      // only outcome is an error is worse than saying why.
                      <button
                        className="btn sm"
                        disabled={plan.runnable === 0}
                        onClick={(e) => { e.stopPropagation(); runLabel(g.key); }}
                        title={
                          plan.runnable === 0
                            ? `nothing runnable carries ⌗${g.key}`
                            : `run every test carrying ⌗${g.key}`
                        }
                      >
                        ▷ Run label
                        {plan.runnable === 0 ? <span className="gc">nothing runnable</span> : null}
                        {plan.runnable > 0 && plan.targets.length === 1 ? ` on ${plan.targets[0]}` : null}
                        {plan.targets.length > 1 ? <span className="warn">⚠ {plan.targets.length} targets</span> : null}
                      </button>
                    ) : isUnlab ? (
                      <span className="gc">no label to run</span>
                    ) : isNo ? (
                      <span className="gc">not covered by any suite</span>
                    ) : (
                      <button className="btn sm" onClick={(e) => { e.stopPropagation(); nav(`/suites/${g.suiteId}`); }}>
                        open suite →
                      </button>
                    )}
                  </span>
                </div>
                {isClosed
                  ? null
                  : rows.map((t) => (
                      <TestRow
                        key={t.id}
                        t={t}
                        others={memberCount.get(t.id) ?? 0}
                        inSuite={groupBy === "suite" && !isNo}
                        match={labelFilter && labelFilter !== UNLABELLED ? labelFilter : plan ? g.key : null}
                        excludedFromLabelRun={!!plan && t.quarantined}
                        last={latest.get(t.id)}
                        onOpen={() => nav(`/tests/${t.id}`)}
                        onIncident={(id) => nav(`/incidents/${id}`)}
                      />
                    ))}
              </div>
            );
          })
        )}
      </div>
    </Shell>
  );
}

type LaunchPlanSplit = { target_key: string | null; test_count: number }[];

/** Launch feedback: running / done / busy / spans-targets / error — every outcome the API can return. */
function LaunchNote({ launch, onDismiss }: { launch: NonNullable<Launch>; onDismiss: () => void }) {
  const done = launch.kind === "running" || launch.kind === "done" ? (launch.sr?.results ?? []).length : 0;
  return (
    <div className={`launchnote ${launch.kind}`}>
      {launch.kind === "running" ? (
        <>
          <span className="pill v-run"><span className="g">◐</span>running</span>
          <span className="t">
            ⌗{launch.label}
            {launch.sr ? ` on ${launch.sr.target_id.slice(0, 8)} — ${done} finished` : " — launching…"}
          </span>
        </>
      ) : launch.kind === "done" ? (
        <>
          <Pill verdict={launch.sr?.verdict ?? "green"} />
          <span className="t">
            ⌗{launch.label} — {done} test{done === 1 ? "" : "s"}
            {launch.sr?.park_reason ? ` · ${launch.sr.park_reason}` : ""}
          </span>
          <span className="reslist">
            {(launch.sr?.results ?? []).map((r) => (
              <Link className="res" key={r.id} to={`/runs/${r.id}`}>
                {r.test_key} <Pill verdict={r.verdict} />
                {r.duration_ms ? <span className="gc">{fmtDuration(r.duration_ms)}</span> : null}
              </Link>
            ))}
          </span>
        </>
      ) : launch.kind === "busy" ? (
        <>
          <span className="pill v-park"><span className="g">⏸</span>target busy</span>
          <span className="t">this target is already running a suite — try again when it finishes</span>
        </>
      ) : launch.kind === "spans" ? (
        <>
          <span className="pill v-heals"><span className="g">⚠</span>{launch.split.length} targets</span>
          <span className="t">
            ⌗{launch.label} covers tests canonical to more than one environment —{" "}
            {launch.split.map((s) => `${s.target_key ?? "unknown"} (${s.test_count})`).join(" · ")}. Running
            it as one run would execute the rest read-only, unable to heal or re-author.
          </span>
        </>
      ) : launch.kind === "error" ? (
        <>
          <span className="pill v-red"><span className="g">✕</span>not launched</span>
          <span className="t">{launch.detail}</span>
        </>
      ) : null}
      <button className="kbtn dismiss" onClick={onDismiss}>dismiss</button>
    </div>
  );
}

function TestRow({
  t,
  others,
  inSuite,
  match,
  excludedFromLabelRun,
  last,
  onOpen,
  onIncident,
}: {
  t: TestListItem;
  others: number;
  inSuite: boolean;
  match: string | null;
  excludedFromLabelRun: boolean;
  last: RunListItem | undefined;
  onOpen: () => void;
  onIncident: (id: string) => void;
}) {
  return (
    <div className="trow" onClick={onOpen} title={`open ${t.test_key} in the test viewer`}>
      <div className="nmwrap">
        <div className="nm">
          {t.test_key}
          <span className="ver">v{t.head_version}</span>
          {t.quarantined ? <span className="qtag">quarantined</span> : null}
          {inSuite && others > 1 ? (
            <span className="alsoin">also in {others - 1} suite{others - 1 === 1 ? "" : "s"}</span>
          ) : null}
        </div>
        <div className="cov">
          <LabelChips labels={t.labels} match={match} />
          {t.labels.length ? null : <span className="none">no labels</span>}
          {excludedFromLabelRun ? <span className="excl">excluded from the run — quarantined</span> : null}
          <span className="stub">◔ G2 — add coverage[] to GET /tests</span>
        </div>
      </div>
      <div className="right">
        {last?.incident_id ? (
          <span className="inc" onClick={(e) => { e.stopPropagation(); onIncident(last.incident_id!); }}>
            → {last.incident_id.slice(0, 8)}
          </span>
        ) : null}
        {last?.error_class ? <span className="ec">{last.error_class}</span> : null}
        {last ? (
          <Pill verdict={last.verdict} />
        ) : (
          <span className="mono" style={{ color: "var(--faint)", fontSize: 11 }}>never run</span>
        )}
      </div>
    </div>
  );
}
