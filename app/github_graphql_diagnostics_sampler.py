"""GitHub GraphQL Consumption Diagnostics — generic periodic sampler (v0.1).

A GENERIC, business-logic-free async timer: no DB access, no `gh` calls, no
knowledge of sessions/diagnostics at all. Modeled on
`app.codex_rate_limits_scheduler.CodexRateLimitsScheduler`'s task lifecycle
(`asyncio.create_task` / cancel+await pattern), but simpler -- there is no
cold-start cache-freshness check here, only a plain fixed-interval loop.

All business logic (what a "tick" actually does -- fetching, persisting,
attribution, deciding whether sampling should continue) lives entirely in the
injected `on_tick` callable, owned by
`app.github_graphql_diagnostics_controller.GitHubGraphQLDiagnosticsController`.
This module knows nothing about that logic and must stay that way.

`on_tick` returns a `bool`: `True` means "keep sampling", `False` means
"nothing left to observe -- stop the loop". This lets the controller signal
"the last active session just ended (auto-stop / exhaustion / a race that
found zero active sessions at tick start)" WITHOUT the sampler ever needing
to cancel-and-await its own currently-running task from within itself (a
self-referential shutdown that would risk deadlocking or corrupting the
task's own bookkeeping — see the controller's module docstring for the
incident this replaces). When `on_tick` returns `False`, `_run` simply
returns normally and its `finally` clears `self._task`, so `is_running`
correctly reports `False` afterward and a later `start()` call creates a
fresh task exactly as if `stop()` had been called externally.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class GitHubGraphQLDiagnosticsSampler:
    def __init__(
        self,
        *,
        interval_seconds: int,
        on_tick: Callable[[], Awaitable[bool]],
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
        """Idempotent. Cancels the task and awaits it. Also safe to call
        after the loop already ended naturally (on_tick returned False) --
        `self._task` is already `None` in that case, so this is a no-op."""
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
                    should_continue = await self._on_tick()
                except Exception:
                    # One bad tick (fetch failure, DB error, whatever) must
                    # never kill the loop -- the caller's on_tick is
                    # responsible for its OWN error handling/recording; this
                    # is only an extra safety net. An unexpected exception
                    # here is treated as "keep trying next cycle", not as a
                    # signal to stop.
                    should_continue = True
                if not should_continue:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            # Reached on a natural `return` above (on_tick said stop) as
            # well as on cancellation -- either way, this task is done, so
            # clear the reference now rather than only inside `stop()`.
            # `stop()` calling `self._task = None` again afterward is
            # harmless (idempotent).
            self._task = None
