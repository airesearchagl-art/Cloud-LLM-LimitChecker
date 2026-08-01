import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import codex_rate_limits_scheduler as scheduler_module
from app.codex_rate_limits_adapter import CodexRateLimitsFetchResult
from app.codex_rate_limits_cache import write_cache_atomic
from app.codex_rate_limits_scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CodexRateLimitsScheduler,
    auto_refresh_enabled_from_env,
    auto_refresh_interval_seconds_from_env,
)
from app.codex_rate_limits_state import (
    CodexRateLimitsController,
    CodexRateLimitsRefreshInProgressError,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_incrementing_clock(start: datetime, step: timedelta):
    """Each call returns the previous value then advances — so consecutive
    scheduler attempts never collide with the controller's own 30s cooldown
    (unlike a frozen `lambda: NOW`, which would make every attempt after the
    first look like it happened at the same instant as the last one)."""
    state = {"now": start}

    def clock() -> datetime:
        current = state["now"]
        state["now"] = state["now"] + step
        return current

    return clock

FIVE_HOUR_WINDOW = {
    "used_percentage": 40.0,
    "remaining_percentage": 60.0,
    "resets_at": "2999-01-01T00:00:00+00:00",
    "window_duration_minutes": 300,
}
WEEKLY_WINDOW = {
    "used_percentage": 20.0,
    "remaining_percentage": 80.0,
    "resets_at": "2999-01-08T00:00:00+00:00",
    "window_duration_minutes": 10080,
}


def fake_success(windows: dict):
    def fetch(*, now=None, **kwargs):
        return CodexRateLimitsFetchResult(
            success=True, windows=windows, error_type=None, user_message=None, collected_at=now
        )

    return fetch


def fake_failure(error_type: str, user_message: str):
    def fetch(*, now=None, **kwargs):
        return CodexRateLimitsFetchResult(
            success=False, windows=None, error_type=error_type, user_message=user_message, collected_at=now
        )

    return fetch


def fake_raise(exc: Exception):
    def fetch(*, now=None, **kwargs):
        raise exc

    return fetch


# ---------------------------------------------------------------------------
# 1-4: env設定の解釈
# ---------------------------------------------------------------------------


def test_default_interval_is_600_seconds():
    assert auto_refresh_interval_seconds_from_env({}) == 600
    assert DEFAULT_INTERVAL_SECONDS == 600


def test_env_overrides_interval():
    assert auto_refresh_interval_seconds_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_SECONDS": "120"}) == 120


@pytest.mark.parametrize("raw", ["1", "30", "59"])
def test_below_minimum_is_safely_corrected(raw):
    result = auto_refresh_interval_seconds_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_SECONDS": raw})
    assert result in (MIN_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS)
    assert result >= MIN_INTERVAL_SECONDS


@pytest.mark.parametrize("raw", ["not-a-number", "", "12.5", "None"])
def test_invalid_env_value_falls_back_to_default(raw):
    assert auto_refresh_interval_seconds_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_SECONDS": raw}) == DEFAULT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# 5-6: enabled default true / false
# ---------------------------------------------------------------------------


def test_enabled_default_true():
    assert auto_refresh_enabled_from_env({}) is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "NO", "off", "OFF"])
def test_enabled_false_values(raw):
    assert auto_refresh_enabled_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_ENABLED": raw}) is False


def test_enabled_true_for_other_values():
    assert auto_refresh_enabled_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_ENABLED": "yes"}) is True
    assert auto_refresh_enabled_from_env({"CLOUD_LLM_CODEX_AUTO_REFRESH_ENABLED": "1"}) is True


# ---------------------------------------------------------------------------
# 7-10, 28: lifecycle — module import starts nothing; start()/stop() idempotent
# ---------------------------------------------------------------------------


def test_constructing_scheduler_does_not_start_a_task(tmp_path):
    controller = CodexRateLimitsController()
    sched = CodexRateLimitsScheduler(
        controller=controller,
        fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
        cache_path=tmp_path / "c.json",
    )
    assert sched._task is None
    assert sched.status()["auto_refresh_running"] is False


