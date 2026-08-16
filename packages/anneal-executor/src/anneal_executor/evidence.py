"""Evidence capture (always-on): trace + video + final screenshot per run.

Evidence is executor-homed by D31 (no pytest process exists). Always-on video/trace/screenshot
plus storage-state injection live here as a
context manager the scheduler (M4) and the golden harness both use. Same-host XHR filtering + `_rsc=`
exclusion are a WARM/HOT surface-map concern (M2); cold evidence is passive.
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .netcapture import NetworkCollector


class Evidence:
    def __init__(self):
        self.trace_path: str | None = None
        self.video_path: str | None = None
        self.network_log_path: str | None = None
        self.screenshot_path: str | None = None
        self.video_epoch: float | None = None   # monotonic ref at context creation = video t=0


@asynccontextmanager
async def recording(browser, artifacts, run_id: str, *, base_url: str | None = None,
                    storage_state=None, viewport=None):
    run_dir = Path(artifacts.run_dir(run_id))
    video_dir = run_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    ctx = await browser.new_context(
        base_url=base_url,
        storage_state=storage_state,
        record_video_dir=str(video_dir),
        viewport=viewport or {"width": 1280, "height": 720},
    )
    video_epoch = time.monotonic()             # Playwright starts recording at context creation
    await ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = await ctx.new_page()
    collector = NetworkCollector(video_epoch)     # passive, always-on network log (additive evidence)
    collector.attach(page)
    ev = Evidence()
    ev.video_epoch = video_epoch
    try:
        yield page, ev
    finally:
        try:
            ev.screenshot_path = str(run_dir / "final.png")
            await page.screenshot(path=ev.screenshot_path)
        except Exception:
            ev.screenshot_path = None
        ev.trace_path = str(run_dir / "trace.zip")
        try:
            await ctx.tracing.stop(path=ev.trace_path)
        except Exception:
            ev.trace_path = None
        # Drain the per-request reads while the context is still open, then persist the log.
        try:
            await collector.flush()
            ev.network_log_path = str(run_dir / "network.json")
            Path(ev.network_log_path).write_text(json.dumps(collector.entries))
        except Exception:
            ev.network_log_path = None
        vid = page.video
        await ctx.close()                          # flushes the video to disk
        try:
            ev.video_path = await vid.path() if vid else None
        except Exception:
            ev.video_path = None
