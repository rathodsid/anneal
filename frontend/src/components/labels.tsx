// Label chips + the one label editor, shared by the test viewer and the author form so the project's
// vocabulary converges instead of forking per screen (existing labels are offered before "create").
//
// Chips are deliberately HUE-FREE: styles/tokens.css locks colour+glyph as the verdict language, and a
// coloured label taxonomy would compete with the one signal that must never be misread.
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { PROJECT } from "../lib/config";

type LabelCount = components["schemas"]["LabelCount"];

// mirrors the API's shape rule (anneal_api.models.LABEL_RE) — stated up front in the menu rather than
// discovered as a 422
const SHAPE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
export const labelShapeHint = "a–z 0–9 - _ · max 32";
export const isValidLabel = (s: string) => SHAPE.test(s);

/** Every label in use in this project, with counts. Refetched on `bump`. */
export function useProjectLabels(bump: unknown = 0) {
  const [labels, setLabels] = useState<LabelCount[]>([]);
  useEffect(() => {
    api
      .GET("/projects/{project}/labels", { params: { path: { project: PROJECT } } })
      .then(({ data }) => setLabels(data ?? []))
      .catch(() => setLabels([]));
  }, [bump]);
  return labels;
}

export function LabelChip({ label, on }: { label: string; on?: boolean }) {
  return (
    <span className={"lchip" + (on ? " on" : "")}>
      <span className="h">⌗</span>
      {label}
    </span>
  );
}

/** Read-only chip row (test rows in the catalog). */
export function LabelChips({ labels, match }: { labels: string[]; match?: string | null }) {
  if (!labels.length) return null;
  return (
    <>
      {labels.map((l) => (
        <LabelChip key={l} label={l} on={!!match && l === match} />
      ))}
    </>
  );
}

/**
 * Editable label set. `onChange` receives the whole next set — the API replaces the set too, so there
 * is no add/remove verb to race. Purely presentational about persistence: the viewer PATCHes, the
 * author form keeps it in local state until freeze.
 */
export function LabelEditor({
  value,
  options,
  onChange,
  busy,
}: {
  value: string[];
  options: LabelCount[];
  onChange: (next: string[]) => void;
  busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const wrap = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const typed = q.trim().toLowerCase();
  const existing = options.filter((o) => !value.includes(o.label) && o.label.includes(typed));
  const canCreate = !!typed && isValidLabel(typed) && !value.includes(typed) &&
    !options.some((o) => o.label === typed);

  function add(label: string) {
    onChange([...value, label]);
    setQ("");
    setOpen(false);
  }

  return (
    <>
      {value.map((l) => (
        <span className="lchip x" key={l}>
          <span className="h">⌗</span>
          {l}
          <button
            className="rm"
            disabled={busy}
            title={`remove ${l}`}
            onClick={() => onChange(value.filter((v) => v !== l))}
          >
            ×
          </button>
        </span>
      ))}
      <span className="ddwrap" ref={wrap}>
        <button className="lchip add" disabled={busy} onClick={() => setOpen((o) => !o)}>
          + label
        </button>
        {open ? (
          <div className="menu">
            <input
              className="lq"
              autoFocus
              placeholder="find or create…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canCreate) add(typed);
                if (e.key === "Enter" && !canCreate && existing.length) add(existing[0].label);
                if (e.key === "Escape") setOpen(false);
              }}
            />
            {existing.length ? <div className="mh">existing in this project</div> : null}
            {existing.map((o) => (
              <button className="mi" key={o.label} onClick={() => add(o.label)}>
                <span>⌗ {o.label}</span>
                <span className="n">{o.test_count} test{o.test_count === 1 ? "" : "s"}</span>
              </button>
            ))}
            {canCreate ? (
              <>
                {existing.length ? <div className="sep" /> : null}
                <button className="mi" onClick={() => add(typed)}>
                  <span>＋ create “{typed}”</span>
                </button>
              </>
            ) : null}
            {typed && !isValidLabel(typed) ? (
              <div className="mi bad">
                <span>“{typed}” is not a valid label</span>
              </div>
            ) : null}
            <div className="mh">shape</div>
            <div className="mi hint">
              <span>{labelShapeHint}</span>
            </div>
          </div>
        ) : null}
      </span>
    </>
  );
}
