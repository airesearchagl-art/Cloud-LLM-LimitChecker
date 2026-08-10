"""Tests for app/github_graphql_diagnostics.py.

Note on scope (regression guard): this module intentionally exports no
proportional-allocation function. GitHub's rate_limit API gives no basis
for splitting an observed delta across overlapping activity sessions, so
no such function should ever be added here -- see
`test_module_exports_no_proportional_allocation_function` below.
"""

from datetime import datetime, timedelta, timezone

import app.github_graphql_diagnostics as diagnostics_module
from app.github_graphql_diagnostics import (
    ActivityWindow,
    classify_attribution,
    compute_graphql_delta,
    compute_session_total_delta,
    count_active_sessions_at,
)
from app.github_rate_limit import ResourceRateLimit

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
RESET_A = datetime(2026, 7, 26, 13, 0, 0, tzinfo=timezone.utc)
RESET_B = datetime(2026, 7, 26, 14, 0, 0, tzinfo=timezone.utc)


def make_resource(*, used=100, reset_at_utc=RESET_A, collected_at=NOW) -> ResourceRateLimit:
    limit = 5000
    remaining = None if used is None else limit - used
    return ResourceRateLimit(
        resource="graphql",
        status="Normal",
        limit=limit,
        used=used,
        remaining=remaining,
        usage_percent=None if used is None else used / limit * 100,
        remaining_percent=None if remaining is None else remaining / limit * 100,
        reset_at_utc=reset_at_utc,
        reset_at_local=reset_at_utc,
        seconds_until_reset=None,
        collected_at=collected_at,
        error_message=None,
    )


# ---------------------------------------------------------------------------
# compute_graphql_delta
# ---------------------------------------------------------------------------


# 1. 同一reset内でusedが増加 -> 正しい正のdelta、outcome "ok"
def test_compute_graphql_delta_same_reset_increase_is_ok():
    previous = make_resource(used=100, reset_at_utc=RESET_A)
    current = make_resource(used=150, reset_at_utc=RESET_A)
    result = compute_graphql_delta(previous, current)
    assert result.delta == 50
    assert result.outcome == "ok"


# 2. 同一reset内でused不変 -> delta 0 (Noneではない)、outcome "ok"
def test_compute_graphql_delta_same_reset_unchanged_is_zero_not_none():
    previous = make_resource(used=100, reset_at_utc=RESET_A)
    current = make_resource(used=100, reset_at_utc=RESET_A)
    result = compute_graphql_delta(previous, current)
    assert result.delta == 0
    assert result.delta is not None
    assert result.outcome == "ok"


# 3. reset_at_utcが異なる -> delta None、outcome "reset_boundary"
def test_compute_graphql_delta_different_reset_is_reset_boundary():
    previous = make_resource(used=4900, reset_at_utc=RESET_A)
    current = make_resource(used=50, reset_at_utc=RESET_B)
    result = compute_graphql_delta(previous, current)
    assert result.delta is None
    assert result.outcome == "reset_boundary"


# 4. 同一reset内でusedが減少 -> delta None、outcome "counter_regression"
def test_compute_graphql_delta_same_reset_decrease_is_counter_regression():
    previous = make_resource(used=200, reset_at_utc=RESET_A)
    current = make_resource(used=150, reset_at_utc=RESET_A)
    result = compute_graphql_delta(previous, current)
    assert result.delta is None
    assert result.outcome == "counter_regression"


# 5. previous=None -> delta None、outcome "no_previous"
def test_compute_graphql_delta_no_previous_sample():
    current = make_resource(used=100, reset_at_utc=RESET_A)
    result = compute_graphql_delta(None, current)
    assert result.delta is None
    assert result.outcome == "no_previous"


# 6a. previous.usedがNone -> reset_boundaryとして扱う（比較不能を安全側に倒す実装方針）
def test_compute_graphql_delta_previous_used_none_is_reset_boundary():
    previous = make_resource(used=None, reset_at_utc=RESET_A)
    current = make_resource(used=100, reset_at_utc=RESET_A)
    result = compute_graphql_delta(previous, current)
    assert result.delta is None
    assert result.outcome == "reset_boundary"


