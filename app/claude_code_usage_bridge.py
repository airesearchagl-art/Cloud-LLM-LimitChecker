"""Claude Code `statusLine` bridge — extracts rate-limit fields, nothing else.

Intended to be configured as the `statusLine` command in `~/.claude/settings.json`
(opt-in, manual — see docs/claude-code-usage-bridge.md). Claude Code invokes this
script and passes its documented statusLine JSON payload on stdin; this script
must always print a short status line string back on stdout, no matter what.

Only `rate_limits.five_hour` / `rate_limits.seven_day` are ever read from that
payload. Everything else in the payload (session_id, transcript_path, cwd,
model, cost, token counts, ...) is never inspected, never stored, and the raw
stdin text is never logged or printed — a malformed or unreadable payload must
degrade to a generic status line, not an exception or a stack trace on stderr.

This module intentionally has no dependency outside the Python standard
library and outside `app.claude_code_usage_cache`, since it runs at the
frequency Claude Code chooses to invoke `statusLine`, not the frequency this
app polls it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.claude_code_usage_cache import (  # noqa: E402
    SCHEMA_VERSION,
    SOURCE_NAME,
    resolve_cache_path,
    write_cache_atomic,
)

FALLBACK_STATUS_LINE = "Claude usage: waiting for first response"


def _extract_window(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None

    used = raw.get("used_percentage")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    if not (0 <= used <= 100):
        return None

    resets_at = raw.get("resets_at")
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    try:
        reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    used_percentage = float(used)
    return {
        "used_percentage": used_percentage,
        "remaining_percentage": max(0.0, 100.0 - used_percentage),
        "resets_at": reset_dt.isoformat(),
    }


def extract_usage_record(payload: object, *, now: datetime) -> dict:
    """Build the cache record from a statusLine JSON payload. Never raises."""
    rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    five_hour = _extract_window(rate_limits.get("five_hour")) if isinstance(rate_limits, dict) else None
    seven_day = _extract_window(rate_limits.get("seven_day")) if isinstance(rate_limits, dict) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "observed_at": now.isoformat(),
        "five_hour": five_hour,
        "seven_day": seven_day,
    }


def _fmt_used(used_percentage: float) -> str:
    return f"{used_percentage:.0f}%"


def format_status_line(record: dict) -> str:
    five_hour = record.get("five_hour")
    seven_day = record.get("seven_day")
    if five_hour is None and seven_day is None:
        return FALLBACK_STATUS_LINE

    parts = []
    if five_hour is not None:
        parts.append(f"5h: {_fmt_used(five_hour['used_percentage'])} used")
    if seven_day is not None:
        parts.append(f"7d: {_fmt_used(seven_day['used_percentage'])} used")
    return "Claude " + " | ".join(parts)


def main(*, stdin: TextIO | None = None, now: datetime | None = None, cache_path: Path | None = None) -> int:
    stream = stdin if stdin is not None else sys.stdin
    current_time = now if now is not None else datetime.now(timezone.utc)

    try:
        raw_stdin = stream.read()
    except Exception:
        raw_stdin = ""

    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}

    record = extract_usage_record(payload, now=current_time)

    try:
        write_cache_atomic(record, cache_path if cache_path is not None else resolve_cache_path())
    except Exception:
        # Fail-open: the status line must still render even if the cache write fails.
        pass

    print(format_status_line(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
