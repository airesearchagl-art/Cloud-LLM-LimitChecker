import pytest
from pydantic import ValidationError

from app.collectors.types import validate_normalized_record, validate_normalized_records
from tests.test_claude_collector import FakeClaudeUsageCostCollector
from tests.test_gemini_collector import FakeGeminiUsageCostCollector
from tests.test_openai_collector import FakeOpenAIUsageCostCollector


def test_openai_collector_records_match_common_schema() -> None:
    records = FakeOpenAIUsageCostCollector(api_key="test-key").collect()

    validated = validate_normalized_records(records)

    assert validated
    assert {record.vendor for record in validated} == {"openai"}
    assert {record.source_type for record in validated} == {"api_openai_management"}


def test_gemini_collector_records_match_common_schema() -> None:
    records = FakeGeminiUsageCostCollector(
        access_token="test-token",
        project_id="test-project",
    ).collect()

    validated = validate_normalized_records(records)

    assert validated
    assert {record.vendor for record in validated} == {"gemini"}
    assert {record.source_type for record in validated} == {"api_gemini_management"}


def test_claude_collector_records_match_common_schema() -> None:
    records = FakeClaudeUsageCostCollector(api_key="test-key").collect()

    validated = validate_normalized_records(records)

    assert validated
    assert {record.vendor for record in validated} == {"claude"}
    assert {record.source_type for record in validated} == {"api_claude_management"}


def valid_record(**overrides) -> dict:
    record = {
        "vendor": "openai",
        "service_provider": "OpenAI",
        "model_name": "openai_api",
        "limit_type": "requests",
        "metric_kind": "usage",
        "used_value": 1.0,
        "unit": "requests",
        "recorded_at": "2026-05-24T00:00:00+09:00",
        "period_start": "2026-05-23T00:00:00+09:00",
        "period_end": "2026-05-24T00:00:00+09:00",
        "source_type": "api_openai_management",
    }
    record.update(overrides)
    return record


def test_validate_record_rejects_missing_vendor() -> None:
    record = valid_record()
    record.pop("vendor")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_non_numeric_used_value() -> None:
    record = valid_record()
    record["used_value"] = "not-a-number"

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_missing_recorded_at() -> None:
    record = valid_record()
    record.pop("recorded_at")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


# ---------------------------------------------------------------------------
# metric_kind / canonical unit / period contract
# ---------------------------------------------------------------------------


def test_validate_record_rejects_missing_metric_kind() -> None:
    record = valid_record()
    record.pop("metric_kind")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_unsupported_metric_kind() -> None:
    record = valid_record(metric_kind="not_a_real_kind")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_unknown_unit() -> None:
    record = valid_record(unit="dollars")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_missing_period() -> None:
    record = valid_record()
    record.pop("period_start")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_naive_period() -> None:
    record = valid_record(period_start="2026-05-23T00:00:00", period_end="2026-05-24T00:00:00")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_period_start_after_end() -> None:
    record = valid_record(
        period_start="2026-05-24T00:00:00+09:00",
        period_end="2026-05-23T00:00:00+09:00",
    )

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_period_start_equal_end() -> None:
    same = "2026-05-24T00:00:00+09:00"
    record = valid_record(period_start=same, period_end=same)

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_usd_unit_with_usage_metric_kind() -> None:
    # cost and usage must never be conflated: a currency amount can never be
    # tagged as metric_kind="usage".
    record = valid_record(metric_kind="usage", unit="usd")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_token_unit_with_cost_metric_kind() -> None:
    record = valid_record(metric_kind="cost", unit="input_tokens")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_rejects_quota_unit_with_usage_metric_kind() -> None:
    # a quota ceiling must never be conflated with usage history.
    record = valid_record(metric_kind="usage", unit="quota_count")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_accepts_usd_unit_with_budget_metric_kind() -> None:
    record = valid_record(metric_kind="budget", unit="usd")

    validated = validate_normalized_record(record)

    assert validated.metric_kind == "budget"


def test_validate_record_rejects_unknown_field() -> None:
    record = valid_record(unexpected_field="should be rejected")

    with pytest.raises(ValidationError):
        validate_normalized_record(record)


def test_validate_record_accepts_all_canonical_units_with_matching_kind() -> None:
    cases = [
        ("usage", "requests"),
        ("usage", "input_tokens"),
        ("usage", "output_tokens"),
        ("usage", "cache_read_tokens"),
        ("usage", "cache_creation_tokens"),
        ("usage", "total_tokens"),
        ("cost", "usd"),
        ("budget", "usd"),
        ("quota", "quota_count"),
    ]
    for metric_kind, unit in cases:
        validated = validate_normalized_record(valid_record(metric_kind=metric_kind, unit=unit))
        assert validated.metric_kind == metric_kind
        assert validated.unit == unit
