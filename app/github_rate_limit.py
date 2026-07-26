"""GitHub API Rate Limit Monitoring — fixture-first domain logic (Phase A).

This module contains pure data types and pure functions only. It never calls
`gh`, subprocess, or any network API — that is deliberately out of scope for
this phase (see Roadmap "10. GitHub API Rate Limit Monitoring", phase B/F).

Design notes:
- Unknown resource keys present in a fixture's "resources" mapping (anything
  other than core/graphql/search) are silently ignored rather than rejected.
  GitHub's real `rate_limit` response carries additional resource kinds that
  are outside this feature's initial monitoring scope, and rejecting them
  would make this module fragile to unrelated fields in the real API surface.
- A resource that cannot be parsed (missing, wrong shape, invalid values) is
  represented as a resource in "Error" status rather than raising, so that
  Overall judgement can still be computed from whichever primary resource is
  usable. Only a structurally unusable payload (not a mapping, or missing the
  "resources" key) raises ValueError, since there is nothing to parse at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Literal

ResourceName = Literal["core", "graphql", "search"]
ResourceStatus = Literal["Normal", "Warning", "Exhausted", "Reset overdue", "Error"]
OverallStatus = Literal["Normal", "Warning", "Limited", "Error"]

TARGET_RESOURCES: tuple[ResourceName, ...] = ("core", "graphql", "search")
PRIMARY_RESOURCES: tuple[ResourceName, ...] = ("core", "graphql")

_RESOURCE_LABELS: dict[ResourceName, str] = {
    "core": "REST API core",
    "graphql": "GraphQL API",
    "search": "Search API",
}

_STATUS_PRIORITY: dict[ResourceStatus, int] = {
    "Error": 0,
    "Reset overdue": 1,
    "Exhausted": 2,
    "Warning": 3,
    "Normal": 4,
}

_UNAVAILABLE_MESSAGE = "GitHub rate limit data unavailable"


@dataclass(frozen=True, slots=True)
class ResourceRateLimit:
    resource: ResourceName
    status: ResourceStatus
    limit: int | None
    used: int | None
    remaining: int | None
    usage_percent: float | None
    remaining_percent: float | None
    reset_at_utc: datetime | None
    reset_at_local: datetime | None
    seconds_until_reset: int | None  # negative once reset time has passed
    collected_at: datetime
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubRateLimitOverall:
    status: OverallStatus
    reason: str


@dataclass(frozen=True, slots=True)
class GitHubRateLimitReport:
    resources: dict[ResourceName, ResourceRateLimit]
    overall: GitHubRateLimitOverall
    collected_at: datetime


class _ResourceDataError(ValueError):
    """Internal-only: signals a single resource's raw data is unusable."""


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")


