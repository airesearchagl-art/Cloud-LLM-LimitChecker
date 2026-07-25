from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Limit, Service, UsageRecord
from app.time_utils import app_tz


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(app_tz())


def make_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def create_service(db: Session, name: str = "Test") -> Service:
    service = Service(
        name=name,
        provider="TestProvider",
        plan_name="Manual",
        account_type="web_subscription",
        created_at=dt("2026-01-01T00:00:00+09:00"),
        updated_at=dt("2026-01-01T00:00:00+09:00"),
    )
    db.add(service)
    db.flush()
    return service


def create_limit(db: Session, service: Service, max_value: float | None = 100.0) -> Limit:
    limit = Limit(
        service_id=service.id,
        model_name="manual_model",
        limit_type="messages",
        max_value=max_value,
        unit="messages",
        reset_interval_type="days",
        reset_interval_value=1,
        next_reset_at=dt("2026-01-02T00:00:00+09:00"),
        warning_threshold=70,
        critical_threshold=85,
        source_type="manual_required",
        created_at=dt("2026-01-01T00:00:00+09:00"),
        updated_at=dt("2026-01-01T00:00:00+09:00"),
    )
    db.add(limit)
    db.flush()
    return limit


def add_usage_record(db: Session, limit: Limit, used_value: float, recorded_at: str) -> UsageRecord:
    record = UsageRecord(
        limit_id=limit.id,
        used_value=used_value,
        recorded_at=dt(recorded_at),
        source_type="manual",
        note=None,
    )
    db.add(record)
    db.flush()
    return record
