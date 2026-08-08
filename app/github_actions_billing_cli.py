"""GitHub Actions monthly billing — CLI fetch adapter.

Thin subprocess wrapper around the GitHub CLI (`gh api ...`), mirroring
app.github_rate_limit_cli's design: this module owns the process boundary
(subprocess, timeouts, JSON decoding) and never reads `.env`, never reads or
logs `GH_TOKEN`/`GITHUB_TOKEN` or any other credential value — it only
inspects `gh`'s exit code and stderr text to classify *why* a call failed.
Reuses the existing `gh` CLI authentication that app.github_rate_limit_cli
already relies on; this module never creates, stores, or requests a new
token/credential itself.

Two sequential, read-only GET calls are needed (billing usage is scoped to
a specific username, unlike `gh api rate_limit`):
  1. `gh api user` — to learn the authenticated login (for the billing path)
     and the account's plan name (`plan.name`, possibly absent/null if the
     current credential cannot see it — see module docstring in
     app.github_actions_billing for why that is *not* treated as a fetch
     failure).
  2. `gh api /users/{login}/settings/billing/usage/summary?year=Y&month=M&product=actions`
     — the actual minutes usage. This endpoint is officially Public Preview
     and requires the token to have "Plan: read" permission; a token missing
     that permission is expected to surface as a 403 or (as observed with
     this app's own `gh` CLI OAuth token, which lacks the classic `user`
     scope) a 404 accompanied by `gh`'s own "needs the ... scope" hint text.
     Either shape is classified as `permission_required` here rather than a
     generic error, so the UI can point at the actual missing permission
     instead of a bare "API unavailable" message.

Design notes (same rationale as app.github_rate_limit_cli):
- `user_message` is always one of a small set of fixed, generic strings keyed
  by `error_type` — it never includes any excerpt of stdout/stderr.
- Auth/permission classification only looks at `gh`'s own stderr wording, in
  small dedicated predicate functions. If the wording doesn't clearly match a
  known pattern, this module does not guess.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.github_actions_billing import GitHubActionsBillingReport, build_billing_report

DEFAULT_TIMEOUT_SECONDS = 10

FetchErrorType = Literal[
    "cli_not_installed",
    "not_authenticated",
    "authentication_expired",
    "permission_required",
    "api_unavailable",
    "secondary_rate_limit",
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
    "permission_required": (
        "The current GitHub credential does not have permission to read Actions billing usage "
        '(requires the "Plan: read" permission / "user" scope).'
    ),
    "api_unavailable": "GitHub billing data is unavailable for this account.",
    "secondary_rate_limit": "GitHub secondary rate limit reached.",
    "timeout": "Fetching GitHub Actions billing usage timed out.",
    "command_failed": "The GitHub CLI command failed.",
    "api_error": "The GitHub API returned an error.",
    "invalid_json": "GitHub CLI returned output that is not valid JSON.",
    "invalid_response": "GitHub CLI response did not contain the expected billing usage structure.",
    "unknown": "An unexpected error occurred while fetching GitHub Actions billing usage.",
}


@dataclass(frozen=True, slots=True)
class GitHubActionsBillingFetchResult:
    success: bool
    plan_name: str | None
    usage_items: list | None
    billing_year: int | None
    billing_month: int | None
    error_type: FetchErrorType | None
    user_message: str | None
    return_code: int | None
    collected_at: datetime


def _failure(
    error_type: FetchErrorType,
    return_code: int | None,
    collected_at: datetime,
) -> GitHubActionsBillingFetchResult:
    return GitHubActionsBillingFetchResult(
        success=False,
        plan_name=None,
        usage_items=None,
        billing_year=None,
        billing_month=None,
        error_type=error_type,
        user_message=_USER_MESSAGES[error_type],
        return_code=return_code,
        collected_at=collected_at,
    )


def _looks_unauthenticated(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(phrase in lowered for phrase in ("gh auth login", "not logged into", "no valid credentials"))


def _looks_authentication_expired(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(phrase in lowered for phrase in ("bad credentials", "401", "token expired", "credentials expired"))


def _looks_like_secondary_rate_limit(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in ("secondary rate limit", "secondary_rate_limit", "abuse detection mechanism")
    )


def _looks_like_missing_scope_or_permission(stderr: str) -> bool:
    """Matches gh's own "needs the ... scope" hint and generic 403/permission
    wording. Checked before the plain-404 `api_unavailable` fallback, since a
    missing-scope failure from this app's own `gh` CLI token was observed to
    surface as HTTP 404 (not 403) with this specific hint text."""
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in ("needs the", "http 403", "resource not accessible", "requires the plan")
    )


def _looks_like_not_found(stderr: str) -> bool:
    lowered = stderr.lower()
    return "http 404" in lowered or "not found" in lowered


def _looks_like_api_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return "http 4" in lowered or "http 5" in lowered or "api error" in lowered


def _classify_failure(stderr: str) -> FetchErrorType:
    if _looks_unauthenticated(stderr):
        return "not_authenticated"
    if _looks_authentication_expired(stderr):
        return "authentication_expired"
    if _looks_like_secondary_rate_limit(stderr):
        return "secondary_rate_limit"
    if _looks_like_missing_scope_or_permission(stderr):
        return "permission_required"
    if _looks_like_not_found(stderr):
        return "api_unavailable"
    if _looks_like_api_error(stderr):
        return "api_error"
    return "command_failed"


# The billing usage summary endpoint is officially Public Preview and
# "subject to change" — pinning both the Accept media type and the REST API
# version (current per docs.github.com/en/rest/about-the-rest-api/api-versions
# as of this writing) makes a future breaking change on GitHub's side fail
# loudly (a version GitHub no longer serves errors instead of silently
# reinterpreting the request) rather than silently changing behavior here.
# Applied identically to both `gh api` calls (user + billing) for one
# consistent version policy.
GITHUB_API_VERSION = "2026-03-10"
_API_HEADERS: tuple[str, ...] = (
    "-H",
    "Accept: application/vnd.github+json",
    "-H",
    f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
)


def _run_gh_api(path: str, *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", "api", path, *_API_HEADERS],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def fetch_github_actions_billing(
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> GitHubActionsBillingFetchResult:
    """Fetch the authenticated user's plan and this month's Actions billing
    usage via `gh api`. Never raises for expected failure modes (missing
    CLI, timeout, non-zero exit, malformed output) — those are all
    represented as a failed `GitHubActionsBillingFetchResult` instead.

    `now` may be injected for deterministic tests; it defaults to the real
    current time and is always normalized to timezone-aware UTC for
    `collected_at`, and its year/month (UTC) are used as the billing period
    requested from the summary endpoint.
    """
    collected_at = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    billing_year = collected_at.year
    billing_month = collected_at.month

    try:
        user_result = _run_gh_api("user", timeout=timeout)
    except FileNotFoundError:
        return _failure("cli_not_installed", None, collected_at)
    except subprocess.TimeoutExpired:
        return _failure("timeout", None, collected_at)
    except Exception:
        return _failure("unknown", None, collected_at)

    if user_result.returncode != 0:
        error_type = _classify_failure(user_result.stderr or "")
        return _failure(error_type, user_result.returncode, collected_at)

    try:
        user_payload = json.loads(user_result.stdout)
    except json.JSONDecodeError:
        return _failure("invalid_json", user_result.returncode, collected_at)

    if not isinstance(user_payload, dict) or not isinstance(user_payload.get("login"), str):
        return _failure("invalid_response", user_result.returncode, collected_at)

    login = user_payload["login"]
    plan = user_payload.get("plan")
    plan_name = plan.get("name") if isinstance(plan, dict) and isinstance(plan.get("name"), str) else None

    billing_path = (
        f"/users/{login}/settings/billing/usage/summary"
        f"?year={billing_year}&month={billing_month}&product=actions"
    )
    try:
        billing_result = _run_gh_api(billing_path, timeout=timeout)
    except FileNotFoundError:
        return _failure("cli_not_installed", None, collected_at)
    except subprocess.TimeoutExpired:
        return _failure("timeout", None, collected_at)
    except Exception:
        return _failure("unknown", None, collected_at)

    if billing_result.returncode != 0:
        error_type = _classify_failure(billing_result.stderr or "")
        return _failure(error_type, billing_result.returncode, collected_at)

    try:
        billing_payload = json.loads(billing_result.stdout)
    except json.JSONDecodeError:
        return _failure("invalid_json", billing_result.returncode, collected_at)

    if not isinstance(billing_payload, dict) or not isinstance(billing_payload.get("usageItems"), list):
        return _failure("invalid_response", billing_result.returncode, collected_at)

    return GitHubActionsBillingFetchResult(
        success=True,
        plan_name=plan_name,
        usage_items=billing_payload["usageItems"],
        billing_year=billing_year,
        billing_month=billing_month,
        error_type=None,
        user_message=None,
        return_code=billing_result.returncode,
        collected_at=collected_at,
    )


def build_github_actions_billing_report(
    fetch_result: GitHubActionsBillingFetchResult,
) -> GitHubActionsBillingReport | None:
    """Bridge a successful fetch into the domain report. Returns `None` when
    `fetch_result` was not successful — callers should inspect
    `fetch_result.error_type` / `user_message` in that case instead."""
    if not fetch_result.success or fetch_result.usage_items is None:
        return None
    return build_billing_report(
        plan_name=fetch_result.plan_name,
        usage_items=fetch_result.usage_items,
        billing_year=fetch_result.billing_year,
        billing_month=fetch_result.billing_month,
        now=fetch_result.collected_at,
        source="github_billing_api",
    )
