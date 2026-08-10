"""GitHub GraphQL Consumption Diagnostics — session-lifecycle controller (v0.1).

Owns the async-facing business logic that ties together:
- `app.github_graphql_diagnostics_identity` (a one-shot `gh api user` call
  made once per session start, to attach a login/user id to the session),
- `app.github_rate_limit_cli` (reused unchanged for `gh api rate_limit`),
- `app.github_graphql_diagnostics` (reused unchanged for all delta/
  attribution/reset-boundary/session-total math -- this module never
  reimplements any of it),
- `app.crud` / `app.models` (persistence),
- `app.github_graphql_diagnostics_sampler.GitHubGraphQLDiagnosticsSampler`
  (the generic periodic timer that drives `_scheduled_tick`).

Locking discipline: TWO separate locks, for two separate concerns.
- `self._operation_lock` (`asyncio.Lock`) serializes the ENTIRE body of
  `start_session` / `stop_session` / `_scheduled_tick` against each other,
  process-wide -- so at most one of these operations (and therefore at most
  one in-flight `gh api rate_limit` / `gh api user` call from this feature)
  is ever running at a time, and DB-visible decisions like "is there an
  active session from a different GitHub account" can never race against a
  concurrent operation that hasn't committed yet.
- `self._lock` (`threading.RLock`) protects the small critical section
  inside `_take_and_classify_sample` that reads/mutates
  `self._last_sample_resource`, released before any blocking I/O. This is
  now a secondary safety net (the operation lock above already guarantees
  only one caller reaches this section at a time) rather than the sole
  guarantee, but is kept for defense in depth.

The public methods are `async def` because their callers are FastAPI route
handlers; all actual blocking work (subprocess calls, DB session use) is
offloaded via `asyncio.to_thread`, so holding `self._operation_lock` across
an operation never blocks the rest of the app -- only another call into this
same feature's start/stop/tick operations queues behind it.

This feature never calls GitHub's GraphQL API. It only ever runs
`gh api rate_limit` (via `app.github_rate_limit_cli.fetch_github_rate_limit`,
for baseline/final/scheduled samples) and, once per session start, `gh api
user` (via `app.github_graphql_diagnostics_identity.fetch_github_identity`)
-- both REST endpoints. No `/graphql` request is ever constructed anywhere
in this module.

Sampler auto-stop: when the last ACTIVE session ends for any reason --
explicit `stop_session`, an auto-transition (`MAX_DURATION` /
`GRAPHQL_EXHAUSTED`) during a scheduled tick, or a tick that fires and finds
zero active sessions already (a race against one of the above) -- the
sampler is stopped, and no further `gh api rate_limit` calls are made until
a new session starts. This is achieved WITHOUT the sampler ever
cancelling-and-awaiting its own currently-running task from within itself
(which would risk deadlocking or corrupting its task bookkeeping): see
`app.github_graphql_diagnostics_sampler.GitHubGraphQLDiagnosticsSampler`'s
`on_tick -> bool` contract -- `_scheduled_tick` simply returns `False` when
there is nothing left to observe, and the sampler's own loop exits
naturally in response, clearing its task reference the same way an external
`stop()` call would.

Fetch-failure fail-closed: `_take_and_classify_sample` resets
`self._last_sample_resource` to `None` whenever a fetch fails, rather than
leaving the last known-good value in place. This means the NEXT successful
sample is always treated as a fresh baseline (delta=None,
outcome="no_previous") instead of being diffed against a sample from before
an unmeasured gap -- a gap that could span an unrelated session's start/end
boundary and would otherwise silently fold untracked consumption into what
looks like a precise, attributable delta.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable

from sqlalchemy.orm import Session

from app import crud, models
from app.database import SessionLocal
from app.github_graphql_diagnostics import (
    ActivityWindow,
    classify_attribution,
    compute_graphql_delta,
    compute_session_total_delta,
    count_active_sessions_at,
)
from app.github_graphql_diagnostics_config import (
    diagnostics_enabled_from_env,
    max_minutes_from_env,
    sample_seconds_from_env,
)
from app.github_graphql_diagnostics_identity import (
    GitHubIdentityFetchResult,
    fetch_github_identity,
)
from app.github_graphql_diagnostics_sampler import GitHubGraphQLDiagnosticsSampler
from app.github_rate_limit import ResourceRateLimit
from app.github_rate_limit_cli import (
    GitHubRateLimitFetchResult,
    build_github_rate_limit_report,
    fetch_github_rate_limit,
)


class GitHubGraphQLDiagnosticsDisabledError(RuntimeError):
    pass


class GitHubGraphQLDiagnosticsAccountContextChangedError(RuntimeError):
    pass


class GitHubGraphQLDiagnosticsIdentityFetchError(RuntimeError):
    def __init__(self, result: GitHubIdentityFetchResult) -> None:
        super().__init__(result.user_message or "GitHub identity fetch failed")
        self.result = result


class GitHubGraphQLDiagnosticsSessionNotFoundError(RuntimeError):
    pass


def _normalize_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime read back from the database.

    SQLite (used in dev/tests; see `app.database.DATABASE_URL`'s default)
    has no native timezone-aware storage type -- even a column declared
    `DateTime(timezone=True)` silently round-trips a tz-aware value as
    naive on that backend. Every datetime this controller ever writes is
    UTC (see `tz: tzinfo = timezone.utc` default and `self._clock()`), so a
    naive value read back is unambiguous: it is re-attached to UTC rather
    than left to raise or silently compare unequal against a freshly
    computed timezone-aware UTC value (e.g. `ResourceRateLimit.reset_at_utc`,
    `self._clock()`). An already-aware value (e.g. under Postgres, which
    does preserve the offset) is normalized to UTC too, so both backends
    behave identically from this module's point of view.
    """
    if value is None:
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _session_to_dict(session: models.GitHubDiagnosticSession) -> dict:
    started_at = _normalize_utc(session.started_at)
    ended_at = _normalize_utc(session.ended_at)
    reset_at_start = _normalize_utc(session.reset_at_start)
    return {
        "id": session.id,
        "actor_type": session.actor_type,
        "label": session.label,
        "repository": session.repository,
        "pr_number": session.pr_number,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "github_login": session.github_login,
        "github_user_id": session.github_user_id,
        "reset_at_start": reset_at_start.isoformat() if reset_at_start else None,
        "graphql_used_start": session.graphql_used_start,
        "graphql_used_end": session.graphql_used_end,
        "graphql_delta_total": session.graphql_delta_total,
        "attribution_status": session.attribution_status,
        "status": session.status,
        "stop_reason": session.stop_reason,
    }


