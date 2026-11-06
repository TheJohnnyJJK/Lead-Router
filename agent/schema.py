"""Data contracts for the lead-routing agent.

Every field the LLM (or the heuristic fallback) is allowed to produce is
declared here, once, and enforced by Pydantic on the way out of the model
call. This is what `client.messages.parse(..., output_format=LeadQualification)`
validates against in agent/scoring.py.

Field bounds below (max_length, ge/le) aren't just data hygiene - POST
/qualify is an unauthenticated endpoint (see agent/main.py), so an attacker
could otherwise submit an arbitrarily large `message` and force every
LLM-scored request to burn far more tokens (and money) than a real lead
ever would. Capping input size is the cheap, boring fix for that.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# Literal types double as validation and documentation: FastAPI rejects any
# request whose `source` isn't one of these four strings with a 422, and
# nobody has to go hunting through the code to find the allowed values.
Source = Literal["web_form", "referral", "cold_email", "content_download"]
Tier = Literal["hot", "warm", "cold", "spam"]
Owner = Literal["sales-enterprise", "sales-smb", "nurture", "discard"]
# "llm" is Claude, "local" is the Ollama model, "heuristic" is the keyword checklist.
Scorer = Literal["llm", "local", "heuristic"]

# Shared length caps, used on both the inbound lead and the LLM's own
# output, so the same rule protects "what a caller sends us" and "what we
# accept back from the model" (a model can misbehave just as easily as a
# webhook payload can).
_NAME_MAX = 200
_COMPANY_MAX = 300
_PHONE_MAX = 50
# generous for a real inbound lead; not generous enough to be a cost-DoS vector
_MESSAGE_MAX = 5_000
_REASONING_MAX = 1_000


class LeadIn(BaseModel):
    """What arrives at POST /qualify - the shape n8n's webhook forwards.

    Every field is bounded on purpose: this model is the API's outermost
    input boundary, so it's the one place that has to assume the caller
    (or whatever's upstream of the caller) is not trustworthy by default.
    """

    # min_length=1 rejects "" as well as a missing field - Pydantic alone
    # would accept an empty string here since it's still valid `str`.
    name: str = Field(min_length=1, max_length=_NAME_MAX)
    # EmailStr (via the `pydantic[email]` extra) checks real email shape -
    # a plain `str` would silently accept "not an email" and pass it
    # straight through to the LLM prompt and the database.
    email: EmailStr
    company: str | None = Field(default=None, max_length=_COMPANY_MAX)
    phone: str | None = Field(default=None, max_length=_PHONE_MAX)
    message: str = Field(min_length=1, max_length=_MESSAGE_MAX)
    source: Source = "web_form"


class LLMScore(BaseModel):
    """The structured-output schema handed to Claude - the only fields the
    model is allowed to return. There is deliberately no tier or owner here:
    agent/rules.py decides those from the two scores. Pydantic validates the
    model's JSON against these bounds before anything else sees it."""

    icp_fit_score: int = Field(
        ge=0, le=100, description="How well the lead matches the ICP, 0-100"
    )
    urgency_score: int = Field(
        ge=0, le=100, description="How time-sensitive the need appears, 0-100"
    )
    reasoning: str = Field(
        max_length=_REASONING_MAX,
        description="One or two sentences a human rep can sanity-check in five seconds",
    )
    confidence: float = Field(ge=0, le=1, description="How sure you are of both scores, 0-1")


class LeadQualification(BaseModel):
    """What the agent decides about a lead, whichever scorer produced it.
    On the Claude path this is built by agent/rules.py from an LLMScore; the
    heuristic fallback and the spam prefilter build it directly."""

    tier: Tier
    icp_fit_score: int = Field(
        ge=0, le=100, description="How well the lead matches the ICP, 0-100"
    )
    urgency_score: int = Field(
        ge=0, le=100, description="How time-sensitive the need appears, 0-100"
    )
    suggested_owner: Owner
    reasoning: str = Field(
        max_length=_REASONING_MAX,
        description="One or two sentences a human rep can sanity-check in five seconds",
    )
    confidence: float = Field(ge=0, le=1)
    # Set by agent/rules.py, never by a model (LLMScore has no such field):
    # true when a rule decided a human must look regardless of confidence.
    forced_review: bool = False


class LeadRecord(BaseModel):
    """A qualified lead as stored and returned - LeadIn + LeadQualification
    plus the operational metadata (which scorer ran, how long it took).

    This is a separate model from LeadIn/LeadQualification on purpose,
    even though it repeats their fields: it represents a *stored* row (it
    has an `id` and a `created_at` neither input model has), and keeping
    it distinct means a future change to the input contract can't silently
    change what's already sitting in the database.
    """

    id: int
    name: str
    email: str
    company: str | None = None
    phone: str | None = None
    message: str
    source: Source
    tier: Tier
    icp_fit_score: int
    urgency_score: int
    suggested_owner: Owner
    reasoning: str
    confidence: float
    # True when confidence was under business.yaml's review cutoff - n8n
    # sends these to the review channel instead of routing by tier.
    needs_review: bool
    scorer: Scorer
    latency_ms: float
    created_at: str
    # When n8n confirmed delivery (POST /leads/{id}/routed). None = not yet.
    routed_at: str | None = None
    # Not stored - set on the response when this lead was already qualified
    # (same email + message), so n8n knows not to route it a second time.
    duplicate: bool = False
