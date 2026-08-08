"""Cross-layer regression coverage: collector output -> importer persistence,
exercised end to end with a synthetic vendor response (no real credentials,
no network). This pins the OpenAI recorded_at defect fix in
app/collectors/importer.py (the persisted UsageRecord.recorded_at must be
derived from the validated period_end, never parsed from the vendor's raw
recorded_at string) and fixes the shared collector -> importer contract for
Claude and Gemini too.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select

from app import models
from app.collectors.claude_collector import ClaudeUsageCostCollector
from app.collectors.gemini_collector import GeminiUsageCostCollector
from app.collectors.importer import import_normalized_records, plan_normalized_records
from app.collectors.openai_collector import OpenAIUsageCostCollector
from app.collectors.types import validate_normalized_record
from app.time_utils import app_tz
from tests.helpers import make_session

# ---------------------------------------------------------------------------
# OpenAI: official Usage API bucket shape — start_time/end_time are Unix
# timestamps (per developers.openai.com), never ISO strings. The collector
# attaches them to recorded_at as-is (see
# OpenAIUsageCostCollector._recorded_at_label), so recorded_at is a string
# like "1763982000" that datetime.fromisoformat cannot parse.
# ---------------------------------------------------------------------------


class _FakeOpenAICollector(OpenAIUsageCostCollector):
    def __init__(self, *args, num_model_requests: float = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self._num_model_requests = num_model_requests

    def _get_json(self, path, params):
        if path.endswith("/usage/completions"):
            return {
                "data": [
                    {
                        "start_time": 1763895600,
                        "end_time": 1763982000,
                        "results": [
                            {
                                "model": "gpt-test",
                                "num_model_requests": self._num_model_requests,
                                "project_id": "project-test",
                            }
                        ],
                    }
                ]
            }
        return {"data": []}


def _openai_rows(num_model_requests: float = 3) -> list[dict]:
    return _FakeOpenAICollector(api_key="test-key", num_model_requests=num_model_requests).collect()


def test_openai_collector_produces_unparseable_recorded_at_string() -> None:
    # Documents the actual defect condition: the raw recorded_at is the
    # bucket's Unix end_time as a string, not an ISO datetime.
    rows = _openai_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row["recorded_at"] == "1763982000"
    assert row["limit_type"] == "requests"
    assert row["metric_kind"] == "usage"


def test_openai_row_passes_normalized_record_validation() -> None:
    rows = _openai_rows()

    normalized = validate_normalized_record(rows[0])

    assert normalized.period_start.tzinfo is not None
    assert normalized.period_end.tzinfo is not None
    assert normalized.period_start < normalized.period_end


def test_openai_cross_layer_dry_run_reports_import_without_writing() -> None:
    with next(make_session()) as db:
        result = plan_normalized_records(db, _openai_rows())

        service_count = db.scalar(select(func.count(models.Service.id)))
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert result.records_saved == 0
    assert result.outcomes[0].reason == "dry_run"
    assert service_count == 0
    assert usage_count == 0
    assert import_count == 0


def test_openai_cross_layer_real_import_persists_recorded_at_from_period_end() -> None:
    # This is the regression test for the defect: previously, importing this
    # exact OpenAI-shaped batch raised CollectorImportError because
    # parse_recorded_at("1763982000") failed datetime.fromisoformat parsing.
    expected_recorded_at = datetime.fromtimestamp(1763982000, tz=timezone.utc).astimezone(app_tz())

    with next(make_session()) as db:
        saved = import_normalized_records(db, _openai_rows())

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        record = db.scalar(select(models.UsageRecord))

    assert saved == 1
    assert usage_count == 1
    assert import_count == 1
    assert record is not None
    assert record.used_value == 3.0
    # SQLite round-trips DateTime(timezone=True) as a naive value (the wall
    # clock in app_tz() at insert time), so compare naive-to-naive.
    assert record.recorded_at == expected_recorded_at.replace(tzinfo=None)


def test_openai_cross_layer_same_bucket_rerun_is_duplicate_no_new_rows() -> None:
    with next(make_session()) as db:
        first = import_normalized_records(db, _openai_rows())
        second = import_normalized_records(db, _openai_rows())

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))

    assert first == 1
    assert second == 0
    assert usage_count == 1
    assert import_count == 1


def test_openai_cross_layer_revised_value_same_bucket_updates_no_new_rows() -> None:
    with next(make_session()) as db:
        import_normalized_records(db, _openai_rows(num_model_requests=3))
        saved = import_normalized_records(db, _openai_rows(num_model_requests=7))

        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))
        import_count = db.scalar(select(func.count(models.CollectorImport.id)))
        record = db.scalar(select(models.UsageRecord))

    assert saved == 1
    assert usage_count == 1
    assert import_count == 1
    assert record is not None
    assert record.used_value == 7.0


# ---------------------------------------------------------------------------
# Claude / Gemini: small collector -> import smoke tests to pin the same
# shared importer contract (persistable metric_kind imports cleanly, no
# network/credentials involved).
# ---------------------------------------------------------------------------


class _FakeClaudeCollector(ClaudeUsageCostCollector):
    def _get_json(self, path, params):
        if path.endswith("/usage_report/messages"):
            return {
                "data": [
                    {
                        "starting_at": "2026-05-23T00:00:00Z",
                        "ending_at": "2026-05-24T00:00:00Z",
                        "results": [
                            {
                                "model": "claude-test",
                                "uncached_input_tokens": 11,
                                "output_tokens": 6,
                                "workspace_id": "workspace-test",
                            }
                        ],
                    }
                ],
                "has_more": False,
            }
        return {"data": [], "has_more": False}


def test_claude_collector_to_importer_smoke() -> None:
    rows = _FakeClaudeCollector(
        api_key="test-key", organization_id="org-test", workspace_id="workspace-test"
    ).collect()

    with next(make_session()) as db:
        saved = import_normalized_records(db, rows)
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == len(rows) > 0
    assert usage_count == len(rows)


class _FakeGeminiCollector(GeminiUsageCostCollector):
    def _get_json(self, url, params):
        if "timeSeries" in url:
            return {
                "timeSeries": [
                    {
                        "metric": {"labels": {"model": "gemini-test"}},
                        "points": [
                            {
                                "interval": {
                                    "startTime": "2026-05-23T00:00:00Z",
                                    "endTime": "2026-05-24T00:00:00Z",
                                },
                                "value": {"int64Value": "42"},
                            }
                        ],
                    }
                ]
            }
        return {"metrics": []}


def test_gemini_collector_to_importer_smoke() -> None:
    rows = _FakeGeminiCollector(access_token="test-token", project_id="proj-test").collect()

    with next(make_session()) as db:
        saved = import_normalized_records(db, rows)
        usage_count = db.scalar(select(func.count(models.UsageRecord.id)))

    assert saved == len(rows) > 0
    assert usage_count == len(rows)