def _sample_to_dict(sample: models.GitHubRateSample) -> dict:
    collected_at = _normalize_utc(sample.collected_at)
    graphql_reset_at = _normalize_utc(sample.graphql_reset_at)
    return {
        "id": sample.id,
        "collected_at": collected_at.isoformat() if collected_at else None,
        "core_used": sample.core_used,
        "graphql_used": sample.graphql_used,
        "search_used": sample.search_used,
        "graphql_limit": sample.graphql_limit,
        "graphql_remaining": sample.graphql_remaining,
        "graphql_reset_at": graphql_reset_at.isoformat() if graphql_reset_at else None,
        "graphql_delta": sample.graphql_delta,
        "fetch_status": sample.fetch_status,
        "attribution_status": sample.attribution_status,
        "trigger_session_id": sample.trigger_session_id,
    }


class GitHubGraphQLDiagnosticsController:
    def __init__(
        self,
        *,
        session_factory: Callable[[], "Session"] = SessionLocal,
        fetch: Callable[..., GitHubRateLimitFetchResult] = fetch_github_rate_limit,
        identity_fetch: Callable[..., GitHubIdentityFetchResult] = fetch_github_identity,
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo = timezone.utc,
        sample_seconds: int | None = None,
        max_minutes: int | None = None,
    ) -> None:
        self._lock = threading.RLock()
        # Serializes the ENTIRE body of start_session / stop_session /
        # _scheduled_tick against each other, process-wide -- not just the
        # small _last_sample_resource critical section below. Without this,
        # two concurrent operations (e.g. two overlapping `start` calls, or
        # a `start` racing a scheduled tick) could each independently read
        # "no active session yet" / fetch / decide account-context state
        # before either has committed, allowing more than one
        # gh api rate_limit (or gh api user) call in flight at once and, for
        # account-context checking specifically, letting two different
        # accounts' sessions slip past the mismatch check simultaneously.
        # `asyncio.Lock` (not `threading.Lock`) is correct here: every
        # acquire/release happens on the event loop thread (these are all
        # `async def` methods), while the actual blocking subprocess/DB work
        # inside the locked section is still offloaded via
        # `asyncio.to_thread` so other, unrelated requests are never stalled
        # by this lock -- only another call into this same feature's
        # start/stop/tick operations queues behind it.
        self._operation_lock = asyncio.Lock()
        self._session_factory = session_factory
        self._fetch = fetch
        self._identity_fetch = identity_fetch
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz = tz
        self._sample_seconds = sample_seconds if sample_seconds is not None else sample_seconds_from_env()
        self._max_minutes = max_minutes if max_minutes is not None else max_minutes_from_env()

        # Process-local cache of the most recent successfully-parsed
        # `graphql` ResourceRateLimit, used as `previous` for the NEXT delta
        # computation. This is a GLOBAL cursor (one per controller
        # instance/process), not per-session -- samples are a single global
        # timeline (see module docstring / spec).
        self._last_sample_resource: ResourceRateLimit | None = None

        self._sampler = GitHubGraphQLDiagnosticsSampler(
            interval_seconds=self._sample_seconds,
            on_tick=self._scheduled_tick,
        )

    @property
    def sampler_running(self) -> bool:
        return self._sampler.is_running

    async def shutdown(self) -> None:
        """Gracefully stop the sampler on app shutdown (called from FastAPI's
        lifespan `finally` block, mirroring `CodexRateLimitsScheduler.stop()`
        there). Idempotent -- a no-op if the sampler was never started."""
        await self._sampler.stop()

    def status_snapshot(self) -> dict:
        """Sync, cheap, DB read via a fresh session_factory() session (opened
        and closed locally). Returns plain dicts, never ORM objects, since
        the session is closed before this method returns."""
        db = self._session_factory()
        try:
            active_sessions = crud.list_active_diagnostic_sessions(db)
            last_sample = crud.get_latest_rate_sample(db)
            return {
                "enabled": diagnostics_enabled_from_env(),
                "sampler_running": self._sampler.is_running,
                "sample_seconds": self._sample_seconds,
                "max_minutes": self._max_minutes,
                "active_sessions": [_session_to_dict(session) for session in active_sessions],
                "last_sample": _sample_to_dict(last_sample) if last_sample is not None else None,
            }
        finally:
            db.close()

    # -- sampling -----------------------------------------------------------

    def _take_and_classify_sample(self, *, now: datetime, active_session_count: int) -> dict:
        """Runs on a worker thread (called only from inside an
        asyncio.to_thread-offloaded sync helper). Calls the injected
        `self._fetch` (blocking subprocess call), builds the Phase A report,
        computes the delta against `self._last_sample_resource` under
        `self._lock`, classifies attribution via
        `app.github_graphql_diagnostics.classify_attribution`, and updates
        `self._last_sample_resource` on a successful fetch. On failure it is
        reset to `None` (fail-closed), so the NEXT successful sample is
        always treated as a fresh baseline rather than being diffed across
        an unmeasured gap.

        The lock is held only around the small state read/mutate section,
        never across `self._fetch` itself -- same "release lock during I/O"
        discipline as `GitHubRateLimitController.refresh`.
        """
        result = self._fetch(now=now)
        report = build_github_rate_limit_report(result, tz=self._tz) if result.success else None
        graphql = report.resources["graphql"] if report is not None else None

        with self._lock:
            if graphql is not None:
                delta_result = compute_graphql_delta(self._last_sample_resource, graphql)
                fetch_status = delta_result.outcome
                attribution = classify_attribution(
                    delta_outcome=delta_result.outcome,
                    active_session_count=active_session_count,
                    fetch_failed=False,
                )
                delta_value = delta_result.delta
                self._last_sample_resource = graphql
            else:
                # The fetch itself failed (or produced no usable graphql
                # resource) -- delta_outcome doesn't apply in this branch.
                # "no_previous" is passed only to satisfy classify_attribution's
                # parameter type; it has no effect on the result because
                # fetch_failed=True takes unconditional precedence in that
                # function's precedence rules.
                delta_value = None
                fetch_status = "fetch_failed"
                attribution = classify_attribution(
                    delta_outcome="no_previous",
                    active_session_count=active_session_count,
                    fetch_failed=True,
                )
                # Fail-closed: a fetch failure invalidates delta continuity.
                # An unknown amount of GraphQL consumption could have
                # occurred during the gap this failure represents -- outside
                # any tracked activity window boundary -- so the NEXT
                # successful sample must never be diffed against whatever
                # was last known-good before this failure (that would
                # silently fold an unmeasured gap into what looks like a
                # precise, attributable delta). Resetting to None here means
                # the next successful sample is treated as "no_previous"
                # (a fresh baseline), exactly like the very first sample
                # this controller ever takes.
                self._last_sample_resource = None

        return {
            "report": report,
            "graphql": graphql,
            "delta": delta_value,
            "fetch_status": fetch_status,
            "attribution": attribution,
        }

    def _persist_sample(
        self,
        db: Session,
        *,
        now: datetime,
        sample_info: dict,
        trigger_session_id: int | None,
    ) -> models.GitHubRateSample:
        report = sample_info["report"]
        graphql = sample_info["graphql"]
        core = report.resources["core"] if report is not None else None
        search = report.resources["search"] if report is not None else None
        return crud.create_rate_sample(
            db,
            collected_at=now,
            core_used=core.used if core is not None else None,
            graphql_used=graphql.used if graphql is not None else None,
            search_used=search.used if search is not None else None,
            graphql_limit=graphql.limit if graphql is not None else None,
            graphql_remaining=graphql.remaining if graphql is not None else None,
            graphql_reset_at=graphql.reset_at_utc if graphql is not None else None,
            graphql_delta=sample_info["delta"],
            fetch_status=sample_info["fetch_status"],
            attribution_status=sample_info["attribution"],
            trigger_session_id=trigger_session_id,
        )

    def _auto_stop_session(
        self,
        db: Session,
        session: models.GitHubDiagnosticSession,
        *,
        now: datetime,
        graphql: ResourceRateLimit,
        stop_reason: str,
        status: str,
    ) -> None:
        crud.update_diagnostic_session(
            db,
            session,
            ended_at=now,
            graphql_used_end=graphql.used,
            graphql_delta_total=compute_session_total_delta(
                graphql_used_start=session.graphql_used_start,
                graphql_used_end=graphql.used,
                reset_at_start=_normalize_utc(session.reset_at_start),
                reset_at_end=graphql.reset_at_utc,
            ),
            status=status,
            stop_reason=stop_reason,
        )

    # -- start/stop -----------------------------------------------------------

    def _start_session_sync(
        self,
        *,
        actor_type: str,
        label: str,
        repository: str | None,
        pr_number: int | None,
        now: datetime,
        identity: GitHubIdentityFetchResult,
    ) -> dict:
        db = self._session_factory()
        try:
            active = crud.list_active_diagnostic_sessions(db)
            if active and any(existing.github_user_id != identity.user_id for existing in active):
                raise GitHubGraphQLDiagnosticsAccountContextChangedError(
                    "an existing active session belongs to a different GitHub account context"
                )

            # The baseline sample's delta represents the interval BEFORE
            # this new session existed (previous sample -> this instant) --
            # the new session was not yet active for any part of that
            # interval, so it must NOT be counted here. Counting it would
            # attribute pre-existing consumption to a session that hadn't
            # started yet. Only sessions that were already ACTIVE before
            # this call count toward this baseline sample's attribution;
            # the new session's own activity is only reflected in samples
            # taken AFTER it exists (the next scheduled tick, or its own
            # stop's final sample).
            sample_info = self._take_and_classify_sample(now=now, active_session_count=len(active))
            graphql = sample_info["graphql"]

            already_exhausted = graphql is not None and graphql.remaining == 0
            session = crud.create_diagnostic_session(
                db,
                actor_type=actor_type,
                label=label,
                repository=repository,
                pr_number=pr_number,
                started_at=now,
                github_login=identity.login,
                github_user_id=identity.user_id,
                reset_at_start=graphql.reset_at_utc if graphql is not None else None,
                graphql_used_start=graphql.used if graphql is not None else None,
                # v0.1 known simplification: the session's OWN
                # attribution_status is set to this baseline sample's
                # attribution and never updated again after creation. It may
                # become stale/not reflect the session's whole history by
                # the time it ends -- acceptable for v0.1, since the
                # authoritative per-instant attribution always lives on the
                # individual GitHubRateSample rows, not on the session row.
                attribution_status=sample_info["attribution"],
                status="EXHAUSTED" if already_exhausted else "ACTIVE",
            )

            if already_exhausted:
                # There is no quota left to observe change in -- starting a
                # nominally-ACTIVE session would be misleading, so it is
                # created already in its terminal EXHAUSTED state.
                session = crud.update_diagnostic_session(
                    db,
                    session,
                    stop_reason="GRAPHQL_EXHAUSTED",
                    ended_at=now,
                    graphql_used_end=graphql.used,
                    graphql_delta_total=compute_session_total_delta(
                        graphql_used_start=graphql.used,
                        graphql_used_end=graphql.used,
                        reset_at_start=graphql.reset_at_utc,
                        reset_at_end=graphql.reset_at_utc,
                    ),
                )

            sample = self._persist_sample(db, now=now, sample_info=sample_info, trigger_session_id=session.id)

            return {
                "session": _session_to_dict(session),
                "sample": _sample_to_dict(sample),
                "start_sampler": not already_exhausted,
            }
        finally:
            db.close()

    async def start_session(
        self,
        *,
        actor_type: str,
        label: str,
        repository: str | None,
        pr_number: int | None,
    ) -> dict:
        """Returns {"session": <GitHubDiagnosticSession fields as a dict>,
        "sample": <GitHubRateSample fields as a dict>} describing the newly
        created session and its baseline sample.

        Raises `GitHubGraphQLDiagnosticsDisabledError` if the feature is not
        enabled via config, `GitHubGraphQLDiagnosticsIdentityFetchError` if
        `gh api user` fails, or
        `GitHubGraphQLDiagnosticsAccountContextChangedError` if an existing
        ACTIVE session belongs to a different GitHub account -- in the
        latter two cases no session row is created and no existing session
        is touched.
        """
        if not diagnostics_enabled_from_env():
            raise GitHubGraphQLDiagnosticsDisabledError()

        async with self._operation_lock:
            identity = await asyncio.to_thread(self._identity_fetch)
            if not identity.success:
                raise GitHubGraphQLDiagnosticsIdentityFetchError(identity)

            now = self._clock()
            result = await asyncio.to_thread(
                self._start_session_sync,
                actor_type=actor_type,
                label=label,
                repository=repository,
                pr_number=pr_number,
                now=now,
                identity=identity,
            )

            if result["start_sampler"]:
                # Safe to call unconditionally here (this coroutine runs on
                # the event loop thread) -- GitHubGraphQLDiagnosticsSampler.
                # start() is itself idempotent, so a second concurrent
                # session start never creates a second sampler task. Also
                # still holding self._operation_lock here is what actually
                # guarantees "second concurrent" is impossible in the first
                # place -- see the lock's docstring in __init__.
                self._sampler.start()

        return {"session": result["session"], "sample": result["sample"]}

    def _stop_session_sync(self, *, session_id: int, now: datetime) -> dict | None:
        db = self._session_factory()
        try:
            session = crud.get_diagnostic_session(db, session_id)
            if session is None:
                return None

            if session.status != "ACTIVE":
                # Idempotent case: no new sample, no state change.
                active_count = crud.count_active_diagnostic_sessions(db)
                return {
                    "payload": {"session": _session_to_dict(session), "sample": None},
                    "active_count_after": active_count,
                }

            active = crud.list_active_diagnostic_sessions(db)
            sample_info = self._take_and_classify_sample(now=now, active_session_count=len(active))
            sample = self._persist_sample(db, now=now, sample_info=sample_info, trigger_session_id=session.id)
            graphql = sample_info["graphql"]

            graphql_used_end = graphql.used if graphql is not None else None
            reset_at_end = graphql.reset_at_utc if graphql is not None else None
            session = crud.update_diagnostic_session(
                db,
                session,
                ended_at=now,
                graphql_used_end=graphql_used_end,
                graphql_delta_total=compute_session_total_delta(
                    graphql_used_start=session.graphql_used_start,
                    graphql_used_end=graphql_used_end,
                    reset_at_start=_normalize_utc(session.reset_at_start),
                    reset_at_end=reset_at_end,
                ),
                status="STOPPED",
                stop_reason="USER_STOP",
            )

            active_count_after = crud.count_active_diagnostic_sessions(db)
            return {
                "payload": {"session": _session_to_dict(session), "sample": _sample_to_dict(sample)},
                "active_count_after": active_count_after,
            }
        finally:
            db.close()

    async def stop_session(self, *, session_id: int) -> dict:
        """Same return shape as `start_session`, except `"sample"` is `None`
        for the idempotent already-stopped case (no new sample is taken).

        Raises `GitHubGraphQLDiagnosticsSessionNotFoundError` if `session_id`
        does not exist.
        """
        async with self._operation_lock:
            now = self._clock()
            result = await asyncio.to_thread(self._stop_session_sync, session_id=session_id, now=now)
            if result is None:
                raise GitHubGraphQLDiagnosticsSessionNotFoundError()

            if result["active_count_after"] == 0:
                await self._sampler.stop()

        return result["payload"]

    # -- scheduled tick -------------------------------------------------------

    def _scheduled_tick_sync(self, *, now: datetime) -> bool:
        """Returns whether the sampler should keep running. `False` means:
        active sessions were already zero at the very start of this tick (a
        race between this tick firing and the last session ending some
        other way), or they became zero as a result of this tick's own
        auto-transitions below -- in either case, there is nothing left to
        observe and no `gh api rate_limit` call is made (or, if one was
        already made this tick, no further ticks will be scheduled)."""
        db = self._session_factory()
        try:
            active = crud.list_active_diagnostic_sessions(db)
            if not active:
                # Race: the last active session ended (explicit stop, or an
                # auto-transition from a previous tick that -- per the
                # known v0.1 simplification -- didn't stop the sampler
                # itself) between this tick being scheduled and firing.
                # Nothing to sample for; skip the fetch entirely and signal
                # the sampler to stop.
                return False

            windows = [
                ActivityWindow(started_at=_normalize_utc(session.started_at), ended_at=_normalize_utc(session.ended_at))
                for session in active
            ]
            active_session_count = count_active_sessions_at(now, windows)

            sample_info = self._take_and_classify_sample(now=now, active_session_count=active_session_count)
            self._persist_sample(db, now=now, sample_info=sample_info, trigger_session_id=None)

            graphql = sample_info["graphql"]
            if graphql is None:
                # A single transient fetch failure must never auto-stop an
                # in-progress session in v0.1 -- the sample above is already
                # recorded with fetch_status="fetch_failed" and
                # attribution_status="FETCH_FAILED"; active sessions are
                # left exactly as they are. Sessions are still present, so
                # sampling continues.
                return True

            exhausted = graphql.remaining == 0
            for session in active:
                elapsed = now - _normalize_utc(session.started_at)
                if elapsed >= timedelta(minutes=self._max_minutes):
                    self._auto_stop_session(
                        db, session, now=now, graphql=graphql, stop_reason="MAX_DURATION", status="AUTO_STOPPED"
                    )
                elif exhausted:
                    self._auto_stop_session(
                        db, session, now=now, graphql=graphql, stop_reason="GRAPHQL_EXHAUSTED", status="EXHAUSTED"
                    )

            # Re-check: if the auto-transitions above just emptied the
            # active set, signal the sampler to stop -- no self-referential
            # cancel-from-within-self needed (see
            # GitHubGraphQLDiagnosticsSampler's on_tick contract).
            return crud.count_active_diagnostic_sessions(db) > 0
        finally:
            db.close()

    async def _scheduled_tick(self) -> bool:
        """Invoked by `GitHubGraphQLDiagnosticsSampler` every
        `sample_seconds`, on the event loop thread (per the sampler's
        `start()` contract). All actual work is offloaded to a worker
        thread. Returns whether the sampler should keep running (see
        `_scheduled_tick_sync`'s docstring)."""
        async with self._operation_lock:
            now = self._clock()
            return await asyncio.to_thread(self._scheduled_tick_sync, now=now)

    # -- startup reconciliation -------------------------------------------------

    def _reconcile_on_startup_sync(self) -> dict:
        db = self._session_factory()
        try:
            now = self._clock()
            active = crud.list_active_diagnostic_sessions(db)
            for session in active:
                crud.update_diagnostic_session(
                    db,
                    session,
                    status="ABORTED",
                    stop_reason="PROCESS_RESTART",
                    ended_at=now,
                    graphql_used_end=None,
                    graphql_delta_total=None,
                )
            return {"aborted_session_count": len(active)}
        finally:
            db.close()

    async def reconcile_on_startup(self) -> dict:
        """Call once, from the FastAPI lifespan startup (the caller wires
        this up; this method only implements the reconciliation itself).

        Any session left with status=="ACTIVE" in the DB is a leftover from
        a prior process that didn't shut down cleanly -- it is marked
        status="ABORTED", stop_reason="PROCESS_RESTART", with
        graphql_used_end and graphql_delta_total left `None` (the old
        process's fetch context is gone; no final sample is attempted or
        fabricated for it).

        Never starts the sampler as a side effect -- per v0.1 policy, the
        sampler only starts in response to a NEW `start_session` call after
        a restart, never automatically.
        """
        return await asyncio.to_thread(self._reconcile_on_startup_sync)
