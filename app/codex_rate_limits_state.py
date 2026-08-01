"""Codex App Server rate limit refresh — process-local state (cooldown, in-progress guard).

Mirrors `app.github_rate_limit_state.GitHubRateLimitController`: in-memory,
per-process only, no persistence across restarts, not shared across worker
processes if this app ever runs as more than one. This controller does not
store the rate limit *data* itself — that lives in the file-based cache
(`app.codex_rate_limits_cache`), so `GET /api/codex-rate-limits` keeps
working even before any refresh has run in the current process. This
controller only decides *whether* a refresh is currently allowed to run and
records the outcome of the last attempt.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.codex_rate_limits_adapter import CodexRateLimitsFetchResult
from app.codex_rate_limits_cache import (
    SCHEMA_VERSION,
    SOURCE_NAME,
    CacheValidationError,
    resolve_cache_path,
    validate_cache_record,
    write_cache_atomic,
)

DEFAULT_COOLDOWN_SECONDS = 30

_INVALID_FETCH_RESULT_ERROR = (
    "invalid_response",
    "Codex App Server response did not contain the expected rate limit structure.",
)
_UNEXPECTED_ERROR = ("unknown_error", "An unexpected error occurred while fetching the Codex rate limit.")


class CodexRateLimitsRefreshCooldownError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"refresh is on cooldown for {retry_after_seconds} more second(s)")
        self.retry_after_seconds = retry_after_seconds


class CodexRateLimitsRefreshInProgressError(RuntimeError):
    pass


class CodexRateLimitsController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refreshing = False
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: tuple[str | None, str | None] | None = None

    def status(self, *, now: datetime, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> dict:
        with self._lock:
            cooldown_remaining = 0
            if self._last_attempt_at is not None:
                elapsed = (now - self._last_attempt_at).total_seconds()
                if elapsed < cooldown_seconds:
                    cooldown_remaining = int(cooldown_seconds - elapsed) + 1
            error = None
            if self._last_error is not None:
                error_type, user_message = self._last_error
                error = {"error_type": error_type, "user_message": user_message}
            return {
                "refresh_in_progress": self._refreshing,
                "last_attempt_at": self._last_attempt_at.isoformat() if self._last_attempt_at else None,
                "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
                "last_error": error,
                "cooldown_remaining_seconds": cooldown_remaining,
            }

    def refresh(
        self,
        *,
        now: datetime,
        fetch: Callable[..., CodexRateLimitsFetchResult],
        cache_path: Path | None = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> dict:
        """Run one fetch attempt, subject to cooldown and mutual exclusion.

        Only writes the cache file on a *successful* fetch that also
        normalizes cleanly; a failed fetch never touches the existing cache
        (the last successful value, if any, is left exactly as-is).
        """
        with self._lock:
            if self._refreshing:
                raise CodexRateLimitsRefreshInProgressError()
            if self._last_attempt_at is not None:
                elapsed = (now - self._last_attempt_at).total_seconds()
                if elapsed < cooldown_seconds:
                    raise CodexRateLimitsRefreshCooldownError(int(cooldown_seconds - elapsed) + 1)
            self._refreshing = True

        # The (potentially slow) subprocess call happens with the lock
        # released, so a concurrent GET is never blocked on it.
        try:
            result = fetch(now=now)
        except Exception:
            with self._lock:
                self._refreshing = False
                self._last_attempt_at = now
                self._last_error = _UNEXPECTED_ERROR
            raise

        with self._lock:
            self._refreshing = False
            self._last_attempt_at = now
            if result.success:
                self._last_success_at = now
                self._last_error = None
            else:
                self._last_error = (result.error_type, result.user_message)

        if result.success and result.windows is not None:
            record = {
                "schema_version": SCHEMA_VERSION,
                "source": SOURCE_NAME,
                "observed_at": now.isoformat(),
                "five_hour": result.windows.get("five_hour"),
                "weekly": result.windows.get("weekly"),
            }
            try:
                validated = validate_cache_record(record, now=now)
                write_cache_atomic(validated, cache_path if cache_path is not None else resolve_cache_path())
            except CacheValidationError:
                # Defense-in-depth only: a successful fetch's windows are
                # already adapter-normalized, so this should not happen. If
                # it somehow does, don't write a bad cache and don't report
                # success either.
                with self._lock:
                    self._last_error = _INVALID_FETCH_RESULT_ERROR
                return {**self.status(now=now, cooldown_seconds=cooldown_seconds), "success": False}

        return {**self.status(now=now, cooldown_seconds=cooldown_seconds), "success": result.success}
