import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base
from app.github_graphql_diagnostics_controller import (
    GitHubGraphQLDiagnosticsAccountContextChangedError,
    GitHubGraphQLDiagnosticsController,
    GitHubGraphQLDiagnosticsDisabledError,
    GitHubGraphQLDiagnosticsIdentityFetchError,
    GitHubGraphQLDiagnosticsSessionNotFoundError,
)
from app.github_graphql_diagnostics_identity import GitHubIdentityFetchResult
from app.github_rate_limit_cli import GitHubRateLimitFetchResult

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
RESET_AT = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
RESET_EPOCH = int(RESET_AT.timestamp())
LATER_RESET_AT = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
LATER_RESET_EPOCH = int(LATER_RESET_AT.timestamp())


class MutableClock:
    """A settable fake clock -- lets a test advance `now` between two calls
    into the controller (e.g. between start_session and stop_session)
    without any real sleeping."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def resource(limit=5000, used=100, remaining=4900, reset=RESET_EPOCH):
    return {"limit": limit, "used": used, "remaining": remaining, "reset": reset}


def payload(core=None, graphql=None, search=None):
    resources = {}
    if core is not None:
        resources["core"] = core
    if graphql is not None:
        resources["graphql"] = graphql
    if search is not None:
        resources["search"] = search
    return {"resources": resources}


def make_fetch(payload_dict=None, *, error=None):
    def fetch(*, now=None, **kwargs):
        if error is not None:
            error_type, message = error
            return GitHubRateLimitFetchResult(
                success=False, payload=None, error_type=error_type, user_message=message, return_code=1, collected_at=now
            )
        return GitHubRateLimitFetchResult(
            success=True, payload=payload_dict, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    return fetch


def make_identity(*, success=True, login="octocat", user_id=1, error_type=None, user_message=None):
    def identity_fetch(**kwargs):
        if not success:
            return GitHubIdentityFetchResult(
                success=False, login=None, user_id=None, error_type=error_type, user_message=user_message
            )
        return GitHubIdentityFetchResult(success=True, login=login, user_id=user_id, error_type=None, user_message=None)

    return identity_fetch


def make_session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def insert_active_session(session_factory, *, started_at: datetime, github_user_id: int = 1) -> int:
    with session_factory() as db:
        row = models.GitHubDiagnosticSession(
            actor_type="claude_code",
            label="pre-existing",
            repository=None,
            pr_number=None,
            started_at=started_at,
            ended_at=None,
            github_login="octocat",
            github_user_id=github_user_id,
            reset_at_start=RESET_AT,
            graphql_used_start=10,
            attribution_status="UNATTRIBUTED",
            status="ACTIVE",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def make_controller(*, session_factory, fetch, identity_fetch, clock, sample_seconds=1000, max_minutes=15) -> GitHubGraphQLDiagnosticsController:
    return GitHubGraphQLDiagnosticsController(
        session_factory=session_factory,
        fetch=fetch,
        identity_fetch=identity_fetch,
        clock=clock,
        sample_seconds=sample_seconds,
        max_minutes=max_minutes,
    )


# ---------------------------------------------------------------------------
# start_session — disabled / identity failure
# ---------------------------------------------------------------------------


def test_start_session_disabled_raises_and_touches_nothing(monkeypatch):
    monkeypatch.delenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", raising=False)
    session_factory = make_session_factory()

    def explode_fetch(**kwargs):
        raise AssertionError("fetch must not be called when diagnostics are disabled")

    def explode_identity(**kwargs):
        raise AssertionError("identity_fetch must not be called when diagnostics are disabled")

    controller = make_controller(session_factory=session_factory, fetch=explode_fetch, identity_fetch=explode_identity, clock=lambda: NOW)

    async def scenario():
        with pytest.raises(GitHubGraphQLDiagnosticsDisabledError):
            await controller.start_session(actor_type="claude_code", label="test", repository=None, pr_number=None)

    asyncio.run(scenario())

    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).count() == 0
        assert db.query(models.GitHubRateSample).count() == 0


def test_start_session_identity_failure_raises_and_creates_no_session(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    identity_fetch = make_identity(success=False, error_type="not_authenticated", user_message="not authenticated")

    def explode_fetch(**kwargs):
        raise AssertionError("rate_limit fetch must not run when identity fetch fails")

    controller = make_controller(session_factory=session_factory, fetch=explode_fetch, identity_fetch=identity_fetch, clock=lambda: NOW)

    async def scenario():
        with pytest.raises(GitHubGraphQLDiagnosticsIdentityFetchError) as exc_info:
            await controller.start_session(actor_type="claude_code", label="test", repository=None, pr_number=None)
        assert exc_info.value.result.error_type == "not_authenticated"

    asyncio.run(scenario())

    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).count() == 0


# ---------------------------------------------------------------------------
# start_session — baseline sample / multiple sessions / account context
# ---------------------------------------------------------------------------


def test_start_session_records_correct_baseline_sample(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(
        payload(
            core=resource(used=50, remaining=4950),
            graphql=resource(used=200, remaining=4800),
            search=resource(limit=30, used=1, remaining=29),
        )
    )
    identity_fetch = make_identity(login="octocat", user_id=42)
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=identity_fetch, clock=lambda: NOW)

    async def scenario():
        return await controller.start_session(actor_type="claude_code", label="PR review", repository="acme/widgets", pr_number=7)

    result = asyncio.run(scenario())

    assert result["session"]["status"] == "ACTIVE"
    assert result["session"]["github_login"] == "octocat"
    assert result["session"]["github_user_id"] == 42
    assert result["session"]["graphql_used_start"] == 200
    assert result["sample"]["graphql_used"] == 200
    assert result["sample"]["core_used"] == 50
    assert result["sample"]["search_used"] == 1
    assert result["sample"]["fetch_status"] == "no_previous"
    assert result["sample"]["attribution_status"] == "SINGLE_ACTIVITY_CORRELATION"
    assert result["sample"]["trigger_session_id"] == result["session"]["id"]

    with session_factory() as db:
        row = db.query(models.GitHubRateSample).one()
        assert row.graphql_used == 200
        assert row.trigger_session_id == result["session"]["id"]


def test_two_sessions_same_identity_both_created(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900), search=resource()))
    identity_fetch = make_identity(user_id=42)
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=identity_fetch, clock=lambda: NOW)

    async def scenario():
        r1 = await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        r2 = await controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None)
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1["session"]["id"] != r2["session"]["id"]

    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).filter_by(status="ACTIVE").count() == 2


def test_start_session_different_account_context_raises_and_leaves_existing_untouched(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900), search=resource()))
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(login="octocat", user_id=1), clock=lambda: NOW
    )

    async def start_first():
        return await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)

    first = asyncio.run(start_first())

    with session_factory() as db:
        before_row = db.query(models.GitHubDiagnosticSession).filter_by(id=first["session"]["id"]).one()
        before_snapshot = {c.name: getattr(before_row, c.name) for c in models.GitHubDiagnosticSession.__table__.columns}

    controller._identity_fetch = make_identity(login="other-user", user_id=2)

    async def start_second():
        with pytest.raises(GitHubGraphQLDiagnosticsAccountContextChangedError):
            await controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None)

    asyncio.run(start_second())

    with session_factory() as db:
        after_row = db.query(models.GitHubDiagnosticSession).filter_by(id=first["session"]["id"]).one()
        after_snapshot = {c.name: getattr(after_row, c.name) for c in models.GitHubDiagnosticSession.__table__.columns}
        assert after_snapshot == before_snapshot
        assert db.query(models.GitHubDiagnosticSession).count() == 1


# ---------------------------------------------------------------------------
# sampler lifecycle
# ---------------------------------------------------------------------------


def test_sampler_starts_on_first_session_and_task_is_reused(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900), search=resource()))
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW, sample_seconds=1000
    )
    # The sampler's own periodic-sleep behavior is not under test here --
    # only whether start_session results in exactly one live task.
    controller._sampler._sleep = lambda s: asyncio.sleep(1000)

    async def scenario():
        assert controller.sampler_running is False
        await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        assert controller.sampler_running is True
        task_after_first = controller._sampler._task

        await controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None)
        assert controller.sampler_running is True
        assert controller._sampler._task is task_after_first  # no second task was ever created

        await controller._sampler.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


def test_stop_session_active_records_final_sample_and_delta(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
    )

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    session_id = started["session"]["id"]

    clock.advance(timedelta(minutes=5))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=350, remaining=4650, reset=RESET_EPOCH), search=resource()))

    stopped = asyncio.run(controller.stop_session(session_id=session_id))

    assert stopped["session"]["status"] == "STOPPED"
    assert stopped["session"]["stop_reason"] == "USER_STOP"
    assert stopped["session"]["graphql_used_end"] == 350
    assert stopped["session"]["graphql_delta_total"] == 250
    assert stopped["sample"]["graphql_used"] == 350
    assert stopped["sample"]["trigger_session_id"] == session_id


def test_stop_session_already_stopped_is_idempotent(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
    )

    async def scenario():
        started = await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        session_id = started["session"]["id"]
        first_stop = await controller.stop_session(session_id=session_id)
        second_stop = await controller.stop_session(session_id=session_id)
        return first_stop, second_stop

    first_stop, second_stop = asyncio.run(scenario())

    assert second_stop["sample"] is None
    assert second_stop["session"] == first_stop["session"]

    with session_factory() as db:
        # baseline sample + one final sample only -- the idempotent second
        # stop_session call must not create a new sample row.
        assert db.query(models.GitHubRateSample).count() == 2


def test_stop_session_across_reset_boundary_delta_total_is_none(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
    )

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    session_id = started["session"]["id"]

    clock.advance(timedelta(hours=7))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=20, remaining=4980, reset=LATER_RESET_EPOCH), search=resource()))

    stopped = asyncio.run(controller.stop_session(session_id=session_id))

    assert stopped["session"]["graphql_delta_total"] is None
    assert stopped["session"]["graphql_used_end"] == 20


def test_stop_session_reduces_active_to_zero_stops_sampler(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=lambda: NOW,
        sample_seconds=1000,
    )
    controller._sampler._sleep = lambda s: asyncio.sleep(1000)

    async def scenario():
        started = await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        assert controller.sampler_running is True
        await controller.stop_session(session_id=started["session"]["id"])
        assert controller.sampler_running is False

    asyncio.run(scenario())


def test_stop_session_nonexistent_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    controller = make_controller(session_factory=session_factory, fetch=make_fetch(payload()), identity_fetch=make_identity(), clock=lambda: NOW)

    async def scenario():
        with pytest.raises(GitHubGraphQLDiagnosticsSessionNotFoundError):
            await controller.stop_session(session_id=999999)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# scheduled tick — attribution
# ---------------------------------------------------------------------------


def test_scheduled_tick_zero_active_sessions_is_unattributed(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=10, remaining=4990, reset=RESET_EPOCH), search=resource()))
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(), clock=lambda: NOW)

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        sample = db.query(models.GitHubRateSample).one()
        assert sample.attribution_status == "UNATTRIBUTED"
        assert sample.trigger_session_id is None


def test_scheduled_tick_one_active_session_is_single_correlation(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=10, remaining=4990, reset=RESET_EPOCH), search=resource()))
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(), clock=lambda: NOW)

    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=1))

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        sample = db.query(models.GitHubRateSample).one()
        assert sample.attribution_status == "SINGLE_ACTIVITY_CORRELATION"


def test_scheduled_tick_two_overlapping_sessions_is_overlapping(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=10, remaining=4990, reset=RESET_EPOCH), search=resource()))
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(), clock=lambda: NOW)

    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=5))
    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=1))

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        sample = db.query(models.GitHubRateSample).one()
        assert sample.attribution_status == "OVERLAPPING_ACTIVITIES"

    # Structural guarantee: GitHubRateSample carries no per-session numeric
    # split column of any kind -- this module never fabricates a
    # proportional attribution.
    columns = {c.name for c in models.GitHubRateSample.__table__.columns}
    assert not any("session_delta" in name or "per_session" in name or "split" in name for name in columns)


def test_scheduled_tick_exhausted_transitions_all_active_sessions(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(login="octocat", user_id=1),
        clock=clock,
    )

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    session_id = started["session"]["id"]

    clock.advance(timedelta(minutes=1))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=5000, remaining=0, reset=RESET_EPOCH), search=resource()))

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "EXHAUSTED"
        assert session.stop_reason == "GRAPHQL_EXHAUSTED"
        assert session.graphql_used_end == 5000
        assert session.graphql_delta_total == 4900

    # A subsequently-attempted NEW start_session call, with a fresh fetch
    # that also shows remaining==0, must create the new session already in
    # EXHAUSTED state (never ACTIVE).
    second = asyncio.run(controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None))
    assert second["session"]["status"] == "EXHAUSTED"
    assert second["session"]["stop_reason"] == "GRAPHQL_EXHAUSTED"


def test_scheduled_tick_max_duration_auto_stops_session(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
        max_minutes=15,
    )

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    session_id = started["session"]["id"]

    clock.advance(timedelta(minutes=16))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=150, remaining=4850, reset=RESET_EPOCH), search=resource()))

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "AUTO_STOPPED"
        assert session.stop_reason == "MAX_DURATION"
        assert session.graphql_used_end == 150
        assert session.graphql_delta_total == 50


def test_scheduled_tick_fetch_failure_does_not_auto_stop_active_sessions(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
    )

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    session_id = started["session"]["id"]

    clock.advance(timedelta(minutes=1))
    controller._fetch = make_fetch(error=("timeout", "Fetching the GitHub rate limit timed out."))

    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "ACTIVE"
        sample = db.query(models.GitHubRateSample).order_by(models.GitHubRateSample.id.desc()).first()
        assert sample.fetch_status == "fetch_failed"
        assert sample.attribution_status == "FETCH_FAILED"


# ---------------------------------------------------------------------------
# reconcile_on_startup
# ---------------------------------------------------------------------------


def test_reconcile_on_startup_aborts_leftover_active_sessions(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    controller = make_controller(
        session_factory=session_factory, fetch=make_fetch(payload()), identity_fetch=make_identity(), clock=lambda: NOW
    )

    insert_active_session(session_factory, started_at=NOW - timedelta(hours=1))

    result = asyncio.run(controller.reconcile_on_startup())

    assert result["aborted_session_count"] == 1
    assert controller.sampler_running is False

    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).one()
        assert session.status == "ABORTED"
        assert session.stop_reason == "PROCESS_RESTART"
        assert session.graphql_used_end is None
        assert session.graphql_delta_total is None
        assert session.ended_at is not None


# ---------------------------------------------------------------------------
# regression guard — no GraphQL endpoint call anywhere in this test file
# ---------------------------------------------------------------------------


def test_no_graphql_endpoint_referenced_in_this_test_file():
    forbidden_graphql_path = "/" + "graphql"
    forbidden_graphql_subcommand = "api " + "graphql"
    source = Path(__file__).read_text(encoding="utf-8")
    assert forbidden_graphql_path not in source.lower()
    assert forbidden_graphql_subcommand not in source.lower()
