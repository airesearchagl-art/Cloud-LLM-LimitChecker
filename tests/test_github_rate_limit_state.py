from datetime import datetime, timedelta, timezone

import pytest

from app.github_rate_limit_cli import GitHubRateLimitFetchResult
from app.github_rate_limit_state import (
    AUTO_REFRESH_GRACE_SECONDS,
    GitHubRateLimitController,
    GitHubRateLimitRefreshInProgressError,
)

RESET_AT = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
RESET_EPOCH = int(RESET_AT.timestamp())
GRACE = timedelta(seconds=AUTO_REFRESH_GRACE_SECONDS)
DUE_AT = RESET_AT + GRACE
BEFORE_REFRESH_TIME = RESET_AT - timedelta(seconds=100)


def resource(limit=5000, used=100, remaining=4900, reset=None):
    if reset is None:
        reset = RESET_EPOCH + 3600
    return {"limit": limit, "used": used, "remaining": remaining, "reset": reset}


def payload(core=None, graphql=None, search=None):
    resources = {}
    if core is not None:
        resources["core"] = core
    if graphql is not None:
        resources["graphql"] = graphql
    if search is not None:
        resources["search"] = search
    return {"resources": resources}


def fake_fetch(result_payload=None, *, error=None):
    def fetch(*, now=None, **kwargs):
        if error is not None:
            error_type, message = error
            return GitHubRateLimitFetchResult(
                success=False, payload=None, error_type=error_type, user_message=message, return_code=1, collected_at=now
            )
        return GitHubRateLimitFetchResult(
            success=True, payload=result_payload, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    return fetch


# 1. Normalでは予約しない
def test_normal_does_not_schedule_auto_refresh():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=4900), graphql=resource(remaining=4900))),
    )
    assert snapshot["auto_refresh_pending"] is False
    assert snapshot["next_auto_refresh_at"] is None
    assert snapshot["scheduled_reset_at"] is None


# 2. Warningでは予約しない
def test_warning_does_not_schedule_auto_refresh():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(limit=1000, remaining=150), graphql=resource())),
    )
    assert snapshot["auto_refresh_pending"] is False


# 3. core Exhaustedで予約する
def test_core_exhausted_schedules_auto_refresh():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    assert snapshot["auto_refresh_pending"] is True
    assert snapshot["scheduled_reset_at"] == RESET_AT.isoformat()
    assert snapshot["next_auto_refresh_at"] == DUE_AT.isoformat()


# 4. graphql Exhaustedで予約する
def test_graphql_exhausted_schedules_auto_refresh():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(), graphql=resource(remaining=0, reset=RESET_EPOCH))),
    )
    assert snapshot["auto_refresh_pending"] is True
    assert snapshot["scheduled_reset_at"] == RESET_AT.isoformat()


# 5. 両方Exhaustedでは最も早いresetを採用する（方針: 最小値/早い方を選ぶ）
def test_both_exhausted_picks_earliest_reset():
    later_reset = RESET_EPOCH + 300
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(
            payload(
                core=resource(remaining=0, reset=later_reset),
                graphql=resource(remaining=0, reset=RESET_EPOCH),
            )
        ),
    )
    assert snapshot["scheduled_reset_at"] == RESET_AT.isoformat()


# 6. reset + grace前は実行しない
def test_auto_refresh_does_not_run_before_due_time():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    result = controller.maybe_run_auto_refresh(now=DUE_AT - timedelta(seconds=1), fetch=fake_fetch(payload()))
    assert result is None
    assert controller.snapshot()["auto_refresh_pending"] is True


# 7. reset + grace到達時に1回実行
def test_auto_refresh_runs_exactly_at_due_time():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    new_payload = payload(core=resource(remaining=4900, reset=RESET_EPOCH + 3600), graphql=resource())
    result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fake_fetch(new_payload))
    assert result is not None
    assert result["fetched"] is True
    assert result["auto_refresh_pending"] is False
    assert result["last_auto_refresh_at"] == DUE_AT.isoformat()