def test_start_creates_exactly_one_task():
    async def scenario():
        controller = CodexRateLimitsController()
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
            cache_path=Path("unused-c.json"),
            interval_seconds=1000,
            sleep=lambda s: asyncio.sleep(0),
        )
        sched.start()
        task1 = sched._task
        sched.start()  # second call must be a no-op
        task2 = sched._task
        assert task1 is task2
        await sched.stop()

    asyncio.run(scenario())


def test_stop_cancels_task_and_clears_state():
    async def scenario():
        controller = CodexRateLimitsController()
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
            cache_path=Path("unused-c.json"),
            interval_seconds=1000,
            sleep=lambda s: asyncio.sleep(10),
        )
        sched.start()
        await asyncio.sleep(0)
        await sched.stop()
        assert sched._task is None
        assert sched.status()["auto_refresh_running"] is False

    asyncio.run(scenario())


def test_disabled_scheduler_never_starts():
    async def scenario():
        controller = CodexRateLimitsController()

        def explode(*, now=None, **kwargs):
            raise AssertionError("disabled scheduler must never call fetch")

        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=explode,
            cache_path=Path("unused-c.json"),
            enabled=False,
            interval_seconds=1000,
        )
        sched.start()
        assert sched._task is None
        await asyncio.sleep(0.05)
        await sched.stop()

    asyncio.run(scenario())


def test_repeated_start_stop_cycles_never_leave_a_task():
    async def scenario():
        controller = CodexRateLimitsController()
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
            cache_path=Path("unused-c.json"),
            interval_seconds=1000,
            sleep=lambda s: asyncio.sleep(0),
        )
        for _ in range(3):
            sched.start()
            await asyncio.sleep(0)
            await sched.stop()
        assert sched._task is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# initial wait: fresh cache waits a full cycle; missing/stale cache uses the
# short cold-start delay (<=60s), matching the "no rush if already fresh" rule
# ---------------------------------------------------------------------------


def test_initial_wait_uses_short_delay_when_no_cache(tmp_path):
    cache_path = tmp_path / "codex-rate-limits.json"
    controller = CodexRateLimitsController()
    sched = CodexRateLimitsScheduler(
        controller=controller,
        fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
        cache_path=cache_path,
        interval_seconds=600,
        clock=lambda: NOW,
    )
    wait = sched._initial_wait_seconds()
    assert wait == scheduler_module.INITIAL_DELAY_SECONDS
    assert wait <= 60


def test_initial_wait_uses_full_interval_when_cache_is_fresh(tmp_path):
    cache_path = tmp_path / "codex-rate-limits.json"
    record = {
        "schema_version": 1,
        "source": "codex_app_server",
        "observed_at": NOW.isoformat(),
        "five_hour": FIVE_HOUR_WINDOW,
        "weekly": WEEKLY_WINDOW,
    }
    write_cache_atomic(record, cache_path)

    controller = CodexRateLimitsController()
    sched = CodexRateLimitsScheduler(
        controller=controller,
        fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
        cache_path=cache_path,
        interval_seconds=600,
        clock=lambda: NOW,
    )
    wait = sched._initial_wait_seconds()
    assert wait == 600


def test_initial_wait_uses_short_delay_when_cache_is_stale(tmp_path):
    cache_path = tmp_path / "codex-rate-limits.json"
    record = {
        "schema_version": 1,
        "source": "codex_app_server",
        "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "five_hour": FIVE_HOUR_WINDOW,
        "weekly": None,
    }
    write_cache_atomic(record, cache_path)

    controller = CodexRateLimitsController()
    sched = CodexRateLimitsScheduler(
        controller=controller,
        fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
        cache_path=cache_path,
        interval_seconds=600,
        clock=lambda: NOW,
    )
    wait = sched._initial_wait_seconds()
    assert wait == scheduler_module.INITIAL_DELAY_SECONDS


# ---------------------------------------------------------------------------
# 11-12, 18-19, 25-26: one-shot adapter call per cycle, cache update semantics
# ---------------------------------------------------------------------------


