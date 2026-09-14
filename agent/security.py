"""A minimal shared-secret guard for the agent service.

This is a demo/portfolio project talking to itself over a private
docker/localhost network - it doesn't need OAuth. But "no auth at all" on
an endpoint that (a) costs real money per call once an LLM key is set and
(b) writes to the system of record is the wrong default the moment this
ever sits anywhere less trusted than localhost. This gives the cheapest
real control for that: a shared secret in a header, service-to-service,
the same shape as a Stripe/GitHub webhook secret.

It's opt-in by design - unset AGENT_API_KEY and every route stays open,
which is exactly what local development and the test suite want. Set it
(and pass the matching X-API-Key header from n8n's HTTP Request node) the
moment this deploys anywhere reachable beyond your own machine. Real
per-user auth, audit trails, and approval gates are project #09's job
(see the portfolio report), not this one's - this is the floor, not the
ceiling.
"""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency - raises 401 if AGENT_API_KEY is set and the
    caller's X-API-Key header doesn't match it.

    Uses secrets.compare_digest() rather than `==` so the comparison
    doesn't leak timing information about how many leading characters of
    the key were correct - a constant-time compare is the standard way to
    check a secret, even a low-stakes one like this.
    """
    expected = os.environ.get("AGENT_API_KEY")
    if not expected:
        return  # auth disabled - the documented local-dev default
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")
