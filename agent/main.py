"""FastAPI service - this is what n8n's HTTP Request node calls.

POST /qualify    run the graph on one inbound lead, persist it, return the decision
GET  /leads      recent leads, optionally filtered by tier
POST /leads/{id}/routed   n8n confirms it delivered a lead (sets routed_at)
GET  /stats      aggregate counts the dashboard is built from
GET  /audit      rejected requests, scoring failures, crashes and repeat leads
GET  /health     liveness check - deliberately unauthenticated, so a load
                 balancer or `docker compose` healthcheck can hit it without
                 needing the shared secret.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from . import store
from .graph import run_lead
from .limits import BodySizeLimit, RateLimit
from .schema import LeadIn, LeadRecord, Tier
from .security import require_api_key

# Every route below except /health depends on this - see agent/security.py
# for what it actually checks (and why it's opt-in via an env var).
Authed = Annotated[None, Depends(require_api_key)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the app starts (and its counterpart, if we needed one,
    # after `yield` when it shuts down) - the modern replacement for the
    # deprecated @app.on_event("startup") decorator.
    store.init_db()
    yield


# The interactive docs (/docs, /redoc, /openapi.json) hand anyone a map of
# every endpoint and field, without a key. On only when asked for.
_docs = os.environ.get("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")
app = FastAPI(
    title="Lead Router Agent", version="0.1.0", lifespan=lifespan,
    docs_url="/docs" if _docs else None, redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)
# Added last = runs first: rate limit, then size limit, then the app.
app.add_middleware(BodySizeLimit)
app.add_middleware(RateLimit)


@app.exception_handler(RequestValidationError)
async def log_rejected_input(request: Request, exc: RequestValidationError):
    """Records every 422 in the audit log, then returns FastAPI's normal 422.
    Only which fields failed and why is stored - never the submitted values,
    which can be personal data or a 5,000-character message."""
    fields = ", ".join(f"{'.'.join(map(str, e['loc']))}:{e['type']}" for e in exc.errors())
    store.log_event("rejected", "validation", f"{request.method} {request.url.path} - {fields}")
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/qualify", response_model=LeadRecord)
def qualify(lead: LeadIn, _auth: Authed) -> LeadRecord:
    """Run the full graph (enrich -> prefilter -> score -> persist) on one
    inbound lead and return the stored, qualified record."""
    try:
        return run_lead(lead)
    except Exception as exc:
        # Still a 500 to the caller, but no longer an invisible one.
        store.log_event(
            "failed", "qualify_error", f"{type(exc).__name__}: {exc}",
            lead_key=store.idempotency_key(lead),
        )
        raise


@app.post("/leads/{lead_id}/routed", response_model=LeadRecord)
def mark_routed(lead_id: int, _auth: Authed) -> LeadRecord:
    """n8n calls this after it has actually delivered the lead (Slack,
    nurture or discard). Until then the lead counts as saved but not
    delivered, and a retry of the same lead gets routed again instead of
    being dropped as a duplicate."""
    record = store.mark_routed(lead_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such lead")
    return record


@app.get("/leads")
def leads(
    _auth: Authed,
    # Capped at 500 rather than left open-ended: an unbounded `limit` lets
    # any caller force a full-table scan/response by passing a huge number.
    # 500 is comfortably more than a dashboard or a human ever needs at once.
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    tier: Tier | None = None,
) -> list[dict]:
    return store.get_leads(limit=limit, tier=tier)


@app.get("/audit")
def audit(_auth: Authed, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict]:
    return store.get_events(limit=limit)


@app.get("/stats")
def stats(_auth: Authed) -> dict:
    return store.get_stats()
