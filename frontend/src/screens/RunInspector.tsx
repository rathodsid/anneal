// Run inspector (Surface 3) — one run: step tree → evidence stage → step/network/console detail.
// Wired to GET /runs/{id} (meta + artifact paths),
// GET /runs/{id}/steps?after= (the live poll tier — two rows per step, terminal wins), the
// /artifacts/{kind} bytes, and POST /runs/{id}/pin.
//
// Real vs mock: the prototype's per-phase before/action/after "scenes", collapsible modules, and
// per-action "atomics" were mock. Real step_event rows are a flat list with no console column and no
// per-step frame captures — so the center stage shows the run's single screenshot artifact + links
// to the trace/video/network-log bytes, and the Console tab is a genuine .stub.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { artifactUrl } from "../lib/config";
import { fmtDuration } from "../lib/format";
import { Pill } from "../lib/verdict";

type RunDetail = components["schemas"]["RunDetail"];
type StepEvent = components["schemas"]["StepEvent"];

// The captured network log (GET /runs/{id}/artifacts/network_log) — a passive, full record. The
// endpoint returns free-form objects (untyped in openapi), so the shape is declared here.
type NetEntry = {
  url?: string;
  host?: string;
  path?: string;
  method?: string;
  status?: number;
  status_text?: string;
  resource_type?: string;
  duration_ms?: number | null;
  request_size?: number | null;
  response_size?: number | null;
  video_offset_ms?: number | null;
  step_index?: number | null;
  failure?: string | null;
  matched_expectation?: boolean;
};

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// step status → tree glyph class + label
function statusOf(s: StepEvent): { cls: string; glyph: string; label: string } {
  const healed = !!(s.detail && (s.detail as Record<string, unknown>).healed);
  if (s.status === "passed")
    return healed ? { cls: "heal", glyph: "⤴", label: "healed" } : { cls: "pass", glyph: "✓", label: "passed" };
  if (s.status === "failed") return { cls: "fail", glyph: "✕", label: "failed" };
  if (s.status === "skipped") return { cls: "skip", glyph: "○", label: "skipped" };
  return { cls: "run", glyph: "◐", label: "running" };
}

