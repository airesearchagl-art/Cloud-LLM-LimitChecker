"""Background periodic refresh for Codex App Server rate limits.

Owns only: startup, periodic sleep, delegating one fetch attempt per cycle to
the existing `CodexRateLimitsController.refresh` (which in turn calls the
existing one-shot `app.codex_rate_limits_adapter.fetch_codex_rate_limits`),
shutdown, exception isolation, and exposing its own status. It never parses
JSON-RPC itself, never touches authentication, never persists stdout/stderr,
and never calls any account-mutating or task/thread/model method — all of
that already lives in `app.codex_rate_limits_adapter` /
`app.codex_rate_limits_cache` / `app.codex_rate_limits_state`, reused here
completely unchanged.

Process-local only: this app is designed for a single uvicorn worker. Running
with `--workers N>1` would start N independent schedulers, each polling
Codex App Server on its own uncoordinated schedule — not deduplicated, not
locked against each other. No distributed lock is implemented in this phase;
multi-worker deployments are explicitly out of scope (see
`docs/codex-rate-limits-auto.md`).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from app import codex_rate_limits_cache
from app.codex_rate_limits_adapter import CodexRateLimitsFetchResult
from app.codex_rate_limits_state import (
    CodexRateLimitsController,
    CodexRateLimitsRefreshCooldownError,
    CodexRateLimitsRefreshInProgressError,
)

DEFAULT_INTERVAL_SECONDS = 600
MIN_INTERVAL_SECONDS = 60
INITIAL_DELAY_SECONDS = 30

ENV_ENABLED = "CLOUD_LLM_CODEX_AUTO_REFRESH_ENABLED"
ENV_INTERVAL_SECONDS = "CLOUD_LLM_CODEX_AUTO_REFRESH_SECONDS"

_FALSE_VALUES = {"0", "false", "no", "off"}


def auto_refresh_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Default true. Only a recognized falsy string disables it."""
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_ENABLED)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def auto_refresh_interval_seconds_from_env(env: Mapping[str, str] | None = None) -> int:
    """Default 600. Unparseable values fall back to the default; values below
    the 60s floor are clamped up to it rather than silently ignored."""
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_INTERVAL_SECONDS)
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return DEFAULT_INTERVAL_SECONDS
    if value < MIN_INTERVAL_SECONDS:
        return MIN_INTERVAL_SECONDS
    return value


class CodexRateLimitsScheduler:
    def __init__(
        self,
        *,
        controller: CodexRateLimitsController,
        fetch: Callable[..., CodexRateLimitsFetchResult],
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        cache_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], "asyncio.Future"] | None = None,
    ) -> None:
        self._controller = controller
        self._fetch = fetch
        self._enabled = enabled if enabled is not None else auto_refresh_enabled_from_env()
        self._interval_seconds = (
            interval_seconds if interval_seconds is not None else auto_refresh_interval_seconds_from_env()
        )
        self._cache_path = cache_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep

        self._task: asyncio.Task | None = None
        self._running = False
        self._next_attempt_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_type: str | None = None

    def status(self) -> dict:
        return {
            "auto_refresh_enabled": self._enabled,
            "auto_refresh_interval_seconds": self._interval_seconds,
            "auto_refresh_running": self._running,
            "next_auto_refresh_at": self._next_attempt_at.isoformat() if self._next_attempt_at else None,
            "last_auto_refresh_attempt_at": self._last_attempt_at.isoformat() if self._last_attempt_at else None,
            "last_auto_refresh_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "last_auto_refresh_error_type": self._last_error_type,
        }

    def _resolved_cache_path(self) -> Path:
        return self._cache_path if self._cache_path is not None else codex_rate_limits_cache.resolve_cache_path()

    def start(self) -> None:
        """Idempotent: a second call while a task is already running is a no-op.

        Never called at module import time — only from the FastAPI lifespan
        startup phase (see `app.main`), and only if `enabled` is true.
        """
        if self._task is not None:
            return
        if not self._enabled:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancels the background task and waits for it to actually finish."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._running = False

    def _initial_wait_seconds(self) -> float:
        now = self._clock()
        try:
            snapshot = codex_rate_limits_cache.load_snapshot(now=now, path=self._cache_path)
        except Exception:
            snapshot = {"available": False, "stale": True}
        if snapshot.get("available") and not snapshot.get("stale"):
            # Already fresh — no need to rush; wait out a full cycle instead
            # of the short cold-start delay.
            return float(self._interval_seconds)
        return float(INITIAL_DELAY_SECONDS)

    async def _run(self) -> None:
        self._running = True
        try:
            wait_seconds = self._initial_wait_seconds()
            self._next_attempt_at = self._clock() + timedelta(seconds=wait_seconds)
            await self._sleep(wait_seconds)
            while True:
                await self._attempt()
                self._next_attempt_at = self._clock() + timedelta(seconds=self._interval_seconds)
                await self._sleep(self._interval_seconds)
        finally:
            self._running = False

    async def _attempt(self) -> None:
        """One refresh cycle. Never raises — every outcome (skip, failure,
        success) is isolated here so the loop itself can never die from it."""
        now = self._clock()
        self._last_attempt_at = now
        try:
            result = await asyncio.to_thread(
                self._controller.refresh,
                now=now,
                fetch=self._fetch,
                cache_path=self._resolved_cache_path(),
            )
        except (CodexRateLimitsRefreshCooldownError, CodexRateLimitsRefreshInProgressError):
            # A manual refresh currently owns the controller (or this
            # scheduler's own previous attempt is still within cooldown) —
            # skip this cycle silently. Not a failure: the existing cache and
            # `last_error_type` are left exactly as they were.
            return
        except Exception:
            self._last_error_type = "unknown_error"
            return

        if result.get("success"):
            self._last_success_at = now
            self._last_error_type = None
        else:
            error = result.get("last_error") or {}
            self._last_error_type = error.get("error_type")
