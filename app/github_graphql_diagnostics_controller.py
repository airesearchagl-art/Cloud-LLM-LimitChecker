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

Locking discipline mirrors `app.github_rate_limit_state.GitHubRateLimitController`:
a `threading.RLock` protects the small critical sections that read/mutate
`self._last_sample_resource`, released before any blocking I/O (subprocess
calls, DB session use). The public methods are `async def` because their
callers are FastAPI route handlers; all actual blocking work is offloaded via
`asyncio.to_thread`.

This feature never calls GitHub's GraphQL API. It only ever runs
`gh api rate_limit` (via `app.github_rate_limit_cli.fetch_github_rate_limit`)
and, once per session start/stop/tick, `gh api rate_limit` again for the
periodic samples, plus `gh api user` once per session start (via
`app.github_graphql_diagnostics_identity.fetch_github_identity`) -- both REST
endpoints. No `/graphql` request is ever constructed anywhere in this module.

Known v0.1 simplification (documented here and in the implementation report):
after an auto-transition (max-duration or exhaustion) empties the active
session set during a scheduled tick, the sampler is intentionally NOT
stopped as a side effect of that tick. Stopping it would require awaiting
`self._sampler.stop()` from inside the coroutine the sampler itself is
currently running (`_scheduled_tick`, invoked as the sampler's `on_tick`),
which would mean cancelling-and-awaiting its own currently-running task from
within itself -- a self-referential shutdown that risks deadlocking or
corrupting the sampler's task bookkeeping. Instead, the sampler simply keeps
running after such a tick: subsequent ticks just observe zero active
sessions and record `UNATTRIBUTED` global samples, which is harmless. Only
an explicit `stop_session` call that reduces the active count to 0, or a
process restart, actually stops the sampler after this point.
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
        `self._last_sample_resource` on a successful fetch (left unchanged
        on failure, so the NEXT real sample still diffs against the last
        known-good value rather than against nothing).

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
                # self._last_sample_resource intentionally left unchanged.

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

            # The new session about to be created counts as active for this
            # baseline sample, since collected_at == started_at and
            # count_active_sessions_at's boundary rule counts a session as
            # active at the instant it starts.
            active_session_count_after = len(active) + 1
            sample_info = self._take_and_classify_sample(now=now, active_session_count=active_session_count_after)
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
            # Safe to call unconditionally here (this coroutine runs on the
            # event loop thread) -- GitHubGraphQLDiagnosticsSampler.start()
            # is itself idempotent, so a second concurrent session start
            # never creates a second sampler task.
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
        now = self._clock()
        result = await asyncio.to_thread(self._stop_session_sync, session_id=session_id, now=now)
        if result is None:
            raise GitHubGraphQLDiagnosticsSessionNotFoundError()

        if result["active_count_after"] == 0:
            await self._sampler.stop()

        return result["payload"]

    # -- scheduled tick -------------------------------------------------------

    def _scheduled_tick_sync(self, *, now: datetime) -> None:
        db = self._session_factory()
        try:
            active = crud.list_active_diagnostic_sessions(db)
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
                # left exactly as they are.
                return

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

            # See the class docstring's "Known v0.1 simplification" section:
            # even if the loop above just emptied the active set, the
            # sampler is deliberately NOT stopped here.
        finally:
            db.close()

    async def _scheduled_tick(self) -> None:
        """Invoked by `GitHubGraphQLDiagnosticsSampler` every
        `sample_seconds`, on the event loop thread (per the sampler's
        `start()` contract). All actual work is offloaded to a worker
        thread."""
        now = self._clock()
        await asyncio.to_thread(self._scheduled_tick_sync, now=now)

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
