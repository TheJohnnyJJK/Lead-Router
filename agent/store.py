"""SQLite system of record.

This stands in for "push to the real CRM" - every qualified lead lands
here with a full audit trail (what was decided, which scorer decided it,
how long it took, when). That audit trail is what scripts/seed_history.py
backdates to simulate 60 days of traffic, and what dashboard/generate_dashboard.py
reads to build the ROI report. Swapping in a real CRM later means adding a
second write inside insert_lead() - this table stays as the audit log either way.

Security note: every query in this file uses `?` placeholders and passes
values as a separate tuple to `conn.execute()` - never Python string
formatting/f-strings to build SQL. That's what makes this immune to SQL
injection even though `tier` and `limit` in get_leads() ultimately come
from a caller-supplied query parameter (agent/main.py) - sqlite3 sends the
query and the values to the database separately, so a value can never be
interpreted as part of the SQL statement itself, no matter what it contains.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager

from .schema import LeadIn, LeadQualification, LeadRecord

logger = logging.getLogger(__name__)

# LEAD_ROUTER_DB lets deployment config point this at a different file (or
# an absolute path) without touching code; the default keeps everything
# self-contained next to the project root for local dev.
_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "lead_router.db")
DB_PATH = os.environ.get("LEAD_ROUTER_DB", _DEFAULT_DB_PATH)

# `IF NOT EXISTS` on every statement makes init_db() safe to call on every
# app startup (see agent/main.py's lifespan) without needing a separate
# "has this already run" check or a migrations framework - there's exactly
# one table, so a full migrations setup would be more machinery than the
# problem needs.
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- sha256 of email + message (see idempotency_key()). UNIQUE is what makes
    -- a repeated submission impossible to store twice, even under a race.
    idempotency_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    company TEXT,
    phone TEXT,
    message TEXT NOT NULL,
    source TEXT NOT NULL,
    tier TEXT NOT NULL,
    icp_fit_score INTEGER NOT NULL,
    urgency_score INTEGER NOT NULL,
    suggested_owner TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    confidence REAL NOT NULL,
    needs_review INTEGER NOT NULL,
    scorer TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    created_at TEXT NOT NULL,
    -- Set when n8n confirms it actually delivered the lead (POST
    -- /leads/{id}/routed). NULL means saved but not yet delivered.
    routed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_tier ON leads(tier);

-- Everything that did NOT end as a clean decision: requests rejected at the
-- door (bad input, bad API key), scoring failures that fell back to the
-- heuristic, crashes, and repeat leads that were deliberately not re-routed.
-- Successful decisions are already recorded in `leads` itself.
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event TEXT NOT NULL,      -- rejected | failed | duplicate
    reason TEXT NOT NULL,     -- e.g. validation, auth, llm_scoring, qualify_error
    detail TEXT NOT NULL,
    lead_key TEXT             -- idempotency_key of the lead, when there is one
);
"""


@contextmanager
def _conn():
    """One connection per call, opened and closed around a single unit of
    work - simple and safe for this project's traffic (a demo/portfolio
    service, not a high-concurrency production system), where a
    connection-pooling layer would be unused complexity.

    Commits on the way out, before the connection closes, so every
    function below gets transactional behavior for free: if the `yield`
    body raises, the `finally` still closes the connection, but the
    missing commit means nothing partial was written.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets query results be indexed by column name, e.g. row["tier"]
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Creates the table (and its indexes) if they don't already exist.
    Called once at app startup - see the `lifespan` context manager in
    agent/main.py - and directly by reset_db() below."""
    with _conn() as conn:
        conn.executescript(SCHEMA)
        # Databases created before routed_at existed get the column added.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
        if "routed_at" not in columns:
            conn.execute("ALTER TABLE leads ADD COLUMN routed_at TEXT")


