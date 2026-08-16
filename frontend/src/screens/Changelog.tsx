// Changelog — the project-wide append-only feed (heals, re-authors, rulings, authoring). Wired to GET /projects/{p}/changelog.
//
// The real `kind` is a compound string (e.g. "behavior_ruling:mixed"), so tabs/chips categorize by
// substring. Filter-by-test/kind/since + pagination stay client-side/target-state (the list endpoint
// is unfiltered) — the .stub marker is kept.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { components } from "../api/schema";
import { ChromeToggles } from "../components/chrome";
import { PROJECT } from "../lib/config";
import { relTime } from "../lib/format";

type ChangelogEntry = components["schemas"]["ChangelogEntry"];

type Cat = "all" | "heal" | "reauthor" | "ruling" | "authored";
const TABS: { key: Cat; label: string }[] = [
  { key: "all", label: "All" },
  { key: "heal", label: "Heals" },
  { key: "reauthor", label: "Re-authors" },
  { key: "ruling", label: "Rulings" },
  { key: "authored", label: "Authored" },
];

// map a raw compound kind → filter category + chip class.
function categorize(kind: string): Cat | "rebaseline" | "other" {
  const k = kind.toLowerCase();
  if (k.includes("heal")) return "heal";
  if (k.includes("reauthor")) return "reauthor";
  if (k.includes("ruling")) return "ruling";
  if (k.includes("rebaseline")) return "rebaseline";
  if (k.includes("author")) return "authored";
  return "other";
}
const CHIP: Record<string, string> = {
  heal: "kind-heal",
  reauthor: "kind-reauthor",
  ruling: "kind-ruling",
  authored: "kind-authored",
  rebaseline: "kind-rebaseline",
  other: "kind-rebaseline",
};

export function Changelog() {
  const [entries, setEntries] = useState<ChangelogEntry[] | null>(null);
  const [view, setView] = useState<Cat>("all");

  useEffect(() => {
    api
      .GET("/projects/{project}/changelog", { params: { path: { project: PROJECT } } })
      .then(({ data }) => setEntries(data ?? []));
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: entries?.length ?? 0, heal: 0, reauthor: 0, ruling: 0, authored: 0 };
    for (const e of entries ?? []) {
      const cat = categorize(e.kind);
      if (cat in c && cat !== "all") c[cat]++;
    }
    return c;
  }, [entries]);

  const shown = useMemo(
    () => (entries ?? []).filter((e) => view === "all" || categorize(e.kind) === view),
    [entries, view],
  );

  return (
    <div className="main cl">
      <header className="topbar">
        <nav className="crumbs">
          <b>Changelog</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="content">
        <div className="cl-hd">
          <h1>Changelog</h1>
          <div className="sub">
            The project-wide append-only record of everything that happened to tests — every self-heal,
            re-author, ruling and authoring, newest first. Each entry links back to its test and, where
            relevant, its incident.
          </div>
        </div>

        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.key} role="tab" aria-selected={view === t.key} onClick={() => setView(t.key)}>
              {t.label} <span className="b">{counts[t.key]}</span>
            </button>
          ))}
        </div>
        <div className="toolbar" style={{ marginBottom: 8 }}>
          <span className="stub">◔ filter by test / kind / since is target-state — feed is unfiltered today (views run client-side)</span>
        </div>

        {entries === null ? (
          <div className="async">loading…</div>
        ) : shown.length === 0 ? (
          <div className="cl-empty">No entries of this kind.</div>
        ) : (
          <div className="feed">
            {shown.map((e) => {
              const cat = categorize(e.kind);
              return (
                <div className="clog" key={e.id}>
                  <span className={`kind ${CHIP[cat]}`}>{cat === "other" ? e.kind : cat}</span>
                  <div className="body">
                    <div className="cs">
                      {e.test_id ? (
                        <>
                          <Link className="tk" to={`/tests/${e.test_id}`}>
                            test
                          </Link>{" "}
                          —{" "}
                        </>
                      ) : null}
                      {e.summary}
                    </div>
                    {e.ruling_note ? <div className="rn">"{e.ruling_note}"</div> : null}
                    <div className="cw">
                      <span>{relTime(e.created_at)}</span>
                      {e.incident_id ? (
                        <span>
                          · <Link to={`/incidents/${e.incident_id}`}>incident →</Link>
                        </span>
                      ) : null}
                      {e.test_id ? (
                        <span>
                          · <Link to={`/tests/${e.test_id}`}>open test →</Link>
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="cl-pager">
          <span>{entries ? `Showing ${shown.length} of ${entries.length} entries` : ""}</span>
          <span style={{ marginLeft: "auto" }} />
          <span className="stub">◔ pagination is target-state</span>
        </div>
      </div>
    </div>
  );
}