def test_attempt_calls_the_one_shot_adapter_via_controller(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        call_count = {"n": 0}

        def counting_fetch(*, now=None, **kwargs):
            call_count["n"] += 1
            return CodexRateLimitsFetchResult(
                success=True,
                windows={"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW},
                error_type=None,
                user_message=None,
                collected_at=now,
            )

        sched = CodexRateLimitsScheduler(controller=controller, fetch=counting_fetch, cache_path=cache_path, clock=lambda: NOW)
        await sched._attempt()

        assert call_count["n"] == 1
        assert cache_path.exists()
        status = sched.status()
        assert status["last_auto_refresh_success_at"] is not None
        assert status["last_auto_refresh_attempt_at"] is not None
        assert status["last_auto_refresh_error_type"] is None

    asyncio.run(scenario())


def test_attempt_leaves_existing_cache_untouched_on_failure(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        good_record = {
            "schema_version": 1,
            "source": "codex_app_server",
            "observed_at": NOW.isoformat(),
            "five_hour": FIVE_HOUR_WINDOW,
            "weekly": WEEKLY_WINDOW,
        }
        write_cache_atomic(good_record, cache_path)
        before = cache_path.read_text(encoding="utf-8")

        controller = CodexRateLimitsController()
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_failure("rate_limits_timeout", "Fetching the Codex rate limit timed out."),
            cache_path=cache_path,
            clock=lambda: NOW,
        )
        await sched._attempt()

        after = cache_path.read_text(encoding="utf-8")
        assert before == after
        assert sched.status()["last_auto_refresh_error_type"] == "rate_limits_timeout"
        assert sched.status()["last_auto_refresh_success_at"] is None

    asyncio.run(scenario())


def test_attempt_leaves_existing_cache_untouched_on_exception(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        good_record = {
            "schema_version": 1,
            "source": "codex_app_server",
            "observed_at": NOW.isoformat(),
            "five_hour": FIVE_HOUR_WINDOW,
            "weekly": WEEKLY_WINDOW,
        }
        write_cache_atomic(good_record, cache_path)
        before = cache_path.read_text(encoding="utf-8")

        controller = CodexRateLimitsController()
        secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK"
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_raise(RuntimeError(f"boom {secret_marker}")),
            cache_path=cache_path,
            clock=lambda: NOW,
        )
        await sched._attempt()  # must not raise

        after = cache_path.read_text(encoding="utf-8")
        assert before == after
        status = sched.status()
        assert status["last_auto_refresh_error_type"] == "unknown_error"
        assert secret_marker not in str(status)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 13-14: manual/scheduler mutual exclusion
# ---------------------------------------------------------------------------


def test_attempt_skips_silently_when_controller_is_in_progress(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        with controller._lock:
            controller._refreshing = True  # simulate a manual refresh in flight

        def explode(*, now=None, **kwargs):
            raise AssertionError("fetch must not run while controller is already refreshing")

        sched = CodexRateLimitsScheduler(controller=controller, fetch=explode, cache_path=cache_path, clock=lambda: NOW)
        await sched._attempt()  # must not raise, must not call fetch

        assert not cache_path.exists()
        assert sched.status()["last_auto_refresh_error_type"] is None
        assert sched.status()["last_auto_refresh_success_at"] is None

    asyncio.run(scenario())


def test_attempt_skips_silently_during_cooldown(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        controller.refresh(
            now=NOW, fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}), cache_path=cache_path
        )

        def explode(*, now=None, **kwargs):
            raise AssertionError("fetch must not run during cooldown")

        sched = CodexRateLimitsScheduler(
            controller=controller, fetch=explode, cache_path=cache_path, clock=lambda: NOW + timedelta(seconds=5)
        )
        await sched._attempt()  # cooldown from the manual-style refresh above is still active

        assert sched.status()["last_auto_refresh_success_at"] is None

    asyncio.run(scenario())


def test_manual_refresh_sees_already_refreshing_while_scheduler_holds_the_lock():
    controller = CodexRateLimitsController()
    with controller._lock:
        controller._refreshing = True
    with pytest.raises(CodexRateLimitsRefreshInProgressError):
        controller.refresh(now=NOW, fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}))


# ---------------------------------------------------------------------------
# 15-16: exception/timeout isolation — the loop keeps running
# ---------------------------------------------------------------------------


