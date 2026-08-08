import math
from datetime import datetime, timezone

import pytest

from app.github_actions_billing import (
    LARGER_RUNNER_SKUS,
    PLAN_INCLUDED_MINUTES,
    STANDARD_RUNNER_SKUS,
    STORAGE_SKUS,
    aggregate_actions_minutes,
    build_billing_report,
    classify_sku,
    resolve_included_minutes,
)

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


def _item(sku: str, *, product="actions", unit_type="minutes", gross=0, discount=0, net=0) -> dict:
    return {
        "product": product,
        "sku": sku,
        "unitType": unit_type,
        "pricePerUnit": 0.008,
        "grossQuantity": gross,
        "grossAmount": 0,
        "discountQuantity": discount,
        "discountAmount": 0,
        "netQuantity": net,
        "netAmount": 0,
    }


# ---------------------------------------------------------------------------
# plan mapping
# ---------------------------------------------------------------------------


def test_plan_free_maps_to_2000():
    assert resolve_included_minutes("free") == 2000
    assert PLAN_INCLUDED_MINUTES["free"] == 2000


def test_plan_pro_maps_to_3000():
    assert resolve_included_minutes("pro") == 3000
    assert PLAN_INCLUDED_MINUTES["pro"] == 3000


def test_plan_case_insensitive():
    assert resolve_included_minutes("Free") == 2000
    assert resolve_included_minutes("PRO") == 3000


def test_unknown_plan_is_unmapped():
    assert resolve_included_minutes("team") is None
    assert resolve_included_minutes("enterprise") is None
    assert resolve_included_minutes("something_new") is None


def test_missing_plan_is_unmapped():
    assert resolve_included_minutes(None) is None


# ---------------------------------------------------------------------------
# SKU classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sku", sorted(STANDARD_RUNNER_SKUS))
def test_standard_skus_classified_standard(sku):
    assert classify_sku(sku) == "standard"


@pytest.mark.parametrize("sku", sorted(LARGER_RUNNER_SKUS))
def test_larger_runner_skus_classified_larger_runner(sku):
    assert classify_sku(sku) == "larger_runner"


@pytest.mark.parametrize("sku", sorted(STORAGE_SKUS))
def test_storage_skus_classified_storage(sku):
    assert classify_sku(sku) == "storage"


def test_unrecognized_sku_classified_unknown():
    assert classify_sku("actions_some_future_sku") == "unknown"


# ---------------------------------------------------------------------------
# aggregate_actions_minutes: standard SKUs -> discounted_standard_minutes /
# billable_standard_minutes only (never an "included quota used" claim)
# ---------------------------------------------------------------------------


def test_standard_linux_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=100, net=0)])
    assert agg.discounted_standard_minutes == 100
    assert agg.billable_standard_minutes == 0


def test_standard_windows_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_windows", gross=50, discount=50, net=0)])
    assert agg.discounted_standard_minutes == 50


def test_standard_macos_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_macos", gross=20, discount=20, net=0)])
    assert agg.discounted_standard_minutes == 20


def test_standard_linux_arm_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_linux_arm", gross=10, discount=10, net=0)])
    assert agg.discounted_standard_minutes == 10


def test_standard_windows_arm_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_windows_arm", gross=10, discount=10, net=0)])
    assert agg.discounted_standard_minutes == 10


def test_standard_linux_slim_discount_counted():
    agg = aggregate_actions_minutes([_item("actions_linux_slim", gross=5, discount=5, net=0)])
    assert agg.discounted_standard_minutes == 5


def test_mixed_standard_skus_summed():
    agg = aggregate_actions_minutes(
        [
            _item("actions_linux", gross=100, discount=100, net=0),
            _item("actions_windows", gross=50, discount=50, net=0),
            _item("actions_macos", gross=20, discount=20, net=0),
        ]
    )
    assert agg.discounted_standard_minutes == 170


# ---------------------------------------------------------------------------
# storage / cache ignored
# ---------------------------------------------------------------------------


def test_storage_item_ignored_via_unit_type():
    agg = aggregate_actions_minutes(
        [_item("actions_storage", unit_type="GigabyteHours", gross=5, discount=0, net=5)]
    )
    assert agg.discounted_standard_minutes == 0
    assert agg.billable_standard_minutes == 0
    assert agg.paid_non_included_minutes == 0
    assert agg.skipped_unknown_skus == ()


def test_cache_storage_item_ignored_via_unit_type():
    agg = aggregate_actions_minutes(
        [_item("actions_cache_storage", unit_type="GigabyteHours", gross=1, discount=0, net=1)]
    )
    assert agg.discounted_standard_minutes == 0
    assert agg.paid_non_included_minutes == 0


