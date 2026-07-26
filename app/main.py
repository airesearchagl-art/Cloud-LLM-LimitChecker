import os
import base64
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.calculations import alert_items, limit_to_dashboard
from app.collectors.importer import CollectorImportError, import_normalized_records
from app.collectors.claude_collector import (
    ClaudeCollectorConfigError,
    ClaudeManagementAPIError,
    ClaudeManagementNetworkError,
    ClaudeUsageCostCollector,
)
from app.collectors.gemini_collector import (
    GeminiCollectorConfigError,
    GeminiManagementAPIError,
    GeminiManagementNetworkError,
    GeminiUsageCostCollector,
)
from app.collectors.openai_collector import (
    OpenAICollectorConfigError,
    OpenAIManagementAPIError,
    OpenAIManagementNetworkError,
    OpenAIUsageCostCollector,
)
from app.database import Base, SessionLocal, engine, get_db
from app.exporter import export_json, export_limits_csv, export_usage_records_csv
from app.seed import seed_from_yaml
from app.safety import (
    CollectorDailyLimitExceededError,
    UnknownCollectorVendorError,
    assert_collector_daily_limit_not_exceeded,
    collector_dry_run_default,
    collector_enabled,
    normalize_collector_vendor,
    vendor_collectors_enabled,
)


def is_seed_api_enabled() -> bool:
    return os.getenv("ENABLE_SEED_API", "false").strip().lower() == "true"


def is_basic_auth_enabled() -> bool:
    return os.getenv("ENABLE_BASIC_AUTH", "false").strip().lower() == "true"


def basic_auth_credentials() -> tuple[str, str]:
    return os.getenv("BASIC_AUTH_USERNAME", ""), os.getenv("BASIC_AUTH_PASSWORD", "")


def unauthorized_response(detail: str = "authentication required", status_code: int = 401) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="Cloud LLM Limit Checker"'} if status_code == 401 else {}
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


def is_authorized_basic_header(header_value: str | None) -> bool:
    username, password = basic_auth_credentials()
    if not username or not password:
        return False
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value.removeprefix("Basic ").strip()).decode("utf-8")
    except Exception:
        return False
    supplied_username, separator, supplied_password = decoded.partition(":")
    if separator != ":":
        return False
    return secrets.compare_digest(supplied_username, username) and secrets.compare_digest(supplied_password, password)


class OptionalBasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        if not is_basic_auth_enabled():
            return await call_next(request)
        username, password = basic_auth_credentials()
        if not username or not password:
            return unauthorized_response("basic auth is enabled but credentials are not configured", status_code=503)
        if not is_authorized_basic_header(request.headers.get("Authorization")):
            return unauthorized_response()
        return await call_next(request)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_from_yaml(db)
    yield


app = FastAPI(title="Cloud LLM Limit Checker", version="0.1.0", lifespan=lifespan)
app.add_middleware(OptionalBasicAuthMiddleware)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/seed")
def run_seed(db: Session = Depends(get_db)) -> dict[str, int]:
    if not is_seed_api_enabled():
        raise HTTPException(status_code=403, detail="seed API is disabled")
    return seed_from_yaml(db)


@app.get("/api/services", response_model=list[schemas.ServiceRead])
def services(db: Session = Depends(get_db)) -> list[models.Service]:
    return crud.list_services(db)


@app.post("/api/services", response_model=schemas.ServiceRead)
def create_service(payload: schemas.ServiceCreate, db: Session = Depends(get_db)) -> models.Service:
    return crud.create_service(db, payload)


@app.get("/api/limits", response_model=list[schemas.LimitRead])
def limits(service_id: int | None = None, db: Session = Depends(get_db)) -> list[models.Limit]:
    return crud.list_limits(db, service_id)


@app.post("/api/limits", response_model=schemas.LimitRead)
def create_limit(payload: schemas.LimitCreate, db: Session = Depends(get_db)) -> models.Limit:
    if db.get(models.Service, payload.service_id) is None:
        raise HTTPException(status_code=404, detail="service not found")
    return crud.create_limit(db, payload)


@app.put("/api/limits/{limit_id}", response_model=schemas.LimitRead)
def update_limit(limit_id: int, payload: schemas.LimitUpdate, db: Session = Depends(get_db)) -> models.Limit:
    try:
        return crud.update_limit(db, limit_id, payload)
    except crud.LimitNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/limits/{limit_id}/usage", response_model=schemas.UsageRead)
def add_usage(limit_id: int, payload: schemas.UsageCreate, db: Session = Depends(get_db)) -> models.UsageRecord:
    try:
        return crud.add_usage(db, limit_id, payload)
    except crud.LimitNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except crud.UnsupportedUsageModeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except crud.InvalidUsageValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/usage-records", response_model=list[schemas.UsageRecordWithLimit])
def usage_records(limit_id: int | None = None, db: Session = Depends(get_db)) -> list[dict]:
    return crud.list_usage_records_with_limit(db, limit_id=limit_id)


