"""GitHub API Rate Limit Monitoring — process-local refresh state (Phase C backend).

Holds only in-memory, per-process state for the manual "refresh" feature: no
database, no persistence across restarts. If the app ever runs as multiple
worker processes, each worker has its own independent controller instance —
cooldown and "currently refreshing" state are NOT shared across workers.
That is an accepted limitation for this phase, not an oversight.

A single `GitHubRateLimitController` instance is created once (see
`app.main`) and attached to `app.state`, so tests can swap in a fresh
instance per test instead of relying on module-level globals.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone, tzinfo
from typing import Callable

from app.github_rate_limit import GitHubRateLimitReport, ResourceRateLimit
from app.github_rate_limit_cli import GitHubRateLimitFetchResult, build_github_rate_limit_report

DEFAULT_COOLDOWN_SECONDS = 30


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


class GitHubRateLimitController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_report: GitHubRateLimitReport | None = None
        self._last_success_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_error: tuple[str | None, str | None] | None = None
        self._last_fetch_succeeded = False

    def snapshot(self) -> dict:
        with self._lock:
            last_attempt_at = self._last_attempt_at.isoformat() if self._last_attempt_at else None
            last_success_at = self._last_success_at.isoformat() if self._last_success_at else None

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
            else:
                self._last_error = (result.error_type, result.user_message)
                self._last_fetch_succeeded = False
            return self.snapshot()