def test_run_loop_continues_after_exception(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        call_count = {"n": 0}

        def flaky_fetch(*, now=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return CodexRateLimitsFetchResult(
                success=True,
                windows={"five_hour": FIVE_HOUR_WINDOW, "weekly": None},
                error_type=None,
                user_message=None,
                collected_at=now,
            )

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError()

        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=flaky_fetch,
            cache_path=cache_path,
            interval_seconds=1000,
            sleep=fake_sleep,
            clock=make_incrementing_clock(NOW, timedelta(seconds=1000)),
        )
        with pytest.raises(asyncio.CancelledError):
            await sched._run()

        assert call_count["n"] == 2
        assert cache_path.exists()

    asyncio.run(scenario())


def test_run_loop_continues_after_timeout_style_failure(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        call_count = {"n": 0}

        def timeout_then_success(*, now=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return CodexRateLimitsFetchResult(
                    success=False, windows=None, error_type="rate_limits_timeout", user_message="timed out", collected_at=now
                )
            return CodexRateLimitsFetchResult(
                success=True,
                windows={"five_hour": FIVE_HOUR_WINDOW, "weekly": None},
                error_type=None,
                user_message=None,
                collected_at=now,
            )

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 3:
                raise asyncio.CancelledError()

        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=timeout_then_success,
            cache_path=cache_path,
            interval_seconds=1000,
            sleep=fake_sleep,
            clock=make_incrementing_clock(NOW, timedelta(seconds=1000)),
        )
        with pytest.raises(asyncio.CancelledError):
            await sched._run()

        assert call_count["n"] == 2
        assert cache_path.exists()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# "1周期につき最大1回": between consecutive sleeps, exactly one attempt happens
# ---------------------------------------------------------------------------


def test_at_most_one_attempt_per_cycle(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        call_count = {"n": 0}

        def counting_fetch(*, now=None, **kwargs):
            call_count["n"] += 1
            return CodexRateLimitsFetchResult(
                success=True,
                windows={"five_hour": FIVE_HOUR_WINDOW, "weekly": None},
                error_type=None,
                user_message=None,
                collected_at=now,
            )

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) >= 4:
                raise asyncio.CancelledError()

        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=counting_fetch,
            cache_path=cache_path,
            interval_seconds=1000,
            sleep=fake_sleep,
            clock=make_incrementing_clock(NOW, timedelta(seconds=1000)),
        )
        with pytest.raises(asyncio.CancelledError):
            await sched._run()

        # one initial-wait sleep + 3 interval sleeps => 3 attempts happened
        assert call_count["n"] == 3

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# next_auto_refresh_at / attempt / success / error_type surfaced via status()
# ---------------------------------------------------------------------------


def test_status_fields_populate_correctly_on_success(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()
        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW}),
            cache_path=cache_path,
            interval_seconds=600,
            clock=lambda: NOW,
        )
        await sched._attempt()
        status = sched.status()

        assert status["last_auto_refresh_attempt_at"] == NOW.isoformat()
        assert status["last_auto_refresh_success_at"] == NOW.isoformat()
        assert status["last_auto_refresh_error_type"] is None

        for field in ("last_auto_refresh_attempt_at", "last_auto_refresh_success_at"):
            parsed = datetime.fromisoformat(status[field])
            assert parsed.tzinfo is not None

    asyncio.run(scenario())


def test_next_auto_refresh_at_is_set_after_run_starts(tmp_path):
    async def scenario():
        cache_path = tmp_path / "codex-rate-limits.json"
        controller = CodexRateLimitsController()

        async def fake_sleep(seconds):
            raise asyncio.CancelledError()

        sched = CodexRateLimitsScheduler(
            controller=controller,
            fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": None}),
            cache_path=cache_path,
            interval_seconds=600,
            sleep=fake_sleep,
            clock=lambda: NOW,
        )
        with pytest.raises(asyncio.CancelledError):
            await sched._run()

        assert sched.status()["next_auto_refresh_at"] is not None
        parsed = datetime.fromisoformat(sched.status()["next_auto_refresh_at"])
        assert parsed.tzinfo is not None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# safety: no token/traceback leakage, no reset-credit/task/thread/model methods
# ---------------------------------------------------------------------------


def test_scheduler_module_never_references_forbidden_methods():
    src = Path(scheduler_module.__file__).read_text(encoding="utf-8")
    for marker in ("rateLimitResetCredit", "task/create", "thread/create", "codex exec"):
        assert marker not in src


def test_scheduler_module_never_logs_env_values():
    src = Path(scheduler_module.__file__).read_text(encoding="utf-8")
    assert "print(" not in src
    assert "logging" not in src
