"""GitHub Actions monthly billing/minutes monitoring — pure domain logic.

Mirrors the design philosophy of app.github_rate_limit (Phase A): this module
contains pure data types and pure functions only. It never calls `gh`,
subprocess, or any network API. Fetching (app.github_actions_billing_cli) and
process-local caching (app.github_actions_billing_state) are separate layers.

This module answers a genuinely different question from app.github_rate_limit:
that module tracks API *request* quota (core/graphql/search, reset hourly);
this module tracks the *monthly* Actions minutes entitlement tied to the
account's plan (Free/Pro), reset on a monthly billing cycle. The two are
never conflated in report shape or in the UI.

Key semantics, confirmed against official GitHub docs (see
docs/github-actions-billing-monitor.md for the source list):
- GitHub Free: 2,000 included Actions minutes/month. GitHub Pro: 3,000. This
  is a hard fact from the plan itself and is always knowable once the plan
  name resolves — see `resolve_included_minutes`.
- Standard GitHub-hosted runners are free on public repositories and never
  consume included minutes there; self-hosted runners never consume included
  minutes either. Larger runners are *always* billed separately and can
  never draw from the included-minutes allowance ("Included minutes cannot
  be used for larger runners" — GitHub Actions billing docs).

IMPORTANT — what this module deliberately does NOT compute, and why:
GitHub's official "Billing reports reference" documents `discount_amount`
(the summary API's `discountQuantity` is the same concept, just per-SKU
quantity instead of a dollar amount) as follows, quoted verbatim:

    "The amount of usage that was discounted. Usage that is discounted as
    part of your account's included usage is reflected in this field. Also
    includes discounts for GitHub Actions usage for standard GitHub-hosted
    runners in public repositories and for self-hosted runners."

That is an explicit, official statement that `discountQuantity` mixes at
least three different reasons for being free: (1) the plan's monthly
included-minutes allowance (capped, resets monthly), (2) public-repository
standard-runner usage (uncapped, never counts against any quota), and
(3) self-hosted-runner usage (uncapped, never counts against any quota).
The billing usage summary API's `usageItems` carry no repository/visibility
field to separate these — so there is currently no officially documented,
safe way to derive "exact minutes consumed from the plan's included
allowance" (and therefore no safe "exact remaining minutes") from this
endpoint alone. An earlier version of this module summed `discountQuantity`
directly into a `used_included_minutes`/`remaining_minutes` calculation;
that was incorrect and has been removed. `used_included_minutes`,
`remaining_minutes`, and `usage_percentage` are always `None` here (kept as
named fields for API stability, in case a future documented endpoint makes
this exact split available) rather than reporting a number that could be
inflated by unrelated, always-free usage.

What this module *does* still safely report, with names chosen not to
overstate what the field actually proves:
- `discounted_standard_minutes`: sum of `discountQuantity` across standard
  -runner SKUs — some unknown mix of included-allowance / public-repo /
  self-hosted discount. Informational only, never used to compute a
  remaining-quota number.
- `billable_standard_minutes`: sum of `netQuantity` across the same
  items — the portion GitHub actually billed for standard-runner usage.
  Deliberately not called "overage": while non-zero billable minutes on a
  standard SKU likely means the included allowance was exhausted (public
  and self-hosted usage is always fully discounted), this module does not
  assert that reading as confirmed without a documented guarantee.
- `paid_non_included_minutes`: sum of `netQuantity` across larger-runner
  SKUs — this one *is* solidly documented ("always charged for... even when
  you have quota available from your plan"), so it is reported with
  confidence.
- `skipped_unknown_skus`: SKUs recognized as neither standard, larger
  -runner, nor storage — reported but never added to any total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

BillingStatus = Literal[
    "usage_breakdown_inconclusive",
    "plan_unknown",
]

SkuClassification = Literal["standard", "larger_runner", "storage", "unknown"]

ACTIONS_PRODUCT_NAME = "actions"  # case-insensitive match against payload "product"
MINUTES_UNIT_TYPE = "minutes"  # case-insensitive match against payload "unitType"

# Standard GitHub-hosted runner SKUs. Source: docs.github.com "Actions
# runner pricing" / "Product and SKU names" (see
# docs/github-actions-billing-monitor.md). NOTE: membership in this set does
# NOT mean "counts toward included minutes" in a way this module can prove —
# see the module docstring for why discountQuantity cannot be cleanly
# attributed to the plan's included allowance alone.
STANDARD_RUNNER_SKUS: frozenset[str] = frozenset(
    {
        "actions_linux_slim",
        "actions_linux",
        "actions_linux_arm",
        "actions_windows",
        "actions_windows_arm",
        "actions_macos",
    }
)

# Larger runner SKUs. Always billed in full (never discounted against the
# included-minutes allowance) per official docs — listed explicitly (rather
# than "anything non-standard") so this module can label them confidently
# instead of lumping them in with genuinely unrecognized SKUs.
LARGER_RUNNER_SKUS: frozenset[str] = frozenset(
    {
        "actions_linux_2_core_advanced",
        "actions_linux_2_core_arm",
        "actions_linux_4_core",
        "actions_linux_4_core_arm",
        "actions_linux_4_core_gpu",
        "actions_linux_8_core",
        "actions_linux_8_core_arm",
        "actions_linux_32_core",
        "actions_linux_32_core_arm",
        "actions_linux_64_core",
        "actions_linux_64_core_arm",
        "actions_linux_96_core",
        "actions_windows_2_core_advanced",
        "actions_windows_2_core_arm",
        "actions_windows_4_core",
        "actions_windows_4_core_arm",
        "actions_windows_4_core_gpu",
        "actions_windows_8_core",
        "actions_windows_8_core_arm",
        "actions_windows_16_core",
        "actions_windows_32_core",
        "actions_windows_32_core_arm",
        "actions_windows_64_core",
        "actions_windows_64_core_arm",
        "actions_macos_l",
        "actions_macos_xl",
    }
)

# Not minute-denominated (storage is billed in GB-hours) — informational
# only; these are already excluded by the unitType == "minutes" filter, but
# listed explicitly so a future reader can see they were considered, not
# missed.
STORAGE_SKUS: frozenset[str] = frozenset(
    {"actions_storage", "actions_cache_storage", "actions_custom_image_storage"}
)

# Personal-account plan -> included Actions minutes/month. Source: GitHub
# Actions billing docs. Only plan names confirmed here are ever mapped; any
# other value (including a real plan this module has not been taught yet,
# e.g. "team"/"enterprise") resolves to `None` via resolve_included_minutes
# rather than being guessed.
PLAN_INCLUDED_MINUTES: dict[str, int] = {
    "free": 2000,
    "pro": 3000,
}


def classify_sku(sku: str) -> SkuClassification:
    if sku in STANDARD_RUNNER_SKUS:
        return "standard"
    if sku in LARGER_RUNNER_SKUS:
        return "larger_runner"
    if sku in STORAGE_SKUS:
        return "storage"
    return "unknown"


def resolve_included_minutes(plan_name: str | None) -> int | None:
    if plan_name is None:
        return None
    return PLAN_INCLUDED_MINUTES.get(plan_name.strip().lower())


def _finite_non_negative(value: object) -> float | None:
    """Coerces a raw quantity field to a finite, non-negative float, or
    `None` if it cannot be trusted (missing, wrong type, NaN/Infinity,
    negative) — never raises, since one malformed usage item must not make
    the whole monthly report unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


