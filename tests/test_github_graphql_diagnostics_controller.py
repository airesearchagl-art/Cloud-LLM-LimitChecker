import asyncio
import threading
import time
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
    # Finding 2: the baseline sample's delta covers the interval BEFORE this
    # new session existed -- with zero pre-existing active sessions, it must
    # be UNATTRIBUTED, not attributed to the session that's only starting
    # now (this session was not active for any part of that interval).
    assert result["sample"]["attribution_status"] == "UNATTRIBUTED"
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


def test_scheduled_tick_zero_active_sessions_skips_fetch_and_signals_stop(monkeypatch):
    # Finding 1: a tick that fires with zero active sessions already (a race
    # against a stop/auto-transition that happened between scheduling and
    # firing) must not call gh api rate_limit at all, must not record any
    # sample, and must signal the sampler to stop (return False).
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()

    def explode(**kwargs):
        raise AssertionError("fetch must not be called when active sessions is already 0 at tick start")

    controller = make_controller(session_factory=session_factory, fetch=explode, identity_fetch=make_identity(), clock=lambda: NOW)

    should_continue = asyncio.run(controller._scheduled_tick())

    assert should_continue is False
    with session_factory() as db:
        assert db.query(models.GitHubRateSample).count() == 0


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
# Finding 1 -- sampler stops when active sessions reach 0 (any path)
# ---------------------------------------------------------------------------


def test_scheduled_tick_max_duration_only_active_session_signals_stop(monkeypatch):
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

    should_continue = asyncio.run(controller._scheduled_tick())

    # Finding 1: the only active session hit MAX_DURATION during this tick,
    # so there is nothing left to observe -- the tick must signal the
    # sampler to stop.
    assert should_continue is False
    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "AUTO_STOPPED"
        assert session.stop_reason == "MAX_DURATION"


def test_scheduled_tick_graphql_exhausted_only_active_session_signals_stop(monkeypatch):
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

    clock.advance(timedelta(minutes=1))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=5000, remaining=0, reset=RESET_EPOCH), search=resource()))

    should_continue = asyncio.run(controller._scheduled_tick())

    # Finding 1: the tick's own fetch revealed graphql.remaining == 0, and
    # this was the only active session -- must signal the sampler to stop.
    assert should_continue is False
    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "EXHAUSTED"
        assert session.stop_reason == "GRAPHQL_EXHAUSTED"


def test_scheduled_tick_two_active_sessions_only_one_times_out_continues(monkeypatch):
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
    session_a_id = started["session"]["id"]

    clock.advance(timedelta(minutes=16))
    # Session B started only 5 minutes before this tick fires -- unlike
    # session A (started at the original NOW, 16 minutes ago), it must NOT
    # be timed out.
    session_b_id = insert_active_session(session_factory, started_at=clock.now - timedelta(minutes=5), github_user_id=1)
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=150, remaining=4850, reset=RESET_EPOCH), search=resource()))

    should_continue = asyncio.run(controller._scheduled_tick())

    # Finding 1: at least one session (B) is still active after this tick's
    # auto-transitions -- sampling must continue.
    assert should_continue is True
    with session_factory() as db:
        session_a = db.query(models.GitHubDiagnosticSession).filter_by(id=session_a_id).one()
        session_b = db.query(models.GitHubDiagnosticSession).filter_by(id=session_b_id).one()
        assert session_a.status == "AUTO_STOPPED"
        assert session_a.stop_reason == "MAX_DURATION"
        assert session_b.status == "ACTIVE"


def test_sampler_stops_itself_end_to_end_after_max_duration(monkeypatch):
    # The most important Finding 1 test: proves the REAL sampler stops
    # itself end-to-end (no external stop_session call is ever made here),
    # not just that _scheduled_tick_sync returns the right boolean in
    # isolation.
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
        sample_seconds=60,
        max_minutes=15,
    )

    async def fake_sleep(seconds):
        # Deterministic, no real waiting: each simulated tick advances the
        # fake clock by exactly one interval, then yields once so the event
        # loop can schedule other ready callbacks.
        clock.advance(timedelta(seconds=seconds))
        await asyncio.sleep(0)

    controller._sampler._sleep = fake_sleep

    async def scenario():
        started = await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        assert controller.sampler_running is True
        task = controller._sampler._task
        await asyncio.wait_for(task, timeout=5)
        return started["session"]["id"]

    session_id = asyncio.run(scenario())

    assert controller.sampler_running is False
    with session_factory() as db:
        session = db.query(models.GitHubDiagnosticSession).filter_by(id=session_id).one()
        assert session.status == "AUTO_STOPPED"
        assert session.stop_reason == "MAX_DURATION"


