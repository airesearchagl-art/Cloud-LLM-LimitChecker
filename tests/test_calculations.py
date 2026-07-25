from app.calculations import current_usage, limit_to_dashboard, status_for_usage
from tests.helpers import add_usage_record, create_limit, create_service, make_session


def test_status_under_70_is_ok() -> None:
    assert status_for_usage(69.99, 70, 85) == "正常"


def test_status_70_or_more_is_warning() -> None:
    assert status_for_usage(70, 70, 85) == "注意"


def test_status_85_or_more_is_danger() -> None:
    assert status_for_usage(85, 70, 85) == "危険"


def test_status_100_or_more_is_limit_reached() -> None:
    assert status_for_usage(100, 70, 85) == "上限到達"


def test_status_null_percent_is_manual_required() -> None:
    assert status_for_usage(None, 70, 85) == "手入力待ち"


def test_current_usage_excludes_records_outside_current_period() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=100)
        limit.next_reset_at = limit.next_reset_at.replace(year=2999)
        add_usage_record(db, limit, 40, "2998-12-31T23:00:00+09:00")
        add_usage_record(db, limit, 30, "2999-01-01T12:00:00+09:00")
        db.commit()

        used, _ = current_usage(db, limit)

    assert used == 30


def test_current_usage_includes_adjustment_records() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=100)
        limit.next_reset_at = limit.next_reset_at.replace(year=2999)
        add_usage_record(db, limit, 10, "2999-01-01T12:00:00+09:00")
        add_usage_record(db, limit, -3, "2999-01-01T13:00:00+09:00")
        db.commit()

        used, _ = current_usage(db, limit)

    assert used == 7


def test_limit_to_dashboard_returns_manual_required_when_max_value_is_null() -> None:
    with next(make_session()) as db:
        service = create_service(db)
        limit = create_limit(db, service, max_value=None)
        db.commit()

        row = limit_to_dashboard(db, limit)

    assert row["usage_percent"] is None
    assert row["status"] == "手入力待ち"
