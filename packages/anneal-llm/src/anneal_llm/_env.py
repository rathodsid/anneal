"""Minimal .env loader — populate API keys from the gitignored repo .env.

verify.sh's frozen bring-up does not source .env, so the seam loads it: walk up from CWD to the first
.env and set any KEY=VALUE not already in os.environ (existing env always wins). No third-party dep.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env() -> None:
    for base in [Path.cwd(), *Path.cwd().parents]:
        env = base / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
            return
