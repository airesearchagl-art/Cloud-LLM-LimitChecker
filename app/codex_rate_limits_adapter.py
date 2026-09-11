"""Codex App Server fetch adapter for `account/rateLimits/read`.

Thin subprocess/JSON-RPC wrapper around `codex app-server --stdio`. This
module never reads `~/.codex/auth.json`, never reads a credential store, and
never logs stdout/stderr — it only classifies *why* a call failed into a
small fixed `error_type` vocabulary with a generic `user_message`.

Protocol (per OpenAI's official Codex App Server docs and the schema that
Codex CLI 0.153.4 generates):
- stdio transport, newline-delimited JSON (JSONL), no `"jsonrpc":"2.0"` key.
- `initialize` (with `clientInfo`) -> response, then an `initialized`
  notification, then the actual method call.
- `account/rateLimits/read` takes no params, so the request carries no
  `params` key at all. Its result has `rateLimits` (the backward-compatible
  single-bucket view) and `rateLimitsByLimitId` (the multi-bucket view keyed
  by metered `limit_id`, nullable). Each snapshot has `primary` /
  `secondary` windows, each `{usedPercent, windowDurationMins, resetsAt}`
  or null; `resetsAt` is Unix seconds.

Two views are produced from one response:
- Legacy `five_hour` / `weekly`, from `result.rateLimits` only, with the
  historical semantics, unchanged: a null, malformed, or unrecognized-duration
  window is dropped; windows are matched purely by `windowDurationMins`
  (300 / 10080), never by position; two windows resolving to the same slot
  fail the fetch as ambiguous, and no recognized window at all fails it as
  invalid. This is what `GET /api/codex-rate-limits` has always shown, and
  it gates every fetch -- including one with a valid multi-bucket map (a
  compatibility guard for the legacy endpoint, not a generic rule).
- Canonical allowance buckets. When `rateLimitsByLimitId` is non-empty its
  entries, and only its entries, are the buckets: `result.rateLimits` is NOT
  added again. It is the backward-compatible single-bucket view of the same
  allowance (in the observed runtime it restated one map entry), so adding
  it would double count.
  The map is validated strictly against the generated schema's wire types,
  and any invalid entry or non-null window fails the whole fetch -- it never
  falls back to `result.rateLimits`. When the map is null, missing, or
  empty, the single bucket is `result.rateLimits` seen through the legacy
  rules above: exactly the windows the legacy view accepted, nothing
  stricter and nothing more. Either way each window keeps its source slot
  (`primary` / `secondary`); a slot name implies no duration.

This module deliberately calls `account/rateLimits/read` and nothing else:
no `account/rateLimitResetCredit/consume`, no task/thread/prompt method, no
subscription to `account/rateLimits/updated`. From each snapshot it reads
only `limitId`, `limitName`, `planType`, `rateLimitReachedType`, `primary`
and `secondary`; `accountId`, `credits`, `rateLimitResetCredits`,
`rateLimitUpsell`, `individualLimit` and `spendControlReached` are never
read.

The pure protocol logic (`run_json_rpc_session`) is separated from real
process/thread management (`fetch_codex_rate_limits`) so it can be unit
tested against a scripted fake `send`/`recv` pair without ever spawning a
real `codex` process.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from app.codex_rate_limits_cache import LIMIT_ID_ORIGIN_MAP_KEY, LIMIT_ID_ORIGIN_SNAPSHOT_FIELD, SOURCE_SLOTS

APP_SERVER_ARGS: tuple[str, ...] = ("app-server", "--stdio")
CLIENT_INFO = {
    "name": "cloud-llm-limit-checker",
    "title": "Cloud LLM Limit Checker",
    "version": "1",
}

INITIALIZE_TIMEOUT_SECONDS = 10
RATE_LIMITS_TIMEOUT_SECONDS = 15
SHUTDOWN_TIMEOUT_SECONDS = 5

FIVE_HOUR_WINDOW_DURATION_MINUTES = 300
WEEKLY_WINDOW_DURATION_MINUTES = 10080

ErrorType = Literal[
    "executable_not_found",
    "process_start_failed",
    "initialize_timeout",
    "initialize_error",
    "protocol_error",
    "method_not_found",
    "authentication_unavailable",
    "rate_limits_timeout",
    "invalid_response",
    "ambiguous_response",
    "process_exit_failed",
    "unknown_error",
]

_USER_MESSAGES: dict[ErrorType, str] = {
    "executable_not_found": "Codex CLI (codex) is not installed or not on PATH.",
    "process_start_failed": "Codex App Server could not be started.",
    "initialize_timeout": "Codex App Server did not respond to initialize in time.",
    "initialize_error": "Codex App Server rejected the initialize request.",
    "protocol_error": "Codex App Server returned an unexpected protocol response.",
    "method_not_found": "Codex App Server does not support this rate limit method.",
    "authentication_unavailable": "Codex is not authenticated for rate limit access.",
    "rate_limits_timeout": "Fetching the Codex rate limit timed out.",
    "invalid_response": "Codex App Server response did not contain the expected rate limit structure.",
    "ambiguous_response": "Codex App Server returned rate limit windows that could not be distinguished.",
    "process_exit_failed": "Codex App Server process could not be confirmed terminated.",
    "unknown_error": "An unexpected error occurred while fetching the Codex rate limit.",
}


@dataclass(frozen=True, slots=True)
class CodexRateLimitsFetchResult:
    success: bool
    # {"five_hour": window|None, "weekly": window|None}; each window is
    # {"used_percentage", "remaining_percentage", "resets_at" (ISO UTC str),
    # "window_duration_minutes"}. None (not this dataclass) when success is False.
    windows: dict | None
    error_type: ErrorType | None
    user_message: str | None
    collected_at: datetime
    # Canonical allowance buckets, each {"limit_id", "limit_id_origin",
    # "display_name", "plan_type", "rate_limit_reached_type", "windows"}, where
    # each window is {"source_slot", "used_percentage", "remaining_percentage",
    # "resets_at" (ISO UTC str or None), "window_duration_minutes" (int or None)}.
    # Always non-empty when success is True; None when success is False.
    buckets: list[dict] | None = None


class _InvalidRateLimitsPayload(Exception):
    """Internal: a canonical snapshot or window failed validation. Carries no payload."""


def _failure(error_type: ErrorType, collected_at: datetime) -> CodexRateLimitsFetchResult:
    return CodexRateLimitsFetchResult(
        success=False,
        windows=None,
        error_type=error_type,
        user_message=_USER_MESSAGES[error_type],
        collected_at=collected_at,
    )


def _classify_rpc_error(error: object) -> ErrorType:
    """Classify a JSON-RPC error object without ever surfacing its raw text.

    Only `code` (an int) and a lowercased scan of `message` for a few known
    keywords are inspected — the actual message string is never returned to
    the caller.
    """
    if not isinstance(error, dict):
        return "protocol_error"
    if error.get("code") == -32601:
        return "method_not_found"
    message = str(error.get("message", "")).lower()
    if any(word in message for word in ("auth", "unauthenticated", "unauthorized", "sign in", "sign-in")):
        return "authentication_unavailable"
    return "protocol_error"


def _validate_window(raw: object) -> dict | None:
    """Normalize one `primary`/`secondary` window, or return None if absent/invalid.

    Never raises. A window that is null, not an object, or has a
    missing/wrong-typed/out-of-range field is simply treated as unusable —
    the caller decides whether that sinks the whole fetch.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None

    used = raw.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    if not (0 <= used <= 100):
        return None

    duration = raw.get("windowDurationMins")
    if isinstance(duration, bool) or not isinstance(duration, int):
        return None

    resets_at = raw.get("resetsAt")
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    try:
        resets_at_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    used_f = float(used)
    return {
        "used_percentage": used_f,
        # The source has no remaining field: this is a pure derivation from
        # this same window's validated used value, never clamped.
        "remaining_percentage": 100.0 - used_f,
        "resets_at": resets_at_dt.isoformat(),
        "window_duration_minutes": duration,
    }


