"""The deterministic half of LLM scoring: the model proposes scores, this decides.

Claude (or the local model) returns an LLMScore (fit, urgency, reasoning,
confidence) and has no field for the tier or the owner, so it can't pick
them. The thresholds live in business.yaml's `tier_rules`; changing who
counts as "hot" is a config edit you can review, not a prompt tweak you
have to hope the model follows.

Two more rules here came out of the agent-red-team scan (see
SECURITY_FINDINGS.md): a fit cap for leads with nothing verifiable in them,
and scrubbing Slack pings and links out of the model's reasoning.
"""
from __future__ import annotations

import re

from .icp import (
    FREE_EMAIL_DOMAINS,
    HOT_MIN_FIT,
    HOT_MIN_URGENCY,
    REVIEW_MIN_CONFIDENCE,
    SPAM_BELOW_FIT,
    UNVERIFIED_MAX_FIT,
    WARM_MIN_FIT,
)
from .schema import LeadIn, LeadQualification, LLMScore, Owner, Tier

OWNER_BY_TIER: dict[Tier, Owner] = {
    "hot": "sales-enterprise",
    "warm": "sales-smb",
    "cold": "nurture",
    "spam": "discard",
}

# Slack control syntax: <!channel>, <!here>, <@U123> user pings, <#C123>
# channel links, and <https://...|label> links.
_SLACK_ANGLE = re.compile(r"<[!@#][^>]*>|<(?:https?|mailto):[^>]*>", re.IGNORECASE)
# Plain-text broadcast words some Slack setups still turn into pings.
_BROADCAST = re.compile(r"[@!](?:here|channel|everyone)\b", re.IGNORECASE)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)


def decide_tier(icp_fit_score: int, urgency_score: int) -> Tier:
    """Checked top to bottom - the first rule that matches wins."""
    if icp_fit_score < SPAM_BELOW_FIT:
        return "spam"
    if icp_fit_score >= HOT_MIN_FIT and urgency_score >= HOT_MIN_URGENCY:
        return "hot"
    if icp_fit_score >= WARM_MIN_FIT:
        return "warm"
    return "cold"


def needs_review(qual: LeadQualification) -> bool:
    """The confidence gate: below the cutoff, a human looks before anything is
    routed. Applies to every scorer and every tier, so a shaky "spam" call
    isn't silently discarded either. A rule can also force review outright
    (forced_review), because a model's confidence can be talked up."""
    return qual.forced_review or qual.confidence < REVIEW_MIN_CONFIDENCE


def is_unverified(lead: LeadIn) -> bool:
    """No company name and a free email domain: nothing in the lead that
    backs up whatever it claims about itself."""
    domain = lead.email.rsplit("@", 1)[-1].lower()
    return not (lead.company or "").strip() and domain in FREE_EMAIL_DOMAINS


def scrub_reasoning(text: str) -> str:
    """The reasoning is posted into Slack, and a model will sometimes quote
    the lead word for word - even while explaining that it's an attack. A
    sales note never needs a ping or a link, so both are removed here.
    n8n escapes the message again before posting (defense in depth)."""
    text = _SLACK_ANGLE.sub("[removed]", text)
    text = _BROADCAST.sub("[removed]", text)
    return _URL.sub("[link removed]", text)


def qualify_from_scores(scores: LLMScore, lead: LeadIn | None = None) -> LeadQualification:
    fit = scores.icp_fit_score
    reasoning = scrub_reasoning(scores.reasoning)
    forced_review = False
    if lead is not None and is_unverified(lead) and fit > UNVERIFIED_MAX_FIT:
        fit = UNVERIFIED_MAX_FIT
        forced_review = True
        note = (f"[Rule] No company name and a free email address: fit capped at "
                f"{UNVERIFIED_MAX_FIT} and sent to review. ")
        reasoning = (note + reasoning)[:1000]
    tier = decide_tier(fit, scores.urgency_score)
    return LeadQualification(
        tier=tier,
        icp_fit_score=fit,
        urgency_score=scores.urgency_score,
        suggested_owner=OWNER_BY_TIER[tier],
        reasoning=reasoning,
        confidence=scores.confidence,
        forced_review=forced_review,
    )
