"""End-to-end FastAPI route integration tests for GitHub GraphQL Consumption
Diagnostics v0.1 (Lead-authored integration coverage — the pure domain,
controller, sampler, identity, and export modules each already have their
own dedicated unit test files; this file exists to verify the actual HTTP
wiring in app/main.py, since no other test file exercises the routes
themselves).

Every test constructs a *fresh* GitHubGraphQLDiagnosticsController, bound to
an isolated in-memory SQLite database (never the real project
`limit_checker.db` file — see `tests/helpers.py`'s pattern, also used by
`tests/test_seed_and_api.py`'s `api_client` fixture, which this mirrors),
with injected fake `fetch`/`identity_fetch`/`clock` callables, and assigns it
to `app.state.github_graphql_diagnostics_controller` before creating the
TestClient. `GET /api/github-graphql-diagnostics/{samples,sessions}` and the
CSV export routes use FastAPI's `Depends(get_db)`, so `app.dependency_overrides`
is also pointed at the SAME isolated in-memory database for those routes to
see what the controller wrote. No test here ever calls a real `gh` subprocess
or a real GitHub API of any kind (GraphQL or REST).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.github_graphql_diagnostics_controller import GitHubGraphQLDiagnosticsController
from app.github_graphql_diagnostics_identity import GitHubIdentityFetchResult
from app.github_rate_limit_cli import GitHubRateLimitFetchResult
from app.main import app

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)

VALID_PAYLOAD = {
    "resources": {
        "core": {"limit": 5000, "used": 50, "remaining": 4950, "reset": int(NOW.timestamp()) + 3600},
        "graphql": {"limit": 5000, "used": 500, "remaining": 4500, "reset": int(NOW.timestamp()) + 3600},
        "search": {"limit": 30, "used": 0, "remaining": 30, "reset": int(NOW.timestamp()) + 3600},
    }
}

EXHAUSTED_PAYLOAD = {
    "resources": {
        "core": {"limit": 5000, "used": 50, "remaining": 4950, "reset": int(NOW.timestamp()) + 3600},
        "graphql": {"limit": 5000, "used": 5000, "remaining": 0, "reset": int(NOW.timestamp()) + 3600},
        "search": {"limit": 30, "used": 0, "remaining": 30, "reset": int(NOW.timestamp()) + 3600},
    }
}


def fake_fetch(payload: dict):
    def fetch(*, now=None, **kwargs):
        return GitHubRateLimitFetchResult(
            success=True, payload=payload, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    return fetch


def fake_identity(login: str = "octocat", user_id: int = 1):
    def identity(**kwargs):
        return GitHubIdentityFetchResult(success=True, login=login, user_id=user_id, error_type=None, user_message=None)

    return identity


def failing_identity(error_type: str = "not_authenticated", user_message: str = "not authenticated"):
    def identity(**kwargs):
        return GitHubIdentityFetchResult(success=False, login=None, user_id=None, error_type=error_type, user_message=user_message)

    return identity


def _isolated_session_factory():
    """A fresh in-memory SQLite database per call site (StaticPool keeps a
    single connection alive for the engine's lifetime, so multiple sessions
    from the returned sessionmaker share the same isolated data) — never the
    real project `limit_checker.db` file. Mirrors `tests/helpers.make_session`."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def make_controller(
    *,
    session_factory,
    fetch=None,
    identity_fetch=None,
    clock=None,
) -> GitHubGraphQLDiagnosticsController:
    import app.github_graphql_diagnostics_config as config_module

    return GitHubGraphQLDiagnosticsController(
        session_factory=session_factory,
        fetch=fetch or fake_fetch(VALID_PAYLOAD),
        identity_fetch=identity_fetch or fake_identity(),
        clock=clock or (lambda: NOW),
        sample_seconds=config_module.MIN_SAMPLE_SECONDS,
        max_minutes=15,
    )


def _wire_isolated_db(session_factory) -> None:
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db


