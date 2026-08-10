from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(120), nullable=False, default="manual")
    account_type: Mapped[str] = mapped_column(String(40), nullable=False, default="web_subscription")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    limits: Mapped[list["Limit"]] = relationship(back_populates="service", cascade="all, delete-orphan")
    credentials: Mapped[list["ApiCredential"]] = relationship(back_populates="service", cascade="all, delete-orphan")


class Limit(Base):
    __tablename__ = "limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    limit_type: Mapped[str] = mapped_column(String(80), nullable=False)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    reset_interval_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    reset_interval_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warning_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=70.0)
    critical_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=85.0)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    service: Mapped[Service] = relationship(back_populates="limits")
    usage_records: Mapped[list["UsageRecord"]] = relationship(back_populates="limit", cascade="all, delete-orphan")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="limit", cascade="all, delete-orphan")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    limit_id: Mapped[int] = mapped_column(ForeignKey("limits.id"), nullable=False, index=True)
    used_value: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    limit: Mapped[Limit] = relationship(back_populates="usage_records")


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    credential_name: Mapped[str] = mapped_column(String(120), nullable=False)
    env_var_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    service: Mapped[Service] = relationship(back_populates="credentials")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    limit_id: Mapped[int] = mapped_column(ForeignKey("limits.id"), nullable=False, index=True)
    alert_level: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    limit: Mapped[Limit] = relationship(back_populates="alerts")


class CollectorRun(Base):
    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vendor: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="started", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_saved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CollectorImport(Base):
    __tablename__ = "collector_imports"
    __table_args__ = (UniqueConstraint("import_key", name="uq_collector_imports_import_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    import_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    vendor: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    usage_record_id: Mapped[int] = mapped_column(ForeignKey("usage_records.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GitHubDiagnosticSession(Base):
    """A user-named "activity session" for GitHub GraphQL Consumption
    Diagnostics v0.1 (see `app.github_graphql_diagnostics`'s module docstring
    for the correlation-not-attribution design this table supports)."""

    __tablename__ = "github_diagnostic_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(60), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    repository: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    github_login: Mapped[str | None] = mapped_column(String(120), nullable=True)
    github_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reset_at_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    graphql_used_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_used_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_delta_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attribution_status: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE", index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)


class GitHubRateSample(Base):
    """A single point-in-time sample of the GitHub `graphql` rate-limit
    resource, taken either on a scheduled sampler tick or immediately around
    a session start/stop. Samples form one global timeline; which session(s)
    (if any) were active at a given sample is always derived at query time
    from `collected_at` vs. each session's `started_at`/`ended_at`, never
    from a stored ownership link (see `trigger_session_id` below)."""

    __tablename__ = "github_rate_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    core_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    graphql_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(40), nullable=False)
    attribution_status: Mapped[str] = mapped_column(String(40), nullable=False)
    # NOT an ownership/attribution link -- this only records which session's
    # START or STOP action triggered an *immediate*, out-of-cycle sample (as
    # opposed to a regular scheduled sampler tick, which always has
    # trigger_session_id=None). It never means "this sample belongs to this
    # session"; which session(s) were active during a sample is always
    # derived separately, at query/report time, from this sample's
    # collected_at compared against each session's started_at/ended_at.
    trigger_session_id: Mapped[int | None] = mapped_column(ForeignKey("github_diagnostic_sessions.id"), nullable=True)
