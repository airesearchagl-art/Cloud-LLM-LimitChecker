from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.maintenance import cleanup_legacy_mvp_data
from app.time_utils import parse_datetime


def seed_from_yaml(db: Session, path: str = "config/seed.yaml") -> dict[str, int]:
    legacy_cleanup = cleanup_legacy_mvp_data(db)
    seed_path = Path(path)
    if not seed_path.exists():
        return {"services": 0, "limits": 0, **legacy_cleanup}

    data = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    created_services = 0
    created_limits = 0

    for service_data in data.get("services", []):
        existing = db.scalar(
            select(models.Service).where(
                models.Service.name == service_data["name"],
                models.Service.provider == service_data["provider"],
                models.Service.account_type == service_data.get("account_type", "web_subscription"),
            )
        )
        service = existing
        if service is None:
            service = crud.create_service(db, schemas.ServiceCreate(**{k: v for k, v in service_data.items() if k != "limits"}))
            created_services += 1
        for limit_data in service_data.get("limits", []):
            exists_limit = db.scalar(
                select(models.Limit).where(
                    models.Limit.service_id == service.id,
                    models.Limit.model_name == limit_data["model_name"],
                    models.Limit.limit_type == limit_data["limit_type"],
                )
            )
            if exists_limit is not None:
                continue
            normalized = dict(limit_data)
            normalized["service_id"] = service.id
            normalized["next_reset_at"] = parse_datetime(normalized.get("next_reset_at"))
            crud.create_limit(db, schemas.LimitCreate(**normalized))
            created_limits += 1

    return {"services": created_services, "limits": created_limits, **legacy_cleanup}
