"""GitHub API Rate Limit Monitoring — process-local refresh state (Phase C/E backend).

Holds only in-memory, per-process state for the manual "refresh" feature and
the reset-aware single-shot auto-refresh: no database, no persistence across
restarts. If the app ever runs as multiple worker processes, each worker has
its own independent controller instance — cooldown, "currently refreshing",
and auto-refresh scheduling state are NOT shared across workers. That is an
accepted limitation for this phase, not an oversight.

A single `GitHubRateLimitController` instance is created once (see
`app.main`) and attached to `app.state`, so tests can swap in a fresh
instance per test instead of relying on module-level globals.

Auto-refresh (Phase E) design: after a *successful* fetch (manual or
automatic) that shows `core` or `graphql` as "Exhausted" or "Reset overdue",
`refresh()` / `maybe_run_auto_refresh()` arm at most one future attempt,
timed at that resource's `reset_at_utc` plus a small grace period — never a
polling loop, never a retry-until-success loop. `maybe_run_auto_refresh()`
is a plain, clock-injectable method with no `sleep` of its own: production
code (see `app.main`) is responsible for actually waking it up once, near
the scheduled time, via the real event loop's timer — this module only
decides *whether* an attempt is due "now" and *runs* it, so tests can drive
the whole state machine by passing fake `now` values instead of waiting.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable

from app.github_rate_limit import GitHubRateLimitReport, ResourceRateLimit
from app.github_rate_limit_cli import GitHubRateLimitFetchResult, build_github_rate_limit_report

DEFAULT_COOLDOWN_SECONDS = 30
AUTO_REFRESH_GRACE_SECONDS = 5
_AUTO_REFRESH_TRIGGER_STATUSES = ("Exhausted", "Reset overdue")
_AUTO_REFRESH_TARGET_RESOURCES = ("core", "graphql")


class GitHubRateLimitRefreshCooldownError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"refresh is on cooldown for {retry_after_seconds} more second(s)")
        self.retry_after_seconds = retry_after_seconds


class GitHubRateLimitRefreshInProgressError(RuntimeError):
    pass


def _resource_to_dict(resource: ResourceRateLimit) -> dict:
    return {
        "resource": resource.resource,
        "status": resource.status,
        "limit": resource.limit,
        "used": resource.used,
        "remaining": resource.remaining,
        "usage_percent": resource.usage_percent,
        "remaining_percent": resource.remaining_percent,
        "reset_at_utc": resource.reset_at_utc.isoformat() if resource.reset_at_utc else None,
        "reset_at_local": resource.reset_at_local.isoformat() if resource.reset_at_local else None,
        "seconds_until_reset": resource.seconds_until_reset,
        "error_message": resource.error_message,
    }


def _report_to_dict(report: GitHubRateLimitReport) -> dict:
    return {
        "resources": {name: _resource_to_dict(resource) for name, resource in report.resources.items()},
        "overall": {"status": report.overall.status, "reason": report.overall.reason},
        "collected_at": report.collected_at.isoformat(),
    }


def _select_auto_refresh_target_reset(report: GitHubRateLimitReport) -> datetime | None:
    """Earliest `reset_at_utc` among core/graphql resources needing a recheck.

    `search` is deliberately excluded — it is display-only for auto-refresh
    purposes. When both core and graphql qualify, the earliest reset is
    chosen: whichever resource's window ends first is the one worth
    rechecking right after, and a single scheduled attempt covers both
    (the other's data still gets refreshed as part of the same fetch).
    """
    candidates = [
        resource.reset_at_utc
        for name in _AUTO_REFRESH_TARGET_RESOURCES
        if (resource := report.resources.get(name)) is not None
        and resource.status in _AUTO_REFRESH_TRIGGER_STATUSES
        and resource.reset_at_utc is not None
    ]
    return min(candidates) if candidates else None


class GitHubRateLimitController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_report: GitHubRateLimitReport | None = None
        self._last_success_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_error: tuple[str | None, str | None] | None = None
        self._last_fetch_succeeded = False

        # Reset-aware single-shot auto-refresh (Phase E). See module docstring.
        self._next_auto_refresh_at: datetime | None = None
        self._scheduled_reset_at: datetime | None = None
        self._auto_refresh_pending = False
        self._last_auto_refresh_at: datetime | None = None
        self._last_auto_refresh_error: tuple[str | None, str | None] | None = None
        self._auto_refresh_attempted_for_reset: datetime | None = None

    def _reconcile_auto_refresh_schedule_locked(self, report: GitHubRateLimitReport) -> None:
        """Arm, keep, or clear the pending auto-refresh after a successful fetch.

        Caller must hold `self._lock`. At most one pending schedule exists at
        a time, keyed by the target `reset_at_utc` — a reset value already
        attempted (or already scheduled) is never scheduled again, which is
        what guarantees "at most once per reset" end to end.
        """
        target_reset = _select_auto_refresh_target_reset(report)
        if target_reset is None:
            self._auto_refresh_pending = False
            self._next_auto_refresh_at = None
            self._scheduled_reset_at = None
            return
        if self._auto_refresh_attempted_for_reset == target_reset:
            return
        if self._auto_refresh_pending and self._scheduled_reset_at == target_reset:
            return
        self._scheduled_reset_at = target_reset
        self._next_auto_refresh_at = target_reset + timedelta(seconds=AUTO_REFRESH_GRACE_SECONDS)
        self._auto_refresh_pending = True

    def snapshot(self) -> dict:
        with self._lock:
            last_attempt_at = self._last_attempt_at.isoformat() if self._last_attempt_at else None
            last_success_at = self._last_success_at.isoformat() if self._last_success_at else None

            auto_refresh_fields = {
                "next_auto_refresh_at": self._next_auto_refresh_at.isoformat() if self._next_auto_refresh_at else None,
                "auto_refresh_pending": self._auto_refresh_pending,
                "last_auto_refresh_at": self._last_auto_refresh_at.isoformat() if self._last_auto_refresh_at else None,
                "last_auto_refresh_error": (
                    {"error_type": self._last_auto_refresh_error[0], "user_message": self._last_auto_refresh_error[1]}
                    if self._last_auto_refresh_error is not None
                    else None
                ),
                "scheduled_reset_at": self._scheduled_reset_at.isoformat() if self._scheduled_reset_at else None,
            }

            if self._last_fetch_succeeded and self._last_report is not None:
                report_payload = _report_to_dict(self._last_report)
                return {
                    "fetched": True,
                    "refreshing": self._refreshing,
                    "stale": False,
                    "resources": report_payload["resources"],
                    "overall": report_payload["overall"],
                    "collected_at": report_payload["collected_at"],
                    "error": None,
                    "last_attempt_at": last_attempt_at,
                    "last_success_at": last_success_at,
                    "last_known": None,
                    "retry_after_seconds": 0,
                    **auto_refresh_fields,
                }

            error = None
            if self._last_error is not None:
                error_type, user_message = self._last_error
                error = {"error_type": error_type, "user_message": user_message}

            last_known = None
            if self._last_report is not None:
                last_known = {**_report_to_dict(self._last_report), "stale": True}

            never_attempted = self._last_attempt_at is None
            return {
                "fetched": False,
                "refreshing": self._refreshing,
                "stale": last_known is not None,
                "resources": None,
                "overall": {"status": "Unknown", "reason": "not fetched yet"} if never_attempted else None,
                "collected_at": None,
                "error": error,
                "last_attempt_at": last_attempt_at,
                "last_success_at": last_success_at,
                "last_known": last_known,
                "retry_after_seconds": 0,
                **auto_refresh_fields,
            }

    def refresh(
        self,
        *,
        now: datetime,
        fetch: Callable[..., GitHubRateLimitFetchResult],
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        tz: tzinfo = timezone.utc,
    ) -> dict:
        with self._lock:
            if self._refreshing:
                raise GitHubRateLimitRefreshInProgressError()
            if self._last_attempt_at is not None:
                elapsed = (now - self._last_attempt_at).total_seconds()
                if elapsed < cooldown_seconds:
                    raise GitHubRateLimitRefreshCooldownError(int(cooldown_seconds - elapsed) + 1)
            self._refreshing = True

        # The (potentially slow) subprocess call happens with the lock
        # released, so a concurrent GET/snapshot is never blocked on it.
        try:
            result = fetch(now=now)
        except Exception:
            with self._lock:
                self._refreshing = False
                self._last_attempt_at = now
                self._last_fetch_succeeded = False
                self._last_error = ("unknown", "An unexpected error occurred while fetching the GitHub rate limit.")
            raise

        report = build_github_rate_limit_report(result, tz=tz) if result.success else None

        with self._lock:
            self._refreshing = False
            self._last_attempt_at = now
            if result.success and report is not None:
                self._last_report = report
                self._last_success_at = now
                self._last_error = None
                self._last_fetch_succeeded = True
                self._reconcile_auto_refresh_schedule_locked(report)
            else:
                self._last_error = (result.error_type, result.user_message)
                self._last_fetch_succeeded = False
            return self.snapshot()

    def maybe_run_auto_refresh(
        self,
        *,
        now: datetime,
        fetch: Callable[..., GitHubRateLimitFetchResult],
        tz: tzinfo = timezone.utc,
    ) -> dict | None:
        """Run the single scheduled auto-refresh attempt if one is due.

        Exempt from the manual cooldown by design (it fires long after any
        cooldown window from the fetch that scheduled it), but still
        mutually exclusive with a manual `refresh()` via the same
        `_refreshing` flag. If a manual refresh is already in progress when
        this fires, the attempt is skipped and immediately marked as
        consumed for this reset — never retried — to keep the "at most once
        per reset" guarantee unconditional.

        Returns `None` when there was nothing to do (not pending, not due
        yet, or skipped due to a concurrent manual refresh); otherwise the
        same snapshot dict `refresh()` returns.
        """
        with self._lock:
            if not self._auto_refresh_pending or self._next_auto_refresh_at is None:
                return None
            if now < self._next_auto_refresh_at:
                return None
            target_reset = self._scheduled_reset_at
            if self._refreshing:
                self._auto_refresh_pending = False
                self._auto_refresh_attempted_for_reset = target_reset
                return None
            self._refreshing = True

        try:
            result = fetch(now=now)
        except Exception:
            with self._lock:
                self._refreshing = False
                self._auto_refresh_pending = False
                self._auto_refresh_attempted_for_reset = target_reset
                self._last_auto_refresh_at = now
                self._last_auto_refresh_error = (
                    "unknown",
                    "An unexpected error occurred while fetching the GitHub rate limit.",
                )
            raise

        report = build_github_rate_limit_report(result, tz=tz) if result.success else None

        with self._lock:
            self._refreshing = False
            self._auto_refresh_pending = False
            self._auto_refresh_attempted_for_reset = target_reset
            self._last_auto_refresh_at = now
            if result.success and report is not None:
                self._last_report = report
                self._last_success_at = now
                self._last_error = None
                self._last_fetch_succeeded = True
                self._last_auto_refresh_error = None
                self._reconcile_auto_refresh_schedule_locked(report)
            else:
                self._last_error = (result.error_type, result.user_message)
                self._last_fetch_succeeded = False
                self._last_auto_refresh_error = (result.error_type, result.user_message)
            return self.snapshot()
