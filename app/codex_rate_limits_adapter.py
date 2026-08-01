"""Codex App Server fetch adapter for `account/rateLimits/read`.

Thin subprocess/JSON-RPC wrapper around `codex app-server --stdio`. This
module never reads `~/.codex/auth.json`, never reads a credential store, and
never logs stdout/stderr — it only classifies *why* a call failed into a
small fixed `error_type` vocabulary with a generic `user_message`.

Protocol (per OpenAI's official Codex App Server docs, confirmed against a
one-time manual read-only probe before this module was written):
- stdio transport, newline-delimited JSON (JSONL), no `"jsonrpc":"2.0"` key.
- `initialize` (with `clientInfo`) -> response, then an `initialized`
  notification, then the actual method call.
- `account/rateLimits/read` returns `result.rateLimits.primary` /
  `.secondary`, each `{usedPercent, windowDurationMins, resetsAt}` or null.
  Response order is not guaranteed, so windows are matched to five-hour /
  weekly purely by `windowDurationMins`, never by position.

This module deliberately calls `account/rateLimits/read` and nothing else:
no `account/rateLimitResetCredit/consume`, no task/thread/prompt method, no
subscription to `account/rateLimits/updated`. `result.rateLimitsByLimitId`
and `result.rateLimitResetCredits` are never read, even to count entries.

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
        "remaining_percentage": max(0.0, 100.0 - used_f),
        "resets_at": resets_at_dt.isoformat(),
        "window_duration_minutes": duration,
    }


def _map_windows(rate_limits: dict) -> tuple[dict | None, dict | None, bool]:
    """Map `primary`/`secondary` to five_hour/weekly by duration, order-independent.

    Returns (five_hour, weekly, ambiguous). `ambiguous=True` means both
    validated windows resolved to the *same* target slot (duplicate
    `windowDurationMins`) — the caller must treat the whole fetch as failed
    rather than guess which one is which. A window whose duration matches
    neither 300 nor 10080 minutes is silently dropped (not an error).
    """
    primary = _validate_window(rate_limits.get("primary"))
    secondary = _validate_window(rate_limits.get("secondary"))

    five_hour: dict | None = None
    weekly: dict | None = None
    targets_seen: list[str] = []
    for window in (primary, secondary):
        if window is None:
            continue
        duration = window["window_duration_minutes"]
        if duration == FIVE_HOUR_WINDOW_DURATION_MINUTES:
            targets_seen.append("five_hour")
            five_hour = window
        elif duration == WEEKLY_WINDOW_DURATION_MINUTES:
            targets_seen.append("weekly")
            weekly = window
        # else: unknown duration — dropped, not an error.

    if len(targets_seen) != len(set(targets_seen)):
        return None, None, True
    return five_hour, weekly, False


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

    send({"method": "account/rateLimits/read", "id": 2, "params": {}})
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

    five_hour, weekly, ambiguous = _map_windows(rate_limits)
    if ambiguous:
        return _failure("ambiguous_response", now)
    if five_hour is None and weekly is None:
        return _failure("invalid_response", now)

    return CodexRateLimitsFetchResult(
        success=True,
        windows={"five_hour": five_hour, "weekly": weekly},
        error_type=None,
        user_message=None,
        collected_at=now,
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