# ---------------------------------------------------------------------------
# larger runner separated
# ---------------------------------------------------------------------------


def test_larger_runner_separated_from_standard_totals():
    agg = aggregate_actions_minutes([_item("actions_linux_4_core", gross=60, discount=0, net=60)])
    assert agg.discounted_standard_minutes == 0
    assert agg.billable_standard_minutes == 0
    assert agg.paid_non_included_minutes == 60


def test_larger_runner_macos_separated():
    agg = aggregate_actions_minutes([_item("actions_macos_xl", gross=10, discount=0, net=10)])
    assert agg.paid_non_included_minutes == 10
    assert agg.discounted_standard_minutes == 0


# ---------------------------------------------------------------------------
# unknown SKU not included
# ---------------------------------------------------------------------------


def test_unknown_sku_not_added_to_any_total():
    agg = aggregate_actions_minutes([_item("actions_future_runner_type", gross=42, discount=10, net=32)])
    assert agg.discounted_standard_minutes == 0
    assert agg.billable_standard_minutes == 0
    assert agg.paid_non_included_minutes == 0
    assert agg.skipped_unknown_skus == ("actions_future_runner_type",)


# ---------------------------------------------------------------------------
# zero usage
# ---------------------------------------------------------------------------


def test_zero_usage():
    agg = aggregate_actions_minutes([])
    assert agg.discounted_standard_minutes == 0
    assert agg.billable_standard_minutes == 0
    assert agg.paid_non_included_minutes == 0


# ---------------------------------------------------------------------------
# malformed quantity / NaN / Infinity
# ---------------------------------------------------------------------------


def test_malformed_quantity_string_skipped():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount="oops", net=0)])
    assert agg.discounted_standard_minutes == 0


def test_negative_quantity_skipped():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=-5, net=0)])
    assert agg.discounted_standard_minutes == 0


def test_nan_quantity_rejected():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=math.nan, net=0)])
    assert agg.discounted_standard_minutes == 0


def test_infinity_quantity_rejected():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=math.inf, net=0)])
    assert agg.discounted_standard_minutes == 0


def test_missing_quantity_field_treated_as_zero_contribution():
    item = _item("actions_linux", gross=100, discount=100, net=0)
    del item["discountQuantity"]
    agg = aggregate_actions_minutes([item])
    assert agg.discounted_standard_minutes == 0


# ---------------------------------------------------------------------------
# missing usageItems / structural errors
# ---------------------------------------------------------------------------


def test_missing_usage_items_raises_value_error():
    with pytest.raises(ValueError):
        aggregate_actions_minutes(None)  # type: ignore[arg-type]


def test_usage_items_not_a_list_raises_value_error():
    with pytest.raises(ValueError):
        aggregate_actions_minutes({"not": "a list"})  # type: ignore[arg-type]


def test_non_dict_item_in_list_skipped_not_raised():
    agg = aggregate_actions_minutes(["not-a-dict", _item("actions_linux", gross=10, discount=10, net=0)])
    assert agg.discounted_standard_minutes == 10


# ---------------------------------------------------------------------------
# unexpected product / unitType
# ---------------------------------------------------------------------------


def test_unexpected_product_ignored():
    agg = aggregate_actions_minutes([_item("actions_linux", product="copilot", gross=100, discount=100, net=0)])
    assert agg.discounted_standard_minutes == 0


def test_product_match_is_case_insensitive():
    agg = aggregate_actions_minutes([_item("actions_linux", product="Actions", gross=100, discount=100, net=0)])
    assert agg.discounted_standard_minutes == 100


def test_unexpected_unit_type_ignored():
    agg = aggregate_actions_minutes([_item("actions_linux", unit_type="requests", gross=100, discount=100, net=0)])
    assert agg.discounted_standard_minutes == 0


def test_unit_type_match_is_case_insensitive():
    agg = aggregate_actions_minutes([_item("actions_linux", unit_type="Minutes", gross=100, discount=100, net=0)])
    assert agg.discounted_standard_minutes == 100


# ---------------------------------------------------------------------------
# build_billing_report: exact used/remaining/percentage must never be
# fabricated -- always None regardless of input, per the official evidence
# that discountQuantity mixes included-allowance / public-repo / self-hosted
# discount and cannot be safely split apart from this API alone.
# ---------------------------------------------------------------------------


