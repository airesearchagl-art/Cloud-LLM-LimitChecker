import pytest
from sqlalchemy import func, select

from app import models
from app.collectors.importer import CollectorImportError, import_normalized_records
from tests.helpers import make_session


def normalized_record(recorded_at: str = "2026-05-24T12:00:00+09:00") -> dict:
    return {
        "vendor": "openai",
        "service_provider": "OpenAI",
        "model_name": "openai_api",
        "limit_type": "requests",
        "used_value": 3.0,
        "unit": "requests",
        "recorded_at": recorded_at,
        "source_type": "api_openai_management",
        "project_id": "project-test",
    }


def test_import_creates_api_service_limit_and_usage_record() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(db, [normalized_record()])

        service = db.scalar(select(models.Service).where(models.Service.name == "OpenAI API"))
        limit = db.scalar(select(models.Limit).where(models.Limit.model_name == "openai_api"))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 1
    assert service is not None
    assert service.account_type == "api"
    assert limit is not None
    assert limit.max_value is None
    assert limit.reset_interval_type == "days"
    assert usage_count == 1


def test_import_skips_duplicate_import_key() -> None:
    with next(make_session()) as db:
        first = import_normalized_records(db, [normalized_record()])
        second = import_normalized_records(db, [normalized_record()])
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert first == 1
    assert second == 0
    assert usage_count == 1
    assert import_count == 1


def test_import_accepts_date_only_recorded_at() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(db, [normalized_record("2026-05-24")])
        record = db.scalar(select(models.UsageRecord))

    assert saved == 1
    assert record is not None
    assert record.recorded_at.year == 2026


def test_import_accepts_iso_datetime_recorded_at() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(db, [normalized_record("2026-05-24T03:00:00Z")])
        record = db.scalar(select(models.UsageRecord))

    assert saved == 1
    assert record is not None
    assert record.recorded_at is not None


def test_import_rejects_invalid_recorded_at_without_saving() -> None:
    with next(make_session()) as db:
        with pytest.raises(CollectorImportError):
            import_normalized_records(db, [normalized_record("not-a-date")])
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert usage_count == 0
