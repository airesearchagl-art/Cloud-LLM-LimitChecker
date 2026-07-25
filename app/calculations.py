from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.time_utils import advance_next_reset, current_period_start, now_local


def sync_limit_reset(db: Session, limit: models.Limit) -> None:
    advanced = advance_next_reset(limit.next_reset_at, limit.reset_interval_type, limit.reset_interval_value)
    if advanced != limit.next_reset_at:
        limit.next_reset_at = advanced
        limit.updated_at = now_local()
        db.add(limit)


def current_usage(db: Session, limit: models.Limit) -> tuple[float, datetime | None]:
    period_start = current_period_start(limit.next_reset_at, limit.reset_interval_type, limit.reset_interval_value)
    query = select(func.coalesce(func.sum(models.UsageRecord.used_value), 0.0)).where(models.UsageRecord.limit_id == limit.id)
    if period_start is not None:
        query = query.where(models.UsageRecord.recorded_at >= period_start)
    used = float(db.scalar(query) or 0.0)

    last_updated = db.scalar(
        select(func.max(models.UsageRecord.recorded_at)).where(models.UsageRecord.limit_id == limit.id)
    )
    return used, last_updated


def status_for_usage(usage_percent: float | None, warning_threshold: float, critical_threshold: float) -> str:
    if usage_percent is None:
        return "手入力待ち"
    if usage_percent >= 100:
        return "上限到達"
    if usage_percent >= critical_threshold:
        return "危険"
    if usage_percent >= warning_threshold:
        return "注意"
    return "正常"


def limit_to_dashboard(db: Session, limit: models.Limit) -> dict:
    sync_limit_reset(db, limit)
    used, last_updated = current_usage(db, limit)
    remaining = None if limit.max_value is None else max(limit.max_value - used, 0.0)
    percent = None
    if limit.max_value and limit.max_value > 0:
        percent = round((used / limit.max_value) * 100, 2)
    return {
        "limit_id": limit.id,
        "service_id": limit.service_id,
        "service_name": limit.service.name,
        "provider": limit.service.provider,
        "plan_name": limit.service.plan_name,
        "account_type": limit.service.account_type,
        "model_name": limit.model_name,
        "limit_type": limit.limit_type,
        "max_value": limit.max_value,
        "used_value": used,
        "remaining_value": remaining,
        "usage_percent": percent,
        "unit": limit.unit,
        "next_reset_at": limit.next_reset_at,
        "source_type": limit.source_type,
        "status": status_for_usage(percent, limit.warning_threshold, limit.critical_threshold),
        "last_updated_at": last_updated,
    }


def alert_items(db: Session) -> list[dict]:
    items: list[dict] = []
    soon = now_local() + timedelta(hours=12)
    for limit in db.scalars(select(models.Limit).join(models.Service).order_by(models.Service.name)).all():
        row = limit_to_dashboard(db, limit)
        percent = row["usage_percent"]
        if percent is not None and percent >= 70:
            if percent >= 95:
                level = "95%"
            elif percent >= 85:
                level = "85%"
            else:
                level = "70%"
            items.append(
                {
                    "limit_id": limit.id,
                    "alert_level": level,
                    "message": f"{row['service_name']} / {row['model_name']} / {row['limit_type']} が {percent}% に達しています",
                    "usage_percent": percent,
                    "next_reset_at": row["next_reset_at"],
                }
            )
        if row["next_reset_at"] is not None and row["next_reset_at"] <= soon:
            items.append(
                {
                    "limit_id": limit.id,
                    "alert_level": "reset_soon",
                    "message": f"{row['service_name']} / {row['model_name']} のリセットが近づいています",
                    "usage_percent": percent,
                    "next_reset_at": row["next_reset_at"],
                }
            )
    return items
