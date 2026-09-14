"""FastAPI service - this is what n8n's HTTP Request node calls.

POST /qualify    run the graph on one inbound lead, persist it, return the decision
GET  /leads      recent leads, optionally filtered by tier
GET  /stats      aggregate counts the dashboard is built from
GET  /health     liveness check - deliberately unauthenticated, so a load
                 balancer or `docker compose` healthcheck can hit it without
                 needing the shared secret.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query

from . import store
from .graph import run_lead
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


app = FastAPI(title="Lead Router Agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/qualify", response_model=LeadRecord)
def qualify(lead: LeadIn, _auth: Authed) -> LeadRecord:
    """Run the full graph (enrich -> prefilter -> score -> persist) on one
    inbound lead and return the stored, qualified record."""
    return run_lead(lead)


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


@app.get("/stats")
def stats(_auth: Authed) -> dict:
    return store.get_stats()
