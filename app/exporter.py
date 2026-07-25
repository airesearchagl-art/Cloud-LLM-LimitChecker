import csv
from io import StringIO
from typing import Any

from sqlalchemy.orm import Session

from app import crud
from app.calculations import limit_to_dashboard
from app.time_utils import app_tz


LIMIT_CSV_COLUMNS = [
    "service_name",
    "provider",
    "plan_name",
    "account_type",
    "model_name",
    "limit_type",
    "max_value",
    "used_value",
    "remaining_value",
    "usage_percent",
    "unit",
    "status",
    "next_reset_at",
    "source_type",
    "last_updated_at",
]

USAGE_RECORDS_CSV_COLUMNS = [
    "usage_record_id",
    "limit_id",
    "service_name",
    "provider",
    "plan_name",
    "account_type",
    "model_name",
    "limit_type",
    "used_value",
    "unit",
    "source_type",
    "note",
    "recorded_at",
]


def build_export_payload(db: Session) -> dict:
    """Return a shared payload for JSON and CSV exporters."""
    return {
        "services": [
            {
                "id": service.id,
                "name": service.name,
                "provider": service.provider,
                "plan_name": service.plan_name,
                "account_type": service.account_type,
                "created_at": service.created_at.isoformat(),
                "updated_at": service.updated_at.isoformat(),
            }
            for service in crud.list_services(db)
        ],
        "limits": [limit_to_dashboard(db, limit) for limit in crud.list_limits(db)],
    }


def export_json(db: Session) -> dict:
    return build_export_payload(db)


def export_limit_rows(db: Session) -> list[dict]:
    return build_export_payload(db)["limits"]


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=app_tz())
        return value.isoformat()
    return str(value)


def rows_to_csv(rows: list[dict], columns: list[str]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: csv_cell(row.get(column)) for column in columns})
    return "\ufeff" + output.getvalue()


def export_limits_csv(db: Session) -> str:
    return rows_to_csv(export_limit_rows(db), LIMIT_CSV_COLUMNS)


def export_usage_records_csv(db: Session) -> str:
    return rows_to_csv(crud.list_usage_records_with_limit(db), USAGE_RECORDS_CSV_COLUMNS)
