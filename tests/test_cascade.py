"""The local -> Claude cascade in score_lead(). Offline: both models are
replaced with fakes, so these tests only check which one gets asked and
which answer wins."""
import pytest

from agent import scoring, store
from agent.schema import LeadIn, LeadQualification

LEAD = LeadIn(name="Dana", email="dana@brightwater.com", company="Brightwater", message="4 clinics")


def _qual(tier: str, confidence: float) -> LeadQualification:
    owner = {"hot": "sales-enterprise", "warm": "sales-smb", "cold": "nurture", "spam": "discard"}
    return LeadQualification(
        tier=tier, icp_fit_score=50, urgency_score=50, suggested_owner=owner[tier],
        reasoning="fake", confidence=confidence,
    )


@pytest.fixture()
def calls(tmp_path, monkeypatch):
    """Records which fake model was called. Each test sets what they return."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "cascade.db"))
    store.init_db()
    monkeypatch.setenv("SCORER_MODE", "cascade")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen: list[str] = []
    answers: dict = {"local": _qual("warm", 0.9), "claude": _qual("cold", 0.95)}

    def fake(name):
        def run(_lead, **_kw):
            seen.append(name)
            answer = answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return run

    monkeypatch.setattr(scoring, "local_score", fake("local"))
    monkeypatch.setattr(scoring, "llm_score", fake("claude"))
    return seen, answers


def test_confident_local_answer_skips_claude(calls):
    seen, _ = calls
    qual, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, qual.tier, seen) == ("local", "warm", ["local"])


def test_low_confidence_local_answer_goes_to_claude(calls):
    seen, answers = calls
    answers["local"] = _qual("warm", 0.5)
    qual, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, qual.tier, seen) == ("llm", "cold", ["local", "claude"])


def test_hot_lead_is_always_double_checked(calls):
    seen, answers = calls
    answers["local"] = _qual("hot", 0.99)
    _, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, seen) == ("llm", ["local", "claude"])


def test_local_model_down_goes_to_claude_and_is_logged(calls):
    seen, answers = calls
    answers["local"] = ConnectionError("ollama not running")
    _, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, seen) == ("llm", ["local", "claude"])
    assert store.get_events()[0]["reason"] == "local_scoring"


def test_claude_failing_keeps_the_local_answer(calls):
    _, answers = calls
    answers["local"] = _qual("warm", 0.5)
    answers["claude"] = RuntimeError("boom")
    qual, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, qual.tier) == ("local", "warm")


def test_local_mode_never_calls_claude(calls, monkeypatch):
    seen, answers = calls
    monkeypatch.setenv("SCORER_MODE", "local")
    answers["local"] = _qual("hot", 0.3)
    _, scorer, _ = scoring.score_lead(LEAD)
    assert (scorer, seen) == ("local", ["local"])


def test_everything_down_falls_back_to_heuristic(calls, monkeypatch):
    _, answers = calls
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    answers["local"] = ConnectionError("ollama not running")
    _, scorer, _ = scoring.score_lead(LEAD)
    assert scorer == "heuristic"
