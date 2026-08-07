from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Vendor = Literal["openai", "gemini", "claude"]
SourceType = Literal["api_openai_management", "api_gemini_management", "api_claude_management"]

# What kind of number a record represents. usage/cost are the only kinds this
# app persists as UsageRecord rows; quota (a limit/consumption-ceiling snapshot,
# e.g. Gemini's consumerQuotaMetrics) and budget (a configured spend cap) are
# never usage history and must never be silently saved as if they were.
MetricKind = Literal["usage", "cost", "quota", "budget"]

# Canonical unit vocabulary, restricted to what the official OpenAI/Gemini/Claude
# management API responses we verified actually return (see
# docs/vendor-collector-production-readiness.md for the source-by-source
# evidence). Free-form unit strings are no longer accepted so a typo or a
# vendor response shape change fails validation instead of being silently
# persisted as an unrecognized unit.
CanonicalUnit = Literal[
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
    "usd",
    "quota_count",
]

# unit -> the only metric_kind values that unit is valid for. This is what
# prevents e.g. a token count from being mislabeled as a cost, or a currency
# amount from being mislabeled as usage.
_UNIT_METRIC_KINDS: dict[str, set[str]] = {
    "requests": {"usage"},
    "input_tokens": {"usage"},
    "output_tokens": {"usage"},
    "cache_read_tokens": {"usage"},
    "cache_creation_tokens": {"usage"},
    "total_tokens": {"usage"},
    "usd": {"cost", "budget"},
    "quota_count": {"quota"},
}


class CollectorNormalizedRecord(BaseModel):
    vendor: Vendor
    service_provider: str
    model_name: str
    limit_type: str
    metric_kind: MetricKind
    used_value: float
    unit: CanonicalUnit
    recorded_at: str
    # Timezone-aware bucket boundaries for the value being reported. Required
    # (not inferred from recorded_at) so import identity and stale/duplicate
    # detection never depend on a loosely-typed display string.
    period_start: datetime
    period_end: datetime
    # Vendor-reported bucket size, e.g. "1d" / "1h" / "1m" — informational,
    # not currently used for identity (period_start/period_end already pin
    # the exact window).
    bucket_width: str | None = None
    # A vendor-provided stable identifier for this exact row, when one exists.
    # None for every vendor/endpoint verified so far (all are aggregated
    # buckets, not individually IDed events) — kept for forward compatibility
    # if a vendor later exposes one.
    source_record_id: str | None = None
    source_type: SourceType
    project_id: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    raw_label: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("service_provider", "model_name", "limit_type", "recorded_at", "source_type")
    @classmethod
    def require_non_empty_string(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("value must not be empty")
        return value

    @field_validator("period_start", "period_end")
    @classmethod
    def require_timezone_aware_period(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("period_start/period_end must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_period_and_unit(self) -> "CollectorNormalizedRecord":
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be before period_end")
        allowed_kinds = _UNIT_METRIC_KINDS[self.unit]
        if self.metric_kind not in allowed_kinds:
            raise ValueError(
                f"unit {self.unit!r} is not valid for metric_kind {self.metric_kind!r} "
                f"(allowed: {sorted(allowed_kinds)})"
            )
        return self


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


# metric_kind values that are ever eligible for UsageRecord persistence. quota
# and budget are computed/validated/returned (e.g. in a dry_run preview or a
# future dedicated view) but never written as usage history — see the
# persistence policy in docs/vendor-collector-production-readiness.md.
PERSISTABLE_METRIC_KINDS: frozenset[str] = frozenset({"usage", "cost"})
