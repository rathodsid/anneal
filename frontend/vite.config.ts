import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: point VITE_API_BASE at the API (http://localhost:8080) and add the dev origin to
// ANNEAL_CORS_ORIGINS on the API side. Prod: built dist/ is served same-origin by FastAPI.
export default defineConfig({
  plugins: [react()],
  // The design system lives in ../design/tokens.css (single source of truth, another agent's lane —
  // we import it, never re-inline it). Allow Vite's dev server to read it from the repo root.
  server: { fs: { allow: [".."] } },
});