export function RunInspector() {
  const { id = "" } = useParams();
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rawSteps, setRawSteps] = useState<StepEvent[]>([]);
  const [pinning, setPinning] = useState(false);
  const [shotError, setShotError] = useState(false);

  const [sel, setSel] = useState(0);
  const [tab, setTab] = useState<"step" | "net" | "console">("step");
  const [netFilter, setNetFilter] = useState<"all" | "api" | "failed">("all");
  const [netScope, setNetScope] = useState<"run" | "step">("run");
  const [allNet, setAllNet] = useState<NetEntry[]>([]);
  const [playing, setPlaying] = useState(false);
  const playTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [videoDur, setVideoDur] = useState<number | null>(null); // seconds, from loadedmetadata

  // finished flag for the step-poll's stop condition (avoids a stale closure).
  const finishedRef = useRef(false);

  // meta + artifact paths + verdict + pin
  useEffect(() => {
    let live = true;
    setDetail(null);
    setError(null);
    setShotError(false);
    api
      .GET("/runs/{run_result_id}", { params: { path: { run_result_id: id } } })
      .then(({ data, error }) => {
        if (!live) return;
        if (error) setError(JSON.stringify(error));
        else if (data) {
          setDetail(data);
          finishedRef.current = !!data.finished_at;
        }
      })
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [id]);

  // run-level captured network log — a full passive record (all hosts, timing), correlated to steps
  // server-side. Fetched directly (the endpoint is untyped bytes/JSON, not via openapi-fetch). Re-runs
  // when the run reaches a terminal state so the log is complete (it's persisted on run finish).
  useEffect(() => {
    let live = true;
    fetch(artifactUrl(id, "network_log"))
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: NetEntry[]) => live && setAllNet(Array.isArray(rows) ? rows : []))
      .catch(() => live && setAllNet([]));
    return () => {
      live = false;
    };
  }, [id, detail?.finished_at]);

  // live step poll: GET /steps?after=cursor, accumulate, stop once the run is terminal and drained.
  useEffect(() => {
    let stopped = false;
    let cursor = 0;
    let iters = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setRawSteps([]);

    const tick = async () => {
      if (stopped) return;
      const { data, error } = await api.GET("/runs/{run_result_id}/steps", {
        params: { path: { run_result_id: id }, query: { after: cursor } },
      });
      if (stopped) return;
      if (error) return; // detail effect surfaces run-level errors
      const gotNew = !!(data && data.length);
      if (gotNew) {
        cursor = Math.max(cursor, ...data!.map((s) => s.id));
        setRawSteps((prev) => [...prev, ...data!]);
      }
      iters += 1;
      if ((finishedRef.current && !gotNew) || iters > 300) return; // terminal + drained, or safety cap
      timer = setTimeout(tick, gotNew ? 200 : 1000); // drain fast, then idle-poll for a live run
    };
    tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [id]);

  // pair the two-row-per-step events (running → terminal); last row per step_index wins.
  const steps = useMemo(() => {
    const byIdx = new Map<number, StepEvent>();
    for (const ev of rawSteps) byIdx.set(ev.step_index, ev);
    return [...byIdx.values()].sort((a, b) => a.step_index - b.step_index);
  }, [rawSteps]);

  // per-step video span from the SAME two rows: running row = start offset, terminal row = end
  // offset (step_event.video_offset_ms — ms from run-video start at context creation).
  const offsets = useMemo(() => {
    const m = new Map<number, { start?: number; end?: number }>();
    for (const ev of rawSteps) {
      const o = ev.video_offset_ms;
      if (o == null) continue;
      const e = m.get(ev.step_index) ?? {};
      if (ev.status === "running") e.start = o;
      else e.end = o;
      m.set(ev.step_index, e);
    }
    return m;
  }, [rawSteps]);
  const markerMs = (s: StepEvent): number | null => {
    const o = offsets.get(s.step_index);
    return o?.start ?? o?.end ?? null;
  };
  // real time-positioned markers need a playable video with a known duration AND offsets
  const timeScaled = videoDur != null && videoDur > 0 && steps.some((s) => markerMs(s) != null);

  // click a step/marker → select it AND scrub the video to that step's start offset
  const jumpTo = (i: number) => {
    setSel(i);
    const t = steps[i] ? markerMs(steps[i]) : null;
    if (videoRef.current && t != null) videoRef.current.currentTime = t / 1000;
  };

  // video scrub → highlight the step whose offset span the playhead is inside
  const onVideoTime = () => {
    const v = videoRef.current;
    if (!v || !timeScaled) return;
    const ms = v.currentTime * 1000;
    let best = -1;
    steps.forEach((s, i) => {
      const t = markerMs(s);
      if (t != null && t <= ms) best = i;
    });
    if (best >= 0) setSel(best);
  };

  // once steps arrive, default-select the first failure, else the first step.
  useEffect(() => {
    if (steps.length && sel >= steps.length) setSel(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps.length]);

  // replay: step through client-side (the real "replay" is the video artifact; live tail is stubbed).
  useEffect(() => {
    if (!playing) return;
    if (playTimer.current) clearInterval(playTimer.current);
    playTimer.current = setInterval(() => {
      setSel((i) => {
        if (i >= steps.length - 1) {
          setPlaying(false);
          return i;
        }
        return i + 1;
      });
    }, 780);
    return () => {
      if (playTimer.current) clearInterval(playTimer.current);
    };
  }, [playing, steps.length]);

  async function togglePin() {
    if (!detail || pinning) return;
    setPinning(true);
    const next = !detail.pinned;
    const { data, error } = await api.POST("/runs/{run_result_id}/pin", {
      params: { path: { run_result_id: id } },
      body: { pinned: next },
    });
    if (!error && data) setDetail({ ...detail, pinned: data.pinned });
    setPinning(false);
  }

  if (error) return shell(<div className="async err">Couldn't load run: {error}</div>, id);
  if (!detail) return shell(<div className="async">loading run…</div>, id);

  const cur = steps[sel];
  const isApi = (n: NetEntry) => n.resource_type === "fetch" || n.resource_type === "xhr";
  const isFail = (n: NetEntry) => !!n.failure || (n.status ?? 0) >= 400;
  // scope: whole run (the new full-capture view) or just the selected step's requests (correlated
  // server-side by video offset). Fall back to run scope when a step has no step_index match.
  const scopeNet =
    netScope === "step" && cur ? allNet.filter((n) => n.step_index === cur.step_index) : allNet;
  const fails = scopeNet.filter(isFail).length;
  const shownNet =
    netFilter === "failed" ? scopeNet.filter(isFail) : netFilter === "api" ? scopeNet.filter(isApi) : scopeNet;
  // trace/video/screenshot are captured files (gate on the *_path); network_log is served from the
  // captured log (or synthesized for old runs) server-side, so it's always servable.
  const arts: { kind: "trace" | "video" | "screenshot" | "network_log"; label: string; enabled: boolean; download?: boolean }[] = [
    { kind: "trace", label: "trace ↓", enabled: !!detail.trace_path, download: true },
    { kind: "video", label: "video ↗", enabled: !!detail.video_path },
    { kind: "screenshot", label: "screenshot ↗", enabled: !!detail.screenshot_path },
    { kind: "network_log", label: "network log ↗", enabled: true },
  ];

  return shell(
    <>
      <div className="insp">
        {/* left — step tree */}
        <div className="steps">
          <div className="steps-h">
            <span className="t">Steps</span>
            <span className="n">{steps.length} steps</span>
          </div>
          <div>
            {steps.map((s, i) => {
              const st = statusOf(s);
              return (
                <button key={s.step_index} className={`step ${i === sel ? "sel" : ""}`} onClick={() => jumpTo(i)}>
                  <span className={`sg ${st.cls}`}>{st.glyph}</span>
                  <div className="si">
                    <div className="intent">{s.intent || s.action}</div>
                    <div className="act">
                      {s.action}
                      {markerMs(s) != null ? <span className="vidts"> · @{(markerMs(s)! / 1000).toFixed(1)}s</span> : null}
                    </div>
                  </div>
                  <span className="dur">{fmtDuration(s.elapsed_ms)}</span>
                </button>
              );
            })}
            {steps.length === 0 ? <div className="dempty" style={{ padding: "14px 16px" }}>no steps recorded</div> : null}
          </div>
        </div>

        {/* center — evidence stage */}
        <div className="stage">
          <div className="frame-head">
            <span className="lab">evidence</span>
            <span className="arts">
              {arts.map((a) => (
                <a
                  key={a.kind}
                  className={`art ${a.enabled ? "" : "off"}`}
                  href={a.enabled ? artifactUrl(id, a.kind) : undefined}
                  {...(a.download ? { download: `${id}-trace.zip` } : { target: "_blank", rel: "noreferrer" })}
                  title={a.enabled ? `${a.kind} artifact` : `no ${a.kind} captured`}
                >
                  {a.label}
                </a>
              ))}
            </span>
            <span className="now">{cur ? cur.intent || cur.action : ""}</span>
          </div>
          <div className="frame-wrap">
            <div className="browser">
              <div className="chrome">
                <span className="dots">
                  <i style={{ background: "#e0605a" }} />
                  <i style={{ background: "#e0b13a" }} />
                  <i style={{ background: "#3ecf8e" }} />
                </span>
                <span className="u">{detail.environment}</span>
                <span className="badge">{detail.video_path ? "video" : "screenshot"}</span>
              </div>
              <div className="shot">
                {detail.video_path ? (
                  <video
                    ref={videoRef}
                    controls
                    src={artifactUrl(id, "video")}
                    onLoadedMetadata={(e) => setVideoDur(e.currentTarget.duration || null)}
                    onTimeUpdate={onVideoTime}
                    style={{ width: "100%", display: "block", background: "#000" }}
                    data-testid="run-video"
                  />
                ) : detail.screenshot_path && !shotError ? (
                  <img src={artifactUrl(id, "screenshot")} alt="run screenshot" onError={() => setShotError(true)} />
                ) : (
                  <div className="noshot">
                    no screenshot captured for this run
                    <br />
                    <span className="stub" style={{ marginTop: 10 }}>
                      ◔ per-step before/after frames are target-state — one final screenshot per run today
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
          <div className="timeline">
            <button
              className="play"
              onClick={() => {
                const v = videoRef.current;
                if (detail.video_path && v) (v.paused ? v.play() : v.pause());
                else setPlaying((p) => !p);
              }}
              title={detail.video_path ? "play the run video" : "replay steps"}
              disabled={!steps.length}
            >
              {playing ? "❚❚" : "▶"}
            </button>
            <div className="tl-mid">
              <div className="tl-cap">
                <span className="stepname">{cur ? cur.intent || cur.action : "—"}</span>
                <span className="idx">· step {steps.length ? sel + 1 : 0} / {steps.length}</span>
                <span className="time">{cur ? fmtDuration(cur.elapsed_ms) : ""}</span>
              </div>
              <div className="tl-track" title="click a marker to jump the video to that step">
                <div className="tl-line" />
                {(() => {
                  // marker position: always index-spaced (equidistant) so steps are easy to navigate —
                  // real per-step video offsets cluster in time and bunch markers together. Click-to-jump
                  // and scrub-to-highlight still map to the true video offset via markerMs, below.
                  const pct = (i: number): number =>
                    steps.length > 1 ? (i / (steps.length - 1)) * 100 : 0;
                  const headPct = steps.length ? pct(sel) : 0;
                  return (
                    <>
                      <div className="tl-fill" style={{ width: `${headPct}%` }} />
                      {steps.map((s, i) => {
                        const st = statusOf(s);
                        const t = markerMs(s);
                        return (
                          <button
                            key={s.step_index}
                            className={`tl-mk ${st.cls === "pass" || st.cls === "run" ? "" : st.cls} ${i === sel ? "cur" : ""}`}
                            style={{ left: `${pct(i)}%` }}
                            data-video-offset-ms={t ?? undefined}
                            title={`step ${i + 1} · ${s.intent || s.action}${t != null ? ` · @${(t / 1000).toFixed(1)}s` : ""}`}
                            onClick={() => jumpTo(i)}
                          />
                        );
                      })}
                      <div className="tl-head" style={{ left: `${headPct}%` }} />
                    </>
                  );
                })()}
              </div>
            </div>
          </div>
        </div>

        {/* right — detail tabs */}
        <div className="insp-detail">
          <div className="dtabs">
            <button aria-selected={tab === "step"} onClick={() => setTab("step")}>
              Step
            </button>
            <button aria-selected={tab === "net"} onClick={() => setTab("net")}>
              Network{" "}
              {allNet.length ? (
                <>
                  <span className="badge">{allNet.length}</span>
                  {allNet.some(isFail) ? <span className="dot" title="has failed requests" /> : null}
                </>
              ) : null}
            </button>
            <button aria-selected={tab === "console"} onClick={() => setTab("console")}>
              Console <span className="stub">stub</span>
            </button>
          </div>
          <div className="dbody">
            {tab === "step" && cur ? <StepPane s={cur} idx={sel} total={steps.length} /> : null}
            {tab === "step" && !cur ? <div className="dempty">no step selected</div> : null}
            {tab === "net" ? (
              <>
                <div className="filters" style={{ gap: 6 }}>
                  <button className="fchip" aria-selected={netScope === "run"} onClick={() => setNetScope("run")}>
                    Whole run <span className="c">{allNet.length}</span>
                  </button>
                  <button
                    className="fchip"
                    aria-selected={netScope === "step"}
                    onClick={() => setNetScope("step")}
                    disabled={!cur}
                    title="only requests that fired during the selected step"
                  >
                    This step
                  </button>
                </div>
                <div className="filters">
                  <button className="fchip" aria-selected={netFilter === "all"} onClick={() => setNetFilter("all")}>
                    All <span className="c">{scopeNet.length}</span>
                  </button>
                  <button className="fchip" aria-selected={netFilter === "api"} onClick={() => setNetFilter("api")}>
                    API <span className="c">{scopeNet.filter(isApi).length}</span>
                  </button>
                  <button
                    className="fchip danger"
                    aria-selected={netFilter === "failed"}
                    onClick={() => setNetFilter("failed")}
                  >
                    Failed <span className="c">{fails}</span>
                  </button>
                </div>
                {shownNet.length ? (
                  shownNet.map((n, i) => {
                    const bad = isFail(n);
                    const off = n.video_offset_ms;
                    const scrub = () => {
                      if (videoRef.current && off != null) videoRef.current.currentTime = off / 1000;
                    };
                    return (
                      <div
                        key={i}
                        className={`nrow ${bad ? "fail" : ""}`}
                        onClick={scrub}
                        style={off != null ? { cursor: "pointer" } : undefined}
                        title={n.url}
                      >
                        <div className="l1">
                          <span className={`m ${n.method ?? ""}`}>{n.method}</span>
                          <span className="u">{n.path || n.url}</span>
                          <span className={`st ${bad ? "bad" : "ok"}`}>{n.failure ? "ERR" : n.status}</span>
                        </div>
                        <div className="l2" style={{ display: "flex", gap: 10, flexWrap: "wrap", opacity: 0.8 }}>
                          {n.host ? <span>{n.host}</span> : null}
                          {n.resource_type ? <span>{n.resource_type}</span> : null}
                          <span>{fmtDuration(n.duration_ms)}</span>
                          {n.response_size != null ? <span>{fmtBytes(n.response_size)}</span> : null}
                          {off != null ? <span>@{(off / 1000).toFixed(1)}s</span> : null}
                          {n.matched_expectation ? <span className="exp">✓ expected</span> : null}
                          {n.failure ? <span className="st bad">{n.failure}</span> : null}
                        </div>
                      </div>
                    );
                  })
                ) : (
                  <div className="dempty">
                    {allNet.length
                      ? `No ${netFilter === "all" ? "" : netFilter + " "}requests${netScope === "step" ? " for this step" : ""}.`
                      : "No network log captured for this run."}
                  </div>
                )}
              </>
            ) : null}
            {tab === "console" ? (
              <div className="dempty">
                <span className="stub">◔ console capture is target-state</span>
                <p style={{ marginTop: 12, lineHeight: 1.6 }}>
                  There is no <span className="mono">console</span> column on <span className="mono">step_event</span>{" "}
                  yet, so per-step browser logs aren't recorded. This tab stays stubbed until that lands.
                </p>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </>,
    id,
    detail,
    pinning,
    togglePin,
  );
}

function StepPane({ s, idx, total }: { s: StepEvent; idx: number; total: number }) {
  const st = statusOf(s);
  const d = (s.detail ?? {}) as Record<string, unknown>;
  const verdict = st.cls === "heal" ? "green_with_heals" : st.cls === "fail" ? "red" : st.cls === "run" ? "running" : "green";
  return (
    <>
      <div className="d-crumb">
        step {idx + 1} of {total}
      </div>
      <div className="d-title">
        <Pill verdict={verdict} />
        <span className="tt">{s.intent || s.action}</span>
      </div>

      <div className="dsec">
        <h5>What the engine did</h5>
        <div className="reason">
          Engine action <span className="val">{s.action}</span> — {st.label}
          {s.rung_used ? (
            <>
              {" "}on locator rung <span className="val">{s.rung_used}</span>
            </>
          ) : null}
          .
        </div>
      </div>

      {s.rung_used ? (
        <div className="dsec">
          <h5>Locator</h5>
          <div className={`rung hit ${st.cls === "heal" ? "heal" : ""}`}>
            <span className="rg">{st.cls === "heal" ? "⤴" : "✓"}</span> {s.rung_used}
            <span className="rn">{st.cls === "heal" ? "healed" : "primary"}</span>
          </div>
        </div>
      ) : null}

      <div className="dsec">
        <h5>Timing &amp; detail</h5>
        <div className="dkv">
          <span className="k">status</span>
          <span className="v">{st.label}</span>
          <span className="k">elapsed</span>
          <span className="v">{fmtDuration(s.elapsed_ms)}</span>
          <span className="k">budget</span>
          <span className="v">{fmtDuration(s.budget_ms)}</span>
          {"waits" in d ? (
            <>
              <span className="k">waits</span>
              <span className="v">{String(d.waits)}</span>
            </>
          ) : null}
          {"healed" in d ? (
            <>
              <span className="k">healed</span>
              <span className="v">{String(d.healed)}</span>
            </>
          ) : null}
          {"fallback" in d ? (
            <>
              <span className="k">fallback</span>
              <span className="v">{String(d.fallback)}</span>
            </>
          ) : null}
          {s.error_class ? (
            <>
              <span className="k">error_class</span>
              <span className="v">{s.error_class}</span>
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

// shared shell wrapper (topbar + verdict/meta + pin) so loading/error states keep the chrome.
function shell(
  body: React.ReactNode,
  id: string,
  detail?: RunDetail | null,
  pinning?: boolean,
  togglePin?: () => void,
) {
  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <Link to="/runs">Runs</Link> <span className="sep">/</span> <b>{id.slice(0, 8)}</b>
        </nav>
        {detail ? (
          <div className="runmeta">
            <Pill verdict={detail.verdict} />
            <Link className="chip" to={`/tests/${detail.test_id}`} title="open the test viewer">
              {detail.test_key}
            </Link>
            <span className="chip">{detail.environment}{detail.off_canonical ? " · off-canonical" : ""}</span>
            <span className="chip">{fmtDuration(detail.duration_ms)}</span>
            <span className="stub">◔ live progress streams in target-state · completed run</span>
          </div>
        ) : null}
        <div className="top-actions">
          {detail && togglePin ? (
            <button className={`kbtn ${detail.pinned ? "on" : ""}`} onClick={togglePin} disabled={pinning}>
              📌 {detail.pinned ? "pinned" : "pin"}
            </button>
          ) : null}
          <ChromeToggles />
        </div>
      </header>
      {body}
    </div>
  );
}