def idempotency_key(lead: LeadIn) -> str:
    """Same sender + same message = same lead. Lower-cased email and
    stripped whitespace, so a resubmitted form or a retried webhook matches."""
    raw = f"{lead.email.strip().lower()}\n{lead.message.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_duplicate(lead: LeadIn) -> LeadRecord | None:
    """The stored decision for this lead, or None. Marked duplicate=True only
    if it was also delivered (routed_at set); a lead that was saved but never
    routed comes back with duplicate=False so n8n routes it now."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE idempotency_key = ?", (idempotency_key(lead),)
        ).fetchone()
    if row is None:
        return None
    return LeadRecord(**dict(row), duplicate=row["routed_at"] is not None)


def insert_lead(
    lead: LeadIn,
    qual: LeadQualification,
    scorer: str,
    latency_ms: float,
    needs_review: bool,
    created_at: str | None = None,
) -> LeadRecord:
    """Writes one qualified lead and returns it back as a LeadRecord,
    now carrying the database-assigned id and the resolved timestamp.

    `created_at` is normally left as None here too - the graph only passes
    an explicit value when scripts/seed_history.py is backdating history,
    so a live /qualify call always gets "now".
    """
    created_at = created_at or datetime.datetime.utcnow().isoformat()
    try:
        with _conn() as conn:
            # Every `?` below is filled from the tuple that follows, in order -
            # this is the parameterized-query pattern described in the module
            # docstring, and it's what keeps this call injection-proof.
            cur = conn.execute(
                """INSERT INTO leads
                   (idempotency_key, name, email, company, phone, message, source, tier,
                    icp_fit_score, urgency_score, suggested_owner, reasoning, confidence,
                    needs_review, scorer, latency_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    idempotency_key(lead), lead.name, lead.email, lead.company, lead.phone,
                    lead.message, lead.source, qual.tier, qual.icp_fit_score,
                    qual.urgency_score, qual.suggested_owner, qual.reasoning,
                    qual.confidence, needs_review, scorer, latency_ms, created_at,
                ),
            )
            row_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # Two identical leads raced past run_lead()'s duplicate check and this
        # one lost on the UNIQUE key: return the first decision, store nothing.
        existing = find_duplicate(lead)
        if existing is None:
            raise  # some other constraint failed - not a duplicate
        return existing
    # Built by hand from `lead`/`qual` rather than re-reading the row back
    # from the database - we already have every field in memory, so a
    # second round-trip query would just be a slower way to get the same data.
    return LeadRecord(
        id=row_id, name=lead.name, email=lead.email, company=lead.company,
        phone=lead.phone, message=lead.message, source=lead.source,
        tier=qual.tier, icp_fit_score=qual.icp_fit_score, urgency_score=qual.urgency_score,
        suggested_owner=qual.suggested_owner, reasoning=qual.reasoning, confidence=qual.confidence,
        needs_review=needs_review, scorer=scorer, latency_ms=latency_ms, created_at=created_at,
    )


def mark_routed(lead_id: int) -> LeadRecord | None:
    """Records that n8n delivered this lead. Idempotent: the first delivery
    time is kept if n8n reports twice. None if there's no such lead."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE leads SET routed_at = ? WHERE id = ? AND routed_at IS NULL", (now, lead_id)
        )
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return LeadRecord(**dict(row)) if row else None


def log_event(event: str, reason: str, detail: str = "", lead_key: str | None = None) -> None:
    """Appends one row to audit_events. Never raises: if the database itself
    is what's broken, the original error matters more than this record, so a
    failed write is sent to the service log instead."""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO audit_events (created_at, event, reason, detail, lead_key) "
                "VALUES (?,?,?,?,?)",
                (datetime.datetime.utcnow().isoformat(), event, reason, detail[:500], lead_key),
            )
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("could not write audit event %s/%s", event, reason)


def get_events(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_leads(limit: int = 50, tier: str | None = None) -> list[dict]:
    """Most recent leads first, optionally filtered to one tier. Backs
    GET /leads; `limit` is capped to 500 at the API layer (agent/main.py),
    not here - this function will honor whatever it's given, so callers
    are responsible for bounding it."""
    with _conn() as conn:
        if tier:
            rows = conn.execute(
                "SELECT * FROM leads WHERE tier = ? ORDER BY id DESC LIMIT ?", (tier, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    # sqlite3.Row supports dict(row) directly thanks to row_factory above -
    # this is what turns database rows into plain JSON-serializable dicts.
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Every aggregate number the ROI dashboard and GET /stats need, computed
    with SQL (COUNT/GROUP BY) rather than pulled into Python and summed by
    hand - the database is faster at this than iterating rows in Python,
    and it scales the same way regardless of how many leads have piled up."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        by_tier = {r["tier"]: r["n"] for r in conn.execute(
            "SELECT tier, COUNT(*) AS n FROM leads GROUP BY tier"
        )}
        by_scorer = {r["scorer"]: r["n"] for r in conn.execute(
            "SELECT scorer, COUNT(*) AS n FROM leads GROUP BY scorer"
        )}
        avg_latency = conn.execute(
            "SELECT scorer, AVG(latency_ms) AS avg_ms FROM leads GROUP BY scorer"
        ).fetchall()
        # substr(created_at, 1, 10) chops an ISO timestamp down to just the
        # "YYYY-MM-DD" date part, so this groups by calendar day regardless
        # of what time within the day each lead arrived.
        daily = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
            "FROM leads GROUP BY day ORDER BY day"
        ).fetchall()
    return {
        "total": total,
        "by_tier": by_tier,
        "by_scorer": by_scorer,
        "avg_latency_ms": {r["scorer"]: r["avg_ms"] for r in avg_latency},
        "daily": [{"day": r["day"], "n": r["n"]} for r in daily],
    }


def reset_db() -> None:
    """Deletes the database file outright and recreates an empty table.
    Used by tests (each gets its own temp-file database, but this still
    guarantees a clean slate) and by scripts/seed_history.py's default
    (no `--keep` flag) mode, which wants a blank history to backfill from scratch."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
