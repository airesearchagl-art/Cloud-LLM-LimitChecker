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