# 8. 1マイクロ秒前では実行しない
def test_auto_refresh_does_not_run_one_microsecond_before_due():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    result = controller.maybe_run_auto_refresh(
        now=DUE_AT - timedelta(microseconds=1), fetch=fake_fetch(payload())
    )
    assert result is None


# 9. 同一resetで2回実行しない
def test_auto_refresh_does_not_run_twice_for_same_reset():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    call_count = {"n": 0}

    def counting_fetch(*, now=None, **kwargs):
        call_count["n"] += 1
        return GitHubRateLimitFetchResult(
            success=True,
            payload=payload(core=resource(remaining=4900, reset=RESET_EPOCH + 3600), graphql=resource()),
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    first = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=counting_fetch)
    second = controller.maybe_run_auto_refresh(now=DUE_AT + timedelta(seconds=1), fetch=counting_fetch)
    assert first is not None
    assert second is None
    assert call_count["n"] == 1


# 10. refresh後も同一resetでremaining=0なら再試行しない
def test_no_retry_when_reset_value_unchanged_after_auto_refresh():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    still_exhausted_same_reset = payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())
    result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fake_fetch(still_exhausted_same_reset))
    assert result["auto_refresh_pending"] is False
    assert result["scheduled_reset_at"] is None or result["scheduled_reset_at"] == RESET_AT.isoformat()
    # Pending must not be re-armed for the identical reset value.
    assert controller.snapshot()["auto_refresh_pending"] is False


# 11. 新しいreset値なら再予約できる
def test_new_reset_value_can_be_rescheduled():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    new_reset_epoch = RESET_EPOCH + 1000
    exhausted_new_reset = payload(core=resource(remaining=0, reset=new_reset_epoch), graphql=resource())
    result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fake_fetch(exhausted_new_reset))
    assert result["auto_refresh_pending"] is True
    assert result["scheduled_reset_at"] == datetime.fromtimestamp(new_reset_epoch, tz=timezone.utc).isoformat()


# 12. 自動refresh失敗後も同一resetで再試行しない
def test_no_retry_after_auto_refresh_failure():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    result = controller.maybe_run_auto_refresh(
        now=DUE_AT, fetch=fake_fetch(error=("timeout", "Fetching timed out."))
    )
    assert result["auto_refresh_pending"] is False
    assert result["last_auto_refresh_error"] == {"error_type": "timeout", "user_message": "Fetching timed out."}
    again = controller.maybe_run_auto_refresh(now=DUE_AT + timedelta(seconds=5), fetch=fake_fetch(payload()))
    assert again is None


# 13. 自動refresh中の手動refresh重複防止
def test_manual_refresh_blocked_during_auto_refresh():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )

    def fetch_that_tries_manual_refresh(*, now=None, **kwargs):
        with pytest.raises(GitHubRateLimitRefreshInProgressError):
            controller.refresh(now=now, fetch=fake_fetch(payload()))
        return GitHubRateLimitFetchResult(
            success=True,
            payload=payload(core=resource(remaining=4900, reset=RESET_EPOCH + 3600), graphql=resource()),
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fetch_that_tries_manual_refresh)
    assert result is not None
    assert controller.snapshot()["refreshing"] is False


