from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    remaining_percentage: float
    resets_at: datetime

    @model_validator(mode="before")
    @classmethod
    def reject_bool_remaining_percentage(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("remaining_percentage"), bool):
            raise ValueError("remaining_percentage must not be a boolean")
        return data

    @model_validator(mode="after")
    def validate_window_input(self) -> "CodexUsageWindowInput":
        if not (0 <= self.remaining_percentage <= 100):
            raise ValueError("remaining_percentage must be between 0 and 100")
        if self.resets_at.tzinfo is None or self.resets_at.tzinfo.utcoffset(self.resets_at) is None:
            raise ValueError("resets_at must be timezone-aware")
        return self


class CodexUsageInput(BaseModel):
    five_hour: CodexUsageWindowInput | None = None
    weekly: CodexUsageWindowInput | None = None

    @model_validator(mode="after")
    def validate_at_least_one_window(self) -> "CodexUsageInput":
        if self.five_hour is None and self.weekly is None:
            raise ValueError("at least one of five_hour or weekly is required")
        return self


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

    model_config = {"from_attributes": True}