def _recognized_legacy_windows(rate_limits: dict) -> list[tuple[str, str, dict]]:
    """Every window the legacy view accepts, as `(source_slot, legacy_key, window)`.

    Legacy semantics, unchanged: a null or malformed window (per
    `_validate_window`) and a window whose duration matches neither 300 nor
    10080 minutes are silently dropped, not errors.
    """
    recognized = []
    for slot in SOURCE_SLOTS:
        window = _validate_window(rate_limits.get(slot))
        if window is None:
            continue
        duration = window["window_duration_minutes"]
        if duration == FIVE_HOUR_WINDOW_DURATION_MINUTES:
            recognized.append((slot, "five_hour", window))
        elif duration == WEEKLY_WINDOW_DURATION_MINUTES:
            recognized.append((slot, "weekly", window))
    return recognized


def _map_windows(recognized: list[tuple[str, str, dict]]) -> tuple[dict | None, dict | None, bool]:
    """Map recognized windows to five_hour/weekly by duration, order-independent.

    Returns (five_hour, weekly, ambiguous). `ambiguous=True` means both
    validated windows resolved to the *same* target slot (duplicate
    `windowDurationMins`) — the caller must treat the whole fetch as failed
    rather than guess which one is which.
    """
    keys = [legacy_key for _, legacy_key, _ in recognized]
    if len(keys) != len(set(keys)):
        return None, None, True
    by_key = {legacy_key: window for _, legacy_key, window in recognized}
    return by_key.get("five_hour"), by_key.get("weekly"), False