def _coerce_resource_fields(raw: dict) -> tuple[int, int, int, int]:
    if not isinstance(raw, dict):
        raise _ResourceDataError("resource value is not a mapping")

    missing = [key for key in ("limit", "used", "remaining", "reset") if key not in raw]
    if missing:
        raise _ResourceDataError(f"missing fields: {', '.join(missing)}")

    limit, used, remaining, reset = raw["limit"], raw["used"], raw["remaining"], raw["reset"]

    for field_name, value in (("limit", limit), ("used", used), ("remaining", remaining)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise _ResourceDataError(f"{field_name} must be an integer")
        if value < 0:
            raise _ResourceDataError(f"{field_name} must not be negative")

    if not isinstance(reset, int) or isinstance(reset, bool):
        raise _ResourceDataError("reset must be an integer unix timestamp")

    if limit == 0:
        raise _ResourceDataError("limit must not be zero")

    if remaining > limit:
        raise _ResourceDataError("remaining must not exceed limit")

    return limit, used, remaining, reset


def _error_resource(resource: ResourceName, collected_at: datetime, message: str) -> ResourceRateLimit:
    return ResourceRateLimit(
        resource=resource,
        status="Error",
        limit=None,
        used=None,
        remaining=None,
        usage_percent=None,
        remaining_percent=None,
        reset_at_utc=None,
        reset_at_local=None,
        seconds_until_reset=None,
        collected_at=collected_at,
        error_message=message,
    )


def build_resource_rate_limit(
    resource: ResourceName,
    raw: dict | None,
    *,
    now: datetime,
    tz: tzinfo = timezone.utc,
) -> ResourceRateLimit:
    """Build a single resource's rate-limit state from raw fixture data.

    `raw` is never mutated. `raw is None` (resource absent from the fetched
    payload), any structurally/semantically invalid `raw`, and a `reset` value
    datetime cannot represent (e.g. out of platform range) all produce an
    "Error" status resource instead of raising. `collected_at` is always
    `now` normalized to UTC — Phase A never reads a caller-supplied
    `collected_at` out of `raw`, since real GitHub API responses have no such
    field.

    `seconds_until_reset` keeps its sign: negative means reset time has
    already passed (status "Reset overdue").
    """
    _require_aware(now)
    normalized_now = now.astimezone(timezone.utc)

    if raw is None:
        return _error_resource(resource, normalized_now, _UNAVAILABLE_MESSAGE)

    try:
        limit, used, remaining, reset_epoch = _coerce_resource_fields(raw)
    except _ResourceDataError as exc:
        return _error_resource(resource, normalized_now, str(exc))

    try:
        reset_at_utc = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _error_resource(resource, normalized_now, "reset timestamp is out of range")

    reset_at_local = reset_at_utc.astimezone(tz)
    now_epoch = int(now.timestamp())

    # Compare aware datetimes directly rather than truncated epoch seconds,
    # so a reset overrun of less than one second is still "Reset overdue".
    if remaining == 0:
        status: ResourceStatus = "Exhausted" if normalized_now <= reset_at_utc else "Reset overdue"
    elif remaining * 5 <= limit:
        status = "Warning"
    else:
        status = "Normal"

    return ResourceRateLimit(
        resource=resource,
        status=status,
        limit=limit,
        used=used,
        remaining=remaining,
        usage_percent=used / limit * 100,
        remaining_percent=remaining / limit * 100,
        reset_at_utc=reset_at_utc,
        reset_at_local=reset_at_local,
        seconds_until_reset=reset_epoch - now_epoch,
        collected_at=normalized_now,
        error_message=None,
    )


def determine_overall(core: ResourceRateLimit, graphql: ResourceRateLimit) -> GitHubRateLimitOverall:
    """Overall judgement from the two primary resources (search is excluded).

    On a tie in severity between core and graphql, core is reported as the
    named cause (arbitrary but deterministic; the spec does not order ties).
    """
    worst_name, worst = min(
        (("core", core), ("graphql", graphql)),
        key=lambda pair: _STATUS_PRIORITY[pair[1].status],
    )
    label = _RESOURCE_LABELS[worst_name]

    if worst.status == "Error":
        return GitHubRateLimitOverall(status="Error", reason=worst.error_message or _UNAVAILABLE_MESSAGE)
    if worst.status == "Reset overdue":
        return GitHubRateLimitOverall(status="Limited", reason=f"{label} reset overdue")
    if worst.status == "Exhausted":
        return GitHubRateLimitOverall(status="Limited", reason=f"{label} exhausted")
    if worst.status == "Warning":
        return GitHubRateLimitOverall(status="Warning", reason=f"{label} approaching limit")
    return GitHubRateLimitOverall(status="Normal", reason="core and graphql are within normal limits")


def parse_github_rate_limit(
    payload: dict,
    *,
    now: datetime,
    tz: tzinfo = timezone.utc,
) -> GitHubRateLimitReport:
    """Parse a `{"resources": {...}}`-shaped payload into a full report.

    Raises ValueError only when the payload itself is structurally unusable
    (not a mapping, or missing a "resources" mapping). Missing/invalid
    individual resources are represented as "Error" status entries instead.
    """
    _require_aware(now)

    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
        raise ValueError("invalid GitHub rate limit payload structure")

    resources_raw = payload["resources"]
    resources: dict[ResourceName, ResourceRateLimit] = {
        name: build_resource_rate_limit(name, resources_raw.get(name), now=now, tz=tz)
        for name in TARGET_RESOURCES
    }
    overall = determine_overall(resources["core"], resources["graphql"])
    return GitHubRateLimitReport(resources=resources, overall=overall, collected_at=now.astimezone(timezone.utc))
