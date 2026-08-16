// The ONE typed API client (A0) — every fetch in the console goes through this; never hand-write
// request/response shapes. Regenerate schema.d.ts with `npm run gen:api` after any API change.
import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_BASE ?? "", // "" = same-origin (prod, served by FastAPI)
});
