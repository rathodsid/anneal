"""Dump /openapi.json without a running server — the typed-client generation source (A0).
Usage: uv run python scripts/dump-openapi.py <out.json>   (run from frontend/; see package.json gen:api)
"""
import json
import sys

from anneal_api.app import create_app

out = sys.argv[1] if len(sys.argv) > 1 else "openapi.json"
# engine/artifacts are only stashed on app.state — no DB needed to render the schema
spec = create_app(engine=object(), artifacts=object()).openapi()
with open(out, "w") as f:
    json.dump(spec, f, indent=2)
print(f"wrote {out} ({len(spec['paths'])} paths)")
