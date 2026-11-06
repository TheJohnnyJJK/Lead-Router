"""Two cheap guards in front of every route, added after the 2026-11-06 attack
(SECURITY_FINDINGS.md): a request-size limit and a per-IP rate limit.

Both are plain ASGI middleware rather than FastAPI dependencies, so they run
before the body is read and before auth - which is the point. The attack
showed a 30 MB body being accepted, and 300 wrong-key requests in under 5
seconds each writing an audit row.

They're deliberately simple and in-memory: right for one process. With
several workers or servers, put the same limits in the reverse proxy
(nginx `client_max_body_size` / `limit_req`) instead.
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import store

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024)))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))
_EXEMPT_PATHS = {"/health"}  # load balancers poll this; it touches nothing


async def _reply(send, status: int, detail: str, extra_headers=()) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode()), *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


class BodySizeLimit:
    """413 for any body over MAX_BODY_BYTES - checked against Content-Length
    up front, and counted while streaming for chunked uploads that don't
    send one. A real lead is a few KB; LeadIn caps the message at 5,000
    characters anyway."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            store.log_event("rejected", "body_too_large", f"{declared.decode()} bytes declared")
            return await _reply(send, 413, f"request body over {self.max_bytes} bytes")

        seen = 0
        started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            store.log_event("rejected", "body_too_large", f"over {self.max_bytes} bytes (streamed)")
            if not started:
                await _reply(send, 413, f"request body over {self.max_bytes} bytes")


class RateLimit:
    """Fixed one-minute window per client IP. Over the limit: 429 with
    Retry-After. Only the first refusal per IP per window is written to
    audit_events, so a flood can't turn into a flood of audit rows.

    Uses the socket's address, not X-Forwarded-For, because that header is
    whatever the caller says it is. Behind a proxy, rate-limit at the proxy."""

    def __init__(self, app, per_minute: int = RATE_LIMIT_PER_MINUTE):
        self.app, self.per_minute = app, per_minute
        self._windows: dict[str, tuple[int, int]] = {}  # ip -> (window, count)
        self._lock = threading.Lock()

    def _check(self, ip: str) -> tuple[bool, bool, int]:
        """(allowed, first_refusal_this_window, seconds_until_reset)"""
        now = time.time()
        window = int(now // 60)
        with self._lock:
            if len(self._windows) > 10_000:  # forget old windows; bounded memory
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
            w, n = self._windows.get(ip, (window, 0))
            if w != window:
                w, n = window, 0
            n += 1
            self._windows[ip] = (w, n)
        return n <= self.per_minute, n == self.per_minute + 1, 60 - int(now % 60)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in _EXEMPT_PATHS:
            return await self.app(scope, receive, send)
        ip = (scope.get("client") or ("unknown", 0))[0]
        allowed, first_refusal, retry_after = self._check(ip)
        if allowed:
            return await self.app(scope, receive, send)
        if first_refusal:
            store.log_event("rejected", "rate_limited",
                            f"{ip} went over {self.per_minute} requests/minute")
        await _reply(send, 429, "too many requests",
                     [(b"retry-after", str(retry_after).encode())])
