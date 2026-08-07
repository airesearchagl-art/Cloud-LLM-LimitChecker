import pytest
from sqlalchemy import func, select

from app import crud, models
from app.collectors.importer import CollectorImportError, import_normalized_records, plan_normalized_records
from tests.helpers import make_session


def normalized_record(recorded_at: str = "2026-05-24T12:00:00+09:00", **overrides) -> dict:
    record = {
        "vendor": "openai",
        "service_provider": "OpenAI",
        "model_name": "openai_api",
        "limit_type": "requests",
        "metric_kind": "usage",
        "used_value": 3.0,
        "unit": "requests",
        "recorded_at": recorded_at,
        "period_start": "2026-05-24T00:00:00+09:00",
        "period_end": "2026-05-25T00:00:00+09:00",
        "source_type": "api_openai_management",
        "project_id": "project-test",
    }
    record.update(overrides)
    return record


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


def test_import_rolls_back_when_commit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with next(make_session()) as db:
        original_commit = db.commit
        calls = {"count": 0}

        def failing_commit() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("commit failed")
            original_commit()

        monkeypatch.setattr(db, "commit", failing_commit)

        with pytest.raises(CollectorImportError):
            import_normalized_records(db, [normalized_record()])

        service_count = db.scalar(select(func.count(models.Service.id)))
        limit_count = db.scalar(select(func.count(models.Limit.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert service_count == 0
    assert limit_count == 0
    assert usage_count == 0
    assert import_count == 0


def test_session_is_reusable_after_commit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with next(make_session()) as db:
        original_commit = db.commit
        calls = {"count": 0}

        def failing_commit() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("commit failed")
            original_commit()

        monkeypatch.setattr(db, "commit", failing_commit)

        with pytest.raises(CollectorImportError):
            import_normalized_records(db, [normalized_record()])

        saved = import_normalized_records(db, [normalized_record()])
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 1
    assert usage_count == 1


def test_failed_collector_run_recorded_after_commit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with next(make_session()) as db:
        run = crud.create_collector_run(db, "openai", dry_run=False)

        original_commit = db.commit
        calls = {"count": 0}

        def failing_commit() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("commit failed")
            original_commit()

        monkeypatch.setattr(db, "commit", failing_commit)

        try:
            import_normalized_records(db, [normalized_record()])
        except CollectorImportError as exc:
            crud.finish_collector_run_failed(db, run.id, str(exc))

        run_count = db.scalar(select(func.count(models.CollectorRun.id)))
        failed_run = db.scalar(select(models.CollectorRun).where(models.CollectorRun.id == run.id))
        service_count = db.scalar(select(func.count(models.Service.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert run_count == 1
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert service_count == 0
    assert usage_count == 0


# ---------------------------------------------------------------------------
# import identity: revision-safe upsert / metric-kind persistence policy /
# dry_run planning
# ---------------------------------------------------------------------------


def test_import_updates_existing_record_on_revised_value() -> None:
    with next(make_session()) as db:
        first = import_normalized_records(db, [normalized_record(used_value=3.0)])
        second = import_normalized_records(db, [normalized_record(used_value=5.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        record = db.scalar(select(models.UsageRecord))

    assert first == 1
    assert second == 1
    assert usage_count == 1
    assert import_count == 1
    assert record is not None
    assert record.used_value == 5.0


def test_import_does_not_duplicate_same_value_seen_across_pagination_within_one_batch() -> None:
    # Two identical rows in the SAME collect() result (as pagination could
    # produce if a cursor is re-fetched) must resolve to exactly one saved
    # record, not two.
    with next(make_session()) as db:
        saved = import_normalized_records(db, [normalized_record(), normalized_record()])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert saved == 1
    assert usage_count == 1
    assert import_count == 1


def test_import_creates_separate_records_for_different_period() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(
            db,
            [
                normalized_record(period_start="2026-05-24T00:00:00+09:00", period_end="2026-05-25T00:00:00+09:00"),
                normalized_record(period_start="2026-05-25T00:00:00+09:00", period_end="2026-05-26T00:00:00+09:00"),
            ],
        )
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 2
    assert usage_count == 2


def test_import_creates_separate_records_for_different_scope() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(
            db,
            [
                normalized_record(project_id="project-a"),
                normalized_record(project_id="project-b"),
            ],
        )
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 2
    assert usage_count == 2


def test_import_creates_separate_records_for_different_metric_kind() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(
            db,
            [
                normalized_record(metric_kind="usage", unit="requests", used_value=3.0),
                normalized_record(metric_kind="cost", unit="usd", limit_type="api_cost", used_value=1.5),
            ],
        )
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 2
    assert usage_count == 2


def test_import_skips_quota_metric_kind_without_persisting() -> None:
    with next(make_session()) as db:
        saved = import_normalized_records(
            db,
            [normalized_record(metric_kind="quota", unit="quota_count", limit_type="requests_per_day")],
        )
        service_count = db.scalar(select(func.count(models.Service.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == 0
    assert service_count == 0
    assert usage_count == 0


def test_plan_normalized_records_reports_unsupported_metric_kind_reason() -> None:
    with next(make_session()) as db:
        result = plan_normalized_records(
            db,
            [normalized_record(metric_kind="quota", unit="quota_count", limit_type="requests_per_day")],
        )

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "unsupported_metric_kind"


def test_plan_normalized_records_does_not_write_to_db() -> None:
    with next(make_session()) as db:
        result = plan_normalized_records(db, [normalized_record()])

        service_count = db.scalar(select(func.count(models.Service.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "dry_run"
    assert service_count == 0
    assert usage_count == 0
    assert import_count == 0


def test_plan_normalized_records_reports_would_update_without_writing() -> None:
    with next(make_session()) as db:
        import_normalized_records(db, [normalized_record(used_value=3.0)])

        result = plan_normalized_records(db, [normalized_record(used_value=9.0)])

        record = db.scalar(select(models.UsageRecord))

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "dry_run"
    assert record is not None
    assert record.used_value == 3.0  # unchanged — dry_run never writes


def test_plan_normalized_records_reports_duplicate_for_unchanged_value_without_writing() -> None:
    with next(make_session()) as db:
        import_normalized_records(db, [normalized_record(used_value=3.0)])

        result = plan_normalized_records(db, [normalized_record(used_value=3.0)])

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "duplicate"


def test_plan_normalized_records_reports_invalid_record_without_raising() -> None:
    with next(make_session()) as db:
        malformed = normalized_record()
        malformed.pop("vendor")

        result = plan_normalized_records(db, [malformed, normalized_record()])

        service_count = db.scalar(select(func.count(models.Service.id)))

    reasons = {outcome.reason for outcome in result.outcomes}
    assert "invalid_record" in reasons
    assert "dry_run" in reasons
    assert result.records_saved == 0
    assert service_count == 0
