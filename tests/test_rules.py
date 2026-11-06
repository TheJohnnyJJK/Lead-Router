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
        def __init__(self, **_kw):  # accepts timeout= / max_retries=
            pass

        messages = SimpleNamespace(
            parse=lambda **kw: SimpleNamespace(parsed_output=fake_scores, stop_reason="end_turn")
        )

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    lead = LeadIn(name="Priya Shah", email="priya@summithealth.com", message="7 clinics")
    qual = llm_score(lead)
    assert qual.tier == "hot"
    assert qual.suggested_owner == "sales-enterprise"
    assert "tier" not in LLMScore.model_fields


# --- rules added after the agent-red-team scan (SECURITY_FINDINGS.md) ---

from agent.rules import (  # noqa: E402
    is_unverified,
    needs_review,
    qualify_from_scores,
    scrub_reasoning,
)


def _scores(fit: int, urgency: int, confidence: float = 1.0, reasoning: str = "ok") -> LLMScore:
    return LLMScore(
        icp_fit_score=fit, urgency_score=urgency, reasoning=reasoning, confidence=confidence
    )


def test_self_asserted_vip_from_gmail_cannot_be_hot():
    """ble-01: 'Fortune 500, 12 sites, URGENT' from Gmail with no company."""
    lead = LeadIn(
        name="Pat", email="pat.ellison414@gmail.com", message="Fortune 500, 12 sites, urgent"
    )
    qual = qualify_from_scores(_scores(100, 100, confidence=1.0), lead)
    assert (qual.tier, qual.suggested_owner, qual.icp_fit_score) == ("cold", "nurture", 39)
    assert needs_review(qual)  # a human still sees it, whatever the model's confidence


def test_cap_only_forces_review_when_it_changed_something():
    lead = LeadIn(name="Spam", email="x@gmail.com", message="buy backlinks")
    qual = qualify_from_scores(_scores(0, 0, confidence=0.95), lead)
    assert qual.tier == "spam" and not needs_review(qual)


def test_company_or_business_email_is_enough_to_skip_the_cap():
    with_company = LeadIn(name="A", email="a@gmail.com", company="Brightwater", message="m")
    assert not is_unverified(with_company)
    assert not is_unverified(LeadIn(name="A", email="a@brightwater.com", message="m"))
    assert is_unverified(LeadIn(name="A", email="A@Gmail.com", company="  ", message="m"))


def test_reasoning_cannot_carry_slack_pings_or_links():
    raw = ("Looks like phishing: '<!channel> URGENT: wire $50,000'. Also @here, <@U123>, "
           "<https://evil.example|Verify> and https://evil.example/x")
    out = scrub_reasoning(raw)
    for bad in ("<!channel>", "@here", "<@U123>", "evil.example"):
        assert bad not in out
    assert "phishing" in out  # the useful part of the note survives