def _legacy_view_bucket(rate_limits: dict, recognized: list[tuple[str, str, dict]]) -> dict:
    """The single canonical bucket when there is no multi-bucket map.

    Seen through the legacy rules only, so a response the legacy view
    accepts is never rejected here: the windows are exactly the ones the
    legacy view accepted (with their real source slot), and a metadata field
    that is not a string is dropped to null -- the legacy view never read
    metadata, so it cannot start failing a fetch now. No id is synthesized.
    """
    limit_id = _string_or_none(rate_limits.get("limitId")) or None
    return {
        "limit_id": limit_id,
        "limit_id_origin": LIMIT_ID_ORIGIN_SNAPSHOT_FIELD if limit_id is not None else None,
        "display_name": _string_or_none(rate_limits.get("limitName")),
        "plan_type": _string_or_none(rate_limits.get("planType")),
        "rate_limit_reached_type": _string_or_none(rate_limits.get("rateLimitReachedType")),
        "windows": [{"source_slot": slot, **window} for slot, _, window in recognized],
    }


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _wire_int(value: object) -> int | None:
    """A canonical-path `integer` exactly as the generated schema types it on the wire.

    Only a real `int` passes: `bool` and every float -- even an integral one
    such as `42.0` -- are rejected. Returns None for anything else.
    """
    return value if type(value) is int else None


def _optional_string(value: object) -> str | None:
    """An open-string field: any string (unknown values included) or null."""
    if value is None or isinstance(value, str):
        return value
    raise _InvalidRateLimitsPayload()


def _canonical_window(raw: object, *, source_slot: str) -> dict:
    """Validate one non-null `primary`/`secondary` window of a multi-bucket entry.

    Strict wire types (generated schema): `usedPercent` int32, required and
    0..100; `windowDurationMins` and `resetsAt` int64 or null. Raises
    `_InvalidRateLimitsPayload` instead of dropping the window: silently
    discarding a window the source did send would make the bucket look
    healthier than the response was. A null duration or reset stays null; a
    duration outside 300/10080 is kept as-is (the schema sets no minimum).
    """
    if not isinstance(raw, dict):
        raise _InvalidRateLimitsPayload()

    used = _wire_int(raw.get("usedPercent"))
    if used is None or not (0 <= used <= 100):
        raise _InvalidRateLimitsPayload()

    duration = None
    if raw.get("windowDurationMins") is not None:
        duration = _wire_int(raw.get("windowDurationMins"))
        if duration is None or not (_INT64_MIN <= duration <= _INT64_MAX):
            raise _InvalidRateLimitsPayload()

    resets_at = None
    if raw.get("resetsAt") is not None:
        seconds = _wire_int(raw.get("resetsAt"))
        if seconds is None or seconds < 0:
            raise _InvalidRateLimitsPayload()
        try:
            resets_at = datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            raise _InvalidRateLimitsPayload() from None

    used_f = float(used)
    return {
        "source_slot": source_slot,
        "used_percentage": used_f,
        # Pure derivation from this same window's validated used value only.
        "remaining_percentage": 100.0 - used_f,
        "resets_at": resets_at,
        "window_duration_minutes": duration,
    }