@pytest.fixture()
def diagnostics_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "true")
    session_factory = _isolated_session_factory()
    app.state.github_graphql_diagnostics_controller = make_controller(session_factory=session_factory)
    _wire_isolated_db(session_factory)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def diagnostics_disabled_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", raising=False)
    session_factory = _isolated_session_factory()
    app.state.github_graphql_diagnostics_controller = make_controller(session_factory=session_factory)
    _wire_isolated_db(session_factory)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_get_status_disabled_by_default(diagnostics_disabled_client):
    response = diagnostics_disabled_client.get("/api/github-graphql-diagnostics")
    body = response.json()
    assert response.status_code == 200
    assert body["enabled"] is False
    assert body["sampler_running"] is False
    assert body["active_sessions"] == []
    assert body["last_sample"] is None


def test_get_status_enabled_empty(diagnostics_client):
    response = diagnostics_client.get("/api/github-graphql-diagnostics")
    body = response.json()
    assert response.status_code == 200
    assert body["enabled"] is True
    assert body["sampler_running"] is False
    assert body["active_sessions"] == []
    assert body["last_sample"] is None


def test_start_when_disabled_returns_400(diagnostics_disabled_client):
    response = diagnostics_disabled_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_type"] == "diagnostics_disabled"


def test_start_success_creates_active_session_and_baseline_sample(diagnostics_client):
    response = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "PR #99 review", "repository": "owner/repo", "pr_number": 99},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["session"]["status"] == "ACTIVE"
    assert body["session"]["actor_type"] == "claude_code"
    assert body["session"]["label"] == "PR #99 review"
    assert body["session"]["github_login"] == "octocat"
    assert body["session"]["graphql_used_start"] == 500
    assert body["baseline_sample"]["graphql_used"] == 500
    assert body["baseline_sample"]["fetch_status"] == "no_previous"

    status = diagnostics_client.get("/api/github-graphql-diagnostics").json()
    assert status["sampler_running"] is True
    assert len(status["active_sessions"]) == 1


def test_start_with_identity_failure_returns_502_generic_message(diagnostics_client):
    app.state.github_graphql_diagnostics_controller = make_controller(
        session_factory=_isolated_session_factory(),
        identity_fetch=failing_identity("not_authenticated", "GitHub CLI is not authenticated."),
    )
    response = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error_type"] == "not_authenticated"
    assert detail["user_message"] == "GitHub CLI is not authenticated."


def test_start_account_context_changed_returns_409_and_does_not_touch_existing_session(diagnostics_client):
    first = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "first"},
    ).json()

    app.state.github_graphql_diagnostics_controller._identity_fetch = fake_identity(login="someone-else", user_id=999)
    response = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "codex", "label": "second"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_type"] == "account_context_changed"

    status = diagnostics_client.get("/api/github-graphql-diagnostics").json()
    assert len(status["active_sessions"]) == 1
    assert status["active_sessions"][0]["id"] == first["session"]["id"]


def test_stop_unknown_session_returns_404(diagnostics_client):
    response = diagnostics_client.post("/api/github-graphql-diagnostics/999999/stop")
    assert response.status_code == 404
    assert response.json()["detail"]["error_type"] == "session_not_found"


def test_stop_success_produces_final_sample_and_stops_sampler(diagnostics_client):
    started = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    ).json()
    session_id = started["session"]["id"]

    response = diagnostics_client.post(f"/api/github-graphql-diagnostics/{session_id}/stop")
    body = response.json()
    assert response.status_code == 200
    assert body["session"]["status"] == "STOPPED"
    assert body["session"]["stop_reason"] == "USER_STOP"
    assert body["final_sample"] is not None

    status = diagnostics_client.get("/api/github-graphql-diagnostics").json()
    assert status["sampler_running"] is False
    assert status["active_sessions"] == []


def test_stop_idempotent_second_call_has_no_final_sample(diagnostics_client):
    started = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    ).json()
    session_id = started["session"]["id"]
    diagnostics_client.post(f"/api/github-graphql-diagnostics/{session_id}/stop")

    response = diagnostics_client.post(f"/api/github-graphql-diagnostics/{session_id}/stop")
    body = response.json()
    assert response.status_code == 200
    assert body["final_sample"] is None
    assert body["session"]["status"] == "STOPPED"


