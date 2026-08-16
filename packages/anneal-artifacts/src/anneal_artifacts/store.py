"""LocalDiskStore (D34) — the v1 laptop-compose ArtifactStore. Implements ports.ArtifactStore.

Artifacts (trace/video/screenshot/network-log) live under a per-run directory on local disk; refs are
absolute paths. A MinioStore is the later swap behind the same Protocol.
"""
from __future__ import annotations

from pathlib import Path


class LocalDiskStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def run_dir(self, run_id: str) -> str:
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def path_for(self, run_id: str, name: str) -> str:
        return str(Path(self.run_dir(run_id)) / name)

    def put(self, run_id: str, name: str, data: bytes) -> str:
        p = self.path_for(run_id, name)
        Path(p).write_bytes(data)
        return p