def _canonical_bucket(snapshot: dict, *, limit_id: str, limit_id_origin: str) -> dict:
    """Project one multi-bucket `RateLimitSnapshot` onto an allowlisted canonical bucket.

    Strict: a non-string metadata field or an invalid non-null window raises
    `_InvalidRateLimitsPayload`. Both windows null is a valid, metadata-only
    bucket (`windows == []`).
    """
    windows = [
        _canonical_window(snapshot.get(slot), source_slot=slot)
        for slot in SOURCE_SLOTS
        if snapshot.get(slot) is not None
    ]
    return {
        "limit_id": limit_id,
        "limit_id_origin": limit_id_origin,
        "display_name": _optional_string(snapshot.get("limitName")),
        "plan_type": _optional_string(snapshot.get("planType")),
        "rate_limit_reached_type": _optional_string(snapshot.get("rateLimitReachedType")),
        "windows": windows,
    }


def _select_canonical_buckets(
    result: dict, rate_limits: dict, recognized: list[tuple[str, str, dict]]
) -> list[dict]:
    """Pick exactly one source view for the canonical buckets (never both).

    - `rateLimitsByLimitId` null / missing / `{}`: the single bucket is
      `result.rateLimits` through the legacy rules (`_legacy_view_bucket`);
      this path never raises.
    - `rateLimitsByLimitId` non-empty: its entries, and only its entries, are
      the buckets; the map key is the id. An entry whose own non-null
      `limitId` disagrees with its key, or any other invalid entry, raises
      `_InvalidRateLimitsPayload` -- there is no fallback to `rateLimits`.
    - `rateLimitsByLimitId` neither an object nor null: raises.
    """
    by_limit_id = result.get("rateLimitsByLimitId")
    if by_limit_id is None or (isinstance(by_limit_id, dict) and not by_limit_id):
        return [_legacy_view_bucket(rate_limits, recognized)]

    if not isinstance(by_limit_id, dict):
        raise _InvalidRateLimitsPayload()

    buckets = []
    # Sorted so the bucket order does not depend on the backend's key order.
    for key in sorted(by_limit_id):
        snapshot = by_limit_id[key]
        if not key or not isinstance(snapshot, dict):
            raise _InvalidRateLimitsPayload()
        snapshot_limit_id = snapshot.get("limitId")
        if snapshot_limit_id is not None and snapshot_limit_id != key:
            raise _InvalidRateLimitsPayload()
        buckets.append(_canonical_bucket(snapshot, limit_id=key, limit_id_origin=LIMIT_ID_ORIGIN_MAP_KEY))
    return buckets


