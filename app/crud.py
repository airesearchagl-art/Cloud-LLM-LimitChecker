from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.time_utils import app_tz, now_local


class LimitNotFoundError(Exception):
    pass


class UnsupportedUsageModeError(Exception):
    pass


class InvalidUsageValueError(Exception):
    pass


def create_service(db: Session, payload: schemas.ServiceCreate) -> models.Service:
    now = now_local()
    service = models.Service(**payload.model_dump(), created_at=now, updated_at=now)
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


def create_limit(db: Session, payload: schemas.LimitCreate) -> models.Limit:
    now = now_local()
    limit = models.Limit(**payload.model_dump(), created_at=now, updated_at=now)
    db.add(limit)
    db.commit()
    db.refresh(limit)
    return limit


def update_limit(db: Session, limit_id: int, payload: schemas.LimitUpdate) -> models.Limit:
    limit = db.get(models.Limit, limit_id)
    if limit is None:
        raise LimitNotFoundError("limit not found")

    limit.model_name = payload.model_name
    limit.max_value = payload.max_value
    limit.unit = payload.unit
    limit.reset_interval_type = payload.reset_interval_type
    limit.next_reset_at = payload.next_reset_at
    limit.updated_at = now_local()
    db.add(limit)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(limit)
    return limit


def add_usage(db: Session, limit_id: int, payload: schemas.UsageCreate) -> models.UsageRecord:
    limit = db.get(models.Limit, limit_id)
    if limit is None:
        raise LimitNotFoundError("limit not found")
    if payload.mode == "set":
        raise UnsupportedUsageModeError("usage mode 'set' is reserved for a future implementation")
    if payload.mode == "add" and payload.used_value <= 0:
        raise InvalidUsageValueError("mode 'add' requires used_value > 0")
    if payload.mode == "adjust":
        if payload.used_value == 0:
            raise InvalidUsageValueError("mode 'adjust' requires non-zero used_value")
        if not payload.note or not payload.note.strip():
            raise InvalidUsageValueError("mode 'adjust' requires note")

    source_type = payload.source_type
    if payload.mode == "adjust" and source_type == "manual":
        source_type = "manual_adjustment"

    record = models.UsageRecord(
        limit_id=limit_id,
        used_value=payload.used_value,
        recorded_at=payload.recorded_at or now_local(),
        source_type=source_type,
        note=payload.note,
    )
    limit.updated_at = now_local()
    db.add(record)
    db.add(limit)
    db.commit()
    db.refresh(record)
    return record


def list_services(db: Session) -> list[models.Service]:
    return list(db.scalars(select(models.Service).order_by(models.Service.name)).all())


def list_limits(db: Session, service_id: int | None = None) -> list[models.Limit]:
    stmt = select(models.Limit).join(models.Service).order_by(models.Service.name, models.Limit.model_name)
    if service_id is not None:
        stmt = stmt.where(models.Limit.service_id == service_id)
    return list(db.scalars(stmt).all())


def list_usage_records_with_limit(db: Session, limit_id: int | None = None) -> list[dict]:
    stmt = (
        select(models.UsageRecord, models.Limit, models.Service)
        .join(models.Limit, models.UsageRecord.limit_id == models.Limit.id)
        .join(models.Service, models.Limit.service_id == models.Service.id)
        .order_by(models.UsageRecord.recorded_at.desc(), models.UsageRecord.id.desc())
    )
    if limit_id is not None:
        stmt = stmt.where(models.UsageRecord.limit_id == limit_id)

    rows = []
    for record, limit, service in db.execute(stmt).all():
        rows.append(
            {
                "usage_record_id": record.id,
                "limit_id": record.limit_id,
                "service_name": service.name,
                "provider": service.provider,
                "plan_name": service.plan_name,
                "account_type": service.account_type,
                "model_name": limit.model_name,
                "limit_type": limit.limit_type,
                "used_value": record.used_value,
                "unit": limit.unit,
                "source_type": record.source_type,
                "note": record.note,
                "recorded_at": record.recorded_at,
            }
        )
    return rows


def create_collector_run(db: Session, vendor: str, dry_run: bool) -> models.CollectorRun:
    now = now_local()
    run = models.CollectorRun(
        vendor=vendor.strip().lower(),
        dry_run=dry_run,
        status="started",
        started_at=now,
        created_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _get_collector_run(db: Session, run_id: int) -> models.CollectorRun:
    run = db.get(models.CollectorRun, run_id)
    if run is None:
        raise ValueError("collector run not found")
    return run


def finish_collector_run_success(
    db: Session,
    run_id: int,
    records_found: int,
    records_saved: int,
) -> models.CollectorRun:
    run = _get_collector_run(db, run_id)
    run.status = "success"
    run.finished_at = now_local()
    run.records_found = records_found
    run.records_saved = records_saved
    run.error_message = None
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_collector_run_failed(db: Session, run_id: int, error_message: str) -> models.CollectorRun:
    run = _get_collector_run(db, run_id)
    run.status = "failed"
    run.finished_at = now_local()
    run.error_message = error_message
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_collector_run_blocked(db: Session, run_id: int, error_message: str) -> models.CollectorRun:
    run = _get_collector_run(db, run_id)
    run.status = "blocked"
    run.finished_at = now_local()
    run.error_message = error_message
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def count_collector_runs_today(db: Session, vendor: str) -> int:
    tz = app_tz()
    start = now_local().astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    count = db.scalar(
        select(func.count(models.CollectorRun.id)).where(
            models.CollectorRun.vendor == vendor.strip().lower(),
            models.CollectorRun.created_at >= start,
            models.CollectorRun.created_at < end,
        )
    )
    return int(count or 0)


def list_collector_runs(db: Session, vendor: str | None = None) -> list[models.CollectorRun]:
    stmt = select(models.CollectorRun).order_by(models.CollectorRun.started_at.desc(), models.CollectorRun.id.desc())
    if vendor:
        stmt = stmt.where(models.CollectorRun.vendor == vendor.strip().lower())
    return list(db.scalars(stmt).all())
