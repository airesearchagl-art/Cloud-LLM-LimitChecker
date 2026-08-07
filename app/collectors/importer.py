import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.collectors.types import (
    PERSISTABLE_METRIC_KINDS,
    CollectorNormalizedRecord,
    validate_normalized_record,
)
from app.time_utils import app_tz, now_local


class CollectorImportError(RuntimeError):
    pass


SERVICE_BY_VENDOR = {
    "openai": {"name": "OpenAI API", "provider": "OpenAI"},
    "gemini": {"name": "Gemini API", "provider": "Google"},
    "claude": {"name": "Claude API", "provider": "Anthropic"},
}

_VALUE_EQUALITY_TOLERANCE = 1e-9


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
    # Identity is the (vendor, source, scope, metric, unit, period) tuple —
    # never used_value (a revised value for the same identity must UPDATE the
    # existing row, not create a duplicate or be silently ignored — see
    # _plan_and_apply). period_start/period_end (not the display-only
    # recorded_at string) pin the exact bucket, since they are validated
    # timezone-aware datetimes rather than a loosely-typed string.
    normalized = validate_normalized_record(record)
    payload = {
        "vendor": normalized.vendor,
        "source_type": normalized.source_type,
        "project_id": normalized.project_id,
        "organization_id": normalized.organization_id,
        "workspace_id": normalized.workspace_id,
        "model_name": normalized.model_name,
        "limit_type": normalized.limit_type,
        "metric_kind": normalized.metric_kind,
        "unit": normalized.unit,
        "period_start": normalized.period_start.isoformat(),
        "period_end": normalized.period_end.isoformat(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Pre-existing collector_imports rows (written before metric_kind/period_start/
# period_end/canonical units were introduced) were keyed on:
#   vendor, source_type, project_id, organization_id, workspace_id,
#   model_name, limit_type, unit, recorded_at
# — with `unit` a free string rather than today's canonical vocabulary. Only
# the unit representation actually changed for token-count rows (both
# input_tokens and output_tokens previously shared the generic unit
# "tokens"); "requests" and currency codes like "usd" were already identical
# to their current canonical form. cache_read_tokens/cache_creation_tokens/
# total_tokens/quota_count have no legacy equivalent — they did not exist
# before this PR — so there is nothing to look up for them.
_LEGACY_UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("tokens",),
    "output_tokens": ("tokens",),
    "requests": ("requests",),
    "usd": ("usd",),
}


def _legacy_recorded_at_candidates(record: CollectorNormalizedRecord) -> list[str]:
    # The legacy scheme's `recorded_at` was always derived from the bucket's
    # end-of-window value, but exactly how differed by vendor (some
    # reformatted through app_tz(), one passed the raw API string through
    # unchanged) — there is no single byte-exact reconstruction available
    # without re-running the old collector code. Rather than guess wrong and
    # miss a real match, try every representation an old row could plausibly
    # have used; the SHA256 comparison is still exact, so this only ever
    # widens what CAN be found — it can never cause a false match.
    seen: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in seen:
            seen.append(value)

    add(record.recorded_at)
    add(record.period_end.isoformat())
    try:
        add(record.period_end.astimezone(app_tz()).isoformat())
    except (OverflowError, OSError):
        pass
    return seen


def build_legacy_import_key_candidates(record: CollectorNormalizedRecord) -> list[str]:
    """Compute the import_key(s) the same record could have hashed to under
    the pre-metric_kind/period legacy scheme, so a pre-existing row can be
    found and re-keyed instead of silently duplicated. Best-effort: only
    ever used for lookup (never for writing — new writes always use
    build_import_key's current scheme), and an exact SHA256 match is still
    required, so a wrong/unmatched guess can only miss a real legacy row
    (falling back to creating a new one, i.e. today's behavior), never
    misattribute a record to the wrong existing row."""
    candidates: list[str] = []
    seen_keys: set[str] = set()
    for legacy_unit in _LEGACY_UNIT_ALIASES.get(record.unit, ()):
        for legacy_recorded_at in _legacy_recorded_at_candidates(record):
            payload = {
                "vendor": record.vendor,
                "source_type": record.source_type,
                "project_id": record.project_id,
                "organization_id": record.organization_id,
                "workspace_id": record.workspace_id,
                "model_name": record.model_name,
                "limit_type": record.limit_type,
                "unit": legacy_unit,
                "recorded_at": legacy_recorded_at,
            }
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if key not in seen_keys:
                seen_keys.add(key)
                candidates.append(key)
    return candidates


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


@dataclass(slots=True)
class ImportOutcome:
    """Per-record import decision. Returned to the caller (and, for
    POST /api/collect/{vendor}, surfaced in the API response) but never
    persisted to the database — CollectorRun only stores aggregate
    records_found/records_saved counts, so this breakdown only exists for the
    lifetime of a single request/response."""

    reason: str
    vendor: str
    model_name: str
    limit_type: str
    metric_kind: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class ImportResult:
    records_saved: int = 0
    outcomes: list[ImportOutcome] = field(default_factory=list)


def _values_equal(a: float, b: float) -> bool:
    return abs(a - b) < _VALUE_EQUALITY_TOLERANCE


def _plan_and_apply(
    db: Session,
    records: list[CollectorNormalizedRecord | dict],
    *,
    persist: bool,
) -> ImportResult:
    result = ImportResult()
    try:
        for raw_record in records:
            try:
                record = validate_normalized_record(raw_record)
            except Exception as exc:
                if persist:
                    # Preserves the original fail-fast/whole-batch-rollback
                    # contract for real writes (see
                    # test_import_rolls_back_earlier_records_when_later_record_fails).
                    raise
                label = raw_record.get("model_name", "unknown") if isinstance(raw_record, dict) else "unknown"
                result.outcomes.append(
                    ImportOutcome(
                        reason="invalid_record",
                        vendor=raw_record.get("vendor", "unknown") if isinstance(raw_record, dict) else "unknown",
                        model_name=label,
                        limit_type=raw_record.get("limit_type", "unknown") if isinstance(raw_record, dict) else "unknown",
                        detail="record failed normalized-record validation",
                    )
                )
                continue

            if record.metric_kind not in PERSISTABLE_METRIC_KINDS:
                result.outcomes.append(
                    ImportOutcome(
                        reason="unsupported_metric_kind",
                        vendor=record.vendor,
                        model_name=record.model_name,
                        limit_type=record.limit_type,
                        metric_kind=record.metric_kind,
                        detail=f"metric_kind={record.metric_kind!r} is never persisted as a UsageRecord",
                    )
                )
                continue

            recorded_at = parse_recorded_at(record.recorded_at)
            import_key = build_import_key(record)
            existing = db.scalar(
                select(models.CollectorImport).where(models.CollectorImport.import_key == import_key)
            )
            matched_legacy_key: str | None = None
            if existing is None:
                # New-key match takes precedence and is always checked first
                # (above) — legacy candidates are only ever consulted when no
                # current-scheme row exists, so a legacy match can never
                # compete with or override a real new-scheme row.
                for legacy_key in build_legacy_import_key_candidates(record):
                    legacy_match = db.scalar(
                        select(models.CollectorImport).where(models.CollectorImport.import_key == legacy_key)
                    )
                    if legacy_match is not None:
                        existing = legacy_match
                        matched_legacy_key = legacy_key
                        break

            if existing is not None:
                existing_usage_record = db.get(models.UsageRecord, existing.usage_record_id)
                same_value = existing_usage_record is not None and _values_equal(
                    existing_usage_record.used_value, record.used_value
                )
                legacy_detail = (
                    "matched a legacy import key; re-keyed to the current scheme"
                    if matched_legacy_key is not None
                    else None
                )
                if same_value:
                    if persist and matched_legacy_key is not None:
                        existing.import_key = import_key
                        db.add(existing)
                        db.flush()
                    result.outcomes.append(
                        ImportOutcome(
                            reason="duplicate",
                            vendor=record.vendor,
                            model_name=record.model_name,
                            limit_type=record.limit_type,
                            metric_kind=record.metric_kind,
                            detail=legacy_detail,
                        )
                    )
                    continue
                if not persist:
                    result.outcomes.append(
                        ImportOutcome(
                            reason="dry_run",
                            vendor=record.vendor,
                            model_name=record.model_name,
                            limit_type=record.limit_type,
                            metric_kind=record.metric_kind,
                            detail=legacy_detail or "would update the existing record with a revised value",
                        )
                    )
                    continue
                existing_usage_record.used_value = record.used_value
                existing_usage_record.recorded_at = recorded_at
                existing_usage_record.limit.updated_at = now_local()
                if matched_legacy_key is not None:
                    existing.import_key = import_key
                    db.add(existing)
                db.add(existing_usage_record)
                db.flush()
                result.records_saved += 1
                result.outcomes.append(
                    ImportOutcome(
                        reason="updated",
                        vendor=record.vendor,
                        model_name=record.model_name,
                        limit_type=record.limit_type,
                        metric_kind=record.metric_kind,
                        detail=legacy_detail,
                    )
                )
                continue

            if not persist:
                result.outcomes.append(
                    ImportOutcome(
                        reason="dry_run",
                        vendor=record.vendor,
                        model_name=record.model_name,
                        limit_type=record.limit_type,
                        metric_kind=record.metric_kind,
                        detail="would be imported as a new record",
                    )
                )
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
            result.records_saved += 1
            result.outcomes.append(
                ImportOutcome(
                    reason="imported",
                    vendor=record.vendor,
                    model_name=record.model_name,
                    limit_type=record.limit_type,
                    metric_kind=record.metric_kind,
                )
            )
        if persist:
            db.commit()
    except CollectorImportError:
        if persist:
            db.rollback()
        raise
    except Exception as exc:
        if persist:
            db.rollback()
        raise CollectorImportError(str(exc)) from exc

    return result


def import_normalized_records(db: Session, records: list[CollectorNormalizedRecord | dict]) -> int:
    """Persist eligible records (writes + commits). Returns the count actually
    saved (imported + updated, matching the pre-existing contract). Kept as
    the original int-returning entry point for backward compatibility; see
    import_normalized_records_detailed for the same write path with
    per-record reasons, and plan_normalized_records for the dry_run-safe
    (no-write) variant."""
    return _plan_and_apply(db, records, persist=True).records_saved


def import_normalized_records_detailed(
    db: Session, records: list[CollectorNormalizedRecord | dict]
) -> ImportResult:
    """Same write path as import_normalized_records, but returns the full
    ImportResult (records_saved + per-record outcomes) instead of just a
    count."""
    return _plan_and_apply(db, records, persist=True)


def plan_normalized_records(db: Session, records: list[CollectorNormalizedRecord | dict]) -> ImportResult:
    """Read-only: validates records and computes exactly what
    import_normalized_records WOULD do (imported/updated/duplicate/
    unsupported_metric_kind/invalid_record), without writing anything to the
    database. Used for dry_run so a preview reflects real import decisions
    instead of just a raw fetched-row count."""
    return _plan_and_apply(db, records, persist=False)
