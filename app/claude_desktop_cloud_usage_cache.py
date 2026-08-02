"""Read/write access to the local Claude Desktop Cloud usage manual-snapshot cache file.

Claude Desktop's Code tab, when its Environment is set to Cloud, runs the
session in an isolated Anthropic-managed virtual machine (see
docs/claude-code-usage-bridge.md for the official-documentation research
behind this). That VM has no access to this machine's filesystem, so the
`statusLine` bridge (`app.claude_code_usage_bridge`) — which only ever runs
where Claude Code itself invokes it — cannot be reached from a Cloud
session. This module stores a value the user manually reads off the
official usage panel in Claude Desktop and types into this app; it is never
populated by scraping the Desktop UI, reading Claude's own session/transcript
files, or calling any undocumented API.

The cache file lives outside this repository and outside `~/.claude/`, so it
is clearly separated from both the checked-out project and Claude Code's own
state. It is deliberately a separate file from the statusLine bridge's own
`claude-code-usage.json` — the two sources are never merged at the cache
layer; a client-side selection rule (see `resolveClaudeCodeUsageDisplay` in
static/compact.js) picks whichever valid snapshot has the newer `observed_at`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SOURCE_NAME = "claude_desktop_cloud_manual"

# Manual confirmation, not a push/poll feed: staleness here means "the user
# hasn't re-checked Claude Desktop's usage panel in a while", not "fetch
# failed". Deliberately a much longer window than the statusLine bridge's
# own push-based cache (15 minutes), since re-checking is a manual chore —
# matches the same rationale used for Codex's manual snapshot cache.
STALE_THRESHOLD_SECONDS = 24 * 60 * 60

# used_percentage + remaining_percentage must sum to ~100; this only allows for
# float rounding, not for genuinely inconsistent data.
PERCENTAGE_SUM_TOLERANCE = 0.5

_CACHE_DIR_NAME = "Cloud-LLM-LimitChecker"
_CACHE_FILE_NAME = "claude-desktop-cloud-usage.json"

STATUS_NOT_OBSERVED = "not_observed"
STATUS_INVALID_CACHE = "invalid_cache"
STATUS_OK = "ok"
STATUS_STALE = "stale"

_INVALID_CACHE_ERROR_MESSAGE = "usage cache could not be read"

_EMPTY_SNAPSHOT_BASE = {
    "observed_at": None,
    "source": None,
    "five_hour": None,
    "seven_day": None,
    "error_message": None,
}


class CacheValidationError(ValueError):
    """Raised by `validate_cache_record` when a record doesn't conform to the documented schema."""


def _parse_aware_utc_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime string, rejecting anything that isn't timezone-aware.

    Naive datetimes are never assumed to be UTC (or the user's local zone) —
    they're rejected outright, since guessing would silently paper over a
    corrupt or hand-edited cache file.
    """
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
    """Validate one usage window (`five_hour` / `seven_day`). `None` is a valid, expected value."""
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

    return {
        "used_percentage": float(used),
        "remaining_percentage": float(remaining),
        "resets_at": resets_at.isoformat(),
    }


def validate_cache_record(record: object, *, now: datetime) -> dict:
    """Validate a JSON-decoded cache record against the documented schema.

    Used both before writing (by the `PUT /api/claude-code-usage/manual`
    handler) and before reading (`load_snapshot` below), so a record can
    only ever reach display or disk after passing the exact same checks.
    Raises `CacheValidationError` (never anything else) on any violation;
    returns a normalized dict — aware UTC ISO timestamps, floats — on
    success. Never includes any part of the raw input in the exception
    message (no cache content, no secrets).
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

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": _validate_window(record.get("five_hour")),
        "seven_day": _validate_window(record.get("seven_day")),
    }


def resolve_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the cache file path. `env` is injectable so tests never touch the real %LOCALAPPDATA%."""
    environ = env if env is not None else os.environ
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        # Non-Windows fallback (this project targets Windows, but avoid crashing elsewhere).
        base = Path(environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return base / _CACHE_DIR_NAME / _CACHE_FILE_NAME


def write_cache_atomic(record: dict, path: Path) -> None:
    """Write `record` as JSON to `path`, replacing any existing file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-claude-desktop-cloud-usage-", suffix=".json")
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
    """Read-only load of the cache. Never runs Claude, never touches the network.

    Never raises: any I/O error, malformed JSON, or schema violation (checked by
    `validate_cache_record`, not by relying on the API's response_model to catch it
    downstream) becomes `status: invalid_cache` instead of propagating.
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
    # Even a recent manual entry must not be shown as current once its own
    # reset time has passed: without a fresh confirmation, the percentage on
    # file no longer describes the active window.
    reset_exceeded = _window_reset_exceeded(validated["five_hour"], now=now) or _window_reset_exceeded(
        validated["seven_day"], now=now
    )
    stale = age_seconds > STALE_THRESHOLD_SECONDS or reset_exceeded

    return {
        "available": True,
        "stale": stale,
        "status": STATUS_STALE if stale else STATUS_OK,
        "observed_at": validated["observed_at"],
        "source": validated["source"],
        "five_hour": validated["five_hour"],
        "seven_day": validated["seven_day"],
        "error_message": None,
    }