# 6b. current.reset_at_utcがNone -> reset_boundaryとして扱う
def test_compute_graphql_delta_current_reset_at_utc_none_is_reset_boundary():
    previous = make_resource(used=100, reset_at_utc=RESET_A)
    current = make_resource(used=100, reset_at_utc=None)
    result = compute_graphql_delta(previous, current)
    assert result.delta is None
    assert result.outcome == "reset_boundary"


# ---------------------------------------------------------------------------
# classify_attribution
# ---------------------------------------------------------------------------


# 7. fetch_failed=True -> "FETCH_FAILED"（他の引数に関わらず優先）
def test_classify_attribution_fetch_failed_takes_precedence_over_ok():
    assert (
        classify_attribution(delta_outcome="ok", active_session_count=2, fetch_failed=True)
        == "FETCH_FAILED"
    )


def test_classify_attribution_fetch_failed_takes_precedence_over_reset_boundary():
    assert (
        classify_attribution(
            delta_outcome="reset_boundary", active_session_count=0, fetch_failed=True
        )
        == "FETCH_FAILED"
    )


# 8. delta_outcome="reset_boundary" (fetch_failed=False) -> "RESET_BOUNDARY"
def test_classify_attribution_reset_boundary():
    assert (
        classify_attribution(delta_outcome="reset_boundary", active_session_count=1)
        == "RESET_BOUNDARY"
    )


# 9. delta_outcome="counter_regression" (fetch_failed=False) -> "COUNTER_REGRESSION"
def test_classify_attribution_counter_regression():
    assert (
        classify_attribution(delta_outcome="counter_regression", active_session_count=1)
        == "COUNTER_REGRESSION"
    )


# 10. delta_outcome="ok", active_session_count=0 -> "UNATTRIBUTED"
def test_classify_attribution_ok_with_zero_sessions_is_unattributed():
    assert classify_attribution(delta_outcome="ok", active_session_count=0) == "UNATTRIBUTED"


# 11. delta_outcome="ok", active_session_count=1 -> "SINGLE_ACTIVITY_CORRELATION"
def test_classify_attribution_ok_with_one_session_is_single_activity_correlation():
    assert (
        classify_attribution(delta_outcome="ok", active_session_count=1)
        == "SINGLE_ACTIVITY_CORRELATION"
    )


# 12. delta_outcome="ok", active_session_count=2 -> "OVERLAPPING_ACTIVITIES"
def test_classify_attribution_ok_with_two_sessions_is_overlapping_activities():
    assert (
        classify_attribution(delta_outcome="ok", active_session_count=2)
        == "OVERLAPPING_ACTIVITIES"
    )


# 13. delta_outcome="ok", active_session_count=5 -> 依然として "OVERLAPPING_ACTIVITIES"
def test_classify_attribution_ok_with_many_sessions_is_still_overlapping_activities():
    assert (
        classify_attribution(delta_outcome="ok", active_session_count=5)
        == "OVERLAPPING_ACTIVITIES"
    )


# 14. delta_outcome="no_previous" は "ok" と同じsession-count分岐に従う
def test_classify_attribution_no_previous_behaves_like_ok_for_session_count():
    assert (
        classify_attribution(delta_outcome="no_previous", active_session_count=1)
        == "SINGLE_ACTIVITY_CORRELATION"
    )
    assert (
        classify_attribution(delta_outcome="no_previous", active_session_count=0)
        == "UNATTRIBUTED"
    )
    assert (
        classify_attribution(delta_outcome="no_previous", active_session_count=2)
        == "OVERLAPPING_ACTIVITIES"
    )


# ---------------------------------------------------------------------------
# count_active_sessions_at
# ---------------------------------------------------------------------------


# 15. windowが0件 -> 0
def test_count_active_sessions_at_no_windows_is_zero():
    assert count_active_sessions_at(NOW, []) == 0


# 16. instantを覆う1件のwindow（開始が前、終了が後 or None） -> 1
def test_count_active_sessions_at_one_window_covering_instant():
    window = ActivityWindow(started_at=NOW - timedelta(hours=1), ended_at=NOW + timedelta(hours=1))
    assert count_active_sessions_at(NOW, [window]) == 1

    open_ended = ActivityWindow(started_at=NOW - timedelta(hours=1), ended_at=None)
    assert count_active_sessions_at(NOW, [open_ended]) == 1