def test_sampler_restarts_normally_after_natural_stop(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)
    controller = make_controller(
        session_factory=session_factory,
        fetch=make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())),
        identity_fetch=make_identity(user_id=1),
        clock=clock,
        sample_seconds=60,
        max_minutes=15,
    )

    async def fake_sleep(seconds):
        clock.advance(timedelta(seconds=seconds))
        await asyncio.sleep(0)

    controller._sampler._sleep = fake_sleep

    async def scenario():
        await controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None)
        first_task = controller._sampler._task
        await asyncio.wait_for(first_task, timeout=5)
        assert controller.sampler_running is False

        # A NEW start_session call after the sampler naturally stopped must
        # succeed and start sampling again -- is_running was correctly reset
        # to False, and start() is not left in some broken half-stopped
        # state.
        second = await controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None)
        assert controller.sampler_running is True
        second_task = controller._sampler._task
        assert second_task is not None
        assert second_task is not first_task
        await controller._sampler.stop()
        return second

    second = asyncio.run(scenario())
    assert second["session"]["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Finding 2 -- baseline attribution excludes the new session itself
# ---------------------------------------------------------------------------


def test_start_session_baseline_with_one_pre_existing_active_session_is_single_correlation(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=200, remaining=4800, reset=RESET_EPOCH), search=resource()))
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW)

    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=5), github_user_id=1)

    result = asyncio.run(controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None))

    # Finding 2: only the pre-existing session (A) counts toward this
    # baseline sample's attribution -- the new session (B) was not active
    # for any part of the interval this baseline sample covers, so counting
    # it would incorrectly report OVERLAPPING_ACTIVITIES instead of
    # SINGLE_ACTIVITY_CORRELATION.
    assert result["sample"]["attribution_status"] == "SINGLE_ACTIVITY_CORRELATION"
    # Invariant: the new session's own attribution_status field reflects the
    # same value as its baseline sample.
    assert result["session"]["attribution_status"] == "SINGLE_ACTIVITY_CORRELATION"


def test_start_session_baseline_with_two_pre_existing_active_sessions_is_overlapping(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=200, remaining=4800, reset=RESET_EPOCH), search=resource()))
    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW)

    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=5), github_user_id=1)
    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=3), github_user_id=1)

    result = asyncio.run(controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None))

    # Finding 2: exactly the two pre-existing sessions (A and C) count --
    # correctly excluding the new session (B) itself.
    assert result["sample"]["attribution_status"] == "OVERLAPPING_ACTIVITIES"
    assert result["session"]["attribution_status"] == "OVERLAPPING_ACTIVITIES"

    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).filter_by(status="ACTIVE").count() == 3


# ---------------------------------------------------------------------------
# Finding 3 -- process-wide serialization (max 1 concurrent fetch)
# ---------------------------------------------------------------------------


