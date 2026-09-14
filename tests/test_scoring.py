"""Offline, deterministic - exercises the heuristic scorer only, so this
suite never needs an API key and never makes a network call."""
from agent.schema import LeadIn
from agent.scoring import heuristic_score


def test_obvious_spam_is_discarded():
    lead = LeadIn(name="A B", email="a@gmail.com", message="asdf", source="web_form")
    qual = heuristic_score(lead)
    assert qual.tier == "spam"
    assert qual.suggested_owner == "discard"


def test_vendor_pitch_with_company_name_is_still_spam():
    lead = LeadIn(
        name="Chris Media", email="chris@growthmarketingsolutions.com",
        company="Growth Marketing Solutions",
        message="We can boost your SEO ranking, reply for a free audit.",
        source="cold_email",
    )
    qual = heuristic_score(lead)
    assert qual.tier == "spam"


def test_multi_site_urgent_company_scores_hot():
    lead = LeadIn(
        name="Priya Shah", email="priya.shah@summithealth.com",
        company="Summit Health Network (7 clinics)",
        message="We manage 7 clinics and need a new vendor asap, contract is up next month.",
        source="web_form",
    )
    qual = heuristic_score(lead)
    assert qual.tier == "hot"
    assert qual.suggested_owner == "sales-enterprise"


def test_residential_without_company_is_cold_not_discarded():
    lead = LeadIn(
        name="Aisha Bello", email="aisha.bello@yahoo.com",
        message="Hi, I need my house cleaned before guests arrive, do you do residential?",
        source="web_form",
    )
    qual = heuristic_score(lead)
    assert qual.tier == "cold"
    assert qual.suggested_owner == "nurture"  # a real person, not spam


def test_negated_urgency_is_a_known_heuristic_weakness():
    """Documents the false positive the eval harness catches: naive substring
    matching on 'urgent' fires even when the message negates it. This is why
    the LLM scorer exists - this test pins the current (imperfect) behavior
    rather than silently hiding it."""
    lead = LeadIn(
        name="Owen Larsen", email="owen.larsen@thornburylaw.com",
        company="Thornbury Law Offices",
        message="Considering a switch for our office, nothing urgent though.",
        source="web_form",
    )
    qual = heuristic_score(lead)
    # known-wrong: should be "warm" - see data/golden_eval_set.json warm-02
    assert qual.tier == "hot"
