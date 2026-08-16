import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Design system: the canonical tokens live in ./styles/tokens.css (single source of truth,
// imported — never re-inlined). Base + shell CSS follows.
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/runs.css";
import "./styles/inspector.css";
import "./styles/suite.css";
import "./styles/suites.css";
import "./styles/labels.css";
import "./styles/tests.css";
import "./styles/testviewer.css";
import "./styles/moneyshot.css";
import "./styles/changelog.css";
import "./styles/target.css";
import "./styles/author.css";
import "./styles/targetauth.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