@dataclass(frozen=True, slots=True)
class ActionsMinutesAggregate:
    discounted_standard_minutes: float
    billable_standard_minutes: float
    paid_non_included_minutes: float
    skipped_unknown_skus: tuple[str, ...]


def aggregate_actions_minutes(usage_items: list) -> ActionsMinutesAggregate:
    """Aggregate a billing usage summary's `usageItems` into minutes totals.

    Only items with product == "actions" (case-insensitive) and
    unitType == "minutes" (case-insensitive) are considered at all — this is
    what excludes storage/cache items without needing to enumerate every
    storage SKU. Within those:
    - a STANDARD_RUNNER_SKUS item contributes its `discountQuantity` to
      `discounted_standard_minutes` and its `netQuantity` to
      `billable_standard_minutes`. Neither is treated as "included quota
      consumed" or "overage" — see the module docstring for why
      discountQuantity cannot be safely attributed to the plan's allowance
      alone (it also covers public-repo and self-hosted discounts).
    - a LARGER_RUNNER_SKUS item contributes its `netQuantity` to
      `paid_non_included_minutes` — larger runners are always billed in full
      and never draw from the included allowance, so their discountQuantity
      is expected to be 0 and is never added to `discounted_standard_minutes`.
    - any other SKU is recorded in `skipped_unknown_skus` and contributes to
      no total at all (never guessed into either bucket).

    A malformed individual item (missing/non-numeric/negative/NaN/Infinity
    quantity fields) is skipped, not raised — this mirrors
    app.github_rate_limit's per-resource "Error" philosophy: one bad item
    must not make the whole report unusable. Raises ValueError only when
    `usage_items` itself is not a list (the payload is structurally
    unusable, nothing to aggregate at all).
    """
    if not isinstance(usage_items, list):
        raise ValueError("usageItems must be a list")

    discounted_standard = 0.0
    billable_standard = 0.0
    paid_non_included = 0.0
    skipped: list[str] = []

    for raw_item in usage_items:
        if not isinstance(raw_item, dict):
            continue
        product = raw_item.get("product")
        unit_type = raw_item.get("unitType")
        if not isinstance(product, str) or product.strip().lower() != ACTIONS_PRODUCT_NAME:
            continue
        if not isinstance(unit_type, str) or unit_type.strip().lower() != MINUTES_UNIT_TYPE:
            continue

        sku = raw_item.get("sku")
        if not isinstance(sku, str):
            continue
        classification = classify_sku(sku)

        discount_quantity = _finite_non_negative(raw_item.get("discountQuantity"))
        net_quantity = _finite_non_negative(raw_item.get("netQuantity"))

        if classification == "standard":
            if discount_quantity is not None:
                discounted_standard += discount_quantity
            if net_quantity is not None:
                billable_standard += net_quantity
        elif classification == "larger_runner":
            if net_quantity is not None:
                paid_non_included += net_quantity
        elif classification == "unknown":
            skipped.append(sku)
        # "storage" is unreachable here (the unitType filter above already
        # excludes it) — classify_sku still reports it for callers that
        # inspect a SKU directly, e.g. tests.

    return ActionsMinutesAggregate(
        discounted_standard_minutes=discounted_standard,
        billable_standard_minutes=billable_standard,
        paid_non_included_minutes=paid_non_included,
        skipped_unknown_skus=tuple(skipped),
    )


