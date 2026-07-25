from datetime import datetime

from app.calculations import limit_to_dashboard
from app.time_utils import advance_next_reset, app_tz, current_period_start
from tests.helpers import create_limit, create_service, make_session


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(app_tz())


def test_hours_reset_calculation() -> None:
    next_reset = dt("2026-01-01T03:00:00+09:00")
    reference = dt("2026-01-01T01:00:00+09:00")
    assert current_period_start(next_reset, "hours", 3, reference_now=reference) == dt("2026-01-01T00:00:00+09:00")


def test_days_reset_calculation() -> None:
    next_reset = dt("2026-01-02T00:00:00+09:00")
    reference = dt("2026-01-01T12:00:00+09:00")
    assert current_period_start(next_reset, "days", 1, reference_now=reference) == dt("2026-01-01T00:00:00+09:00")


def test_weeks_reset_calculation() -> None:
    next_reset = dt("2026-01-08T00:00:00+09:00")
    reference = dt("2026-01-04T00:00:00+09:00")
    assert current_period_start(next_reset, "weeks", 1, reference_now=reference) == dt("2026-01-01T00:00:00+09:00")


def test_months_reset_uses_calendar_month() -> None:
    next_reset = dt("2026-02-28T00:00:00+09:00")
    reference = dt("2026-02-01T00:00:00+09:00")
    assert advance_next_reset(next_reset, "months", 1, reference_now=reference) == next_reset
    assert current_period_start(next_reset, "months", 1, reference_now=reference) == dt("2026-01-28T00:00:00+09:00")


def test_past_next_reset_advances_to_future() -> None:
    next_reset = dt("2026-01-01T00:00:00+09:00")
    reference = dt("2026-01-03T10:00:00+09:00")
    assert advance_next_reset(next_reset, "days", 1, reference_now=reference) == dt("2026-01-04T00:00:00+09:00")


def test_null_max_value_returns_null_usage_percent() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=None)
        db.commit()
        db.refresh(limit)

        row = limit_to_dashboard(db, limit)

    assert row["usage_percent"] is None
    assert row["remaining_value"] is None
