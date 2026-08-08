# GitHub Actions Monthly Billing Monitor — Official Sources & Design Notes

This document records the official GitHub documentation this feature's
domain logic (`app/github_actions_billing.py`) and CLI adapter
(`app/github_actions_billing_cli.py`) are built against, and the
interpretation decisions made where the official docs required judgment.

## What this feature is (and is not)

`GET /api/github-actions-billing` reports the **monthly Actions minutes
entitlement** tied to the account's plan (GitHub Free: 2,000 min/month,
GitHub Pro: 3,000 min/month). This is a completely different quota from the
existing `GET /api/github-rate-limit` (hourly API **request** quota:
core/graphql/search). The two are never conflated in code, API response
shape, or the UI — separate cards, separate sections, separate controllers.

## Official sources consulted

- `docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions`
  — Free: 2,000 min/month, 500 MB storage. Pro: 3,000 min/month, 1 GB
  storage. "The use of standard GitHub-hosted runners is free" on public
  repositories. Actions is "free for self-hosted runners." "Larger runners
  are always charged for, even when used by public repositories or when you
  have quota available from your plan." "For private repositories, each
  GitHub account receives a quota of free minutes... depending on the
  account's plan" — i.e. the included-minutes allowance specifically applies
  to *private*-repository usage; public-repo and self-hosted usage are
  unconditionally free and unrelated to that cap.
- `docs.github.com/en/billing/reference/billing-reports` — the authoritative
  definition of what a "discount" is, quoted verbatim: *"The amount of usage
  that was discounted. Usage that is discounted as part of your account's
  included usage is reflected in this field. Also includes discounts for
  GitHub Actions usage for standard GitHub-hosted runners in public
  repositories and for self-hosted runners."* This is the load-bearing
  fact behind this feature's current design — see "Why exact remaining
  minutes cannot currently be computed" below.
- `docs.github.com/en/billing/reference/actions-runner-pricing` — per-minute
  rates for standard runners (informational; not used for any quota math).
- `docs.github.com/en/billing/reference/product-and-sku-names` — the
  authoritative SKU list this module's `STANDARD_RUNNER_SKUS`,
  `LARGER_RUNNER_SKUS`, and `STORAGE_SKUS` are transcribed from.
- `docs.github.com/en/rest/billing/usage` — the `GET
  /users/{username}/settings/billing/usage/summary` endpoint: **Public
  Preview**, "subject to change." Query params: `year`, `month`, `day`,
  `repository`, `product`, `sku`. Response `usageItems[]` fields used here:
  `product`, `sku`, `unitType`, `discountQuantity`, `netQuantity` (also
  present but unused: `grossQuantity`, `pricePerUnit`, `grossAmount`,
  `discountAmount`, `netAmount`). This endpoint's `usageItems[]` carries no
  `repository`/visibility field, so a response item cannot be attributed to
  a specific repository (or that repository's public/private status) from
  this endpoint alone.
  - The sibling detailed endpoint `GET /users/{username}/settings/billing/usage`
    (GA, per-day granular items) is *not* used here — its `usageItems[]`
    shape is `date`/`product`/`sku`/`quantity`/`unitType`/`pricePerUnit`/
    `grossAmount`/`discountAmount`/`netAmount`/`repositoryName` (dollar
    amounts, not a gross/discount/net *quantity* split), and per its own
    docs "is only available to users with access to the enhanced billing
    platform" — narrower account availability than the summary endpoint.
    Even if it were used, `repositoryName` alone does not indicate whether
    that repository was public or private at the time of the run (a repo's
    visibility can also change over time) — an extra API call per distinct
    repository would be needed to resolve current visibility, which is not
    a documented part of this billing endpoint's contract and was judged
    out of scope rather than treated as an "official supported" path.
- `docs.github.com/en/rest/about-the-rest-api/api-versions` — current REST
  API version `2026-03-10` (also `2022-11-28` still supported). Pinned via
  `X-GitHub-Api-Version` on every `gh api` call this feature makes.
- Fine-grained PAT / GitHub App user access token permission: personal
  billing usage endpoints require the **"Plan" permission (read)**.
- `GET /user` — `plan.name` (string), `plan.space`, `plan.collaborators`,
  `plan.private_repos`. The docs do not enumerate every possible
  `plan.name` value, which is exactly why this module never guesses beyond
  the two names it has confirmed the included-minutes allowance for (see
  below).

## Why exact remaining minutes cannot currently be computed

An earlier version of this feature summed `discountQuantity` directly into
`used_included_minutes` and derived `remaining_minutes` /
`usage_percentage` from it. **That was incorrect and has been reverted.**
The Billing reports reference page states, verbatim, that the discount
field reflects *at least three different reasons* for usage being free:
(1) the plan's included-minutes allowance, (2) standard-runner usage on
public repositories, and (3) self-hosted-runner usage — and the summary
API gives no way to tell which reason(s) applied to a given `usageItems[]`
entry (no repository or visibility field). Concretely: if a Free-plan
account ran a large number of public-repo CI jobs on a standard runner,
100% of that would appear as `discountQuantity`, indistinguishable in the
API response from actual consumption of the 2,000-minute private-repo
allowance. Summing `discountQuantity` as "quota consumed" would therefore
over-report usage for any account that uses public repositories or
self-hosted runners at all, potentially showing an account as
near-exhausted or over its limit when it has not consumed any of its
private-repo allowance.

No other currently-documented GitHub REST API was found that exposes
"exact minutes consumed from the plan's included allowance" directly. As a
result:

- `included_minutes` is still reported with full confidence — it is a hard
  fact derived from the plan name alone (Free = 2,000, Pro = 3,000), not
  from the billing usage data.
- `used_included_minutes`, `remaining_minutes`, and `usage_percentage` are
  **always `None`** (kept as named fields, for API stability, in case a
  future documented endpoint closes this gap).
- `discounted_standard_minutes` (sum of `discountQuantity` for standard
  -runner SKUs) and `billable_standard_minutes` (sum of `netQuantity` for
  the same SKUs) are reported as informational, safely-named values —
  never described as "included quota used" or "overage".
- Report `status` is `"usage_breakdown_inconclusive"` whenever the plan is
  known (this is expected to be the normal, ongoing state given the current
  API), or `"plan_unknown"` when it is not.

If GitHub later documents a way to isolate private-repo-allowance
consumption specifically (e.g. a `repository`/visibility field on the
summary endpoint, or a dedicated "included usage remaining" field), this
module and this document should be revisited together — see
`docs/github-actions-billing-monitor.md`'s own history for precedent (this
section replaces an earlier, incorrect interpretation for exactly that
reason).

