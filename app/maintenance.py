from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def normalize_legacy_mvp_sample_defaults(db: Session, dry_run: bool = False) -> int:
    """旧MVPの実在制限に見える初期サンプルだけを手入力待ちへ戻す。"""
    updated = 0
    replacements = [
        ("ChatGPT", "GPT-5", "messages", 80.0, "manual_model_name"),
        ("ChatGPT", "Image generation", "images", 20.0, "manual_image_feature"),
        ("OpenAI API", "configurable", "api_cost", 50.0, "manual_api_project"),
        ("Gemini", "Gemini", "messages", None, "manual_model_name"),
        ("Claude", "Claude", "messages", None, "manual_model_name"),
    ]
    for service_name, old_model, limit_type, old_max, new_model in replacements:
        stmt = (
            select(models.Limit)
            .join(models.Service)
            .where(
                models.Service.name == service_name,
                models.Limit.model_name == old_model,
                models.Limit.limit_type == limit_type,
            )
        )
        if old_max is not None:
            stmt = stmt.where(models.Limit.max_value == old_max)
        for limit in db.scalars(stmt).all():
            updated += 1
            if dry_run:
                continue
            limit.model_name = new_model
            limit.max_value = None
            limit.reset_interval_type = "manual"
            limit.reset_interval_value = 1
            limit.next_reset_at = None
            limit.source_type = "manual_required"
            if service_name in {"ChatGPT", "Gemini", "Claude"}:
                limit.service.plan_name = f"Please enter your actual {service_name} plan manually"
            elif service_name == "OpenAI API":
                limit.service.plan_name = "Please enter your API budget manually"
            db.add(limit)
            db.add(limit.service)
    if not dry_run:
        db.commit()
    return updated


def remove_empty_legacy_transitional_seed_services(db: Session, dry_run: bool = False) -> int:
    removed = 0
    transitional_names = {"ChatGPT Web", "Gemini Web", "Claude Web"}
    services = db.scalars(select(models.Service).where(models.Service.name.in_(transitional_names))).all()
    for service in services:
        has_usage = any(limit.usage_records for limit in service.limits)
        if has_usage:
            continue
        removed += 1
        if not dry_run:
            db.delete(service)
    if not dry_run:
        db.commit()
    return removed


def find_empty_duplicate_manual_required_limits(db: Session) -> list[models.Limit]:
    duplicates: list[models.Limit] = []
    seen: set[tuple[int, str, str]] = set()
    limits = db.scalars(
        select(models.Limit)
        .where(
            models.Limit.source_type == "manual_required",
            models.Limit.max_value.is_(None),
        )
        .order_by(models.Limit.service_id, models.Limit.model_name, models.Limit.limit_type, models.Limit.id)
    ).all()
    for limit in limits:
        key = (limit.service_id, limit.model_name, limit.limit_type)
        if key not in seen:
            seen.add(key)
            continue
        if limit.usage_records:
            continue
        duplicates.append(limit)
    return duplicates


def remove_empty_duplicate_manual_required_limits(db: Session, dry_run: bool = False) -> int:
    duplicates = find_empty_duplicate_manual_required_limits(db)
    if dry_run:
        return len(duplicates)
    for limit in duplicates:
        db.delete(limit)
    db.commit()
    return len(duplicates)


def cleanup_legacy_mvp_data(db: Session, dry_run: bool = False) -> dict[str, int]:
    normalized = normalize_legacy_mvp_sample_defaults(db, dry_run=dry_run)
    removed = remove_empty_legacy_transitional_seed_services(db, dry_run=dry_run)
    removed += remove_empty_duplicate_manual_required_limits(db, dry_run=dry_run)
    return {"normalized": normalized, "removed": removed}
