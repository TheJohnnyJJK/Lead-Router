"""FastAPI TestClient hitting the real routes. Runs offline (no
ANTHROPIC_API_KEY in the test environment -> heuristic scorer path)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_lead_router.db"
    monkeypatch.setenv("LEAD_ROUTER_DB", str(db_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)  # auth off by default in tests

    from agent import store
    store.DB_PATH = str(db_path)  # module-level constant read at import time

    from agent.main import app
    with TestClient(app) as test_client:  # context manager triggers lifespan startup
        yield test_client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_qualify_spam(client):
    resp = client.post("/qualify", json={
        "name": "Test User", "email": "test@test.com", "message": "asdf", "source": "web_form",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "spam"
    assert body["scorer"] == "heuristic"
    assert "id" in body


def test_qualify_hot_lead_then_appears_in_stats_and_leads(client):
    payload = {
        "name": "Priya Shah", "email": "priya.shah@summithealth.com",
        "company": "Summit Health Network (7 clinics)",
        "message": "We manage 7 clinics and need a new vendor asap, contract is up next month.",
        "source": "web_form",
    }
    resp = client.post("/qualify", json=payload)
    assert resp.status_code == 200
    assert resp.json()["tier"] == "hot"

    leads = client.get("/leads", params={"tier": "hot"}).json()
    assert len(leads) == 1
    assert leads[0]["email"] == "priya.shah@summithealth.com"

    stats = client.get("/stats").json()
    assert stats["total"] == 1
    assert stats["by_tier"]["hot"] == 1


def test_low_confidence_lead_is_flagged_for_review(client):
    # The heuristic scores this hot at confidence 0.55, under business.yaml's
    # 0.6 cutoff - so it's flagged for a human instead of auto-routed.
    resp = client.post("/qualify", json={
        "name": "Priya Shah", "email": "priya.shah@summithealth.com",
        "company": "Summit Health Network", "message": "7 clinics, need a vendor asap",
    })
    assert resp.json()["tier"] == "hot"
    assert resp.json()["needs_review"] is True


def test_prefiltered_spam_is_not_flagged_for_review(client):
    resp = client.post("/qualify", json={"name": "A", "email": "a@b.com", "message": "asdf"})
    assert resp.json()["needs_review"] is False


def test_repeated_lead_returns_stored_decision_without_a_second_row(client):
    lead = {"name": "Owen", "email": "owen@thornburylaw.com", "company": "Thornbury Law",
            "message": "Considering a switch for our office."}
    first = client.post("/qualify", json=lead).json()
    # Same sender and message, different case/whitespace: still the same lead.
    again = client.post("/qualify", json={
        **lead, "email": "Owen@ThornburyLaw.com",
        "message": "  Considering a switch for our office. ",
    }).json()
    assert first["duplicate"] is False
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    assert client.get("/stats").json()["total"] == 1


def test_unique_key_holds_even_if_the_duplicate_check_is_skipped(client):
    # Simulates two identical requests racing past run_lead()'s lookup: the
    # second INSERT hits the UNIQUE constraint and gets the first row back.
    from agent import store
    from agent.schema import LeadIn
    from agent.scoring import heuristic_score

    lead = LeadIn(name="A", email="a@corp.com", company="Corp", message="Office cleaning?")
    qual = heuristic_score(lead)
    first = store.insert_lead(lead, qual, "heuristic", 1.0, needs_review=False)
    second = store.insert_lead(lead, qual, "heuristic", 1.0, needs_review=False)
    assert second.duplicate is True and second.id == first.id
    assert store.get_stats()["total"] == 1


def test_qualify_rejects_missing_required_field(client):
    resp = client.post("/qualify", json={"name": "No Email"})
    assert resp.status_code == 422


def test_qualify_rejects_invalid_email_shape(client):
    resp = client.post("/qualify", json={
        "name": "Bad Email", "email": "not-an-email", "message": "hello", "source": "web_form",
    })
    assert resp.status_code == 422


def test_qualify_rejects_truly_empty_message(client):
    # A genuinely empty string is rejected at the API boundary (min_length=1
    # on LeadIn.message). A whitespace-only message is a different case,
    # deliberately allowed through validation and caught instead by the
    # graph's own prefilter logic - see data/golden_eval_set.json edge-05.
    resp = client.post("/qualify", json={
        "name": "Empty Message", "email": "a@b.com", "message": "", "source": "web_form",
    })
    assert resp.status_code == 422


def test_qualify_rejects_oversized_message(client):
    resp = client.post("/qualify", json={
        "name": "Too Long", "email": "a@b.com", "message": "x" * 5_001, "source": "web_form",
    })
    assert resp.status_code == 422


def test_leads_rejects_limit_above_cap(client):
    resp = client.get("/leads", params={"limit": 5000})
    assert resp.status_code == 422


def test_auth_blocks_requests_without_the_key_once_enabled(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-secret-123")
    resp = client.post("/qualify", json={
        "name": "Test User", "email": "test@test.com", "message": "asdf", "source": "web_form",
    })
    assert resp.status_code == 401


def test_auth_accepts_requests_with_the_correct_key(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-secret-123")
    resp = client.post(
        "/qualify",
        json={
            "name": "Test User", "email": "test@test.com",
            "message": "asdf", "source": "web_form",
        },
        headers={"X-API-Key": "test-secret-123"},
    )
    assert resp.status_code == 200


def test_rejected_input_is_recorded_without_its_values(client):
    client.post("/qualify", json={"name": "X", "email": "not-an-email", "message": "secret text"})
    [event] = client.get("/audit").json()
    assert (event["event"], event["reason"]) == ("rejected", "validation")
    assert "email" in event["detail"]
    assert "secret text" not in event["detail"] and "not-an-email" not in event["detail"]


def test_bad_api_key_is_recorded(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-secret-123")
    client.get("/stats", headers={"X-API-Key": "wrong"})
    events = client.get("/audit", headers={"X-API-Key": "test-secret-123"}).json()
    assert [(e["event"], e["reason"]) for e in events] == [("rejected", "auth")]


def test_llm_failure_is_recorded_and_lead_still_routed(client, monkeypatch):
    import anthropic

    from agent import scoring

    def broken(_lead):
        raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(scoring, "llm_score", broken)
    body = client.post("/qualify", json={
        "name": "B", "email": "b@corp.com", "company": "Corp", "message": "Office cleaning?",
    }).json()
    assert body["scorer"] == "heuristic"
    [event] = client.get("/audit").json()
    assert (event["event"], event["reason"]) == ("failed", "llm_scoring")
    assert "network error" in event["detail"]


def test_repeat_lead_is_recorded(client):
    lead = {"name": "C", "email": "c@corp.com", "message": "Office cleaning?"}
    client.post("/qualify", json=lead)
    client.post("/qualify", json=lead)
    [event] = client.get("/audit").json()
    assert (event["event"], event["reason"]) == ("duplicate", "repeat_lead")


def test_crash_during_qualify_is_recorded(client, monkeypatch):
    from agent import main

    def crash(_lead):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main, "run_lead", crash)
    with pytest.raises(RuntimeError):  # TestClient re-raises server errors
        client.post("/qualify", json={"name": "D", "email": "d@corp.com", "message": "hi"})
    [event] = client.get("/audit").json()
    assert (event["event"], event["reason"]) == ("failed", "qualify_error")
    assert "database is locked" in event["detail"]
