// Anneal console — app root: chrome (theme/stubs) + router + the shared shell layout.
// This slice wires Runs list → Run inspector; the other nav targets render a Placeholder until
// their screens land (docs/BUILD-SPEC.md §3 drill order).
import { BrowserRouter, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { ChromeProvider, ChromeToggles } from "./components/chrome";
import { Sidebar } from "./components/Sidebar";
import { RunsList } from "./screens/RunsList";
import { RunInspector } from "./screens/RunInspector";
import { Suite } from "./screens/Suite";
import { Suites } from "./screens/Suites";
import { Tests } from "./screens/Tests";
import { TestViewer } from "./screens/TestViewer";
import { IncidentsList, MoneyShot } from "./screens/MoneyShot";
import { Changelog } from "./screens/Changelog";
import { Sessions } from "./screens/Sessions";
import { Territory } from "./screens/Territory";
import { Author } from "./screens/Author";
import { TargetAuth } from "./screens/TargetAuth";

function Layout() {
  return (
    <div className="app">
      <Sidebar />
      <Outlet />
    </div>
  );
}

function Placeholder({ title }: { title: string }) {
  return (
    <div className="main">
      <header className="topbar">
        <nav className="crumbs">
          <b>{title}</b>
        </nav>
        <div className="top-actions">
          <ChromeToggles />
        </div>
      </header>
      <div className="placeholder">
        <h1>{title}</h1>
        <p>This screen isn't built yet. Runs list → Run inspector is the first wired slice.</p>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ChromeProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/runs" replace />} />
            <Route path="/runs" element={<RunsList />} />
            <Route path="/runs/:id" element={<RunInspector />} />
            <Route path="/tests" element={<Tests />} />
            <Route path="/tests/:id" element={<TestViewer />} />
            <Route path="/author" element={<Author />} />
            <Route path="/suites" element={<Suites />} />
            <Route path="/suites/:id" element={<Suite />} />
            <Route path="/incidents" element={<IncidentsList />} />
            <Route path="/incidents/:id" element={<MoneyShot />} />
            <Route path="/changelog" element={<Changelog />} />
            <Route path="/target-auth" element={<TargetAuth />} />
            <Route path="/territory" element={<Territory />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="*" element={<Placeholder title="Not found" />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ChromeProvider>
  );
}