@app.post("/api/collect/{vendor}", response_model=schemas.CollectorRunRead)
def run_collector(
    vendor: str,
    dry_run: bool | None = None,
    db: Session = Depends(get_db),
) -> models.CollectorRun:
    try:
        vendor_name = normalize_collector_vendor(vendor)
    except UnknownCollectorVendorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dry_run_value = collector_dry_run_default() if dry_run is None else dry_run
    if not vendor_collectors_enabled():
        run = crud.create_collector_run(db, vendor_name, dry_run_value)
        crud.finish_collector_run_blocked(db, run.id, "vendor collectors are disabled")
        raise HTTPException(status_code=403, detail="vendor collectors are disabled")
    if not collector_enabled(vendor_name):
        run = crud.create_collector_run(db, vendor_name, dry_run_value)
        crud.finish_collector_run_blocked(db, run.id, f"{vendor_name} collector is disabled")
        raise HTTPException(status_code=403, detail=f"{vendor_name} collector is disabled")

    try:
        assert_collector_daily_limit_not_exceeded(db, vendor_name)
    except CollectorDailyLimitExceededError as exc:
        run = crud.create_collector_run(db, vendor_name, dry_run_value)
        crud.finish_collector_run_blocked(db, run.id, str(exc))
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    run = crud.create_collector_run(db, vendor_name, dry_run_value)
    if vendor_name == "gemini":
        return run_gemini_collector(db, run, dry_run_value)
    if vendor_name == "claude":
        return run_claude_collector(db, run, dry_run_value)
    if vendor_name != "openai":
        return crud.finish_collector_run_success(db, run.id, records_found=0, records_saved=0)

    return run_openai_collector(db, run, dry_run_value)


def run_openai_collector(db: Session, run: models.CollectorRun, dry_run: bool) -> models.CollectorRun:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        crud.finish_collector_run_blocked(db, run.id, "OPENAI_API_KEY is not configured")
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is not configured")
    try:
        rows = OpenAIUsageCostCollector(api_key=api_key).collect()
    except OpenAICollectorConfigError as exc:
        crud.finish_collector_run_blocked(db, run.id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OpenAIManagementAPIError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OpenAIManagementNetworkError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        crud.finish_collector_run_failed(db, run.id, "unexpected OpenAI collector error")
        raise HTTPException(status_code=500, detail="unexpected OpenAI collector error") from exc
    return finish_collector_import(db, run, rows, dry_run)


def run_gemini_collector(db: Session, run: models.CollectorRun, dry_run: bool) -> models.CollectorRun:
    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    access_token = os.getenv("GOOGLE_CLOUD_ACCESS_TOKEN", "").strip() or None
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip() or os.getenv("GEMINI_PROJECT_ID", "").strip() or None
    if not api_key and not access_token:
        crud.finish_collector_run_blocked(
            db,
            run.id,
            "GEMINI_API_KEY or Google Cloud management credentials are not configured",
        )
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY or Google Cloud management credentials are not configured",
        )
    try:
        rows = GeminiUsageCostCollector(
            api_key=api_key,
            access_token=access_token,
            project_id=project_id,
        ).collect()
    except GeminiCollectorConfigError as exc:
        crud.finish_collector_run_blocked(db, run.id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GeminiManagementAPIError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except GeminiManagementNetworkError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        crud.finish_collector_run_failed(db, run.id, "unexpected Gemini collector error")
        raise HTTPException(status_code=500, detail="unexpected Gemini collector error") from exc
    return finish_collector_import(db, run, rows, dry_run)


def run_claude_collector(db: Session, run: models.CollectorRun, dry_run: bool) -> models.CollectorRun:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    organization_id = os.getenv("ANTHROPIC_ORGANIZATION_ID", "").strip() or None
    workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip() or None
    if not api_key:
        crud.finish_collector_run_blocked(db, run.id, "ANTHROPIC_API_KEY is not configured")
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY is not configured")
    try:
        rows = ClaudeUsageCostCollector(
            api_key=api_key,
            organization_id=organization_id,
            workspace_id=workspace_id,
        ).collect()
    except ClaudeCollectorConfigError as exc:
        crud.finish_collector_run_blocked(db, run.id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClaudeManagementAPIError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ClaudeManagementNetworkError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        crud.finish_collector_run_failed(db, run.id, "unexpected Claude collector error")
        raise HTTPException(status_code=500, detail="unexpected Claude collector error") from exc
    return finish_collector_import(db, run, rows, dry_run)


def finish_collector_import(
    db: Session,
    run: models.CollectorRun,
    rows: list[dict],
    dry_run: bool,
) -> models.CollectorRun:
    if dry_run:
        return crud.finish_collector_run_success(db, run.id, records_found=len(rows), records_saved=0)
    try:
        records_saved = import_normalized_records(db, rows)
    except CollectorImportError as exc:
        crud.finish_collector_run_failed(db, run.id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return crud.finish_collector_run_success(db, run.id, records_found=len(rows), records_saved=records_saved)


@app.get("/api/collector-runs", response_model=list[schemas.CollectorRunRead])
def collector_runs(vendor: str | None = None, db: Session = Depends(get_db)) -> list[models.CollectorRun]:
    return crud.list_collector_runs(db, vendor=vendor)


@app.get("/api/dashboard", response_model=list[schemas.DashboardLimit])
def dashboard(db: Session = Depends(get_db)) -> list[dict]:
    rows = [limit_to_dashboard(db, limit) for limit in crud.list_limits(db)]
    db.commit()
    return rows


@app.get("/api/alerts", response_model=list[schemas.AlertRead])
def alerts(db: Session = Depends(get_db)) -> list[dict]:
    return alert_items(db)


@app.get("/api/export/json")
def export(db: Session = Depends(get_db)) -> JSONResponse:
    return JSONResponse(jsonable_encoder(export_json(db)))


@app.get("/api/export/limits.csv")
def export_limits_as_csv(db: Session = Depends(get_db)) -> Response:
    return Response(
        content=export_limits_csv(db),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="limits.csv"'},
    )


@app.get("/api/export/usage-records.csv")
def export_usage_records_as_csv(db: Session = Depends(get_db)) -> Response:
    return Response(
        content=export_usage_records_csv(db),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="usage-records.csv"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
