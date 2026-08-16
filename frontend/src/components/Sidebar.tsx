// Left grouped nav (Author / Execution / Target), ported from the prototypes' shell. Only Runs is
// wired this slice; the rest route to a Placeholder until their screens land. The Incidents count is
// fetched live (not the prototype's hard-coded 5).
import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { api } from "../api/client";
import { PROJECT } from "../lib/config";

const link =
  ({ isActive }: { isActive: boolean }) => "nav-item" + (isActive ? " active" : "");

export function Sidebar() {
  const [incidents, setIncidents] = useState<number | null>(null);

  useEffect(() => {
    api
      .GET("/projects/{project}/incidents", { params: { path: { project: PROJECT } } })
      .then(({ data }) => setIncidents(data ? data.length : 0))
      .catch(() => setIncidents(null));
  }, []);

  return (
    <aside className="sidebar">
      <div className="ws">
        <span className="logo">A</span> Anneal
      </div>
      <div className="nav-group">
        <h4>Author</h4>
        <NavLink className={link} to="/author">
          <span className="ico">✎</span> New test
        </NavLink>
      </div>
      <div className="nav-group">
        <h4>Execution</h4>
        <NavLink className={link} to="/suites">
          <span className="ico">▷</span> Suites
        </NavLink>
        <NavLink className={link} to="/tests">
          <span className="ico">⊞</span> Tests
        </NavLink>
        <NavLink className={link} to="/runs">
          <span className="ico">≡</span> Runs
        </NavLink>
        <NavLink className={link} to="/incidents">
          <span className="ico">⚑</span> Incidents
          {incidents ? <span className="count">{incidents}</span> : null}
        </NavLink>
        <NavLink className={link} to="/changelog">
          <span className="ico">↻</span> Changelog
        </NavLink>
      </div>
      <div className="nav-group">
        <h4>Target</h4>
        <NavLink className={link} to="/target-auth">
          <span className="ico">◎</span> Targets &amp; auth
        </NavLink>
        <NavLink className={link} to="/territory">
          <span className="ico">◈</span> Territory
        </NavLink>
        <NavLink className={link} to="/sessions">
          <span className="ico">⚿</span> Sessions
        </NavLink>
      </div>
      <div className="side-foot">anneal</div>
    </aside>
  );
}
