// Display formatting shared across screens.

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${s}s`;
}

// Relative time like the prototype's "2h ago" / "1d ago". Falls back to "—" for null.
export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 45) return "just now";
  if (secs < 90) return "1m ago";
  const mins = secs / 60;
  if (mins < 60) return `${Math.round(mins)}m ago`;
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.round(hrs)}h ago`;
  const days = hrs / 24;
  if (days < 30) return `${Math.round(days)}d ago`;
  return new Date(iso).toLocaleDateString();
}

// Absolute timestamp for tooltips.
export function fmtAbs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
