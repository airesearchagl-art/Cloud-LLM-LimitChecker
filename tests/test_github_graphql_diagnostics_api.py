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

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
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


def test_pagination_reflects_actual_clamped_values(diagnostics_client):
    response = diagnostics_client.get("/api/github-graphql-diagnostics/sessions?limit=99999&offset=-5")
    body = response.json()
    assert response.status_code == 200
    assert body["limit"] == 500  # crud.SAFE_MAX_LIMIT, not the raw 99999 requested
    assert body["offset"] == 0  # negative offset floored to 0, not echoed raw


class _MutableClock:
    """A settable fake clock -- lets a test advance `now` between two calls
    into the controller without any real sleeping. Mirrors the identically-
    named helper in tests/test_github_graphql_diagnostics_controller.py."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def test_sessions_list_includes_max_valid_interval_delta(monkeypatch: pytest.MonkeyPatch):
    # Uses its own advancing clock (rather than the shared frozen-NOW
    # diagnostics_client fixture) so start and stop land at genuinely
    # different instants -- this is also a regression guard for the
    # start-baseline-exclusion fix below: with started_at == stop's
    # collected_at (the old frozen-clock behavior), the fix would exclude
    # the *only* sample in range and wrongly return None instead of a real
    # observed value.
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "true")
    session_factory = _isolated_session_factory()
    clock = _MutableClock(NOW)
    app.state.github_graphql_diagnostics_controller = make_controller(session_factory=session_factory, clock=clock)
    _wire_isolated_db(session_factory)
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/github-graphql-diagnostics/start",
                json={"actor_type": "claude_code", "label": "test"},
            ).json()
            clock.advance(timedelta(seconds=30))
            client.post(f"/api/github-graphql-diagnostics/{started['session']['id']}/stop")

            sessions_body = client.get("/api/github-graphql-diagnostics/sessions?limit=10").json()
            matching = [s for s in sessions_body["items"] if s["id"] == started["session"]["id"]]
            assert len(matching) == 1
            # The start baseline (collected_at == started_at) is excluded;
            # only the final stop sample (30s later) is a candidate here.
            # Its delta is 0 because fake_fetch returns a static payload
            # throughout -- still a valid "ok" observed value, not None.
            assert matching[0]["max_valid_interval_delta"] == 0
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# max_valid_interval_delta: start-baseline exclusion (live-validation fix)
#
# Live validation against a real account found that this derived metric
# could report a session's own start-baseline sample's delta (which
# describes the interval BEFORE the session existed -- already correctly
# excluded from attribution_status/graphql_delta_total via the Finding 2
# baseline-exclusion design) as if it were "the worst interval observed
# during the session". These tests construct GitHubRateSample/
# GitHubDiagnosticSession rows directly (bypassing the controller) for
# precise control over collected_at/fetch_status/graphql_delta combinations
# that would be awkward to drive through the live async start/tick/stop
# flow.
# ---------------------------------------------------------------------------


@pytest.fixture()
def diagnostics_client_with_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "true")
    session_factory = _isolated_session_factory()
    app.state.github_graphql_diagnostics_controller = make_controller(session_factory=session_factory)
    _wire_isolated_db(session_factory)
    try:
        with TestClient(app) as client:
            yield client, session_factory
    finally:
        app.dependency_overrides.clear()


_BASE = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _fetch_max_valid_interval_delta(client: TestClient, session_id: int) -> int | None:
    sessions_body = client.get("/api/github-graphql-diagnostics/sessions?limit=10").json()
    matching = [s for s in sessions_body["items"] if s["id"] == session_id]
    assert len(matching) == 1
    return matching[0]["max_valid_interval_delta"]


def test_max_valid_interval_delta_excludes_start_baseline_sample(diagnostics_client_with_db):
    client, session_factory = diagnostics_client_with_db
    db = session_factory()
    try:
        session = models.GitHubDiagnosticSession(
            actor_type="claude_code",
            label="test",
            started_at=_BASE,
            ended_at=None,
            attribution_status="UNATTRIBUTED",
            status="ACTIVE",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        # Baseline sample: collected_at == started_at, a LARGE delta from
        # the untracked pre-session gap -- must be excluded.
        db.add(
            models.GitHubRateSample(
                collected_at=_BASE,
                graphql_used=3000,
                graphql_delta=2694,
                fetch_status="ok",
                attribution_status="UNATTRIBUTED",
                trigger_session_id=session_id,
            )
        )
        # Periodic sample strictly after start: small delta, must be the
        # one actually reported.
        db.add(
            models.GitHubRateSample(
                collected_at=_BASE + timedelta(seconds=10),
                graphql_used=3010,
                graphql_delta=10,
                fetch_status="ok",
                attribution_status="SINGLE_ACTIVITY_CORRELATION",
                trigger_session_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    assert _fetch_max_valid_interval_delta(client, session_id) == 10


def test_max_valid_interval_delta_none_when_only_baseline_sample_exists(diagnostics_client_with_db):
    client, session_factory = diagnostics_client_with_db
    db = session_factory()
    try:
        session = models.GitHubDiagnosticSession(
            actor_type="claude_code",
            label="test",
            started_at=_BASE,
            ended_at=None,
            attribution_status="UNATTRIBUTED",
            status="ACTIVE",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        db.add(
            models.GitHubRateSample(
                collected_at=_BASE,
                graphql_used=3000,
                graphql_delta=2694,
                fetch_status="ok",
                attribution_status="UNATTRIBUTED",
                trigger_session_id=session_id,
            )
        )
        db.commit()
    finally:
        db.close()

    # Baseline excluded, nothing else in range -- never fabricates a value.
    assert _fetch_max_valid_interval_delta(client, session_id) is None


def test_max_valid_interval_delta_includes_final_stop_sample(diagnostics_client_with_db):
    client, session_factory = diagnostics_client_with_db
    db = session_factory()
    try:
        ended_at = _BASE + timedelta(seconds=20)
        session = models.GitHubDiagnosticSession(
            actor_type="claude_code",
            label="test",
            started_at=_BASE,
            ended_at=ended_at,
            attribution_status="UNATTRIBUTED",
            status="STOPPED",
            stop_reason="USER_STOP",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        db.add(
            models.GitHubRateSample(
                collected_at=_BASE,
                graphql_used=3000,
                graphql_delta=9999,
                fetch_status="ok",
                attribution_status="UNATTRIBUTED",
                trigger_session_id=session_id,
            )
        )
        db.add(
            models.GitHubRateSample(
                collected_at=ended_at,
                graphql_used=3042,
                graphql_delta=42,
                fetch_status="ok",
                attribution_status="SINGLE_ACTIVITY_CORRELATION",
                trigger_session_id=session_id,
            )
        )
        db.commit()
    finally:
        db.close()

    assert _fetch_max_valid_interval_delta(client, session_id) == 42


def test_max_valid_interval_delta_excludes_non_ok_and_null_delta_samples(diagnostics_client_with_db):
    client, session_factory = diagnostics_client_with_db
    db = session_factory()
    try:
        session = models.GitHubDiagnosticSession(
            actor_type="claude_code",
            label="test",
            started_at=_BASE,
            ended_at=None,
            attribution_status="UNATTRIBUTED",
            status="ACTIVE",
        )
        db.add(session)
        db.flush()
        session_id = session.id
        # All of these are strictly after start_at, but none should be
        # picked as the max: RESET_BOUNDARY / COUNTER_REGRESSION /
        # FETCH_FAILED all carry graphql_delta=None by construction; an
        # "ok" sample with a null delta (defensive, shouldn't occur in
        # practice) must also be skipped rather than crash on max(None, ...).
        rows = [
            (10, "reset_boundary", None),
            (20, "counter_regression", None),
            (30, "fetch_failed", None),
            (40, "ok", None),
        ]
        for offset, fetch_status, delta in rows:
            db.add(
                models.GitHubRateSample(
                    collected_at=_BASE + timedelta(seconds=offset),
                    graphql_used=3000,
                    graphql_delta=delta,
                    fetch_status=fetch_status,
                    attribution_status="RESET_BOUNDARY" if fetch_status == "reset_boundary" else "UNATTRIBUTED",
                    trigger_session_id=None,
                )
            )
        # The one genuinely valid sample, smaller offset than the above but
        # still strictly after start -- must be the only candidate.
        db.add(
            models.GitHubRateSample(
                collected_at=_BASE + timedelta(seconds=5),
                graphql_used=3003,
                graphql_delta=3,
                fetch_status="ok",
                attribution_status="SINGLE_ACTIVITY_CORRELATION",
                trigger_session_id=None,
            )
        )
        db.commit()
    finally:
        db.close()

    assert _fetch_max_valid_interval_delta(client, session_id) == 3


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


def test_sessions_and_samples_list_return_timezone_aware_timestamps(diagnostics_client):
    """Regression guard for a real bug found during this round's manual UI
    check: SQLite round-trips a tz-aware datetime as naive even on a column
    declared `DateTime(timezone=True)`. The status snapshot endpoint
    (`GET /api/github-graphql-diagnostics`) already re-attaches UTC via
    `_normalize_utc` before building its response dict, but the `/sessions`
    and `/samples` list routes serialize straight off the ORM row via
    `GitHubDiagnosticSessionRead`/`GitHubRateSampleRead` -- without a fix,
    those two routes would echo back an offset-less ISO string (e.g.
    `"2026-08-10T07:47:21.428467"`), which downstream JS `Date` parsing
    treats as browser-local time instead of UTC, corrupting every
    start/end/collected_at shown in the new Recent Activity Sessions /
    Sample Timeline UI for any non-UTC browser timezone.
    """
    started = diagnostics_client.post(
        "/api/github-graphql-diagnostics/start",
        json={"actor_type": "claude_code", "label": "tz-check"},
    ).json()
    session_id = started["session"]["id"]
    diagnostics_client.post(f"/api/github-graphql-diagnostics/{session_id}/stop")

    sessions_body = diagnostics_client.get("/api/github-graphql-diagnostics/sessions?limit=10").json()
    matching = [s for s in sessions_body["items"] if s["id"] == session_id]
    assert len(matching) == 1
    session = matching[0]
    for field in ("started_at", "ended_at", "reset_at_start"):
        value = session[field]
        assert value is not None
        assert value.endswith("Z") or "+" in value[10:], f"{field} is not timezone-aware: {value!r}"

    samples_body = diagnostics_client.get("/api/github-graphql-diagnostics/samples?limit=10").json()
    assert samples_body["items"]
    for sample in samples_body["items"]:
        collected_at = sample["collected_at"]
        assert collected_at.endswith("Z") or "+" in collected_at[10:], f"collected_at not timezone-aware: {collected_at!r}"
        if sample["graphql_reset_at"] is not None:
            reset_at = sample["graphql_reset_at"]
            assert reset_at.endswith("Z") or "+" in reset_at[10:], f"graphql_reset_at not timezone-aware: {reset_at!r}"
