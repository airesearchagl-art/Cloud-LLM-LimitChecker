import pytest
from sqlalchemy import func, select

from app import crud, models
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


def test_import_rolls_back_earlier_records_when_later_record_fails() -> None:
    with next(make_session()) as db:
        records = [normalized_record(), normalized_record("not-a-date")]
        with pytest.raises(CollectorImportError):
            import_normalized_records(db, records)

        service_count = db.scalar(select(func.count(models.Service.id)))
        limit_count = db.scalar(select(func.count(models.Limit.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert service_count == 0
    assert limit_count == 0
    assert usage_count == 0
    assert import_count == 0


def test_import_wraps_unexpected_error_and_rolls_back() -> None:
    with next(make_session()) as db:
        records = [normalized_record(), normalized_record() | {"model_name": ""}]
        with pytest.raises(CollectorImportError):
            import_normalized_records(db, records)

        service_count = db.scalar(select(func.count(models.Service.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert service_count == 0
    assert usage_count == 0


def test_session_is_reusable_after_rollback() -> None:
    with next(make_session()) as db:
        with pytest.raises(CollectorImportError):
            import_normalized_records(db, [normalized_record("not-a-date")])

        saved = import_normalized_records(db, [normalized_record()])
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 1
    assert usage_count == 1


def test_failed_collector_run_recorded_without_partial_import_data() -> None:
    with next(make_session()) as db:
        run = crud.create_collector_run(db, "openai", dry_run=False)

        records = [normalized_record(), normalized_record("not-a-date")]
        try:
            import_normalized_records(db, records)
        except CollectorImportError as exc:
            crud.finish_collector_run_failed(db, run.id, str(exc))

        run_count = db.scalar(select(func.count(models.CollectorRun.id)))
        failed_run = db.scalar(select(models.CollectorRun).where(models.CollectorRun.id == run.id))
        service_count = db.scalar(select(func.count(models.Service.id)))
        limit_count = db.scalar(select(func.count(models.Limit.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert run_count == 1
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert service_count == 0
    assert limit_count == 0
    assert usage_count == 0
    assert import_count == 0
