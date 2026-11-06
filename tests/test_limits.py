"""The guards added after the 2026-11-06 attack (SECURITY_FINDINGS.md):
size limit, rate limit, the non-ASCII key crash, docs off by default, and
the scoring time budget."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import scoring, store
from agent.limits import BodySizeLimit, RateLimit
from agent.schema import LeadIn


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "limits.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    from agent.main import app
    with TestClient(app) as c:
        yield c


def test_oversized_body_is_refused_before_parsing(client):
    lead = {"name": "Big", "email": "big@corp.com", "message": "hi", "padding": "x" * 200_000}
    resp = client.post("/qualify", json=lead)
    assert resp.status_code == 413
    assert client.get("/stats").json()["total"] == 0


def test_non_ascii_api_key_is_a_401_not_a_crash(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "real-key")
    resp = client.get("/leads", headers={"X-API-Key": "caf\xe9".encode("latin-1")})
    assert resp.status_code == 401


def test_api_docs_are_off_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def _tiny_app(per_minute: int) -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    app.add_middleware(BodySizeLimit)
    app.add_middleware(RateLimit, per_minute=per_minute)
    return TestClient(app)


def test_rate_limit_refuses_and_logs_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "rl.db"))
    store.init_db()
    c = _tiny_app(per_minute=3)
    codes = [c.get("/ping").status_code for _ in range(10)]
    assert codes[:3] == [200, 200, 200] and set(codes[3:]) == {429}
    assert "retry-after" in c.get("/ping").headers
    rows = [e for e in store.get_events() if e["reason"] == "rate_limited"]
    assert len(rows) == 1  # a flood doesn't become a flood of audit rows
    assert c.get("/health").status_code == 200  # health checks are never limited


def test_claude_is_skipped_when_the_time_budget_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "budget.db"))
    store.init_db()
    monkeypatch.setenv("SCORER_MODE", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(scoring, "SCORING_BUDGET_S", 0.0)
    called = []
    monkeypatch.setattr(scoring, "llm_score", lambda *a, **k: called.append(1))
    lead = LeadIn(name="T", email="t@corp.com", company="Corp", message="office cleaning")
    _, scorer, _ = scoring.score_lead(lead)
    assert scorer == "heuristic" and called == []
    assert "skipped Claude" in store.get_events()[0]["detail"]


def test_claude_gets_only_the_time_that_is_left(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "budget2.db"))
    store.init_db()
    monkeypatch.setenv("SCORER_MODE", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(scoring, "SCORING_BUDGET_S", 5.0)
    seen = {}

    def fake(_lead, timeout=None):
        seen["timeout"] = timeout
        raise RuntimeError("stop here")

    monkeypatch.setattr(scoring, "llm_score", fake)
    scoring.score_lead(LeadIn(name="T", email="t@corp.com", message="office cleaning"))
    assert 0 < seen["timeout"] <= 5.0


def test_a_slow_model_cannot_hold_the_request_past_the_budget(tmp_path, monkeypatch):
    """A model call stuck past the budget is abandoned, not waited for."""
    import time

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "budget3.db"))
    store.init_db()
    monkeypatch.setenv("SCORER_MODE", "local")
    monkeypatch.setattr(scoring, "SCORING_BUDGET_S", 0.5)
    monkeypatch.setattr(scoring, "local_score", lambda *a, **k: time.sleep(3))
    start = time.perf_counter()
    _, scorer, _ = scoring.score_lead(LeadIn(name="T", email="t@corp.com", message="office"))
    assert scorer == "heuristic"
    assert time.perf_counter() - start < 1.5
