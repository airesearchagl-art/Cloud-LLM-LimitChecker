"""Read/write access to the local Codex App Server auto-fetch rate limit cache.

Separate file from both `app.claude_code_usage_cache` and
`app.codex_usage_cache` (the manual snapshot) — this one holds the last
*successful* `account/rateLimits/read` result, written only by
`app.codex_rate_limits_state.CodexRateLimitsController.refresh`. `GET
/api/codex-rate-limits` only ever reads this file; it never runs Codex.

The cache file lives outside this repository and outside `~/.codex/`.

Schema versions (the reader accepts both; the writer only writes v2):

- v1: `five_hour` / `weekly` only.
- v2: the same `five_hour` / `weekly` -- still derived from `result.rateLimits`
  with the historical semantics, and still the only input to `GET
  /api/codex-rate-limits` -- plus `buckets`: the canonical allowance buckets
  (see `app.codex_rate_limits_adapter`), each keeping the source's own
  `primary` / `secondary` slot. No marker of which source view the buckets
  came from is stored; the buckets' `limit_id_origin` already says it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)
SOURCE_NAME = "codex_app_server"

# How a bucket's `limit_id` was obtained. Never synthesized: a bucket whose
# source carried no id has `limit_id` and `limit_id_origin` both null.
LIMIT_ID_ORIGIN_MAP_KEY = "map_key"
LIMIT_ID_ORIGIN_SNAPSHOT_FIELD = "snapshot_field"
LIMIT_ID_ORIGINS: tuple[str, ...] = (LIMIT_ID_ORIGIN_MAP_KEY, LIMIT_ID_ORIGIN_SNAPSHOT_FIELD)

# The App Server's own structural window slots. A slot name says nothing
# about the window's duration.
SOURCE_SLOTS: tuple[str, ...] = ("primary", "secondary")

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


def _validate_optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CacheValidationError(f"{field} is not a string")
    return value


def _validate_bucket_window(raw: object) -> dict:
    """Validate one v2 bucket window. Unlike a legacy window, duration and reset may be null."""
    if not isinstance(raw, dict):
        raise CacheValidationError("bucket window is not an object")

    source_slot = raw.get("source_slot")
    if source_slot not in SOURCE_SLOTS:
        raise CacheValidationError("bucket window source_slot is not a known source slot")

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

    raw_resets_at = raw.get("resets_at")
    resets_at = None
    if raw_resets_at is not None:
        resets_at = _parse_aware_utc_datetime(raw_resets_at)
        if resets_at is None:
            raise CacheValidationError("resets_at is not a timezone-aware ISO datetime")

    duration = raw.get("window_duration_minutes")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)):
        raise CacheValidationError("window_duration_minutes is not an int")

    return {
        "source_slot": source_slot,
        "used_percentage": float(used),
        "remaining_percentage": float(remaining),
        "resets_at": resets_at.isoformat() if resets_at is not None else None,
        "window_duration_minutes": duration,
    }


def _validate_bucket(raw: object) -> dict:
    """Validate one v2 bucket. Only allowlisted keys are copied into the result."""
    if not isinstance(raw, dict):
        raise CacheValidationError("bucket is not an object")

    limit_id = _validate_optional_string(raw.get("limit_id"), "limit_id")
    if limit_id == "":
        raise CacheValidationError("limit_id is empty")
    limit_id_origin = raw.get("limit_id_origin")
    if limit_id is None:
        if limit_id_origin is not None:
            raise CacheValidationError("limit_id_origin without limit_id")
    elif limit_id_origin not in LIMIT_ID_ORIGINS:
        raise CacheValidationError("limit_id_origin is not a known origin")

    raw_windows = raw.get("windows")
    if not isinstance(raw_windows, list):
        raise CacheValidationError("bucket windows is not a list")
    windows = [_validate_bucket_window(window) for window in raw_windows]
    slots = [window["source_slot"] for window in windows]
    if len(slots) != len(set(slots)):
        raise CacheValidationError("duplicate source_slot in bucket")

    return {
        "limit_id": limit_id,
        "limit_id_origin": limit_id_origin,
        "display_name": _validate_optional_string(raw.get("display_name"), "display_name"),
        "plan_type": _validate_optional_string(raw.get("plan_type"), "plan_type"),
        "rate_limit_reached_type": _validate_optional_string(
            raw.get("rate_limit_reached_type"), "rate_limit_reached_type"
        ),
        "windows": windows,
    }


def _validate_buckets(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise CacheValidationError("buckets is not a non-empty list")
    buckets = [_validate_bucket(bucket) for bucket in raw]
    # The at-rest form of the no-double-count rule: only the multi-bucket view
    # yields several buckets, and every one of them is keyed by the map. The
    # single-bucket view (`result.rateLimits`) always yields exactly one.
    if len(buckets) > 1 and {bucket["limit_id_origin"] for bucket in buckets} != {LIMIT_ID_ORIGIN_MAP_KEY}:
        raise CacheValidationError("multiple buckets must all come from the multi-bucket view")
    limit_ids = [bucket["limit_id"] for bucket in buckets]
    if len(limit_ids) != len(set(limit_ids)):
        raise CacheValidationError("duplicate limit_id across buckets")
    return buckets


def validate_cache_record(record: object, *, now: datetime) -> dict:
    """Validate a JSON-decoded cache record. Used both before writing and before reading.

    Raises `CacheValidationError` (never anything else) on any violation;
    returns a normalized dict on success. Never includes any part of the raw
    input in the exception message. Only allowlisted keys are ever copied, so
    no account identifier, credit, reset-credit, upsell or spend-control
    field can survive a round-trip even if one were present on disk.
    """
    if not isinstance(record, dict):
        raise CacheValidationError("root is not an object")
    schema_version = record.get("schema_version")
    # `type(...) is int`: neither `True` nor `2.0` compares unequal to a
    # supported version, yet neither is one.
    if type(schema_version) is not int or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
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

    validated = {
        "schema_version": schema_version,
        "source": SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": five_hour,
        "weekly": weekly,
    }
    if schema_version == SCHEMA_VERSION:
        validated["buckets"] = _validate_buckets(record.get("buckets"))
    return validated


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
    if window is None or window["resets_at"] is None:
        return False
    resets_at = datetime.fromisoformat(window["resets_at"])
    return resets_at <= now


def _snapshot_bucket(bucket: dict, *, observation_stale: bool, now: datetime) -> dict:
    """A validated bucket plus its own freshness status.

    A bucket is stale when the observation is, or when one of *its own*
    windows has passed its reset -- another bucket's reset says nothing about
    this one. The top-level `stale` flag keeps its legacy meaning (legacy
    windows only), so `GET /api/codex-rate-limits` is unaffected.
    """
    stale = observation_stale or any(_window_reset_exceeded(window, now=now) for window in bucket["windows"])
    return {**bucket, "status": STATUS_STALE if stale else STATUS_OK}


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
    observation_stale = (now - observed_at).total_seconds() > STALE_THRESHOLD_SECONDS
    reset_exceeded = _window_reset_exceeded(validated["five_hour"], now=now) or _window_reset_exceeded(
        validated["weekly"], now=now
    )
    stale = observation_stale or reset_exceeded

    snapshot = {
        "available": True,
        "stale": stale,
        "status": STATUS_STALE if stale else STATUS_OK,
        "observed_at": validated["observed_at"],
        "source": validated["source"],
        "five_hour": validated["five_hour"],
        "weekly": validated["weekly"],
        "error_message": None,
    }
    # Only a v2 cache carries canonical buckets; a v1 snapshot keeps its
    # exact historical shape.
    if "buckets" in validated:
        snapshot["buckets"] = [
            _snapshot_bucket(bucket, observation_stale=observation_stale, now=now) for bucket in validated["buckets"]
        ]
    return snapshot
