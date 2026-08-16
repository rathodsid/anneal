"""`anneal` CLI — a thin httpx client of the API (D32). run · status · export (never rulings).

ANNEAL_API selects the engine surface (default http://localhost:8080). The CLI forks NO behavior:
`run` POSTs the same launch endpoint a CI deploy-hook calls; `status`/`export` are reads.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx


def _base() -> str:
    return os.getenv("ANNEAL_API", "http://localhost:8080")

_PASS_VERDICTS = {"green", "green_with_heals", "provisional"}


def cmd_run(a, client) -> int:
    # B1: launch is async (202 + suite_run_id immediately). The CLI keeps its blocking UX and CI
    # exit-code gate by polling GET /suite_runs/{id} to a terminal state.
    if a.label:
        # Run every test carrying a label. --target is OPTIONAL: the API derives it from the members'
        # canonical binding, so the console and the CLI can't disagree about which environment a
        # label runs on (D32 — the rule lives in the ONE API, not in each client).
        params = {"trigger": "manual"}
        if a.target:
            params["target_id"] = a.target
        r = client.post(f"/projects/{a.project}/labels/{a.label}/runs", params=params)
        if r.status_code == 404:
            print(f"no tests carry label {a.label!r}")
            return 1
    else:
        r = client.post(f"/projects/{a.project}/suites/{a.suite}/runs",
                        params={"target_id": a.target, "trigger": "manual"})
    if r.status_code == 409:
        detail = (r.json().get("detail") or {})
        if detail.get("error") == "label spans targets":
            # A label whose members are canonical to different environments can't run as one run:
            # whichever target were picked, the rest would run read-only. Fail loudly for CI.
            print(f"label {a.label!r} spans {len(detail['split'])} canonical targets — "
                  f"pick one with --target:")
            for s in detail["split"]:
                print(f"  {s['target_key']}  {s['test_count']} test(s)  target_id={s['target_id']}")
            return 1
        print(f"target busy: active suite_run {detail.get('active_suite_run_id')}")
        return 1
    r.raise_for_status()
    sr_id = r.json()["suite_run_id"]
    if a.label and not a.target:
        d0 = client.get(f"/suite_runs/{sr_id}").json()
        print(f"label {a.label!r} → target {d0['target_id']} (derived from the members)")
    while True:
        d = client.get(f"/suite_runs/{sr_id}").json()
        if d["state"] in ("done", "parked"):
            break
        time.sleep(1.0)
    print(f"suite_run {d['id']}: {d['verdict']} ({d['state']})"
          + (f" park={d['park_reason']}" if d.get("park_reason") else ""))
    for res in d.get("results", []):
        print(f"  {res['test_id'][:8]} {res['verdict']} {res['duration_ms']}ms")
    return 0 if d["verdict"] in _PASS_VERDICTS else 1


def cmd_status(a, client) -> int:
    r = client.get(f"/runs/{a.run}")
    r.raise_for_status()
    d = r.json()
    print(f"{d['test_key']}: {d['verdict']}  (env {d['environment']}, {d.get('duration_ms')}ms)")
    for s in d.get("steps", []):
        print(f"  [{s['step_index']}] {s['step_id']:16} {s['action']:7} {s['status']:6} "
              f"rung={s['rung_used']}")
    return 0


def cmd_export(a, client) -> int:
    r = client.get(f"/export/{a.test}/{a.version}")
    r.raise_for_status()
    sys.stdout.write(r.json()["script"])
    return 0


def cmd_author(a, client) -> int:
    answers = {}
    for kv in (a.answer or []):
        q, _, ans = kv.partition("=")
        answers[q] = ans
    r = client.post(f"/projects/{a.project}/tests",
                    params={"test_key": a.test_key, "target_id": a.target},
                    json={"nl": a.nl, "answers": answers or None})
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "needs_answers":
        print("ambiguity interview — answer each and re-run with --answer 'question=answer':")
        for q in d["questions"]:
            print(f"  ? {q}")
        return 2
    print(f"frozen test {d['test_id']} v{d['version']} ({d['steps']} steps); calibration passed")
    return 0


def cmd_onboard(a, client) -> int:
    # design §3.6: onboard a platform = one call (mirror of POST /projects/{p}/targets). Creds are
    # optional — the env layers (ANNEAL_<REF>_* / ANNEAL_UI_*) can cover them instead.
    auth = {"kind": a.kind}
    if a.creds_ref:
        auth["creds_ref"] = a.creds_ref
    if a.signin_path:
        auth["signin_path"] = a.signin_path
    body = {"key": a.key, "base_url": a.base_url, "auth": auth}
    if a.probe_path:
        body["probe_path"] = a.probe_path
    if a.persona:
        body["persona"] = a.persona
    if a.username:
        body["username"] = a.username
    if a.password:
        body["password"] = a.password
    r = client.post(f"/projects/{a.project}/targets", json=body)
    r.raise_for_status()
    d = r.json()
    print(f"onboarded {d['key']} ({d['target_id'][:8]}) auth={d['auth'].get('kind')} "
          f"creds={'stored' if d['credential_stored'] else 'env/none'}")
    return 0


def cmd_select(a, client) -> int:
    # §11 change-driven selection preview (read-only): per-test score + human reason. No rulings here.
    r = client.post(f"/projects/{a.project}/runs/select",
                    params={"target_id": a.target}, json={"change": a.change, "launch": bool(a.launch)})
    r.raise_for_status()
    d = r.json()
    for p in d["selection"]:
        mark = "✓" if p["selected"] else "·"
        print(f"  {mark} {p['test_key']:<28} {p['score']:.2f}  {p['reason']}")
    print(f"selected {len(d['selected'])}/{len(d['selection'])} tests")
    if d.get("run"):
        print(f"launched selection run {d['run']['suite_run_id']} (async — poll /suite_runs/{{id}})")
    return 0


def main(argv=None, client=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="anneal")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="launch a suite run, or every test carrying a label")
    pr.add_argument("project"); pr.add_argument("suite", nargs="?")
    pr.add_argument("--label", help="run every test carrying this label (instead of a suite)")
    pr.add_argument("--target", help="required for a suite; optional for --label (derived)")
    ps = sub.add_parser("status", help="show a run's verdict + step frames")
    ps.add_argument("run")
    pe = sub.add_parser("export", help="print the standalone Playwright script for a test version")
    pe.add_argument("test"); pe.add_argument("version", type=int)
    pa = sub.add_parser("author", help="author a test from NL (hot lane)")
    pa.add_argument("project"); pa.add_argument("test_key")
    pa.add_argument("--target", required=True); pa.add_argument("--nl", required=True)
    pa.add_argument("--answer", action="append", default=[], help="question=answer (repeatable)")
    psel = sub.add_parser("select", help="change-driven selection preview (§11): per-test reasons")
    psel.add_argument("project"); psel.add_argument("--target", required=True)
    psel.add_argument("--change", required=True, help="NL change description")
    psel.add_argument("--launch", action="store_true", help="also run the selected tests")
    po = sub.add_parser("onboard", help="onboard a platform (target + optional credential) — §3.6")
    po.add_argument("project"); po.add_argument("--key", required=True)
    po.add_argument("--base-url", required=True, dest="base_url")
    po.add_argument("--kind", default="auto", help="auth strategy: clerk|form|auto|none")
    po.add_argument("--creds-ref"); po.add_argument("--signin-path", dest="signin_path")
    po.add_argument("--probe-path", dest="probe_path"); po.add_argument("--persona", default="member")
    po.add_argument("--username"); po.add_argument("--password")
    args = p.parse_args(argv)
    if args.cmd == "run":
        if bool(args.suite) == bool(args.label):
            p.error("give exactly one of SUITE or --label")
        if args.suite and not args.target:
            p.error("--target is required when running a suite")

    own = client is None
    client = client or httpx.Client(base_url=_base(), timeout=300)
    try:
        return {"run": cmd_run, "status": cmd_status, "export": cmd_export, "author": cmd_author,
                "select": cmd_select, "onboard": cmd_onboard}[args.cmd](args, client)
    finally:
        if own:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