### Larger runners never draw from included minutes

Per the official docs quoted above, larger runner SKUs (the full list is in
`LARGER_RUNNER_SKUS`) are always billed via `netQuantity` and are expected
to always have `discountQuantity == 0` — this claim, unlike the
included-minutes split above, *is* solidly documented ("always charged
for... even when you have quota available from your plan"), so
`paid_non_included_minutes` is reported with confidence and is never added
to any of the standard-runner totals.

### Unknown SKUs are never guessed into a bucket

A `sku` value in neither `STANDARD_RUNNER_SKUS` nor `LARGER_RUNNER_SKUS`
nor `STORAGE_SKUS` (e.g. a future SKU GitHub adds after this module was
written) is recorded in `skipped_unknown_skus` and contributes to no total
at all — not included quota, not overage, not "other paid minutes". This
can only ever under-report usage, never mis-attribute it.

### Plan name mapping is a closed allow-list

`PLAN_INCLUDED_MINUTES` only maps `"free"` and `"pro"` (case-insensitive).
Any other value — `None`, `"team"`, `"enterprise"`, or any plan name this
module has not been taught yet — produces `status: "plan_unknown"` with
`included_minutes: null` rather than a guessed number. GitHub's own docs do
not enumerate every possible `plan.name` value, so this allow-list is
deliberately conservative.

### `product` / `unitType` matching is case-insensitive

An observed real API response example used `"product": "Actions"`
(capitalized), while the SKU reference page's product identifier is
`actions` (lowercase, used for the `?product=actions` query parameter).
This module matches both the request-query value and the response's
`product` field case-insensitively against `"actions"`, and `unitType`
case-insensitively against `"minutes"`, so a future casing change on
either side does not silently break aggregation.

## Human Gate: current credential permission status

This app's existing `gh` CLI OAuth token (already used by
`app/github_rate_limit_cli.py`) has scopes `gist, read:org, repo, workflow`
— it does **not** have the classic `user` scope. Read-only, no-credential
-value-shown checks performed during implementation:

- `gh api user` → succeeds, but `plan` is `null` (plan visibility requires
  the `user` scope).
- `gh api "/users/<login>/settings/billing/usage/summary?..."` → HTTP 404,
  with `gh`'s own diagnostic: `This API operation needs the "user" scope.
  To request it, run: gh auth refresh -h github.com -s user`.

Per this session's explicit instructions, **no scope/permission change was
made** (`gh auth refresh` was not run). This is why `_looks_like_missing_scope_or_permission`
in `app/github_actions_billing_cli.py` treats this specific 404 shape as
`permission_required` rather than `api_unavailable` — it is a scope
problem, not a "this account can't use this API at all" problem. Until a
credential with the `user` scope (or a fine-grained PAT with "Plan: read")
is available, live validation of the actual billing payload shape against
real data is not possible; this feature runs in `permission_required` state
with this credential, which is exercised by
`tests/test_github_actions_billing_cli.py` and
`tests/test_github_actions_billing_api.py`. Note that even with such a
credential, live validation could confirm the *shape* of a real response
(field names, presence/absence of a repository field, etc.) but — per
"Why exact remaining minutes cannot currently be computed" above — could
not by itself prove an exact included-minutes-consumed number, since the
account's own mix of public/private/self-hosted usage would still be
unknown from this API alone.

## Current support level summary

| Capability | Level |
|---|---|
| Free/Pro included-minutes allowance (`included_minutes`) | **Supported** — hard fact from `plan.name` |
| Larger-runner-usage separation (`paid_non_included_minutes`) | **Supported** — solidly documented, always billed in full |
| Unknown-SKU safety (`skipped_unknown_skus`) | **Supported** — never guessed into a total |
| Standard-runner discount/billed totals (`discounted_standard_minutes` / `billable_standard_minutes`) | **Partial** — safely summed from documented fields, but not attributable to a single cause |
| Exact minutes consumed from the included allowance (`used_included_minutes`) | **Inconclusive** — no currently-documented API isolates this from public-repo/self-hosted discount |
| Exact remaining minutes (`remaining_minutes`) / usage percentage (`usage_percentage`) | **Inconclusive** — depends on the above |
| Live validation against real account data | **Not performed** — current `gh` credential lacks the `user` scope / "Plan: read" permission (Human Gate, unchanged by this session) |
