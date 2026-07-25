import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.collectors.types import CollectorNormalizedRecord, validate_normalized_record
from app.time_utils import app_tz, now_local


class CollectorImportError(RuntimeError):
    pass


SERVICE_BY_VENDOR = {
    "openai": {"name": "OpenAI API", "provider": "OpenAI"},
    "gemini": {"name": "Gemini API", "provider": "Google"},
    "claude": {"name": "Claude API", "provider": "Anthropic"},
}


def parse_recorded_at(value: str) -> datetime:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CollectorImportError(f"invalid recorded_at: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=app_tz())
    return parsed.astimezone(app_tz())


def build_import_key(record: CollectorNormalizedRecord | dict) -> str:
    normalized = validate_normalized_record(record)
    payload = {
        "vendor": normalized.vendor,
        "source_type": normalized.source_type,
        "project_id": normalized.project_id,
        "organization_id": normalized.organization_id,
        "workspace_id": normalized.workspace_id,
        "model_name": normalized.model_name,
        "limit_type": normalized.limit_type,
        "unit": normalized.unit,
        "recorded_at": normalized.recorded_at,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_api_service(db: Session, record: CollectorNormalizedRecord) -> models.Service:
    mapping = SERVICE_BY_VENDOR[record.vendor]
    service = db.scalar(
        select(models.Service).where(
            models.Service.name == mapping["name"],
            models.Service.provider == mapping["provider"],
            models.Service.account_type == "api",
        )
    )
    if service is not None:
        return service
    now = now_local()
    service = models.Service(
        name=mapping["name"],
        provider=mapping["provider"],
        plan_name="API",
        account_type="api",
        created_at=now,
        updated_at=now,
    )
    db.add(service)
    db.flush()
    return service


def get_or_create_api_limit(
    db: Session,
    service: models.Service,
    record: CollectorNormalizedRecord,
) -> models.Limit:
    limit = db.scalar(
        select(models.Limit).where(
            models.Limit.service_id == service.id,
            models.Limit.model_name == record.model_name,
            models.Limit.limit_type == record.limit_type,
            models.Limit.unit == record.unit,
            models.Limit.source_type == record.source_type,
        )
    )
    if limit is not None:
        return limit
    now = now_local()
    limit = models.Limit(
        service_id=service.id,
        model_name=record.model_name,
        limit_type=record.limit_type,
        max_value=None,
        unit=record.unit,
        reset_interval_type="days",
        reset_interval_value=1,
        next_reset_at=None,
        warning_threshold=70.0,
        critical_threshold=85.0,
        source_type=record.source_type,
        created_at=now,
        updated_at=now,
    )
    db.add(limit)
    db.flush()
    return limit


def import_normalized_records(db: Session, records: list[CollectorNormalizedRecord | dict]) -> int:
    saved = 0
    for raw_record in records:
        record = validate_normalized_record(raw_record)
        recorded_at = parse_recorded_at(record.recorded_at)
        import_key = build_import_key(record)
        existing = db.scalar(select(models.CollectorImport).where(models.CollectorImport.import_key == import_key))
        if existing is not None:
            continue

        service = get_or_create_api_service(db, record)
        limit = get_or_create_api_limit(db, service, record)
        note_parts = [f"Imported from {record.vendor} collector."]
        for key in ("project_id", "organization_id", "workspace_id"):
            value = getattr(record, key)
            if value:
                note_parts.append(f"{key}={value}")
        usage_record = models.UsageRecord(
            limit_id=limit.id,
            used_value=record.used_value,
            recorded_at=recorded_at,
            source_type=record.source_type,
            note=" ".join(note_parts),
        )
        limit.updated_at = now_local()
        db.add(usage_record)
        db.flush()
        db.add(
            models.CollectorImport(
                import_key=import_key,
                vendor=record.vendor,
                source_type=record.source_type,
                usage_record_id=usage_record.id,
                created_at=now_local(),
            )
        )
        saved += 1
    db.commit()
    return saved
