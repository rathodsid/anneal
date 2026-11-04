import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: point VITE_API_BASE at the API (http://localhost:8080) and add the dev origin to
// ANNEAL_CORS_ORIGINS on the API side. Prod: built dist/ is served same-origin by FastAPI.
export default defineConfig({
  plugins: [react()],
});
