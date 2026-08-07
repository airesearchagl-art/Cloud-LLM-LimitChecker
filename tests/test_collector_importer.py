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


# ---------------------------------------------------------------------------
# legacy import_key compatibility: pre-existing collector_imports rows
# written before metric_kind/period_start/period_end/canonical units existed
# must be found and re-keyed, never duplicated.
# ---------------------------------------------------------------------------


def _legacy_import_key(
    *,
    vendor: str,
    source_type: str,
    project_id: str | None,
    organization_id: str | None,
    workspace_id: str | None,
    model_name: str,
    limit_type: str,
    unit: str,
    recorded_at: str,
) -> str:
    import hashlib
    import json

    payload = {
        "vendor": vendor,
        "source_type": source_type,
        "project_id": project_id,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "model_name": model_name,
        "limit_type": limit_type,
        "unit": unit,
        "recorded_at": recorded_at,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seed_legacy_openai_requests_row(
    db, *, used_value: float, legacy_unit: str = "requests", recorded_at: str = "2026-05-24T12:00:00+09:00"
) -> tuple[int, str]:
    """Directly inserts Service/Limit/UsageRecord/CollectorImport rows shaped
    exactly as the pre-metric_kind/period collector code would have written
    them (bypassing import_normalized_records, which only ever writes the
    current scheme) — simulating a real pre-existing local database."""
    from app.collectors.importer import parse_recorded_at
    from app.time_utils import now_local

    now = now_local()
    service = models.Service(
        name="OpenAI API", provider="OpenAI", plan_name="API", account_type="api", created_at=now, updated_at=now
    )
    db.add(service)
    db.flush()
    limit = models.Limit(
        service_id=service.id,
        model_name="openai_api",
        limit_type="requests",
        max_value=None,
        unit=legacy_unit,
        reset_interval_type="days",
        reset_interval_value=1,
        next_reset_at=None,
        warning_threshold=70.0,
        critical_threshold=85.0,
        source_type="api_openai_management",
        created_at=now,
        updated_at=now,
    )
    db.add(limit)
    db.flush()
    usage_record = models.UsageRecord(
        limit_id=limit.id,
        used_value=used_value,
        recorded_at=parse_recorded_at(recorded_at),
        source_type="api_openai_management",
        note="Imported from openai collector.",
    )
    db.add(usage_record)
    db.flush()
    legacy_key = _legacy_import_key(
        vendor="openai",
        source_type="api_openai_management",
        project_id="project-test",
        organization_id=None,
        workspace_id=None,
        model_name="openai_api",
        limit_type="requests",
        unit=legacy_unit,
        recorded_at=recorded_at,
    )
    db.add(
        models.CollectorImport(
            import_key=legacy_key,
            vendor="openai",
            source_type="api_openai_management",
            usage_record_id=usage_record.id,
            created_at=now,
        )
    )
    db.commit()
    return usage_record.id, legacy_key


def test_import_matches_legacy_key_with_same_value_as_duplicate() -> None:
    with next(make_session()) as db:
        usage_record_id, legacy_key = _seed_legacy_openai_requests_row(db, used_value=3.0)

        saved = import_normalized_records(db, [normalized_record(used_value=3.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        record = db.scalar(select(models.UsageRecord).where(models.UsageRecord.id == usage_record_id))

    # no new row: the legacy row was matched and treated as a duplicate
    assert saved == 0
    assert usage_count == 1
    assert import_count == 1
    assert record is not None
    assert record.used_value == 3.0


def test_import_matches_legacy_key_with_revised_value_as_update() -> None:
    with next(make_session()) as db:
        usage_record_id, legacy_key = _seed_legacy_openai_requests_row(db, used_value=3.0)

        saved = import_normalized_records(db, [normalized_record(used_value=9.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        record = db.scalar(select(models.UsageRecord).where(models.UsageRecord.id == usage_record_id))

    # the existing legacy row is updated in place, not duplicated
    assert saved == 1
    assert usage_count == 1
    assert import_count == 1
    assert record is not None
    assert record.used_value == 9.0


def test_import_rekeys_legacy_import_key_to_current_scheme() -> None:
    with next(make_session()) as db:
        usage_record_id, legacy_key = _seed_legacy_openai_requests_row(db, used_value=3.0)

        import_normalized_records(db, [normalized_record(used_value=3.0)])

        from app.collectors.importer import build_import_key

        new_key = build_import_key(normalized_record(used_value=3.0))
        collector_import = db.scalar(
            select(models.CollectorImport).where(models.CollectorImport.usage_record_id == usage_record_id)
        )

    assert collector_import is not None
    assert collector_import.import_key == new_key
    assert collector_import.import_key != legacy_key


def test_import_second_run_after_rekey_finds_new_key_directly_no_duplicate() -> None:
    # After the first run re-keys the legacy row, a second identical import
    # must resolve via the (now current-scheme) import_key directly — no
    # duplicate row, and the legacy candidate search is simply never needed
    # again for this identity.
    with next(make_session()) as db:
        _seed_legacy_openai_requests_row(db, used_value=3.0)

        first = import_normalized_records(db, [normalized_record(used_value=3.0)])
        second = import_normalized_records(db, [normalized_record(used_value=3.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert first == 0
    assert second == 0
    assert usage_count == 1
    assert import_count == 1


def test_import_legacy_token_unit_alias_is_matched() -> None:
    # Pre-existing rows for input_tokens/output_tokens used the generic unit
    # "tokens" rather than today's canonical "input_tokens"/"output_tokens".
    with next(make_session()) as db:
        from app.time_utils import now_local

        now = now_local()
        service = models.Service(
            name="OpenAI API", provider="OpenAI", plan_name="API", account_type="api", created_at=now, updated_at=now
        )
        db.add(service)
        db.flush()
        limit = models.Limit(
            service_id=service.id,
            model_name="openai_api",
            limit_type="input_tokens",
            max_value=None,
            unit="tokens",
            reset_interval_type="days",
            reset_interval_value=1,
            next_reset_at=None,
            warning_threshold=70.0,
            critical_threshold=85.0,
            source_type="api_openai_management",
            created_at=now,
            updated_at=now,
        )
        db.add(limit)
        db.flush()
        from app.collectors.importer import parse_recorded_at

        usage_record = models.UsageRecord(
            limit_id=limit.id,
            used_value=10.0,
            recorded_at=parse_recorded_at("2026-05-24T12:00:00+09:00"),
            source_type="api_openai_management",
            note="Imported from openai collector.",
        )
        db.add(usage_record)
        db.flush()
        legacy_key = _legacy_import_key(
            vendor="openai",
            source_type="api_openai_management",
            project_id="project-test",
            organization_id=None,
            workspace_id=None,
            model_name="openai_api",
            limit_type="input_tokens",
            unit="tokens",
            recorded_at="2026-05-24T12:00:00+09:00",
        )
        db.add(
            models.CollectorImport(
                import_key=legacy_key,
                vendor="openai",
                source_type="api_openai_management",
                usage_record_id=usage_record.id,
                created_at=now,
            )
        )
        db.commit()

        saved = import_normalized_records(
            db,
            [normalized_record(limit_type="input_tokens", unit="input_tokens", used_value=15.0)],
        )
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        record = db.scalar(select(models.UsageRecord).where(models.UsageRecord.id == usage_record.id))

    # matched the legacy "tokens"-unit row and updated it, rather than
    # creating a second row under the new canonical "input_tokens" unit
    assert saved == 1
    assert usage_count == 1
    assert record is not None
    assert record.used_value == 15.0


def test_import_new_scheme_match_takes_precedence_over_legacy() -> None:
    # If a current-scheme row already exists for this identity, it must
    # always win — the legacy candidate search must never even run (the code
    # only consults legacy candidates when `existing is None` after the
    # primary current-scheme lookup), so a legacy row for the same identity
    # discovered later can never get silently merged with the new-scheme row.
    with next(make_session()) as db:
        # 1. First import with no legacy row present at all: creates a fresh
        #    row under the current-scheme key.
        import_normalized_records(db, [normalized_record(used_value=3.0)])
        new_scheme_record_id = db.scalar(select(models.UsageRecord.id))

        # 2. A legacy-scheme row for the SAME identity now also exists (e.g.
        #    from data that predates this migration and was never re-keyed —
        #    an edge case, but one the lookup order must handle safely).
        _seed_legacy_openai_requests_row(db, used_value=999.0)

        # 3. A second import of the identical (new-scheme) value must match
        #    the existing NEW-scheme row directly and treat it as a
        #    duplicate — never touching, updating, or merging with the
        #    separate legacy row.
        saved_second = import_normalized_records(db, [normalized_record(used_value=3.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        new_scheme_record = db.scalar(
            select(models.UsageRecord).where(models.UsageRecord.id == new_scheme_record_id)
        )
        legacy_record = db.scalar(
            select(models.UsageRecord).where(models.UsageRecord.id != new_scheme_record_id)
        )

    assert saved_second == 0  # matched the new-scheme row as a plain duplicate
    assert usage_count == 2  # the new-scheme row and the separate legacy row both still exist
    assert import_count == 2
    assert new_scheme_record is not None
    assert new_scheme_record.used_value == 3.0  # untouched by the duplicate call
    assert legacy_record is not None
    assert legacy_record.used_value == 999.0  # completely untouched


def test_plan_normalized_records_reports_legacy_match_without_writing() -> None:
    with next(make_session()) as db:
        usage_record_id, legacy_key = _seed_legacy_openai_requests_row(db, used_value=3.0)

        result = plan_normalized_records(db, [normalized_record(used_value=9.0)])

        record = db.scalar(select(models.UsageRecord).where(models.UsageRecord.id == usage_record_id))
        collector_import = db.scalar(
            select(models.CollectorImport).where(models.CollectorImport.usage_record_id == usage_record_id)
        )

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "dry_run"
    # dry_run must never write — neither the value nor the import_key change
    assert record is not None
    assert record.used_value == 3.0
    assert collector_import is not None
    assert collector_import.import_key == legacy_key


def test_import_legacy_pagination_replay_within_one_batch_no_duplicate() -> None:
    # Two identical rows in one batch, both matching the SAME pre-existing
    # legacy row, must still resolve to a single re-keyed row — not a
    # duplicate on top of the legacy row, and not two new rows.
    with next(make_session()) as db:
        _seed_legacy_openai_requests_row(db, used_value=3.0)

        saved = import_normalized_records(db, [normalized_record(used_value=3.0), normalized_record(used_value=3.0)])

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert saved == 0
    assert usage_count == 1
    assert import_count == 1


def test_import_legacy_match_rollback_on_later_record_failure() -> None:
    # The re-key mutation must be covered by the same whole-batch rollback
    # as everything else — a later invalid record in the batch must undo the
    # legacy re-key too.
    with next(make_session()) as db:
        usage_record_id, legacy_key = _seed_legacy_openai_requests_row(db, used_value=3.0)

        records = [normalized_record(used_value=9.0), normalized_record(recorded_at="not-a-date")]
        with pytest.raises(CollectorImportError):
            import_normalized_records(db, records)

        record = db.scalar(select(models.UsageRecord).where(models.UsageRecord.id == usage_record_id))
        collector_import = db.scalar(
            select(models.CollectorImport).where(models.CollectorImport.usage_record_id == usage_record_id)
        )

    assert record is not None
    assert record.used_value == 3.0  # unchanged — rolled back
    assert collector_import is not None
    assert collector_import.import_key == legacy_key  # re-key rolled back too
