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
  have quota available from your plan."
- `docs.github.com/en/billing/reference/actions-runner-pricing` — per-minute
  rates for standard runners (informational; not used for quota math).
- `docs.github.com/en/billing/reference/product-and-sku-names` — the
  authoritative SKU list this module's `STANDARD_RUNNER_SKUS`,
  `LARGER_RUNNER_SKUS`, and `STORAGE_SKUS` are transcribed from.
- `docs.github.com/en/rest/billing/usage` — the `GET
  /users/{username}/settings/billing/usage/summary` endpoint: **Public
  Preview**, "subject to change." Query params: `year`, `month`, `day`,
  `repository`, `product`, `sku`. Response `usageItems[]` fields used here:
  `product`, `sku`, `unitType`, `discountQuantity`, `netQuantity` (also
  present but unused: `grossQuantity`, `pricePerUnit`, `grossAmount`,
  `discountAmount`, `netAmount`).
  - The sibling detailed endpoint `GET /users/{username}/settings/billing/usage`
    (GA, per-day granular items) is *not* used here — its `usageItems[]`
    shape is `date`/`product`/`sku`/`quantity`/`unitType`/`pricePerUnit`/
    `grossAmount`/`discountAmount`/`netAmount`/`repositoryName` (dollar
    amounts, not a gross/discount/net *quantity* split), and per its own
    docs "is only available to users with access to the enhanced billing
    platform" — narrower account availability than the summary endpoint.
- Fine-grained PAT / GitHub App user access token permission: personal
  billing usage endpoints require the **"Plan" permission (read)**.
- `GET /user` — `plan.name` (string), `plan.space`, `plan.collaborators`,
  `plan.private_repos`. The docs do not enumerate every possible
  `plan.name` value, which is exactly why this module never guesses beyond
  the two names it has confirmed billing math for (see below).

## Interpretation decisions

### `discountQuantity` / `netQuantity`, not `grossQuantity`

`grossQuantity` is the *total* consumed for a SKU/period — it is **not**
"minutes consumed from the free quota." `discountQuantity` is the portion
GitHub actually discounted (covered by the plan's included allowance);
`netQuantity` is the billable remainder (`grossQuantity - discountQuantity`).
This module sums `discountQuantity` across standard-runner-SKU, `minutes`
-unit-type items for `used_included_minutes`, and `netQuantity` across the
same items for `overage_minutes`. Treating `grossQuantity` alone as "free
quota used" would double-count minutes GitHub has already billed as
overage. This interpretation is a defensible, closely-reasoned reading of
the documented field names, but has not been validated against a real
personal account's live billing data (this app's own current GitHub CLI
credential lacks the required permission — see "Human Gate" below). If a
future live validation session finds different behavior, this module and
this document should be updated together.

### Larger runners never draw from included minutes

Per the official docs quoted above, larger runner SKUs (the full list is in
`LARGER_RUNNER_SKUS`) are always billed via `netQuantity` and are expected
to always have `discountQuantity == 0`. Their `netQuantity` is reported
separately as `paid_non_included_minutes` and is never added to
`used_included_minutes` or `remaining_minutes`.

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
is available, live end-to-end validation of the actual billing math against
real data is not possible; this feature runs in `permission_required` state
with this credential, which is exercised by
`tests/test_github_actions_billing_cli.py` and
`tests/test_github_actions_billing_api.py`.
