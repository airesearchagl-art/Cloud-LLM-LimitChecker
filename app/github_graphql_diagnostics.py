"""GitHub GraphQL Consumption Diagnostics — pure domain logic (v0.1).

This module contains pure data types and pure functions only. It never calls
`gh`, subprocess, network I/O, or `datetime.now()` — all timestamps are taken
as parameters, matching `app/github_rate_limit.py`'s Phase A design. The API
route, DB models, and periodic sampler that will drive these functions are
explicitly out of scope here and live elsewhere in this project.

## What this feature is

The app already polls `GET /rate_limit` (see `app/github_rate_limit.py`).
Diagnostics v0.1 lets a user start/stop named "activity sessions" (e.g.
"Claude Code doing PR review") while the app periodically re-samples the
`graphql` resource block. The goal is to let a user eyeball **temporal
correlation** between GraphQL quota consumption and whichever activity
sessions happened to be running at the time.

## Why this is correlation, never attribution

GitHub's REST `rate_limit` endpoint reports a single account-wide `used`
counter per resource. It does not expose, and has never exposed, a
breakdown of which client, token, or process consumed how many GraphQL
points. Consequently:

- This module never claims "session X consumed Y points". The strongest
  claim any function here makes is "exactly one activity session happened
  to be active while this sample's delta was observed" —
  `SINGLE_ACTIVITY_CORRELATION`. That is still just correlation: the
  active session may not have made any GraphQL calls at all (some other
  process, a browser tab, a CI job using the same token, etc. could have),
  and the observed delta could equally be zero.
- When two or more sessions overlap a sample's interval, this module
  refuses to guess how much each contributed. There is no proportional
  split, no "assume even distribution", no heuristic weighting by session
  duration — that would fabricate a precision GitHub's API does not
  provide. Such samples are simply classified `OVERLAPPING_ACTIVITIES`,
  with no per-session numeric output of any kind.
- Whole-session totals (`compute_session_total_delta`) describe what was
  *observed* during a session's window ("Session observed delta"), never
  what the session *consumed* — the wording distinction is deliberate and
  is carried through to the UI layer that consumes this module's output.

## Delta safety

Two consecutive `graphql` samples can only be diffed meaningfully when they
fall in the same reset window. GitHub resets the `used` counter to 0 (and
publishes a new `reset` epoch) once per window; subtracting `used` values
across a reset boundary would misrepresent a window transition as
in-window consumption, so `compute_graphql_delta` refuses to do it and
reports `"reset_boundary"` instead. Similarly, `used` should be
monotonically non-decreasing within a window; an observed decrease is an
anomaly this module detects and reports (`"counter_regression"`) but never
explains — the possible causes (credential/account context switch,
provider-side inconsistency, etc.) are all speculative and out of scope
for a pure function to assert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.github_rate_limit import ResourceRateLimit

# Outcome of comparing two consecutive graphql ResourceRateLimit samples.
DeltaOutcome = Literal["ok", "no_previous", "reset_boundary", "counter_regression"]

# Classification of what a sample's observed delta can honestly be correlated
# with. This is temporal correlation, never exact attribution -- see the
# module docstring above for the full rationale: GitHub's API does not
# expose per-consumer GraphQL usage breakdown; SINGLE_ACTIVITY_CORRELATION
# still means "exactly one activity session happened to be active", not
# "this session definitely caused this usage"; deltas are never split
# proportionally across overlapping sessions.
AttributionStatus = Literal[
    "UNATTRIBUTED",
    "SINGLE_ACTIVITY_CORRELATION",
    "OVERLAPPING_ACTIVITIES",
    "RESET_BOUNDARY",
    "COUNTER_REGRESSION",
    "FETCH_FAILED",
]


@dataclass(frozen=True, slots=True)
class GraphQLDeltaResult:
    delta: int | None
    outcome: DeltaOutcome


@dataclass(frozen=True, slots=True)
class ActivityWindow:
    """A session's active time span, for the sole purpose of counting overlap
    with a sample instant. `ended_at=None` means the session is still active
    (open-ended). This is intentionally NOT the full session record — just
    the two fields this module's pure functions need."""

    started_at: datetime
    ended_at: datetime | None