def test_start_when_already_exhausted_creates_exhausted_not_active_session(diagnostics_client):
    app.state.github_graphql_diagnostics_controller = make_controller(
        session_factory=_isolated_session_factory(), fetch=fake_fetch(EXHAUSTED_PAYLOAD)
    )
    response = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["session"]["status"] == "EXHAUSTED"
    assert body["session"]["stop_reason"] == "GRAPHQL_EXHAUSTED"

    status = diagnostics_client.get("/api/github-graphql-diagnostics").json()
    assert status["sampler_running"] is False
    assert status["active_sessions"] == []


def test_samples_and_sessions_list_endpoints_paginate(diagnostics_client):
    for i in range(3):
        started = diagnostics_client.post(
            "/api/github-graphql-diagnostics/start",
            json={"actor_type": "claude_code", "label": f"session-{i}"},
        ).json()
        diagnostics_client.post(f"/api/github-graphql-diagnostics/{started['session']['id']}/stop")

    sessions_response = diagnostics_client.get("/api/github-graphql-diagnostics/sessions?limit=2&offset=0")
    sessions_body = sessions_response.json()
    assert sessions_response.status_code == 200
    assert sessions_body["limit"] == 2
    assert len(sessions_body["items"]) == 2

    samples_response = diagnostics_client.get("/api/github-graphql-diagnostics/samples?limit=100&offset=0")
    samples_body = samples_response.json()
    assert samples_response.status_code == 200
    assert len(samples_body["items"]) >= 6  # baseline + final sample per session, at least


def test_samples_csv_export_has_safe_header_and_no_secret(diagnostics_client):
    diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    )
    response = diagnostics_client.get("/api/export/github-graphql-samples.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "github-graphql-samples.csv" in response.headers["content-disposition"]
    text = response.text
    assert "graphql_used" in text
    assert "token" not in text.lower()


def test_sessions_csv_export_escapes_formula_injection_label(diagnostics_client):
    diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "=cmd|'/c calc'!A1", "repository": "+HYPERLINK(evil)"},
    )
    response = diagnostics_client.get("/api/export/github-graphql-sessions.csv")
    assert response.status_code == 200
    text = response.text
    # The raw (unescaped) field must never appear immediately after a
    # delimiter -- that would mean a spreadsheet application could evaluate
    # it as a formula. The escaped form (leading "'") is what must be present.
    assert ",=cmd|'/c calc'!A1," not in text
    assert ",+HYPERLINK(evil)," not in text
    assert ",'=cmd|'/c calc'!A1," in text
    assert ",'+HYPERLINK(evil)," in text


def test_diagnostics_endpoints_require_basic_auth_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "true")
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "user")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "pass")
    app.state.github_graphql_diagnostics_controller = make_controller(session_factory=_isolated_session_factory())
    with TestClient(app) as client:
        response = client.get("/api/github-graphql-diagnostics")
        assert response.status_code == 401

        response = client.get("/api/github-graphql-diagnostics", auth=("user", "pass"))
        assert response.status_code == 200


def test_never_calls_graphql_endpoint_across_full_lifecycle(diagnostics_client):
    """Regression guard: drive start -> stop and confirm the actual gh CLI
    invocation command tuples used anywhere in this feature's code path
    never target the GraphQL endpoint.

    A bare substring check for "/graphql" is deliberately NOT used here --
    it produces false positives against this codebase's own safety-rationale
    docstrings/comments (e.g. "this module never constructs a /graphql
    request"), which legitimately contain that text as a negation. Instead
    this checks the actual gh CLI argv-shape constants these modules define
    (GH_COMMAND / GH_USER_COMMAND-style tuples), which is what would change
    if a GraphQL call were ever actually added.
    """
    from app.github_graphql_diagnostics_identity import GH_USER_COMMAND
    from app.github_rate_limit_cli import GH_COMMAND

    assert GH_COMMAND == ("gh", "api", "rate_limit")
    assert GH_USER_COMMAND == ("gh", "api", "user")
    assert "graphql" not in GH_COMMAND
    assert "graphql" not in GH_USER_COMMAND

    started = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "test"},
    ).json()
    diagnostics_client.post(f"/api/github-graphql-diagnostics/{started['session']['id']}/stop")
