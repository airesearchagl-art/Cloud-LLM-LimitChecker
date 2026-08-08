"""GitHub Actions monthly billing — process-local refresh state.

Holds only in-memory, per-process state: no database, no persistence across
restarts (see app.github_rate_limit_state's module docstring for the same
multi-worker caveat, which applies identically here).

Unlike app.github_rate_limit_state, there is no reset-aware auto-refresh
here: a monthly billing entitlement does not need a precisely-timed re-check
the instant a window resets, and the task that requested this feature
explicitly calls out that billing data does not need sub-minute freshness
("秒単位更新不要"). Instead, `GET /api/github-actions-billing` always serves
whatever was last fetched (never triggers a fetch itself, exactly like
GitHubRateLimitController.snapshot), and a manual
`POST /api/github-actions-billing/refresh` is rate-limited by
DEFAULT_COOLDOWN_SECONDS (15 minutes by default) — this cooldown *is* the
cache: there is no separate "stale" TTL concept beyond it. `stale=True` in
the snapshot simply means "the last successful fetch predates the current
cooldown window", so the UI can show it's safe to offer another refresh.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.github_actions_billing import GitHubActionsBillingReport
from app.github_actions_billing_cli import (
    GitHubActionsBillingFetchResult,
    build_github_actions_billing_report,
)

DEFAULT_COOLDOWN_SECONDS = 900  # 15 minutes — see module docstring.


class GitHubActionsBillingRefreshCooldownError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"refresh is on cooldown for {retry_after_seconds} more second(s)")
        self.retry_after_seconds = retry_after_seconds


class GitHubActionsBillingRefreshInProgressError(RuntimeError):
    pass


def _report_to_dict(report: GitHubActionsBillingReport) -> dict:
    return {
        "status": report.status,
        "plan_name": report.plan_name,
        "included_minutes": report.included_minutes,
        "used_included_minutes": report.used_included_minutes,
        "remaining_minutes": report.remaining_minutes,
        "usage_percentage": report.usage_percentage,
        "overage_minutes": report.overage_minutes,
        "paid_non_included_minutes": report.paid_non_included_minutes,
        "billing_year": report.billing_year,
        "billing_month": report.billing_month,
        "collected_at": report.collected_at.isoformat(),
        "source": report.source,
        "skipped_unknown_skus": list(report.skipped_unknown_skus),
    }


class GitHubActionsBillingController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_report: GitHubActionsBillingReport | None = None
        self._last_success_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_error: tuple[str | None, str | None] | None = None
        self._last_fetch_succeeded = False

    def snapshot(self, *, now: datetime | None = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> dict:
        with self._lock:
            last_attempt_at = self._last_attempt_at.isoformat() if self._last_attempt_at else None
            last_success_at = self._last_success_at.isoformat() if self._last_success_at else None

            stale = False
            if self._last_success_at is not None:
                reference_now = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
                stale = (reference_now - self._last_success_at).total_seconds() > cooldown_seconds

            if self._last_fetch_succeeded and self._last_report is not None:
                return {
                    "fetched": True,
                    "refreshing": self._refreshing,
                    "stale": stale,
                    "live_validation_required": False,
                    **_report_to_dict(self._last_report),
                    "error": None,
                    "last_attempt_at": last_attempt_at,
                    "last_success_at": last_success_at,
                    "retry_after_seconds": 0,
                }

            error = None
            if self._last_error is not None:
                error_type, user_message = self._last_error
                error = {"error_type": error_type, "user_message": user_message}

            last_known = None
            if self._last_report is not None:
                last_known = {**_report_to_dict(self._last_report), "stale": True}

            return {
                "fetched": False,
                "refreshing": self._refreshing,
                "stale": last_known is not None,
                "live_validation_required": True,
                "status": None,
                "plan_name": None,
                "included_minutes": None,
                "used_included_minutes": None,
                "remaining_minutes": None,
                "usage_percentage": None,
                "overage_minutes": None,
                "paid_non_included_minutes": None,
                "billing_year": None,
                "billing_month": None,
                "collected_at": None,
                "source": None,
                "skipped_unknown_skus": [],
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
        fetch,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> dict:
        with self._lock:
            if self._refreshing:
                raise GitHubActionsBillingRefreshInProgressError()
            if self._last_attempt_at is not None:
                elapsed = (now - self._last_attempt_at).total_seconds()
                if elapsed < cooldown_seconds:
                    raise GitHubActionsBillingRefreshCooldownError(int(cooldown_seconds - elapsed) + 1)
            self._refreshing = True

        # The (potentially slow) subprocess calls happen with the lock
        # released, so a concurrent GET/snapshot is never blocked on them.
        try:
            result: GitHubActionsBillingFetchResult = fetch(now=now)
        except Exception:
            with self._lock:
                self._refreshing = False
                self._last_attempt_at = now
                self._last_fetch_succeeded = False
                self._last_error = (
                    "unknown",
                    "An unexpected error occurred while fetching GitHub Actions billing usage.",
                )
            raise

        report = build_github_actions_billing_report(result) if result.success else None

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
            return self.snapshot(now=now, cooldown_seconds=cooldown_seconds)
