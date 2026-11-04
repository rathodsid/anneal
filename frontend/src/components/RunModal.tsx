// The one run-progress modal (suite runs + selection runs): member rows fill in as the async launch
// (202 → poll /suite_runs/{id}, B1) reports results, then the D29 suite verdict. Launch CONTROLS
// (target/trigger pickers) are the caller's: pass them as `controls` and they render until a run exists.
import type React from "react";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { fmtDuration } from "../lib/format";
import { Pill } from "../lib/verdict";

type SuiteRunDetail = components["schemas"]["SuiteRunDetail"];

export type RunModalData = {
  title: string;
  memberKeys: string[];
  suiteRun: SuiteRunDetail | null;
  launching: boolean;
  busyActiveId?: string | null;
  error?: string | null;
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Poll a suite run to a terminal state (done | parked), reporting each snapshot. Resolves when terminal
 *  or after ~5 minutes; the caller clears its `launching` flag afterwards. */
export async function pollSuiteRun(suiteRunId: string, onUpdate: (sr: SuiteRunDetail) => void): Promise<void> {
  for (let i = 0; i < 600; i++) {
    const { data } = await api.GET("/suite_runs/{suite_run_id}", { params: { path: { suite_run_id: suiteRunId } } });
    if (data) {
      onUpdate(data);
      if (data.state === "done" || data.state === "parked") return;
    }
    await sleep(500);
  }
}

export function RunModal({
  state,
  controls,
  bodyClass,
  onClose,
  onOpenRuns,
}: {
  state: RunModalData;
  controls?: React.ReactNode;
  bodyClass?: string;
  onClose: () => void;
  onOpenRuns: (suiteRunId: string) => void;
}) {
  const sr = state.suiteRun;
  const parked = sr?.state === "parked";
  const done = sr?.state === "done" || sr?.state === "parked";
  const byKey = new Map((sr?.results ?? []).map((r) => [r.test_key, r]));

  return (
    <div className="scrim" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-h">
          <span className="tt">
            {state.title}
            {state.launching ? " · running…" : ""}
          </span>
          <button className="x" onClick={onClose}>×</button>
        </div>
        <div className={bodyClass ? `modal-b ${bodyClass}` : "modal-b"}>
          {controls && !sr && !state.busyActiveId && !state.error ? controls : null}

          {state.busyActiveId ? (
            <div className="busy409">
              <b>Target busy</b> — one active run per target (409). An active suite run{" "}
              <span className="mono">{state.busyActiveId.slice(0, 8)}</span> is already in flight. Wait for it to
              finish, then relaunch.
            </div>
          ) : state.error ? (
            <div className="busy409">Launch failed: {state.error}</div>
          ) : parked && (sr?.results?.length ?? 0) === 0 ? (
            <div className="parkbanner">
              <h4>
                <Pill verdict={sr!.verdict ?? "parked_auth"} /> {sr!.park_reason}
              </h4>
              <p>
                The probe gate found the seeded session degraded, so no test was claimed (a degraded session never
                counts as a failure, §9). The suite is <b>parked</b>, not red.
              </p>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn primary sm" disabled>⚿ Re-seed session</button>
                <button className="btn sm" onClick={onClose}>Dismiss</button>
              </div>
              <div className="stub" style={{ marginTop: 10 }}>
                ◔ session health &amp; re-seed are target-state (owned by the auth workstream; no endpoint yet)
              </div>
            </div>
          ) : (
            <>
              {state.memberKeys.map((k) => {
                const r = byKey.get(k);
                return (
                  <div className={`rt-row ${r ? "" : "pending"}`} key={k}>
                    {r ? (
                      <span style={{ flex: "none" }}><Pill verdict={r.verdict} /></span>
                    ) : state.launching ? (
                      <span className="spin">◐</span>
                    ) : (
                      <span style={{ color: "var(--faint)" }}>○</span>
                    )}
                    <span className="k">{k}</span>
                    {r?.error_class ? <span className="ec">{r.error_class}</span> : null}
                    <span className="d">{r ? fmtDuration(r.duration_ms) : ""}</span>
                  </div>
                );
              })}
              {done && sr?.verdict ? (
                <div className="suiteverdict">
                  <span className="lbl">suite verdict</span>
                  <Pill verdict={sr.verdict} />
                  <button className="btn sm" style={{ marginLeft: "auto" }} onClick={() => onOpenRuns(sr.id)}>
                    open runs →
                  </button>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
