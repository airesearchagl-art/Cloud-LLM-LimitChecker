import os
import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.calculations import alert_items, limit_to_dashboard
from app.claude_code_usage_cache import load_snapshot as load_claude_code_usage_snapshot
from app import codex_usage_cache
from app import codex_rate_limits_cache
from app.codex_rate_limits_adapter import fetch_codex_rate_limits
from app.codex_rate_limits_state import (
    CodexRateLimitsController,
    CodexRateLimitsRefreshCooldownError,
    CodexRateLimitsRefreshInProgressError,
)
from app.codex_rate_limits_scheduler import CodexRateLimitsScheduler
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
from app.github_rate_limit_cli import fetch_github_rate_limit
from app.github_rate_limit_state import (
    GitHubRateLimitController,
    GitHubRateLimitRefreshCooldownError,
    GitHubRateLimitRefreshInProgressError,
)
from app.seed import seed_from_yaml
from app.time_utils import app_tz
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
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_from_yaml(db)
    # Captured on the real running event loop so the reset-aware auto-refresh
    # timer (scheduled from a sync, threadpool-executed endpoint) can be
    # registered via `call_soon_threadsafe` onto the loop that's actually
    # running — not a throwaway loop implicitly created on a worker thread.
    app_instance.state.event_loop = asyncio.get_running_loop()
    # Strong references to in-flight auto-refresh Tasks, so they are not
    # garbage-collected mid-execution (a well-known asyncio.create_task
    # pitfall). Discarded automatically once each task finishes.
    app_instance.state.auto_refresh_tasks = set()
    # Exactly one periodic Codex rate limit refresh task per process, started
    # only here (never at module import time) and always cancelled + awaited
    # on shutdown — so re-entering this lifespan (as TestClient does per
    # `with` block) never leaves a task running or accumulates a second one.
    app_instance.state.codex_rate_limits_scheduler.start()
    try:
        yield
    finally:
        await app_instance.state.codex_rate_limits_scheduler.stop()


app = FastAPI(title="Cloud LLM Limit Checker", version="0.1.0", lifespan=lifespan)
app.add_middleware(OptionalBasicAuthMiddleware)

# Process-local only (see GitHubRateLimitController docstring): not persisted,
# not shared across worker processes. Page load never triggers a fetch here —
# only the POST refresh endpoint below calls the CLI adapter.
app.state.github_rate_limit_controller = GitHubRateLimitController()

# Process-local only (see CodexRateLimitsController docstring): the actual
# rate limit data lives in the file-based cache, not here. Page load never
# starts Codex App Server — only the POST refresh endpoint below does.
app.state.codex_rate_limits_controller = CodexRateLimitsController()

# Process-local only (see CodexRateLimitsScheduler docstring): reuses the
# same controller/adapter the manual refresh endpoint uses, so a periodic
# attempt and a manual click are mutually exclusive through the same lock.
# Constructed here (env-driven enabled/interval read once, not re-read on
# each cycle) but only started/stopped inside `lifespan` above.
app.state.codex_rate_limits_scheduler = CodexRateLimitsScheduler(
    controller=app.state.codex_rate_limits_controller,
    fetch=fetch_codex_rate_limits,
)


def _current_utc_time() -> datetime:
    return datetime.now(timezone.utc)


