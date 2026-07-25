from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Vendor = Literal["openai", "gemini", "claude"]
SourceType = Literal["api_openai_management", "api_gemini_management", "api_claude_management"]


class CollectorNormalizedRecord(BaseModel):
    vendor: Vendor
    service_provider: str
    model_name: str
    limit_type: str
    used_value: float
    unit: str
    recorded_at: str
    source_type: SourceType
    project_id: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    raw_label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("service_provider", "model_name", "limit_type", "unit", "recorded_at", "source_type")
    @classmethod
    def require_non_empty_string(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("value must not be empty")
        return value


def normalized_record_to_dict(record: CollectorNormalizedRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


def validate_normalized_record(record: CollectorNormalizedRecord | dict[str, Any]) -> CollectorNormalizedRecord:
    if isinstance(record, CollectorNormalizedRecord):
        return record
    return CollectorNormalizedRecord.model_validate(record)


def validate_normalized_records(
    records: list[CollectorNormalizedRecord | dict[str, Any]],
) -> list[CollectorNormalizedRecord]:
    return [validate_normalized_record(record) for record in records]
