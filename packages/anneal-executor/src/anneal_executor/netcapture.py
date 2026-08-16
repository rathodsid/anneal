"""Passive network capture (observability, always-on alongside trace/video). Target-agnostic, no LLM.

A live collector on the page's request lifecycle: for every request it records method, url, resource
type, status, redacted headers, sizes and timing (from Playwright's `request.timing`), plus the ms
offset from video start so the console can scrub to the moment it fired. This is ADDITIVE evidence —
it never feeds classification or any oracle. The armed-expectation network in `step_event.network`
(§5, `arm_before`) is a separate, frozen correctness signal and is untouched.

Security: production traffic carries auth material (bearer tokens, session cookies). Sensitive
request/response headers are redacted at capture and response bodies are never read. Target-agnostic
(Rule 2): every host is captured, none is hardcoded — the console filters. Pure Playwright, so this is
safe for cold replay (D31: no anneal-llm import).
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

# Redact any header whose (lowercased) name is a known secret carrier or merely smells like one — err
# toward over-redaction. Response bodies are never captured, so only headers can leak auth material.
_SENSITIVE_EXACT = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
                    "x-csrf-token", "x-xsrf-token"}
_SENSITIVE_SUBSTR = ("token", "secret", "api-key", "apikey", "auth", "session", "cookie", "password")
_REDACTED = "«redacted»"

# A chatty SPA can fire thousands of requests; cap the log so one run can't grow it unboundedly.
_MAX_ENTRIES = 2000


def _redact(headers: dict) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        kl = k.lower()
        out[k] = _REDACTED if (kl in _SENSITIVE_EXACT or any(s in kl for s in _SENSITIVE_SUBSTR)) else v
    return out


def _phases(t: dict) -> dict:
    """Waterfall phases (ms) from Playwright's request.timing dict. -1 means 'not applicable' → drop.
    All values share one time origin, so intra-timing differences are valid regardless of that origin."""
    def span(a: str, b: str):
        x, y = t.get(a, -1), t.get(b, -1)
        if x is None or y is None or x < 0 or y < 0 or y < x:
            return None
        return round(y - x, 1)
    out = {
        "dns": span("domainLookupStart", "domainLookupEnd"),
        "connect": span("connectStart", "connectEnd"),
        "ssl": span("secureConnectionStart", "connectEnd"),
        "ttfb": span("requestStart", "responseStart"),
        "download": span("responseStart", "responseEnd"),
    }
    return {k: v for k, v in out.items() if v is not None}


class NetworkCollector:
    """Attach to a page before navigation; drain with `flush()` before the context closes."""

    def __init__(self, video_epoch: float):
        self._video_epoch = video_epoch
        self._starts: dict = {}          # request → monotonic start (for the video offset)
        self._tasks: list = []           # in-flight per-request build coroutines
        self.entries: list[dict] = []

    def attach(self, page) -> None:
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)

    def _on_request(self, request) -> None:
        try:
            self._starts[request] = time.monotonic()
        except Exception:
            pass

    def _on_finished(self, request) -> None:
        self._schedule(request, None)

    def _on_failed(self, request) -> None:
        failure = None
        try:
            failure = request.failure
        except Exception:
            pass
        self._schedule(request, failure or "failed")

    def _schedule(self, request, failure) -> None:
        if len(self._tasks) >= _MAX_ENTRIES:
            return                       # hard cap — drop the tail rather than grow unboundedly
        self._tasks.append(asyncio.ensure_future(self._record(request, failure)))

    async def _record(self, request, failure) -> None:
        try:
            entry = await self._build(request, failure)
        except Exception:
            return                       # a request whose context tore down mid-read is dropped, never fatal
        if entry is not None:
            self.entries.append(entry)

    async def _build(self, request, failure) -> dict:
        start = self._starts.pop(request, None)
        offset = round((start - self._video_epoch) * 1000) if start is not None else None
        parts = urlsplit(request.url)
        entry: dict = {
            "method": request.method,
            "url": request.url,
            "host": parts.netloc,
            "path": parts.path or "/",
            "resource_type": request.resource_type,
            "video_offset_ms": offset,
            "failure": failure,
        }
        try:
            entry["request_headers"] = _redact(await request.all_headers())
        except Exception:
            entry["request_headers"] = {}
        try:
            pd = request.post_data
            entry["request_size"] = len(pd.encode("utf-8", "ignore")) if pd else 0
        except Exception:
            entry["request_size"] = None
        try:
            t = request.timing or {}
            # Playwright timing: `startTime` is a Unix-epoch ms; every other field is relative to it.
            # So `responseEnd` already IS the total elapsed ms from request start (−1 when unavailable).
            dur = t.get("responseEnd", -1)
            entry["duration_ms"] = round(dur, 1) if dur is not None and dur > 0 else None
            entry["timing"] = _phases(t)
        except Exception:
            entry["duration_ms"], entry["timing"] = None, {}

        resp = None
        try:
            resp = await request.response()
        except Exception:
            resp = None
        if resp is not None:
            entry["status"] = resp.status
            try:
                entry["status_text"] = resp.status_text
            except Exception:
                pass
            try:
                rh = _redact(await resp.all_headers())
            except Exception:
                rh = {}
            entry["response_headers"] = rh
            cl = rh.get("content-length")
            try:
                entry["response_size"] = int(cl) if cl is not None else None
            except Exception:
                entry["response_size"] = None
        return entry

    async def flush(self, *, timeout_s: float = 5.0) -> None:
        """Await all in-flight per-request reads (context must still be open), then order the log by
        the moment each request started so the console reads it as a chronological waterfall.

        BOUNDED (like the settle gate): a request whose `response()`/`all_headers()` never resolves —
        a streaming/long-poll socket, a torn-down context — must not hang the run. On timeout we take
        whatever entries completed and drop the stragglers. Capture is best-effort evidence, never
        load-bearing."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout_s)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                for t in pending:
                    t.cancel()
        self._tasks = []
        self.entries.sort(key=lambda e: (e.get("video_offset_ms") is None, e.get("video_offset_ms") or 0))
