import asyncio

import pytest

from app.github_graphql_diagnostics_sampler import GitHubGraphQLDiagnosticsSampler


# 1. start() creates exactly one task and is idempotent on a second call
def test_start_creates_exactly_one_task_and_is_idempotent():
    async def scenario():
        async def on_tick():
            return True

        sampler = GitHubGraphQLDiagnosticsSampler(
            interval_seconds=1000, on_tick=on_tick, sleep=lambda s: asyncio.sleep(0)
        )
        sampler.start()
        task1 = sampler._task
        sampler.start()  # second call must be a no-op
        task2 = sampler._task
        assert task1 is task2
        assert sampler.is_running is True
        await sampler.stop()

    asyncio.run(scenario())


# 2. stop() cancels and is idempotent
def test_stop_cancels_task_and_is_idempotent():
    async def scenario():
        async def on_tick():
            return True

        sampler = GitHubGraphQLDiagnosticsSampler(
            interval_seconds=1000, on_tick=on_tick, sleep=lambda s: asyncio.sleep(10)
        )
        sampler.start()
        await asyncio.sleep(0)
        await sampler.stop()
        assert sampler.is_running is False
        assert sampler._task is None
        # second stop() call must be a no-op, not raise
        await sampler.stop()
        assert sampler.is_running is False

    asyncio.run(scenario())


# 3. is_running reflects state before start / after stop
def test_is_running_false_before_start():
    async def on_tick():
        return True

    sampler = GitHubGraphQLDiagnosticsSampler(interval_seconds=10, on_tick=on_tick)
    assert sampler.is_running is False


# 4. on_tick is awaited once per interval over several simulated cycles
def test_on_tick_awaited_once_per_interval():
    async def scenario():
        tick_count = {"n": 0}

        async def on_tick():
            tick_count["n"] += 1
            return True

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:
                raise asyncio.CancelledError()

        sampler = GitHubGraphQLDiagnosticsSampler(interval_seconds=7, on_tick=on_tick, sleep=fake_sleep)
        sampler.start()
        with pytest.raises(asyncio.CancelledError):
            await sampler._task
        assert tick_count["n"] == 3
        assert sleep_calls == [7, 7, 7, 7]
        sampler._task = None  # already finished (raised) -- avoid double-await in stop()

    asyncio.run(scenario())


# 5. an exception raised inside on_tick does not kill the loop
def test_exception_in_on_tick_does_not_kill_the_loop():
    async def scenario():
        tick_count = {"n": 0}

        async def flaky_on_tick():
            tick_count["n"] += 1
            if tick_count["n"] == 1:
                raise RuntimeError("boom")
            return True

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError()

        sampler = GitHubGraphQLDiagnosticsSampler(interval_seconds=5, on_tick=flaky_on_tick, sleep=fake_sleep)
        sampler.start()
        with pytest.raises(asyncio.CancelledError):
            await sampler._task
        # tick 1 raised RuntimeError (swallowed) -- tick 2 still fired
        # afterwards, proving the exception did not kill the loop. The 3rd
        # sleep raises CancelledError before a 3rd on_tick can run.
        assert tick_count["n"] == 2
        sampler._task = None

    asyncio.run(scenario())


# 6. repeated start/stop cycles never leave a dangling task
def test_repeated_start_stop_cycles_never_leave_a_task():
    async def scenario():
        async def on_tick():
            return True

        sampler = GitHubGraphQLDiagnosticsSampler(
            interval_seconds=1000, on_tick=on_tick, sleep=lambda s: asyncio.sleep(0)
        )
        for _ in range(3):
            sampler.start()
            await asyncio.sleep(0)
            await sampler.stop()
        assert sampler._task is None
        assert sampler.is_running is False

    asyncio.run(scenario())


# 8. on_tick returning False stops the loop naturally (no cancellation),
#    is_running correctly reports False afterward, and a later start() call
#    works normally again -- the Finding 1 contract change this module's
#    docstring documents.
def test_on_tick_false_stops_loop_naturally_and_start_works_again():
    async def scenario():
        async def on_tick():
            return False

        sampler = GitHubGraphQLDiagnosticsSampler(
            interval_seconds=1000, on_tick=on_tick, sleep=lambda s: asyncio.sleep(0)
        )
        sampler.start()
        task = sampler._task
        # The task must complete normally (return, not raise) once on_tick
        # says "stop" -- no external cancellation is involved here at all.
        await asyncio.wait_for(task, timeout=5)
        assert sampler.is_running is False
        assert sampler._task is None

        # A later start() call is not left in some broken half-stopped
        # state -- it must create a fresh task and run normally again.
        sampler.start()
        assert sampler.is_running is True
        assert sampler._task is not task
        await sampler.stop()
        assert sampler.is_running is False

    asyncio.run(scenario())


# 9. this module has no business logic -- no DB / gh / session references
def test_module_is_business_logic_free():
    import inspect

    import app.github_graphql_diagnostics_sampler as sampler_module

    source = inspect.getsource(sampler_module)
    for marker in ("gh api", "subprocess", "crud.", "models.", "/graphql"):
        assert marker not in source
