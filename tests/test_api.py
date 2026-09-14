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