def test_build_billing_report_known_plan_is_inconclusive_not_normal():
    report = build_billing_report(
        plan_name="pro",
        usage_items=[_item("actions_linux", gross=125, discount=125, net=0)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.status == "usage_breakdown_inconclusive"
    assert report.plan_name == "pro"
    assert report.included_minutes == 3000
    # the one hard fact (the plan's allowance) is populated...
    assert report.included_minutes == 3000
    # ...but exact consumption/remaining is never fabricated from discountQuantity
    assert report.used_included_minutes is None
    assert report.remaining_minutes is None
    assert report.usage_percentage is None
    assert report.discounted_standard_minutes == 125
    assert report.billing_year == 2026
    assert report.billing_month == 8
    assert report.source == "github_billing_api"
    assert report.collected_at.tzinfo is not None


def test_build_billing_report_plan_unknown():
    report = build_billing_report(
        plan_name="team",
        usage_items=[],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.status == "plan_unknown"
    assert report.included_minutes is None
    assert report.remaining_minutes is None
    assert report.used_included_minutes is None
    assert report.usage_percentage is None


def test_build_billing_report_missing_plan_is_plan_unknown():
    report = build_billing_report(
        plan_name=None,
        usage_items=[],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.status == "plan_unknown"


def test_build_billing_report_larger_runner_reported_separately():
    report = build_billing_report(
        plan_name="pro",
        usage_items=[
            _item("actions_linux", gross=100, discount=100, net=0),
            _item("actions_linux_4_core", gross=60, discount=0, net=60),
        ],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.discounted_standard_minutes == 100
    assert report.paid_non_included_minutes == 60
    # still never an exact "remaining" number
    assert report.remaining_minutes is None
    assert report.used_included_minutes is None


def test_build_billing_report_naive_now_rejected():
    with pytest.raises(ValueError):
        build_billing_report(
            plan_name="free",
            usage_items=[],
            billing_year=2026,
            billing_month=8,
            now=datetime(2026, 8, 8, 12, 0, 0),
            source="github_billing_api",
        )


# ---------------------------------------------------------------------------
# Required fixtures from the follow-up review: discountQuantity must never
# be treated as proof of included-quota consumption, regardless of its
# underlying cause (public-repo discount, self-hosted discount, or actual
# included-allowance discount -- this API cannot distinguish them).
# ---------------------------------------------------------------------------


def test_public_repo_style_discount_not_treated_as_quota_consumption():
    # A public repository's standard-runner usage is always fully
    # discounted (grossQuantity == discountQuantity, netQuantity == 0) --
    # exactly like this fixture -- regardless of how much of the plan's
    # monthly included allowance remains. This must not be reported as
    # "quota consumed".
    report = build_billing_report(
        plan_name="free",
        usage_items=[_item("actions_linux", gross=5000, discount=5000, net=0)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.used_included_minutes is None
    assert report.remaining_minutes is None
    assert report.usage_percentage is None
    # 5000 > the Free plan's 2000-minute allowance -- if this were (wrongly)
    # treated as quota consumption, remaining would go negative/zero. It
    # must instead simply not exist as a number at all.
    assert report.discounted_standard_minutes == 5000


def test_self_hosted_style_discount_not_treated_as_quota_consumption():
    # Self-hosted runner usage does not appear under the standard-runner
    # SKUs at all (GitHub does not provide the compute), but if a discount
    # for it ever appeared under a standard SKU in a future API revision,
    # this module still must not attribute it to the plan's allowance.
    report = build_billing_report(
        plan_name="pro",
        usage_items=[_item("actions_windows", gross=10000, discount=10000, net=0)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.used_included_minutes is None
    assert report.remaining_minutes is None


@pytest.mark.parametrize("plan_name", ["free", "pro"])
def test_remaining_is_always_none_even_with_known_plan(plan_name):
    report = build_billing_report(
        plan_name=plan_name,
        usage_items=[_item("actions_linux", gross=1, discount=1, net=0)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.remaining_minutes is None
    assert report.used_included_minutes is None
    assert report.usage_percentage is None
    assert report.included_minutes == PLAN_INCLUDED_MINUTES[plan_name]


def test_net_quantity_never_added_to_included_quota_consumption():
    # netQuantity (billable_standard_minutes) must never be summed into any
    # "included quota consumed" total -- it is a separate, safely-named
    # field, not a component of used_included_minutes (which is always None).
    report = build_billing_report(
        plan_name="free",
        usage_items=[_item("actions_linux", gross=2500, discount=2000, net=500)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.used_included_minutes is None
    assert report.billable_standard_minutes == 500
    assert report.discounted_standard_minutes == 2000
