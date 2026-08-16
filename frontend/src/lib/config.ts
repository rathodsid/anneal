// Single-project deployment for now: every path is /projects/{p}/…
// and {p} is the text PK. Dev/golden = "golden"; override with VITE_PROJECT if a deployment differs.
export const PROJECT = import.meta.env.VITE_PROJECT ?? "golden";

// Base URL of the API (same as the typed client). "" = same-origin (prod). Used to build direct
// URLs for binary artifact bytes (<img src>, video, downloads), which don't go through openapi-fetch.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export function artifactUrl(runId: string, kind: "trace" | "video" | "screenshot" | "network_log"): string {
  return `${API_BASE}/runs/${runId}/artifacts/${kind}`;
}

// Default target selection across screens: prefer the canonical production target, then any
// non-expired target, then whatever's first.
export function pickDefaultTarget<T extends { id: string; key: string }>(targets: T[]): string {
  return (
    targets.find((t) => /prod/i.test(t.key))?.id ??
    targets.find((t) => !t.key.includes("expired"))?.id ??
    targets[0]?.id ??
    ""
  );
}
