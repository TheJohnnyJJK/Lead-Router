"""The deterministic half of LLM scoring: Claude proposes scores, this decides.

Claude returns an LLMScore (fit, urgency, reasoning, confidence) and has no
field for the tier or the owner, so it can't pick them. The thresholds live
in business.yaml's `tier_rules`; changing who counts as "hot" is a config
edit you can review, not a prompt tweak you have to hope the model follows.
"""
from __future__ import annotations

from .icp import (
    HOT_MIN_FIT,
    HOT_MIN_URGENCY,
    REVIEW_MIN_CONFIDENCE,
    SPAM_BELOW_FIT,
    WARM_MIN_FIT,
)
from .schema import LeadQualification, LLMScore, Owner, Tier

OWNER_BY_TIER: dict[Tier, Owner] = {
    "hot": "sales-enterprise",
    "warm": "sales-smb",
    "cold": "nurture",
    "spam": "discard",
}


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
    isn't silently discarded either."""
    return qual.confidence < REVIEW_MIN_CONFIDENCE


def qualify_from_scores(scores: LLMScore) -> LeadQualification:
    tier = decide_tier(scores.icp_fit_score, scores.urgency_score)
    return LeadQualification(
        tier=tier,
        icp_fit_score=scores.icp_fit_score,
        urgency_score=scores.urgency_score,
        suggested_owner=OWNER_BY_TIER[tier],
        reasoning=scores.reasoning,
        confidence=scores.confidence,
    )