# Codex usage input is user-typed (never a stored secret), but a mistyped
# value pasted into the wrong field could still be something sensitive-looking.
# FastAPI/Pydantic's default 422 body echoes the raw offending value back in
# each error's `input` (and sometimes `ctx`) — harmless for every other
# endpoint in this app, but specifically avoided here per this endpoint's own
# "never echo raw input" contract. Every other route keeps the default
# behavior unchanged.
@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path != "/api/codex-usage":
        return await request_validation_exception_handler(request, exc)
    sanitized = [{"type": error.get("type"), "loc": error.get("loc"), "msg": error.get("msg")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": sanitized})


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


@app.get("/api/github-rate-limit")
def get_github_rate_limit() -> dict:
    """Return the currently held state only — never runs `gh` here."""
    controller: GitHubRateLimitController = app.state.github_rate_limit_controller
    return controller.snapshot()


def _run_scheduled_auto_refresh() -> None:
    """Timer callback, fired once on the event loop thread by `call_later`.

    Must return immediately: it only schedules an asyncio Task and never
    calls the blocking CLI adapter itself here, or the event loop would be
    frozen for the duration of the `gh` subprocess call (up to its timeout).
    The actual fetch happens inside `_run_scheduled_auto_refresh_async`, off
    the event loop thread via `asyncio.to_thread`.
    """
    task = asyncio.create_task(_run_scheduled_auto_refresh_async())
    app.state.auto_refresh_tasks.add(task)
    task.add_done_callback(app.state.auto_refresh_tasks.discard)


async def _run_scheduled_auto_refresh_async() -> None:
    """Runs the one scheduled auto-refresh attempt without blocking the loop.

    `controller.maybe_run_auto_refresh` (which calls the blocking, subprocess
    based `fetch_github_rate_limit`) runs via `asyncio.to_thread`, so other
    requests keep being served while `gh` runs. If the resulting snapshot
    shows a *new* auto-refresh was armed (a fresh `reset_at_utc` appeared),
    a follow-up one-shot timer is scheduled the same way a manual refresh
    would. Any unexpected exception is swallowed unconditionally — this task
    has no caller waiting on it, and nothing about the exception (which
    could otherwise reference subprocess internals) is surfaced anywhere.
    """
    controller: GitHubRateLimitController = app.state.github_rate_limit_controller
    try:
        snapshot = await asyncio.to_thread(
            controller.maybe_run_auto_refresh,
            now=_current_utc_time(),
            fetch=fetch_github_rate_limit,
            tz=app_tz(),
        )
    except Exception:
        return
    if snapshot is not None:
        _schedule_auto_refresh_if_pending(snapshot)


def _schedule_auto_refresh_if_pending(snapshot: dict) -> None:
    """Arm a one-shot timer on the real event loop for a pending auto-refresh.

    Process-local and best-effort: the timer lives only in this process's
    event loop, is lost on restart, and (like the rest of this controller)
    is not shared across worker processes. `call_soon_threadsafe` is used
    because this runs from a synchronous, threadpool-executed endpoint —
    calling `loop.call_later` directly from that thread would not be safe.
    A stale/duplicate timer firing later is harmless: `maybe_run_auto_refresh`
    always re-checks `auto_refresh_pending` first and no-ops if already
    consumed, so this never causes more than one real attempt per reset.
    """
    if not snapshot.get("auto_refresh_pending") or not snapshot.get("next_auto_refresh_at"):
        return
    loop: asyncio.AbstractEventLoop | None = getattr(app.state, "event_loop", None)
    if loop is None:
        return
    next_at = datetime.fromisoformat(snapshot["next_auto_refresh_at"])
    delay_seconds = max(0.0, (next_at - _current_utc_time()).total_seconds())
    loop.call_soon_threadsafe(loop.call_later, delay_seconds, _run_scheduled_auto_refresh)


@app.post("/api/github-rate-limit/refresh")
def refresh_github_rate_limit() -> dict:
    """The only manually-triggered endpoint that invokes the GitHub CLI adapter."""
    controller: GitHubRateLimitController = app.state.github_rate_limit_controller
    try:
        snapshot = controller.refresh(now=_current_utc_time(), fetch=fetch_github_rate_limit, tz=app_tz())
    except GitHubRateLimitRefreshCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "cooldown_active",
                "user_message": "更新の間隔が短すぎます。しばらく待ってから再度お試しください。",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        ) from exc
    except GitHubRateLimitRefreshInProgressError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "already_refreshing",
                "user_message": "更新を実行中です。しばらく待ってから再度お試しください。",
                "retry_after_seconds": 0,
            },
        ) from exc
    _schedule_auto_refresh_if_pending(snapshot)
    return snapshot


@app.get("/api/claude-code-usage", response_model=schemas.ClaudeCodeUsageSnapshot)
def get_claude_code_usage() -> dict:
    """Read-only load of the local statusLine bridge cache. Never runs Claude Code, never calls out."""
    return load_claude_code_usage_snapshot(now=_current_utc_time())


def _codex_window_input_to_record(window: schemas.CodexUsageWindowInput | None) -> dict | None:
    if window is None:
        return None
    # used_percentage is always derived server-side; the input schema has no
    # such field, so the user can never submit an inconsistent pair.
    return {
        "used_percentage": 100.0 - window.remaining_percentage,
        "remaining_percentage": window.remaining_percentage,
        "resets_at": window.resets_at.isoformat(),
    }


