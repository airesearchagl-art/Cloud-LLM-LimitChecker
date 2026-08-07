from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ServiceCreate(BaseModel):
    name: str
    provider: str
    plan_name: str = "manual"
    account_type: str = "web_subscription"


class ServiceRead(ServiceCreate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LimitCreate(BaseModel):
    service_id: int
    model_name: str
    limit_type: str
    max_value: float | None = None
    unit: str
    reset_interval_type: str = "manual"
    reset_interval_value: int = 1
    next_reset_at: datetime | None = None
    warning_threshold: float = 70.0
    critical_threshold: float = 85.0
    source_type: str = "manual"


class LimitRead(LimitCreate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LimitUpdate(BaseModel):
    model_name: str
    max_value: float | None = None
    unit: str
    reset_interval_type: Literal["hours", "days", "weeks", "months", "manual"] = "manual"
    reset_interval_value: int = 1
    next_reset_at: datetime | None = None

    @model_validator(mode="after")
    def validate_limit_update(self) -> "LimitUpdate":
        if not self.model_name.strip():
            raise ValueError("model_name must not be empty")
        if not self.unit.strip():
            raise ValueError("unit must not be empty")
        if self.max_value is not None and self.max_value < 0:
            raise ValueError("max_value must not be negative")
        if self.reset_interval_value < 1:
            raise ValueError("reset_interval_value must be at least 1")
        return self


class UsageCreate(BaseModel):
    used_value: float
    mode: Literal["add", "adjust", "set"] = "add"
    source_type: str = "manual"
    note: str | None = None
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def validate_usage_mode(self) -> "UsageCreate":
        if self.mode == "add" and self.used_value <= 0:
            raise ValueError("mode 'add' requires used_value > 0")
        if self.mode == "adjust":
            if self.used_value == 0:
                raise ValueError("mode 'adjust' requires non-zero used_value")
            if not self.note or not self.note.strip():
                raise ValueError("mode 'adjust' requires note")
        return self


class UsageRead(BaseModel):
    id: int
    limit_id: int
    used_value: float
    recorded_at: datetime
    source_type: str
    note: str | None

    model_config = {"from_attributes": True}


class UsageRecordWithLimit(BaseModel):
    usage_record_id: int
    limit_id: int
    service_name: str
    provider: str
    plan_name: str
    account_type: str
    model_name: str
    limit_type: str
    used_value: float
    unit: str
    source_type: str
    note: str | None
    recorded_at: datetime


class DashboardLimit(BaseModel):
    limit_id: int
    service_id: int
    service_name: str
    provider: str
    plan_name: str
    account_type: str
    model_name: str
    limit_type: str
    max_value: float | None
    used_value: float
    remaining_value: float | None
    usage_percent: float | None
    unit: str
    next_reset_at: datetime | None
    source_type: str
    status: str
    last_updated_at: datetime | None


class AlertRead(BaseModel):
    limit_id: int
    alert_level: str
    message: str
    usage_percent: float | None
    next_reset_at: datetime | None


class ClaudeCodeUsageWindow(BaseModel):
    used_percentage: float
    remaining_percentage: float
    resets_at: str


class ClaudeCodeUsageSnapshot(BaseModel):
    available: bool
    stale: bool
    status: str
    observed_at: str | None
    source: str | None
    five_hour: ClaudeCodeUsageWindow | None
    seven_day: ClaudeCodeUsageWindow | None
    error_message: str | None


class ClaudeDesktopCloudUsageWindowInput(BaseModel):
    # extra="forbid" rejects any field beyond the two documented here (in
    # particular `used_percentage`, which the server always derives itself
    # and must never accept from the client) instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    remaining_percentage: float
    resets_at: datetime

    @field_validator("remaining_percentage", mode="before")
    @classmethod
    def reject_non_numeric_remaining_percentage(cls, value: object) -> object:
        # Deliberately stricter than Pydantic's default lax-float coercion,
        # which would otherwise accept numeric strings like "42" or "42.0" —
        # this field only ever accepts an actual int/float, matching the
        # manual-entry-only contract documented for this endpoint.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("remaining_percentage must be an int or float (not bool, str, null, list, or dict)")
        return value

    @model_validator(mode="after")
    def validate_window_input(self) -> "ClaudeDesktopCloudUsageWindowInput":
        if not (0 <= self.remaining_percentage <= 100):
            raise ValueError("remaining_percentage must be between 0 and 100")
        if self.resets_at.tzinfo is None or self.resets_at.tzinfo.utcoffset(self.resets_at) is None:
            raise ValueError("resets_at must be timezone-aware")
        return self


class ClaudeDesktopCloudUsageInput(BaseModel):
    # extra="forbid" rejects any unexpected top-level field (e.g. `source`,
    # `observed_at`, or an arbitrary field like `token`) instead of silently
    # ignoring it — both windows are always server-normalized, never passed
    # through from client input.
    model_config = ConfigDict(extra="forbid")

    five_hour: ClaudeDesktopCloudUsageWindowInput | None = None
    seven_day: ClaudeDesktopCloudUsageWindowInput | None = None

    @model_validator(mode="after")
    def validate_both_windows_required(self) -> "ClaudeDesktopCloudUsageInput":
        # Unlike Codex's manual fallback (where auto always wins over manual whenever
        # both are available, so a partial manual snapshot never hides auto data),
        # Claude's display picks whichever snapshot has the newer observed_at as a
        # whole unit (see resolveClaudeCodeUsageDisplay in static/compact.js) and
        # never mixes windows across auto/manual. A partial manual snapshot that's
        # newer than a complete auto snapshot would therefore silently hide a
        # window the user could previously see. Requiring both windows here keeps
        # every manual snapshot self-contained, matching Claude Desktop's own usage
        # panel which always shows both the 5-hour and 7-day windows together.
        if self.five_hour is None or self.seven_day is None:
            raise ValueError("both five_hour and seven_day are required")
        return self


class CodexUsageWindow(BaseModel):
    used_percentage: float
    remaining_percentage: float
    resets_at: str


class CodexUsageSnapshot(BaseModel):
    available: bool
    stale: bool
    status: str
    observed_at: str | None
    source: str | None
    five_hour: CodexUsageWindow | None
    weekly: CodexUsageWindow | None
    error_message: str | None


class CodexUsageWindowInput(BaseModel):
    # extra="forbid" rejects any field beyond the two documented here (in
    # particular `used_percentage`, which the server always derives itself
    # and must never accept from the client) instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    remaining_percentage: float
    resets_at: datetime

    @field_validator("remaining_percentage", mode="before")
    @classmethod
    def reject_non_numeric_remaining_percentage(cls, value: object) -> object:
        # Deliberately stricter than Pydantic's default lax-float coercion,
        # which would otherwise accept numeric strings like "42" or "42.0" —
        # this field only ever accepts an actual int/float, matching the
        # manual-entry-only contract documented for this endpoint.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("remaining_percentage must be an int or float (not bool, str, null, list, or dict)")
        return value

    @model_validator(mode="after")
    def validate_window_input(self) -> "CodexUsageWindowInput":
        if not (0 <= self.remaining_percentage <= 100):
            raise ValueError("remaining_percentage must be between 0 and 100")
        if self.resets_at.tzinfo is None or self.resets_at.tzinfo.utcoffset(self.resets_at) is None:
            raise ValueError("resets_at must be timezone-aware")
        return self


class CodexUsageInput(BaseModel):
    # extra="forbid" rejects any unexpected top-level field (e.g. `source`,
    # `observed_at`, or an arbitrary field like `token`) instead of silently
    # ignoring it — both windows are always server-normalized, never passed
    # through from client input.
    model_config = ConfigDict(extra="forbid")

    five_hour: CodexUsageWindowInput | None = None
    weekly: CodexUsageWindowInput | None = None

    @model_validator(mode="after")
    def validate_at_least_one_window(self) -> "CodexUsageInput":
        if self.five_hour is None and self.weekly is None:
            raise ValueError("at least one of five_hour or weekly is required")
        return self


class CodexRateLimitsWindow(BaseModel):
    used_percentage: float
    remaining_percentage: float
    resets_at: str
    window_duration_minutes: int


class CodexRateLimitsSnapshot(BaseModel):
    fetched: bool
    available: bool
    stale: bool
    status: str
    observed_at: str | None
    source: str | None
    five_hour: CodexRateLimitsWindow | None
    weekly: CodexRateLimitsWindow | None
    error_type: str | None
    user_message: str | None
    refresh_in_progress: bool
    cooldown_remaining_seconds: int
    fallback_available: bool
    fallback_source: str | None
    auto_refresh_enabled: bool
    auto_refresh_interval_seconds: int
    auto_refresh_running: bool
    next_auto_refresh_at: str | None
    last_auto_refresh_attempt_at: str | None
    last_auto_refresh_success_at: str | None
    last_auto_refresh_error_type: str | None


class CollectorImportOutcomeRead(BaseModel):
    reason: str
    vendor: str
    model_name: str
    limit_type: str
    metric_kind: str | None = None
    detail: str | None = None

    model_config = {"from_attributes": True}


class CollectorRunRead(BaseModel):
    id: int
    vendor: str
    dry_run: bool
    status: str
    started_at: datetime
    finished_at: datetime | None
    records_found: int
    records_saved: int
    error_message: str | None
    created_at: datetime
    # Per-record import decisions for this run. Not persisted anywhere (only
    # the aggregate records_found/records_saved counts above are stored on
    # CollectorRun) — populated only on the direct POST /api/collect/{vendor}
    # response; always null when read back later via GET /api/collector-runs.
    outcomes: list[CollectorImportOutcomeRead] | None = None

    model_config = {"from_attributes": True}


class CollectorPreflightStatusRead(BaseModel):
    vendor: str
    configured: bool
    auth_mode: str
    production_ready: bool
    missing_requirements: list[str]
    notes: list[str]

    model_config = {"from_attributes": True}