# 14. 手動refresh中の自動refresh重複防止
def test_auto_refresh_blocked_during_manual_refresh():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )

    def manual_fetch_that_tries_auto_refresh(*, now=None, **kwargs):
        auto_result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fake_fetch(payload()))
        assert auto_result is None
        return GitHubRateLimitFetchResult(
            success=True,
            payload=payload(core=resource(remaining=4900, reset=RESET_EPOCH + 3600), graphql=resource()),
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    controller.refresh(now=DUE_AT, fetch=manual_fetch_that_tries_auto_refresh)
    # The skipped auto-refresh attempt must be consumed, not left pending forever.
    assert controller.snapshot()["auto_refresh_pending"] is False


# 15. GETだけでは予約・実行しない
def test_snapshot_alone_never_schedules_or_runs():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    before = controller.snapshot()
    for _ in range(3):
        after = controller.snapshot()
        assert after == before


# 16. ページロードだけではfetchしない（API層で確認済み、controller層でも明示）
def test_fresh_controller_snapshot_has_no_scheduled_auto_refresh():
    controller = GitHubRateLimitController()
    snapshot = controller.snapshot()
    assert snapshot["auto_refresh_pending"] is False
    assert snapshot["next_auto_refresh_at"] is None


# 17. stale成功値と最新失敗を分離
def test_auto_refresh_failure_does_not_corrupt_stale_success_value():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource(remaining=4900))),
    )
    result = controller.maybe_run_auto_refresh(
        now=DUE_AT, fetch=fake_fetch(error=("api_error", "The GitHub API returned an error."))
    )
    assert result["fetched"] is False
    assert result["resources"] is None
    assert result["stale"] is True
    assert result["last_known"]["resources"]["graphql"]["status"] == "Normal"


# 18. auto refresh時もstdout / stderr非公開
def test_auto_refresh_snapshot_never_exposes_stdout_or_stderr():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    result = controller.maybe_run_auto_refresh(
        now=DUE_AT, fetch=fake_fetch(error=("command_failed", "The GitHub CLI command failed."))
    )
    assert "stdout" not in result
    assert "stderr" not in result
    assert "stdout" not in (result.get("last_auto_refresh_error") or {})
    assert "stderr" not in (result.get("last_auto_refresh_error") or {})


# 19. token風文字列がレスポンスに含まれない
def test_auto_refresh_error_message_never_contains_token_like_string():
    controller = GitHubRateLimitController()
    controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    result = controller.maybe_run_auto_refresh(
        now=DUE_AT, fetch=fake_fetch(error=("authentication_expired", "GitHub CLI authentication appears to be expired or invalid."))
    )
    error_message = result["last_auto_refresh_error"]["user_message"]
    assert "ghp_" not in error_message
    assert "gho_" not in error_message
    assert "GH_TOKEN" not in error_message


# 20. timezone-aware UTC
def test_auto_refresh_timestamps_are_utc_aware():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    for field in ("next_auto_refresh_at", "scheduled_reset_at"):
        parsed = datetime.fromisoformat(snapshot[field])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    result = controller.maybe_run_auto_refresh(now=DUE_AT, fetch=fake_fetch(payload()))
    parsed_last_auto = datetime.fromisoformat(result["last_auto_refresh_at"])
    assert parsed_last_auto.tzinfo is not None
    assert parsed_last_auto.utcoffset() == timedelta(0)


# 21. clock注入でsleep不要（controller自体はnowを注入され、内部でsleepしない）
def test_controller_module_never_sleeps():
    import inspect

    import app.github_rate_limit_state as state_module

    source = inspect.getsource(state_module)
    assert "time.sleep" not in source
    assert "asyncio.sleep" not in source


# 22. controller状態がテスト間で分離される
def test_each_controller_instance_starts_with_independent_state():
    controller_a = GitHubRateLimitController()
    controller_a.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(payload(core=resource(remaining=0, reset=RESET_EPOCH), graphql=resource())),
    )
    controller_b = GitHubRateLimitController()
    assert controller_b.snapshot()["auto_refresh_pending"] is False
    assert controller_a.snapshot()["auto_refresh_pending"] is True


# 23. search Exhaustedだけでは予約しない
def test_search_exhausted_alone_does_not_schedule():
    controller = GitHubRateLimitController()
    snapshot = controller.refresh(
        now=BEFORE_REFRESH_TIME,
        fetch=fake_fetch(
            payload(
                core=resource(remaining=4900),
                graphql=resource(remaining=4900),
                search=resource(limit=30, used=30, remaining=0, reset=RESET_EPOCH),
            )
        ),
    )
    assert snapshot["resources"]["search"]["status"] in ("Exhausted", "Reset overdue")
    assert snapshot["auto_refresh_pending"] is False
