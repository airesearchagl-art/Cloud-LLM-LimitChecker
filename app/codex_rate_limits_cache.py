"""Read/write access to the local Codex App Server auto-fetch rate limit cache.

Separate file from both `app.claude_code_usage_cache` and
`app.codex_usage_cache` (the manual snapshot) — this one holds the last
*successful* `account/rateLimits/read` result, written only by
`app.codex_rate_limits_state.CodexRateLimitsController.refresh`. `GET
/api/codex-rate-limits` only ever reads this file; it never runs Codex.

The cache file lives outside this repository and outside `~/.codex/`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SOURCE_NAME = "codex_app_server"

# Auto-fetched (unlike the 24h manual-confirmation basis), so a much shorter
# window than the manual Codex Usage snapshot: 15 minutes without a
# successful refresh means the displayed percentage should be flagged as
# possibly out of date, not treated as a live reading.
STALE_THRESHOLD_SECONDS = 15 * 60

PERCENTAGE_SUM_TOLERANCE = 0.5

_CACHE_DIR_NAME = "Cloud-LLM-LimitChecker"
_CACHE_FILE_NAME = "codex-rate-limits.json"

STATUS_NOT_OBSERVED = "not_observed"
STATUS_INVALID_CACHE = "invalid_cache"
STATUS_OK = "ok"
STATUS_STALE = "stale"

_INVALID_CACHE_ERROR_MESSAGE = "usage cache could not be read"

_EMPTY_SNAPSHOT_BASE = {
    "observed_at": None,
    "source": None,
    "five_hour": None,
    "weekly": None,
    "error_message": None,
}


class CacheValidationError(ValueError):
    """Raised by `validate_cache_record` when a record doesn't conform to the documented schema."""


def _parse_aware_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_window(raw: object) -> dict | None:
    """Validate one `five_hour` / `weekly` window. `None` is valid and expected."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CacheValidationError("window is not an object")

    used = raw.get("used_percentage")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise CacheValidationError("used_percentage is not a number")
    if not (0 <= used <= 100):
        raise CacheValidationError("used_percentage out of range")

    remaining = raw.get("remaining_percentage")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        raise CacheValidationError("remaining_percentage is not a number")
    if not (0 <= remaining <= 100):
        raise CacheValidationError("remaining_percentage out of range")

    if abs((float(used) + float(remaining)) - 100.0) > PERCENTAGE_SUM_TOLERANCE:
        raise CacheValidationError("used_percentage and remaining_percentage are inconsistent")

    resets_at = _parse_aware_utc_datetime(raw.get("resets_at"))
    if resets_at is None:
        raise CacheValidationError("resets_at is not a timezone-aware ISO datetime")

    duration = raw.get("window_duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise CacheValidationError("window_duration_minutes is not an int")

    return {
        "used_percentage": float(used),
        "remaining_percentage": float(remaining),
        "resets_at": resets_at.isoformat(),
        "window_duration_minutes": duration,
    }


def validate_cache_record(record: object, *, now: datetime) -> dict:
    """Validate a JSON-decoded cache record. Used both before writing and before reading.

    Raises `CacheValidationError` (never anything else) on any violation;
    returns a normalized dict on success. Never includes any part of the raw
    input in the exception message — no `rateLimitsByLimitId`, no
    `rateLimitResetCredits`, no account/session identifiers (those are never
    even looked at, let alone stored).
    """
    if not isinstance(record, dict):
        raise CacheValidationError("root is not an object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise CacheValidationError("schema_version mismatch")
    if record.get("source") != SOURCE_NAME:
        raise CacheValidationError("source mismatch")

    observed_at = _parse_aware_utc_datetime(record.get("observed_at"))
    if observed_at is None:
        raise CacheValidationError("observed_at is not a timezone-aware ISO datetime")

    five_hour = _validate_window(record.get("five_hour"))
    weekly = _validate_window(record.get("weekly"))
    if five_hour is None and weekly is None:
        raise CacheValidationError("at least one of five_hour or weekly is required")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": five_hour,
        "weekly": weekly,
    }


def resolve_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the cache file path. `env` is injectable so tests never touch the real %LOCALAPPDATA%."""
    environ = env if env is not None else os.environ
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path(environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / _CACHE_DIR_NAME / _CACHE_FILE_NAME


def write_cache_atomic(record: dict, path: Path) -> None:
    """Write `record` as JSON to `path`, replacing any existing file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-codex-rate-limits-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def _invalid_cache_snapshot() -> dict:
    return {
        "available": False,
        "stale": False,
        "status": STATUS_INVALID_CACHE,
        **_EMPTY_SNAPSHOT_BASE,
        "error_message": _INVALID_CACHE_ERROR_MESSAGE,
    }


def _window_reset_exceeded(window: dict | None, *, now: datetime) -> bool:
    if window is None:
        return False
    resets_at = datetime.fromisoformat(window["resets_at"])
    return resets_at <= now


def load_snapshot(*, now: datetime, path: Path | None = None) -> dict:
    """Read-only load of the cache. Never runs Codex, never touches the network.

    Never raises: any I/O error, malformed JSON, or schema violation becomes
    `status: invalid_cache` instead of propagating.
    """
    cache_path = path if path is not None else resolve_cache_path()

    try:
        if not cache_path.exists():
            return {"available": False, "stale": False, "status": STATUS_NOT_OBSERVED, **_EMPTY_SNAPSHOT_BASE}
        raw_record = json.loads(cache_path.read_text(encoding="utf-8"))
        validated = validate_cache_record(raw_record, now=now)
    except Exception:
        return _invalid_cache_snapshot()

    observed_at = datetime.fromisoformat(validated["observed_at"])
    age_seconds = (now - observed_at).total_seconds()
    reset_exceeded = _window_reset_exceeded(validated["five_hour"], now=now) or _window_reset_exceeded(
        validated["weekly"], now=now
    )
    stale = age_seconds > STALE_THRESHOLD_SECONDS or reset_exceeded

    return {
        "available": True,
        "stale": stale,
        "status": STATUS_STALE if stale else STATUS_OK,
        "observed_at": validated["observed_at"],
        "source": validated["source"],
        "five_hour": validated["five_hour"],
        "weekly": validated["weekly"],
        "error_message": None,
    }
