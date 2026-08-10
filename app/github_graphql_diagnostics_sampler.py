"""GitHub GraphQL Consumption Diagnostics — generic periodic sampler (v0.1).

A GENERIC, business-logic-free async timer: no DB access, no `gh` calls, no
knowledge of sessions/diagnostics at all. Modeled on
`app.codex_rate_limits_scheduler.CodexRateLimitsScheduler`'s task lifecycle
(`asyncio.create_task` / cancel+await pattern), but simpler -- there is no
cold-start cache-freshness check here, only a plain fixed-interval loop.

All business logic (what a "tick" actually does -- fetching, persisting,
attribution) lives entirely in the injected `on_tick` callable, owned by
`app.github_graphql_diagnostics_controller.GitHubGraphQLDiagnosticsController`.
This module knows nothing about that logic and must stay that way.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class GitHubGraphQLDiagnosticsSampler:
    def __init__(
        self,
        *,
        interval_seconds: int,
        on_tick: Callable[[], Awaitable[None]],
        sleep: Callable[[float], "asyncio.Future"] | None = None,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._on_tick = on_tick
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        """Must be called from the event loop thread (an async caller).
        Idempotent -- a second call while already running is a no-op."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Idempotent. Cancels the task and awaits it."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        try:
            while True:
                await self._sleep(self._interval_seconds)
                try:
                    await self._on_tick()
                except Exception:
                    # One bad tick (fetch failure, DB error, whatever) must
                    # never kill the loop -- the caller's on_tick is
                    # responsible for its OWN error handling/recording; this
                    # is only an extra safety net.
                    pass
        except asyncio.CancelledError:
            raise