def _recv_matching(recv: Callable[[float], dict | None], expected_id: int, timeout_seconds: float) -> dict | None:
    """Read messages via `recv` until one with `id == expected_id` arrives.

    Notifications and responses to a different id are silently skipped (per
    the documented "ignore other messages" contract) — never mistaken for
    the awaited response. Bounded by a single wall-clock deadline.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = recv(remaining)
        if msg is None:
            return None
        if msg.get("id") == expected_id:
            return msg


def run_json_rpc_session(
    *,
    send: Callable[[dict], None],
    recv: Callable[[float], dict | None],
    now: datetime,
) -> CodexRateLimitsFetchResult:
    """Pure protocol logic: initialize -> initialized -> account/rateLimits/read.

    Takes abstract `send`/`recv` callables so this can be exercised against a
    scripted fake transport in tests, with no real process involved. `recv`
    must return the next parsed JSONL message within the given timeout, or
    None on timeout/EOF/unparseable input.
    """
    send({"method": "initialize", "id": 1, "params": {"clientInfo": CLIENT_INFO}})
    init_response = _recv_matching(recv, 1, INITIALIZE_TIMEOUT_SECONDS)
    if init_response is None:
        return _failure("initialize_timeout", now)
    if "error" in init_response:
        return _failure("initialize_error", now)

    send({"method": "initialized", "params": {}})

    # No `params` key: the method takes none (official request example and
    # the generated schema's `params: null`).
    send({"method": "account/rateLimits/read", "id": 2})
    rl_response = _recv_matching(recv, 2, RATE_LIMITS_TIMEOUT_SECONDS)
    if rl_response is None:
        return _failure("rate_limits_timeout", now)
    if "error" in rl_response:
        return _failure(_classify_rpc_error(rl_response["error"]), now)

    result = rl_response.get("result")
    if not isinstance(result, dict):
        return _failure("invalid_response", now)
    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, dict):
        return _failure("invalid_response", now)

    recognized = _recognized_legacy_windows(rate_limits)
    five_hour, weekly, ambiguous = _map_windows(recognized)
    if ambiguous:
        return _failure("ambiguous_response", now)
    # Also gates a response whose multi-bucket map is valid: the legacy
    # endpoint must never report success with nothing to show.
    if five_hour is None and weekly is None:
        return _failure("invalid_response", now)

    try:
        buckets = _select_canonical_buckets(result, rate_limits, recognized)
    except _InvalidRateLimitsPayload:
        return _failure("invalid_response", now)

    return CodexRateLimitsFetchResult(
        success=True,
        windows={"five_hour": five_hour, "weekly": weekly},
        error_type=None,
        user_message=None,
        collected_at=now,
        buckets=buckets,
    )


def _shutdown_process(proc: subprocess.Popen) -> bool:
    """Best-effort, always-run shutdown: close stdin, then wait/terminate/kill.

    Returns True only once the process is confirmed no longer running.
    """
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass

    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return True
    except Exception:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return True
    except Exception:
        pass

    try:
        proc.kill()
        proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        pass
    return proc.poll() is not None


def fetch_codex_rate_limits(*, now: datetime | None = None) -> CodexRateLimitsFetchResult:
    """Run one `account/rateLimits/read` round-trip against a fresh `codex app-server`.

    Never raises for expected failure modes; the `codex` executable is
    resolved via `shutil.which` (no hardcoded path), launched with an
    argument list (`shell=False`), and stderr is discarded entirely
    (`subprocess.DEVNULL`) — never captured, never logged.
    """
    collected_at = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)

    executable = shutil.which("codex")
    if executable is None:
        return _failure("executable_not_found", collected_at)

    try:
        proc = subprocess.Popen(
            [executable, *APP_SERVER_ARGS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError:
        return _failure("process_start_failed", collected_at)

    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                stripped = raw_line.strip()
                if stripped:
                    line_queue.put(stripped)
        except Exception:
            pass
        finally:
            line_queue.put(None)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def send(obj: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(timeout: float) -> dict | None:
        # A single unparseable line (JSONL noise) must not be mistaken for a
        # real timeout/EOF — keep reading within the same budget until a
        # well-formed message arrives, the queue genuinely times out, or the
        # reader thread signals EOF (the `None` sentinel).
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = line_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    try:
        try:
            fetch_result = run_json_rpc_session(send=send, recv=recv, now=collected_at)
        except Exception:
            fetch_result = _failure("unknown_error", collected_at)
    finally:
        cleanup_ok = _shutdown_process(proc)

    if not cleanup_ok:
        return _failure("process_exit_failed", collected_at)
    return fetch_result
