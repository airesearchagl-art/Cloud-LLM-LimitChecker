"""Read/write access to the local Claude Code usage cache file.

This cache holds only the most recently *observed* values from Claude Code's
official `statusLine` JSON payload (see `app.claude_code_usage_bridge`) — it
is never populated by polling Claude Code or Anthropic: `statusLine` is a
push-style hook that Claude Code invokes on its own schedule during an
active session, so a "stale" cache here means "the last observation is old",
not "unavailable".

The cache file lives outside this repository and outside `~/.claude/`, so it
is clearly separated from both the checked-out project and Claude Code's own
state.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
SOURCE_NAME = "claude_code_statusline"

# statusLine is push-style, not pollable: this threshold only distinguishes a
# recent observation from a stale one for display purposes, it does not mean
# "unavailable" the way GitHub CLI's fetch failures do.
STALE_THRESHOLD_SECONDS = 15 * 60

_CACHE_DIR_NAME = "Cloud-LLM-LimitChecker"
_CACHE_FILE_NAME = "claude-code-usage.json"

STATUS_NOT_OBSERVED = "not_observed"
STATUS_INVALID_CACHE = "invalid_cache"
STATUS_OK = "ok"
STATUS_STALE = "stale"

_EMPTY_SNAPSHOT_BASE = {
    "observed_at": None,
    "source": None,
    "five_hour": None,
    "seven_day": None,
    "error_message": None,
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
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-claude-code-usage-", suffix=".json")
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


def load_snapshot(*, now: datetime, path: Path | None = None) -> dict:
    """Read-only load of the cache. Never runs Claude Code, never touches the network."""
    cache_path = path if path is not None else resolve_cache_path()

    if not cache_path.exists():
        return {"available": False, "stale": False, "status": STATUS_NOT_OBSERVED, **_EMPTY_SNAPSHOT_BASE}

    try:
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("cache root is not an object")
        observed_at = datetime.fromisoformat(record["observed_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return {
            "available": False,
            "stale": False,
            "status": STATUS_INVALID_CACHE,
            **_EMPTY_SNAPSHOT_BASE,
            "error_message": "usage cache could not be read",
        }

    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_seconds = (now - observed_at).total_seconds()
    stale = age_seconds > STALE_THRESHOLD_SECONDS

    return {
        "available": True,
        "stale": stale,
        "status": STATUS_STALE if stale else STATUS_OK,
        "observed_at": observed_at.isoformat(),
        "source": record.get("source"),
        "five_hour": record.get("five_hour"),
        "seven_day": record.get("seven_day"),
        "error_message": None,
    }