@app.get("/api/codex-usage", response_model=schemas.CodexUsageSnapshot)
def get_codex_usage() -> dict:
    """Read-only load of the manually saved Codex usage snapshot. Never runs Codex, never calls out."""
    return codex_usage_cache.load_snapshot(now=_current_utc_time())


@app.put("/api/codex-usage", response_model=schemas.CodexUsageSnapshot)
def put_codex_usage(payload: schemas.CodexUsageInput) -> dict:
    """Save a manually-entered Codex usage snapshot. Never runs Codex, never calls out."""
    now = _current_utc_time()
    record = {
        "schema_version": codex_usage_cache.SCHEMA_VERSION,
        "source": codex_usage_cache.SOURCE_NAME,
        "observed_at": now.isoformat(),
        "five_hour": _codex_window_input_to_record(payload.five_hour),
        "weekly": _codex_window_input_to_record(payload.weekly),
    }
    try:
        validated = codex_usage_cache.validate_cache_record(record, now=now)
    except codex_usage_cache.CacheValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid Codex usage input") from exc
    codex_usage_cache.write_cache_atomic(validated, codex_usage_cache.resolve_cache_path())
    return codex_usage_cache.load_snapshot(now=now)


def _codex_rate_limits_response(now: datetime) -> dict:
    snapshot = codex_rate_limits_cache.load_snapshot(now=now)
    controller: CodexRateLimitsController = app.state.codex_rate_limits_controller
    status = controller.status(now=now)
    error = status["last_error"] or {}
    # Never merges the manual snapshot's own values into this response — only
    # whether it exists, so the display layer can decide when to fall back to
    # it (see docs/codex-rate-limits-auto.md).
    manual_fallback = codex_usage_cache.load_snapshot(now=now)
    scheduler: CodexRateLimitsScheduler = app.state.codex_rate_limits_scheduler
    return {
        "fetched": snapshot["available"],
        "available": snapshot["available"],
        "stale": snapshot["stale"],
        "status": snapshot["status"],
        "observed_at": snapshot["observed_at"],
        "source": snapshot["source"],
        "five_hour": snapshot["five_hour"],
        "weekly": snapshot["weekly"],
        "error_type": error.get("error_type"),
        "user_message": error.get("user_message"),
        "refresh_in_progress": status["refresh_in_progress"],
        "cooldown_remaining_seconds": status["cooldown_remaining_seconds"],
        "fallback_available": manual_fallback["available"],
        "fallback_source": codex_usage_cache.SOURCE_NAME if manual_fallback["available"] else None,
        **scheduler.status(),
    }


@app.get("/api/codex-rate-limits", response_model=schemas.CodexRateLimitsSnapshot)
def get_codex_rate_limits() -> dict:
    """Read-only load of the auto-fetch cache + refresh state. Never starts Codex App Server."""
    return _codex_rate_limits_response(_current_utc_time())


@app.post("/api/codex-rate-limits/refresh", response_model=schemas.CodexRateLimitsSnapshot)
def refresh_codex_rate_limits() -> dict:
    """The only endpoint that launches Codex App Server, for a single account/rateLimits/read call."""
    controller: CodexRateLimitsController = app.state.codex_rate_limits_controller
    now = _current_utc_time()
    try:
        controller.refresh(now=now, fetch=fetch_codex_rate_limits, cache_path=codex_rate_limits_cache.resolve_cache_path())
    except CodexRateLimitsRefreshCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "cooldown_active",
                "user_message": "更新の間隔が短すぎます。しばらく待ってから再度お試しください。",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        ) from exc
    except CodexRateLimitsRefreshInProgressError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error_type": "already_refreshing",
                "user_message": "更新を実行中です。しばらく待ってから再度お試しください。",
                "retry_after_seconds": 0,
            },
        ) from exc
    return _codex_rate_limits_response(_current_utc_time())


@app.get("/compact", include_in_schema=False)
def compact_dashboard() -> FileResponse:
    """Read-only compact dashboard. Serves static HTML only, never triggers a fetch."""
    return FileResponse("static/compact.html")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
