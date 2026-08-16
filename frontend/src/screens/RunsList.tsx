// Runs list (Surface 3 landing) — the table of every run, newest first. Wired to GET /projects/{p}/runs.
//
// STALE-MARKER NOTE: the prototype marks server-side verdict/env/trigger/date filters
// + pagination as target-state. Wave A shipped those query params, so per the ground rules the API
// wins — the saved-view tabs, the verdict/env/trigger/date filter pills, and the older/newer pager are
// all wired to the server (marker removed). The verdict filter and the saved-view tabs share one
// verdict/pinned state so they stay in sync. Genuinely still stubbed: the live auto-refresh tail
// (no project-wide run stream) and a server-side test_key *search* (only test_id is a param), so the
// search box filters the current page client-side.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { PROJECT } from "../lib/config";
import { fmtAbs, fmtDuration, relTime } from "../lib/format";
import { Pill } from "../lib/verdict";

type RunListItem = components["schemas"]["RunListItem"];

const PAGE = 25;

// saved-view tabs ⇄ (verdict, pinned) server filters — a single source of truth so the verdict
// dropdown and the tabs never disagree.
const TABS = [
  { key: "all", label: "All runs", verdict: "", pinned: false },
  { key: "fail", label: "Failures", verdict: "red", pinned: false },
  { key: "prov", label: "Provisional", verdict: "provisional", pinned: false },
  { key: "heals", label: "Healed", verdict: "green_with_heals", pinned: false },
  { key: "pinned", label: "Pinned", verdict: "", pinned: true },
] as const;

const VERDICT_OPTS = ["green", "green_with_heals", "provisional", "red", "parked_auth", "parked_env"];

