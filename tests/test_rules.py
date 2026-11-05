"""The tier on the Claude path is decided by agent/rules.py, not the model.
Offline: the one test that touches llm_score() swaps in a fake client."""
from types import SimpleNamespace

import anthropic
import pytest

from agent import icp
from agent.rules import decide_tier
from agent.schema import LeadIn, LLMScore
from agent.scoring import llm_score


@pytest.mark.parametrize(
    ("fit", "urgency", "expected"),
    [
        (icp.SPAM_BELOW_FIT - 1, 100, "spam"),  # urgency can't rescue a non-lead
        (icp.HOT_MIN_FIT, icp.HOT_MIN_URGENCY, "hot"),  # both thresholds are inclusive
        (icp.HOT_MIN_FIT, icp.HOT_MIN_URGENCY - 1, "warm"),  # big account, no rush
        (icp.HOT_MIN_FIT - 1, 100, "warm"),  # urgent, but a small account
        (icp.WARM_MIN_FIT, 0, "warm"),
        (icp.WARM_MIN_FIT - 1, 100, "cold"),
        (icp.SPAM_BELOW_FIT, 0, "cold"),  # a real person outside the ICP
    ],
)
def test_decide_tier_boundaries(fit, urgency, expected):
    assert decide_tier(fit, urgency) == expected


def test_llm_path_takes_tier_from_rules_not_model(monkeypatch):
    """Claude's schema has no tier field. Whatever scores it returns, the
    tier and owner come out of decide_tier()."""
    fake_scores = LLMScore(
        icp_fit_score=85, urgency_score=90, reasoning="7 clinics, contract expiring.",
        confidence=0.9,
    )

    class FakeClient:
        messages = SimpleNamespace(
            parse=lambda **kw: SimpleNamespace(parsed_output=fake_scores, stop_reason="end_turn")
        )

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    lead = LeadIn(name="Priya Shah", email="priya@summithealth.com", message="7 clinics")
    qual = llm_score(lead)
    assert qual.tier == "hot"
    assert qual.suggested_owner == "sales-enterprise"
    assert "tier" not in LLMScore.model_fields
