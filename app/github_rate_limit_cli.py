"""GitHub API Rate Limit Monitoring — CLI fetch adapter (Phase B).

Thin subprocess wrapper around `gh api rate_limit`. This module never calls
the network directly, never reads `.env`, and never reads or logs
`GH_TOKEN`/`GITHUB_TOKEN` or any other credential value — it only inspects
the CLI's exit code and stderr text to classify *why* the call failed.

It is deliberately separate from `app.github_rate_limit` (Phase A): this
module owns the process boundary (subprocess, timeouts, JSON decoding),
while Phase A owns the pure status/Overall judgement logic. A successful
fetch's `payload` is handed to `app.github_rate_limit.parse_github_rate_limit`
unchanged; per-resource anomalies inside an otherwise well-formed payload are
Phase A's responsibility (see its "Error" resource status), not this
module's.

Design notes:
- `user_message` is always one of a small set of fixed, generic strings keyed
  by `error_type` — it never includes any excerpt of stdout/stderr. This is
  a deliberate, simpler-than-required safety margin: it trivially satisfies
  "no token leakage into user_message" without needing to reason about which
  substrings of a CLI error might be sensitive.
- Auth-related classification only looks at `gh`'s own stderr wording, in
  small dedicated predicate functions. If the wording doesn't clearly match
  a known pattern, this module does not guess — it falls back to
  "command_failed" (or "api_error" for HTTP-status-looking failures) rather
  than asserting an authentication problem that may not exist.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.github_rate_limit import GitHubRateLimitReport, parse_github_rate_limit

GH_COMMAND: tuple[str, ...] = ("gh", "api", "rate_limit")
DEFAULT_TIMEOUT_SECONDS = 10

FetchErrorType = Literal[
    "cli_not_installed",
    "not_authenticated",
    "authentication_expired",
    "timeout",
    "command_failed",
    "api_error",
    "invalid_json",
    "invalid_response",
    "unknown",
]

_USER_MESSAGES: dict[FetchErrorType, str] = {
    "cli_not_installed": "GitHub CLI (gh) is not installed or not on PATH.",
    "not_authenticated": "GitHub CLI is not authenticated. Run `gh auth login`.",
    "authentication_expired": "GitHub CLI authentication appears to be expired or invalid.",
    "timeout": "Fetching the GitHub rate limit timed out.",
    "command_failed": "The GitHub CLI command failed.",
    "api_error": "The GitHub API returned an error.",
    "invalid_json": "GitHub CLI returned output that is not valid JSON.",
    "invalid_response": "GitHub CLI response did not contain the expected rate limit structure.",
    "unknown": "An unexpected error occurred while fetching the GitHub rate limit.",
}


@dataclass(frozen=True, slots=True)
class GitHubRateLimitFetchResult:
    success: bool
    payload: dict | None
    error_type: FetchErrorType | None
    user_message: str | None
    return_code: int | None
    collected_at: datetime
    retry_after: int | None = None


def _failure(
    error_type: FetchErrorType,
    return_code: int | None,
    collected_at: datetime,
) -> GitHubRateLimitFetchResult:
    return GitHubRateLimitFetchResult(
        success=False,
        payload=None,
        error_type=error_type,
        user_message=_USER_MESSAGES[error_type],
        return_code=return_code,
        collected_at=collected_at,
    )


def _looks_unauthenticated(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in ("gh auth login", "not logged into", "no valid credentials")
    )


def _looks_authentication_expired(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in ("bad credentials", "401", "token expired", "credentials expired")
    )


def _looks_like_api_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return "http 4" in lowered or "http 5" in lowered or "api error" in lowered


def _classify_failure(stderr: str) -> FetchErrorType:
    if _looks_unauthenticated(stderr):
        return "not_authenticated"
    if _looks_authentication_expired(stderr):
        return "authentication_expired"
    if _looks_like_api_error(stderr):
        return "api_error"
    return "command_failed"


def fetch_github_rate_limit(
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> GitHubRateLimitFetchResult:
    """Run `gh api rate_limit` and classify the outcome.

    Never raises for expected failure modes (missing CLI, timeout, non-zero
    exit, malformed output) — those are all represented as a failed
    `GitHubRateLimitFetchResult` instead. `now` may be injected for
    deterministic tests; it defaults to the real current time and is always
    normalized to timezone-aware UTC for `collected_at`.
    """
    collected_at = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)

    try:
        completed = subprocess.run(
            list(GH_COMMAND),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return _failure("cli_not_installed", None, collected_at)
    except subprocess.TimeoutExpired:
        return _failure("timeout", None, collected_at)
    except Exception:
        return _failure("unknown", None, collected_at)

    if completed.returncode != 0:
        error_type = _classify_failure(completed.stderr or "")
        return _failure(error_type, completed.returncode, collected_at)

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _failure("invalid_json", completed.returncode, collected_at)

    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
        return _failure("invalid_response", completed.returncode, collected_at)

    return GitHubRateLimitFetchResult(
        success=True,
        payload=payload,
        error_type=None,
        user_message=None,
        return_code=completed.returncode,
        collected_at=collected_at,
    )


def build_github_rate_limit_report(
    fetch_result: GitHubRateLimitFetchResult,
    *,
    tz: timezone = timezone.utc,
) -> GitHubRateLimitReport | None:
    """Bridge a successful fetch into Phase A's domain report.

    Returns `None` when `fetch_result` was not successful — callers should
    inspect `fetch_result.error_type` / `user_message` in that case instead.
    """
    if not fetch_result.success or fetch_result.payload is None:
        return None
    return parse_github_rate_limit(fetch_result.payload, now=fetch_result.collected_at, tz=tz)