# 17. instantより前に終了したwindow -> 0
def test_count_active_sessions_at_window_ended_before_instant():
    window = ActivityWindow(
        started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1)
    )
    assert count_active_sessions_at(NOW, [window]) == 0


# 18. instantより後に開始するwindow -> 0
def test_count_active_sessions_at_window_starts_after_instant():
    window = ActivityWindow(started_at=NOW + timedelta(hours=1), ended_at=None)
    assert count_active_sessions_at(NOW, [window]) == 0


# 19. instantを覆う重複した2件のwindow -> 2
def test_count_active_sessions_at_two_overlapping_windows():
    window_a = ActivityWindow(started_at=NOW - timedelta(hours=1), ended_at=NOW + timedelta(hours=1))
    window_b = ActivityWindow(started_at=NOW - timedelta(minutes=30), ended_at=None)
    assert count_active_sessions_at(NOW, [window_a, window_b]) == 2


# 20. 境界: instant == started_at -> active扱い (1)
def test_count_active_sessions_at_boundary_instant_equals_started_at():
    window = ActivityWindow(started_at=NOW, ended_at=NOW + timedelta(hours=1))
    assert count_active_sessions_at(NOW, [window]) == 1


# 21. 境界: instant == ended_at -> active扱い (1)
def test_count_active_sessions_at_boundary_instant_equals_ended_at():
    window = ActivityWindow(started_at=NOW - timedelta(hours=1), ended_at=NOW)
    assert count_active_sessions_at(NOW, [window]) == 1


# ---------------------------------------------------------------------------
# compute_session_total_delta
# ---------------------------------------------------------------------------


# 22. 同一reset、used_end > used_start -> 正しい正のdelta
def test_compute_session_total_delta_same_reset_increase():
    result = compute_session_total_delta(
        graphql_used_start=100,
        graphql_used_end=250,
        reset_at_start=RESET_A,
        reset_at_end=RESET_A,
    )
    assert result == 150


# 23. 同一reset、used_end == used_start -> 0 (Noneではない)
def test_compute_session_total_delta_same_reset_unchanged_is_zero():
    result = compute_session_total_delta(
        graphql_used_start=100,
        graphql_used_end=100,
        reset_at_start=RESET_A,
        reset_at_end=RESET_A,
    )
    assert result == 0
    assert result is not None


# 24. reset_at_start != reset_at_end -> None
# (spec section 8: セッションのwindowがquota resetをまたいだ場合、
#  異なるreset window同士のtotalを合算してはならない)
def test_compute_session_total_delta_crossing_reset_boundary_is_none():
    result = compute_session_total_delta(
        graphql_used_start=4900,
        graphql_used_end=50,
        reset_at_start=RESET_A,
        reset_at_end=RESET_B,
    )
    assert result is None


# 25. used_end < used_start（同一reset） -> None
def test_compute_session_total_delta_regression_within_same_reset_is_none():
    result = compute_session_total_delta(
        graphql_used_start=200,
        graphql_used_end=150,
        reset_at_start=RESET_A,
        reset_at_end=RESET_A,
    )
    assert result is None


# 26. graphql_used_startがNone（baselineが取得できなかった） -> None
def test_compute_session_total_delta_missing_start_is_none():
    result = compute_session_total_delta(
        graphql_used_start=None,
        graphql_used_end=100,
        reset_at_start=RESET_A,
        reset_at_end=RESET_A,
    )
    assert result is None


# 27. graphql_used_endがNone（最終サンプルが取得できなかった） -> None
def test_compute_session_total_delta_missing_end_is_none():
    result = compute_session_total_delta(
        graphql_used_start=100,
        graphql_used_end=None,
        reset_at_start=RESET_A,
        reset_at_end=RESET_A,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Regression guard: no proportional-allocation function was ever added
# ---------------------------------------------------------------------------


def test_module_exports_no_proportional_allocation_function():
    assert not hasattr(diagnostics_module, "allocate_delta")
    forbidden_substrings = ("proportion", "allocat", "split")
    offending_names = [
        name
        for name in dir(diagnostics_module)
        if not name.startswith("_")
        and any(substring in name.lower() for substring in forbidden_substrings)
    ]
    assert offending_names == []
