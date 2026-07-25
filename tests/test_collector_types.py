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
        api_key="test-key",
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


def valid_record() -> dict:
    return {
        "vendor": "openai",
        "service_provider": "OpenAI",
        "model_name": "openai_api",
        "limit_type": "requests",
        "used_value": 1.0,
        "unit": "requests",
        "recorded_at": "2026-05-24T00:00:00+09:00",
        "source_type": "api_openai_management",
    }


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
