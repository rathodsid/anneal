"""`python -m anneal_core.validate <bundle.jsonc>` — the M0 gate's schema check (verify.sh step 1).

Asserts: (1) both JSON schemas are themselves valid draft 2020-12, and (2) the given bundle validates
against schemas/bundle.schema.json. Exits 0 on success, non-zero with the failing paths otherwise.
The JSON Schema is the source of truth (LLD §2); this is the normative check the DB write also runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from .jsoncutil import loads as jsonc_loads


def _find_schema_dir() -> Path:
    for base in [Path.cwd(), *Path.cwd().parents]:
        if (base / "schemas" / "bundle.schema.json").exists():
            return base / "schemas"
    raise SystemExit("could not locate schemas/ directory (run from the repo root)")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m anneal_core.validate <bundle.jsonc>", file=sys.stderr)
        return 2
    bundle_path = Path(argv[0])
    sdir = _find_schema_dir()
    bundle_schema = json.loads((sdir / "bundle.schema.json").read_text())
    stepevent_schema = json.loads((sdir / "step-event.schema.json").read_text())

    # 1. both schemas are themselves valid draft 2020-12
    for name, sch in (("bundle", bundle_schema), ("step-event", stepevent_schema)):
        try:
            Draft202012Validator.check_schema(sch)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ schemas/{name}.schema.json is not valid draft 2020-12: {e}", file=sys.stderr)
            return 1

    # 2. the bundle validates against the bundle schema
    data = jsonc_loads(bundle_path.read_text())
    validator = Draft202012Validator(bundle_schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        for e in errors[:20]:
            loc = "/".join(str(p) for p in e.path) or "<root>"
            print(f"  ✗ {loc}: {e.message}", file=sys.stderr)
        return 1

    print(f"OK: {bundle_path.name} valid against bundle.schema.json "
          f"({len(data.get('steps', []))} steps); both schemas valid draft 2020-12")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
