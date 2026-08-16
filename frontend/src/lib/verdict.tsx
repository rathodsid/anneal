// Colorblind-safe verdict rendering (colour + GLYPH + LABEL), ported from the prototypes'
// G / VC / VL maps. `provisional` is a calm teal half-ring (.g-prov), never amber. Classes come
// from styles/tokens.css.
import type { ReactNode } from "react";

const GLYPH: Record<string, ReactNode> = {
  green: "✓",
  green_with_heals: "⤴",
  provisional: <span className="g-prov" />,
  red: "✕",
  parked_auth: "⏸",
  parked_env: "⏸",
  running: "◐",
  pending: "○",
};

const CLASS: Record<string, string> = {
  green: "v-green",
  green_with_heals: "v-heals",
  provisional: "v-prov",
  red: "v-red",
  parked_auth: "v-park",
  parked_env: "v-park",
  running: "v-run",
  pending: "v-park",
};

const LABEL: Record<string, string> = {
  green: "green",
  green_with_heals: "green · healed",
  provisional: "provisional",
  red: "red",
  parked_auth: "parked · auth",
  parked_env: "parked · env",
  running: "running",
  pending: "pending",
};

export function Pill({ verdict }: { verdict: string }) {
  const cls = CLASS[verdict] ?? "v-park";
  const glyph = GLYPH[verdict] ?? "•";
  const label = LABEL[verdict] ?? verdict;
  return (
    <span className={`pill ${cls}`}>
      <span className="g">{glyph}</span>
      {label}
    </span>
  );
}