def compute_graphql_delta(
    previous: ResourceRateLimit | None,
    current: ResourceRateLimit,
) -> GraphQLDeltaResult:
    """Compare two consecutive `graphql` resource samples.

    - `previous is None` (no prior sample exists yet, e.g. this is the very
      first sample ever taken): delta=None, outcome="no_previous". This is
      not an error -- there is simply nothing to diff against yet.
    - `previous.reset_at_utc != current.reset_at_utc`: the quota window
      reset between samples. delta=None, outcome="reset_boundary". Two
      different reset windows' `used` counters must never be subtracted
      from each other -- that would misrepresent a window transition as
      in-window consumption.
    - Same reset window, but `current.used < previous.used`: an unexplained
      counter decrease within what should be a monotonically-increasing
      window. delta=None, outcome="counter_regression". Never store a
      negative delta, and never guess at a cause (credential/account
      context change, provider-side inconsistency, etc. are all possible
      but this function must not speculate -- it only detects and reports
      the shape of the anomaly).
    - Same reset window, `current.used >= previous.used`: delta =
      `current.used - previous.used` (can be 0), outcome="ok".

    Only `.used` and `.reset_at_utc` are read from each `ResourceRateLimit`
    (both may be `None` if a prior parse failed). If either operand's
    `.used` or `.reset_at_utc` is `None`, this is NOT a valid comparable
    sample -- such a pair is treated equivalently to a reset boundary
    (delta=None, outcome="reset_boundary"), since in both cases there is no
    trustworthy same-window numeric comparison to make, and inventing a
    distinct outcome for "missing data" would just push the same ambiguity
    one layer up without adding information. Does not raise.
    """
    if previous is None:
        return GraphQLDeltaResult(delta=None, outcome="no_previous")

    if (
        previous.used is None
        or previous.reset_at_utc is None
        or current.used is None
        or current.reset_at_utc is None
    ):
        return GraphQLDeltaResult(delta=None, outcome="reset_boundary")

    if previous.reset_at_utc != current.reset_at_utc:
        return GraphQLDeltaResult(delta=None, outcome="reset_boundary")

    if current.used < previous.used:
        return GraphQLDeltaResult(delta=None, outcome="counter_regression")

    return GraphQLDeltaResult(delta=current.used - previous.used, outcome="ok")


def classify_attribution(
    *,
    delta_outcome: DeltaOutcome,
    active_session_count: int,
    fetch_failed: bool = False,
) -> AttributionStatus:
    """Classify a sample's attribution status.

    Precedence (checked in this order):
    1. `fetch_failed=True` -> "FETCH_FAILED" (the sample itself couldn't be
       fetched at all -- this takes priority over everything else).
    2. `delta_outcome == "reset_boundary"` -> "RESET_BOUNDARY".
    3. `delta_outcome == "counter_regression"` -> "COUNTER_REGRESSION".
    4. Otherwise (delta_outcome is "ok" or "no_previous"), classify purely
       by `active_session_count`:
       - `<= 0` -> "UNATTRIBUTED"
       - `== 1` -> "SINGLE_ACTIVITY_CORRELATION"
       - `>= 2` -> "OVERLAPPING_ACTIVITIES"

    This function NEVER computes or returns anything resembling a
    proportional split of the delta across sessions -- there is no
    per-session numeric output here at all, intentionally, since GitHub's
    API provides no basis for such a split.
    """
    if fetch_failed:
        return "FETCH_FAILED"

    if delta_outcome == "reset_boundary":
        return "RESET_BOUNDARY"

    if delta_outcome == "counter_regression":
        return "COUNTER_REGRESSION"

    if active_session_count <= 0:
        return "UNATTRIBUTED"
    if active_session_count == 1:
        return "SINGLE_ACTIVITY_CORRELATION"
    return "OVERLAPPING_ACTIVITIES"


def count_active_sessions_at(instant: datetime, windows: list[ActivityWindow]) -> int:
    """Count how many `windows` were active at `instant`.

    A window is active at `instant` iff `window.started_at <= instant` AND
    (`window.ended_at is None` OR `window.ended_at >= instant`). Boundary
    instants (exactly equal to `started_at` or `ended_at`) count as active:
    a session is considered to be running at the instant it starts and at
    the instant it ends, rather than excluding either edge, since a sample
    taken at exactly that instant genuinely overlapped the session's
    lifetime.
    """
    return sum(
        1
        for window in windows
        if window.started_at <= instant and (window.ended_at is None or window.ended_at >= instant)
    )


def compute_session_total_delta(
    *,
    graphql_used_start: int | None,
    graphql_used_end: int | None,
    reset_at_start: datetime | None,
    reset_at_end: datetime | None,
) -> int | None:
    """Whole-session start-to-end delta, for display as "Session observed
    delta" (see spec section 18: "Activity Window中のobserved GraphQL delta",
    never "consumer usage").

    Returns `None` (not a number) when:
    - either `graphql_used_start` or `graphql_used_end` is `None` (baseline
      or final sample was never successfully captured), OR
    - `reset_at_start != reset_at_end` (the session's window crossed a
      quota reset boundary -- two different reset windows' totals must
      never be summed into a single "session consumption" number; this is
      the v0.1 policy -- see spec section 8), OR
    - `graphql_used_end < graphql_used_start` (a same-window counter
      regression across the whole session -- same safety reasoning as
      `compute_graphql_delta`'s per-sample case).

    Otherwise returns `graphql_used_end - graphql_used_start` (>= 0).
    """
    if graphql_used_start is None or graphql_used_end is None:
        return None

    if reset_at_start != reset_at_end:
        return None

    if graphql_used_end < graphql_used_start:
        return None

    return graphql_used_end - graphql_used_start
