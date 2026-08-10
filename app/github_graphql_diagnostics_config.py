"""GitHub GraphQL Consumption Diagnostics — environment configuration (v0.1).

Modeled on `app.codex_rate_limits_scheduler`'s `auto_refresh_enabled_from_env`
/ `auto_refresh_interval_seconds_from_env` pair: read the environment once,
never raise on a bad value, clamp-not-ignore whenever a floor applies.

Default-enabled vs. default-disabled is deliberately the opposite of the
Codex auto-refresh scheduler. The Codex scheduler defaults to *enabled*
because it only ever reads from an existing local cache file — it adds no
extra calls to anything. This feature is different: every sampler tick issues
an *additional* `gh api rate_limit` call purely for diagnostic purposes, on
top of whatever the rest of the app already does, and GitHub's own docs note
that `rate_limit` polling itself can contribute to secondary rate limiting
if done too aggressively. Because this feature actively adds load rather than
just reading a cache, it must be explicitly opted into (default `False`),
never on by default.
"""

from __future__ import annotations

import os
from typing import Mapping

ENV_ENABLED = "GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED"
ENV_SAMPLE_SECONDS = "GITHUB_GRAPHQL_DIAGNOSTIC_SAMPLE_SECONDS"
ENV_MAX_MINUTES = "GITHUB_GRAPHQL_DIAGNOSTIC_MAX_MINUTES"

DEFAULT_SAMPLE_SECONDS = 10
DEFAULT_MAX_MINUTES = 15

# MIN_SAMPLE_SECONDS is an app-level *operational* safety floor chosen to
# avoid hammering `gh api rate_limit` on a tight loop — it is NOT a value
# documented or mandated by GitHub. GitHub does not publish a minimum polling
# interval for `rate_limit`; this floor exists purely so a misconfigured
# environment variable (e.g. "0" or "1") can't turn this opt-in diagnostic
# feature into an accidental hot loop.
MIN_SAMPLE_SECONDS = 5

_TRUE_VALUES = {"1", "true", "yes", "on"}


def diagnostics_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Default False (opt-in). Only a recognized truthy string enables it.

    Unlike the Codex auto-refresh scheduler (default enabled, since it just
    reads an existing cache), this feature causes ADDITIONAL
    `gh api rate_limit` calls purely for diagnostic sampling, which GitHub's
    docs note is subject to secondary rate limiting -- so it must be
    explicitly opted into, not on by default.
    """
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_ENABLED)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES


def sample_seconds_from_env(env: Mapping[str, str] | None = None) -> int:
    """Default 10. Unparseable -> default. Clamped up to MIN_SAMPLE_SECONDS if
    below it (never silently ignored -- same clamp-not-ignore policy as the
    Codex scheduler's interval floor)."""
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_SAMPLE_SECONDS)
    if raw is None:
        return DEFAULT_SAMPLE_SECONDS
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return DEFAULT_SAMPLE_SECONDS
    if value < MIN_SAMPLE_SECONDS:
        return MIN_SAMPLE_SECONDS
    return value


def max_minutes_from_env(env: Mapping[str, str] | None = None) -> int:
    """Default 15. Unparseable -> default. Clamped up to 1 if <= 0."""
    environ = env if env is not None else os.environ
    raw = environ.get(ENV_MAX_MINUTES)
    if raw is None:
        return DEFAULT_MAX_MINUTES
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return DEFAULT_MAX_MINUTES
    if value <= 0:
        return 1
    return value