def make_concurrency_tracking_fetch(payload_dict, *, delay=0.02):
    """A `fetch` fake that records the maximum number of CONCURRENT calls
    ever observed. Since `self._fetch` is invoked via `asyncio.to_thread`
    (a real OS thread pool, not inline on the event loop), a synchronous
    `time.sleep` here genuinely creates an overlap window if two calls are
    not serialized by `self._operation_lock`."""
    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def fetch(*, now=None, **kwargs):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(delay)
        with lock:
            state["current"] -= 1
        return GitHubRateLimitFetchResult(
            success=True, payload=payload_dict, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    return fetch, state


def make_sequential_identity(user_ids):
    """A call-counter `identity_fetch` fake that returns a DIFFERENT
    identity on each successive call (the Nth call returns `user_ids[N]`,
    clamped to the last entry once the list is exhausted)."""
    calls = {"n": 0}

    def identity_fetch(**kwargs):
        idx = min(calls["n"], len(user_ids) - 1)
        calls["n"] += 1
        user_id = user_ids[idx]
        return GitHubIdentityFetchResult(success=True, login=f"user-{user_id}", user_id=user_id, error_type=None, user_message=None)

    return identity_fetch


def test_concurrent_start_sessions_serialize_fetches(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch, state = make_concurrency_tracking_fetch(
        payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())
    )
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW, sample_seconds=1000
    )
    controller._sampler._sleep = lambda s: asyncio.sleep(1000)

    async def scenario():
        r1, r2 = await asyncio.gather(
            controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None),
            controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None),
        )
        assert controller.sampler_running is True
        await controller._sampler.stop()
        return r1, r2

    r1, r2 = asyncio.run(scenario())

    # Finding 3: self._operation_lock serializes the entire body of both
    # calls -- at most one gh api rate_limit fetch is ever in flight.
    assert state["max"] == 1
    assert r1["session"]["id"] != r2["session"]["id"]
    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).count() == 2


def test_concurrent_start_session_and_scheduled_tick_serialize_fetches(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    insert_active_session(session_factory, started_at=NOW - timedelta(minutes=1), github_user_id=1)
    fetch, state = make_concurrency_tracking_fetch(
        payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())
    )
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW, sample_seconds=1000
    )
    controller._sampler._sleep = lambda s: asyncio.sleep(1000)

    async def scenario():
        await asyncio.gather(
            controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None),
            controller._scheduled_tick(),
        )
        await controller._sampler.stop()

    asyncio.run(scenario())

    assert state["max"] == 1


def test_concurrent_stop_session_and_scheduled_tick_serialize_fetches(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    session_id = insert_active_session(session_factory, started_at=NOW - timedelta(minutes=1), github_user_id=1)
    fetch, state = make_concurrency_tracking_fetch(
        payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource())
    )
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=lambda: NOW, sample_seconds=1000
    )

    async def scenario():
        await asyncio.gather(
            controller.stop_session(session_id=session_id),
            controller._scheduled_tick(),
        )

    asyncio.run(scenario())

    assert state["max"] == 1


def test_concurrent_start_sessions_different_identity_exactly_one_succeeds(monkeypatch):
    # Finding 3's core race scenario: two concurrent start_session calls
    # where the second identity fetch returns a DIFFERENT account than the
    # first. Without process-wide serialization, both could race past the
    # "no active session yet" check before either commits. With it, exactly
    # one succeeds and the other observes the first's already-committed
    # session.
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    fetch = make_fetch(payload(core=resource(), graphql=resource(used=100, remaining=4900, reset=RESET_EPOCH), search=resource()))
    identity_fetch = make_sequential_identity([1, 2])
    controller = make_controller(
        session_factory=session_factory, fetch=fetch, identity_fetch=identity_fetch, clock=lambda: NOW, sample_seconds=1000
    )
    controller._sampler._sleep = lambda s: asyncio.sleep(1000)

    async def scenario():
        results = await asyncio.gather(
            controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None),
            controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None),
            return_exceptions=True,
        )
        await controller._sampler.stop()
        return results

    r1, r2 = asyncio.run(scenario())

    successes = [r for r in (r1, r2) if not isinstance(r, BaseException)]
    errors = [r for r in (r1, r2) if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], GitHubGraphQLDiagnosticsAccountContextChangedError)

    with session_factory() as db:
        assert db.query(models.GitHubDiagnosticSession).count() == 1


# ---------------------------------------------------------------------------
# Finding 4 -- fetch failure breaks delta continuity (fail-closed)
# ---------------------------------------------------------------------------