@dataclass(frozen=True, slots=True)
class GitHubActionsBillingReport:
    status: BillingStatus
    plan_name: str | None
    included_minutes: int | None
    # Always None: no officially documented API currently isolates "minutes
    # consumed from the plan's included allowance" from public-repo/
    # self-hosted discount (see module docstring). Kept as named fields —
    # not removed — so the API/UI contract stays stable if a future,
    # properly documented endpoint makes this exact split available.
    used_included_minutes: float | None
    remaining_minutes: float | None
    usage_percentage: float | None
    discounted_standard_minutes: float | None
    billable_standard_minutes: float | None
    paid_non_included_minutes: float | None
    billing_year: int
    billing_month: int
    collected_at: datetime
    source: str
    skipped_unknown_skus: tuple[str, ...] = field(default_factory=tuple)


def build_billing_report(
    *,
    plan_name: str | None,
    usage_items: list,
    billing_year: int,
    billing_month: int,
    now: datetime,
    source: str,
) -> GitHubActionsBillingReport:
    """Build the full report from an already-fetched plan name and billing
    usage payload. Called only after a successful fetch of both `GET /user`
    and the billing usage summary — fetch-level failure states
    (permission_required / api_unavailable / error) are never represented
    here; they are a wrapping concept owned by the CLI/state layers, exactly
    as app.github_rate_limit_state wraps app.github_rate_limit's report with
    its own "error" dict rather than the domain module knowing about `gh`
    failures.

    `plan_name=None` (or an unrecognized plan) is not a fetch failure — it
    produces a "plan_unknown" report here, on purpose: the fetch succeeded,
    we simply cannot safely map the plan to an included-minutes number.
    When the plan *is* known, status is "usage_breakdown_inconclusive" —
    the included-minutes allowance itself is a known fact, but exactly how
    many of those minutes have been consumed is not currently derivable
    from any documented API (see module docstring).

    Raises ValueError only when `usage_items` is structurally unusable (not
    a list) — callers should pre-validate this from the raw payload before
    calling, matching app.github_rate_limit_cli's pre-validation of
    payload["resources"].
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    collected_at = now.astimezone(timezone.utc)

    aggregate = aggregate_actions_minutes(usage_items)
    included_minutes = resolve_included_minutes(plan_name)
    status: BillingStatus = "plan_unknown" if included_minutes is None else "usage_breakdown_inconclusive"

    return GitHubActionsBillingReport(
        status=status,
        plan_name=plan_name,
        included_minutes=included_minutes,
        used_included_minutes=None,
        remaining_minutes=None,
        usage_percentage=None,
        discounted_standard_minutes=aggregate.discounted_standard_minutes,
        billable_standard_minutes=aggregate.billable_standard_minutes,
        paid_non_included_minutes=aggregate.paid_non_included_minutes,
        billing_year=billing_year,
        billing_month=billing_month,
        collected_at=collected_at,
        source=source,
        skipped_unknown_skus=aggregate.skipped_unknown_skus,
    )
