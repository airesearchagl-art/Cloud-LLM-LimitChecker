"""GitHub GraphQL Consumption Diagnostics — identity adapter (v0.1).

Thin subprocess wrapper around `gh api user`, modeled on
`app.github_rate_limit_cli`'s subprocess/error-classification style: never
raises for an expected failure mode, never leaks stdout/stderr into
`user_message`, classifies failures via small local predicate functions
rather than importing `app.github_rate_limit_cli`'s private helpers — this
codebase's convention is to duplicate small safety helpers per module (see
`app/collectors/openai_collector.py`'s `_safe_finite_float`, duplicated from
`app/collectors/gemini_collector.py`, rather than shared via import).

CRITICAL privacy note: `gh api user` returns a large JSON object (email,
name, avatar_url, company, bio, etc.). This module parses ONLY `login` (str)
and `id` (int) out of that response and discards everything else
immediately — no other field is ever stored, logged, or returned anywhere by
this module or its callers.

This module never calls GitHub's GraphQL API, never constructs a `/graphql`
request, and never invokes `gh api graphql` — it exclusively runs
`gh api user`, a REST endpoint.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Literal

GH_USER_COMMAND: tuple[str, ...] = ("gh", "api", "user")
DEFAULT_TIMEOUT_SECONDS = 10

IdentityFetchErrorType = Literal[
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

_USER_MESSAGES: dict[IdentityFetchErrorType, str] = {
    "cli_not_installed": "GitHub CLI (gh) is not installed or not on PATH.",
    "not_authenticated": "GitHub CLI is not authenticated. Run `gh auth login`.",
    "authentication_expired": "GitHub CLI authentication appears to be expired or invalid.",
    "timeout": "Fetching the GitHub identity timed out.",
    "command_failed": "The GitHub CLI command failed.",
    "api_error": "The GitHub API returned an error.",
    "invalid_json": "GitHub CLI returned output that is not valid JSON.",
    "invalid_response": "GitHub CLI response did not contain the expected identity fields.",
    "unknown": "An unexpected error occurred while fetching the GitHub identity.",
}


@dataclass(frozen=True, slots=True)
class GitHubIdentityFetchResult:
    success: bool
    login: str | None
    user_id: int | None
    error_type: IdentityFetchErrorType | None
    user_message: str | None


def _failure(error_type: IdentityFetchErrorType) -> GitHubIdentityFetchResult:
    return GitHubIdentityFetchResult(
        success=False,
        login=None,
        user_id=None,
        error_type=error_type,
        user_message=_USER_MESSAGES[error_type],
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


def _classify_failure(stderr: str) -> IdentityFetchErrorType:
    if _looks_unauthenticated(stderr):
        return "not_authenticated"
    if _looks_authentication_expired(stderr):
        return "authentication_expired"
    if _looks_like_api_error(stderr):
        return "api_error"
    return "command_failed"


def fetch_github_identity(*, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> GitHubIdentityFetchResult:
    """Run `gh api user` and extract only `login`/`id`.

    Never raises for expected failure modes (missing CLI, timeout, non-zero
    exit, malformed output, a response missing the expected fields) --
    those are all represented as a failed `GitHubIdentityFetchResult`
    instead. No field beyond `login`/`id` is ever read out of the response.
    """
    try:
        completed = subprocess.run(
            list(GH_USER_COMMAND),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return _failure("cli_not_installed")
    except subprocess.TimeoutExpired:
        return _failure("timeout")
    except Exception:
        return _failure("unknown")

    if completed.returncode != 0:
        return _failure(_classify_failure(completed.stderr or ""))

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _failure("invalid_json")

    if not isinstance(payload, dict):
        return _failure("invalid_response")

    login = payload.get("login")
    user_id = payload.get("id")

    if not isinstance(login, str) or not login:
        return _failure("invalid_response")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return _failure("invalid_response")

    return GitHubIdentityFetchResult(
        success=True,
        login=login,
        user_id=user_id,
        error_type=None,
        user_message=None,
    )