def test_failed_baseline_then_successful_tick_is_no_previous(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)

    call_count = {"n": 0}

    def fetch(*, now=None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return GitHubRateLimitFetchResult(
                success=False, payload=None, error_type="timeout", user_message="timed out", return_code=1, collected_at=now
            )
        return GitHubRateLimitFetchResult(
            success=True,
            payload=payload(core=resource(), graphql=resource(used=300, remaining=4700, reset=RESET_EPOCH), search=resource()),
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=clock)

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    assert started["sample"]["fetch_status"] == "fetch_failed"
    assert started["sample"]["graphql_delta"] is None

    clock.advance(timedelta(minutes=1))
    asyncio.run(controller._scheduled_tick())

    with session_factory() as db:
        sample = db.query(models.GitHubRateSample).order_by(models.GitHubRateSample.id.desc()).first()
        # Finding 4: the failed baseline must not leave a stale last-known-
        # good value in place -- this later successful sample is treated as
        # a fresh baseline.
        assert sample.fetch_status == "no_previous"
        assert sample.graphql_delta is None


def test_fetch_failure_between_two_successful_ticks_breaks_delta_continuity(monkeypatch):
    monkeypatch.setenv("GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED", "1")
    session_factory = make_session_factory()
    clock = MutableClock(NOW)

    call_count = {"n": 0}

    def fetch(*, now=None, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return GitHubRateLimitFetchResult(
                success=False, payload=None, error_type="timeout", user_message="timed out", return_code=1, collected_at=now
            )
        used = 100 if call_count["n"] == 1 else 900
        return GitHubRateLimitFetchResult(
            success=True,
            payload=payload(core=resource(), graphql=resource(used=used, remaining=5000 - used, reset=RESET_EPOCH), search=resource()),
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    controller = make_controller(session_factory=session_factory, fetch=fetch, identity_fetch=make_identity(user_id=1), clock=clock)

    started = asyncio.run(controller.start_session(actor_type="claude_code", label="A", repository=None, pr_number=None))
    assert started["sample"]["fetch_status"] == "no_previous"
    assert started["sample"]["graphql_used"] == 100

    clock.advance(timedelta(minutes=1))
    asyncio.run(controller._scheduled_tick())  # call #2 -- fails

    clock.advance(timedelta(minutes=1))
    asyncio.run(controller._scheduled_tick())  # call #3 -- succeeds, used=900

    with session_factory() as db:
        samples = db.query(models.GitHubRateSample).order_by(models.GitHubRateSample.id.asc()).all()
        assert len(samples) == 3
        assert samples[0].fetch_status == "no_previous"
        assert samples[1].fetch_status == "fetch_failed"
        # The sample AFTER the failure must be treated as a fresh baseline,
        # never diffed against the pre-failure successful sample -- a naive
        # diff (900 - 100 = 800) would have produced a real-looking but
        # WRONG number spanning the unmeasured gap.
        assert samples[2].fetch_status == "no_previous"
        assert samples[2].graphql_delta is None
        assert samples[2].graphql_used == 900


def test_fetch_failure_on_stop_then_new_session_baseline_is_no_previous(monkeypatch):
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
    stopped = asyncio.run(controller.stop_session(session_id=session_id))
    assert stopped["sample"]["fetch_status"] == "fetch_failed"

    clock.advance(timedelta(minutes=1))
    controller._fetch = make_fetch(payload(core=resource(), graphql=resource(used=50, remaining=4950, reset=RESET_EPOCH), search=resource()))
    second = asyncio.run(controller.start_session(actor_type="claude_code", label="B", repository=None, pr_number=None))

    # Fail-closed continuity survives across a session boundary, not just
    # within one session's lifetime.
    assert second["sample"]["fetch_status"] == "no_previous"
    assert second["sample"]["graphql_delta"] is None


# ---------------------------------------------------------------------------
# regression guard — no GraphQL endpoint call anywhere in this test file
# ---------------------------------------------------------------------------


def test_no_graphql_endpoint_referenced_in_this_test_file():
    forbidden_graphql_path = "/" + "graphql"
    forbidden_graphql_subcommand = "api " + "graphql"
    source = Path(__file__).read_text(encoding="utf-8")
    assert forbidden_graphql_path not in source.lower()
    assert forbidden_graphql_subcommand not in source.lower()