export function RunsList() {
  const nav = useNavigate();
  const [verdict, setVerdict] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [env, setEnv] = useState("");
  const [trigger, setTrigger] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [showDate, setShowDate] = useState(false);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);

  const [runs, setRuns] = useState<RunListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // one unfiltered snapshot → filter-select options + saved-view count badges (kept stable so options
  // don't vanish when a filter narrows, and counts stay global not per-page).
  const [snapshot, setSnapshot] = useState<RunListItem[]>([]);
  useEffect(() => {
    api
      .GET("/projects/{project}/runs", { params: { path: { project: PROJECT }, query: { limit: 500 } } })
      .then(({ data }) => data && setSnapshot(data))
      .catch(() => {});
  }, []);
  const envOpts = useMemo(() => [...new Set(snapshot.map((r) => r.environment))].sort(), [snapshot]);
  const trigOpts = useMemo(() => [...new Set(snapshot.map((r) => r.trigger))].sort(), [snapshot]);
  const counts = useMemo(
    () => ({
      all: snapshot.length,
      fail: snapshot.filter((r) => r.verdict === "red").length,
      prov: snapshot.filter((r) => r.verdict === "provisional").length,
      heals: snapshot.filter((r) => r.verdict === "green_with_heals").length,
      pinned: snapshot.filter((r) => r.pinned).length,
    }),
    [snapshot],
  );

  const activeTab =
    (pinnedOnly && "pinned") ||
    (verdict === "red" && "fail") ||
    (verdict === "provisional" && "prov") ||
    (verdict === "green_with_heals" && "heals") ||
    (verdict === "" && "all") ||
    "";

  function setTab(t: (typeof TABS)[number]) {
    setVerdict(t.verdict);
    setPinnedOnly(t.pinned);
  }

  useEffect(() => setOffset(0), [verdict, pinnedOnly, env, trigger, since, until]);

  useEffect(() => {
    let live = true;
    setRuns(null);
    setError(null);
    api
      .GET("/projects/{project}/runs", {
        params: {
          path: { project: PROJECT },
          query: {
            limit: PAGE,
            offset,
            ...(verdict ? { verdict } : {}),
            ...(pinnedOnly ? { pinned: true } : {}),
            ...(env ? { environment: env } : {}),
            ...(trigger ? { trigger } : {}),
            ...(since ? { since: `${since}T00:00:00Z` } : {}),
            ...(until ? { until: `${until}T23:59:59Z` } : {}),
          },
        },
      })
      .then(({ data, error }) => {
        if (!live) return;
        if (error) setError(JSON.stringify(error));
        else setRuns(data ?? []);
      })
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [verdict, pinnedOnly, env, trigger, since, until, offset]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (runs ?? []).filter((r) => (needle ? r.test_key.toLowerCase().includes(needle) : true));
  }, [runs, q]);

  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <b>Runs</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>

      <div className="content">
        <div className="runs-hd">
          <div>
            <h1>Runs</h1>
            <div className="sub">
              Every test run against the configured target, newest first. Open one
              to inspect its steps.
            </div>
          </div>
          <div className="live">
            <span className="livedot">
              <span className="d" /> live
            </span>
            <span className="stub">◔ auto-refresh / live tail is target-state (no project-wide run stream)</span>
          </div>
        </div>

        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.key} role="tab" aria-selected={activeTab === t.key} onClick={() => setTab(t)}>
              {t.label} <span className="b">{counts[t.key]}</span>
            </button>
          ))}
        </div>

        <div className="toolbar">
          <label className="search">
            <span className="i">⌕</span>
            <input placeholder="Filter by test key…" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <span className="fbtn">
            verdict
            <span className="cv">
              <select value={verdict} onChange={(e) => { setVerdict(e.target.value); setPinnedOnly(false); }}>
                <option value="">any</option>
                {VERDICT_OPTS.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
              <span className="cr">▾</span>
            </span>
          </span>
          <span className="fbtn">
            environment
            <span className="cv">
              <select value={env} onChange={(e) => setEnv(e.target.value)}>
                <option value="">any</option>
                {envOpts.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
              <span className="cr">▾</span>
            </span>
          </span>
          <span className="fbtn">
            trigger
            <span className="cv">
              <select value={trigger} onChange={(e) => setTrigger(e.target.value)}>
                <option value="">any</option>
                {trigOpts.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
              <span className="cr">▾</span>
            </span>
          </span>
          {showDate ? (
            <span className="fbtn">
              <input type="date" value={since} onChange={(e) => setSince(e.target.value)} title="since" />
              <span className="cv">→</span>
              <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} title="until" />
              <button
                className="cr"
                style={{ border: 0, background: "none", color: "var(--muted)", cursor: "pointer" }}
                onClick={() => { setShowDate(false); setSince(""); setUntil(""); }}
                title="clear date range"
              >
                ×
              </button>
            </span>
          ) : (
            <button className="fbtn dr" onClick={() => setShowDate(true)}>
              ＋ date range
            </button>
          )}
        </div>

        {error ? (
          <div className="async err">Couldn't load runs: {error}</div>
        ) : runs === null ? (
          <div className="async">loading runs…</div>
        ) : shown.length === 0 ? (
          <div className="empty-runs">
            <h4>No runs match</h4>
            <div>Clear the search or pick a different view.</div>
          </div>
        ) : (
          <div className="rtable">
            <div className="rhead">
              <span>verdict</span>
              <span>test</span>
              <span>environment</span>
              <span>ver</span>
              <span>trigger</span>
              <span>duration</span>
              <span>started</span>
              <span />
            </div>
            {shown.map((r) => (
              <button
                key={r.id}
                className="rrow"
                onClick={() => nav(`/runs/${r.id}`)}
                title={`open ${r.id} in the run inspector`}
              >
                <span>
                  <Pill verdict={r.verdict} />
                </span>
                <span className="tk">
                  <span className="k">{r.test_key}</span>
                  {r.error_class ? <span className="ec">{r.error_class}</span> : null}
                  {r.incident_id ? <span className="inc">→ incident</span> : null}
                </span>
                <span className="env">
                  {r.environment}
                  {r.off_canonical ? <span className="off">off-canonical</span> : null}
                </span>
                <span className="ver">v{r.test_version}</span>
                <span className="trig">{r.trigger}</span>
                <span className="dur">{fmtDuration(r.duration_ms)}</span>
                <span className="when" title={fmtAbs(r.started_at)}>
                  {relTime(r.started_at)}
                </span>
                <span className="pin">{r.pinned ? <span title="pinned — exempt from prune">📌</span> : null}</span>
              </button>
            ))}
          </div>
        )}

        <div className="pager">
          <span>
            {runs ? `Showing ${shown.length}${q ? ` of ${runs.length} on this page` : ""}` : ""}
            {offset > 0 ? ` · from #${offset + 1}` : ""}
          </span>
          <span style={{ marginLeft: "auto" }} />
          <button className="btn sm" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))}>
            ← newer
          </button>
          <button className="btn sm" disabled={!runs || runs.length < PAGE} onClick={() => setOffset((o) => o + PAGE)}>
            older →
          </button>
        </div>
      </div>
    </div>
  );
}
