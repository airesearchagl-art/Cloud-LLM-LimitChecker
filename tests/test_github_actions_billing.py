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
    determine_status,
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
# aggregate_actions_minutes: standard SKUs
# ---------------------------------------------------------------------------


def test_standard_linux_counted():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=100, net=0)])
    assert agg.eligible_used_minutes == 100
    assert agg.eligible_overage_minutes == 0


def test_standard_windows_counted():
    agg = aggregate_actions_minutes([_item("actions_windows", gross=50, discount=50, net=0)])
    assert agg.eligible_used_minutes == 50


def test_standard_macos_counted():
    agg = aggregate_actions_minutes([_item("actions_macos", gross=20, discount=20, net=0)])
    assert agg.eligible_used_minutes == 20


def test_standard_linux_arm_counted():
    agg = aggregate_actions_minutes([_item("actions_linux_arm", gross=10, discount=10, net=0)])
    assert agg.eligible_used_minutes == 10


def test_standard_windows_arm_counted():
    agg = aggregate_actions_minutes([_item("actions_windows_arm", gross=10, discount=10, net=0)])
    assert agg.eligible_used_minutes == 10


def test_standard_linux_slim_counted():
    agg = aggregate_actions_minutes([_item("actions_linux_slim", gross=5, discount=5, net=0)])
    assert agg.eligible_used_minutes == 5


def test_mixed_standard_skus_summed():
    agg = aggregate_actions_minutes(
        [
            _item("actions_linux", gross=100, discount=100, net=0),
            _item("actions_windows", gross=50, discount=50, net=0),
            _item("actions_macos", gross=20, discount=20, net=0),
        ]
    )
    assert agg.eligible_used_minutes == 170


# ---------------------------------------------------------------------------
# storage / cache ignored
# ---------------------------------------------------------------------------


def test_storage_item_ignored_via_unit_type():
    agg = aggregate_actions_minutes(
        [_item("actions_storage", unit_type="GigabyteHours", gross=5, discount=0, net=5)]
    )
    assert agg.eligible_used_minutes == 0
    assert agg.eligible_overage_minutes == 0
    assert agg.paid_non_included_minutes == 0
    assert agg.skipped_unknown_skus == ()


def test_cache_storage_item_ignored_via_unit_type():
    agg = aggregate_actions_minutes(
        [_item("actions_cache_storage", unit_type="GigabyteHours", gross=1, discount=0, net=1)]
    )
    assert agg.eligible_used_minutes == 0
    assert agg.paid_non_included_minutes == 0


# ---------------------------------------------------------------------------
# larger runner separated
# ---------------------------------------------------------------------------


def test_larger_runner_separated_from_included_quota():
    agg = aggregate_actions_minutes([_item("actions_linux_4_core", gross=60, discount=0, net=60)])
    assert agg.eligible_used_minutes == 0
    assert agg.eligible_overage_minutes == 0
    assert agg.paid_non_included_minutes == 60


def test_larger_runner_macos_separated():
    agg = aggregate_actions_minutes([_item("actions_macos_xl", gross=10, discount=0, net=10)])
    assert agg.paid_non_included_minutes == 10
    assert agg.eligible_used_minutes == 0


# ---------------------------------------------------------------------------
# unknown SKU not included
# ---------------------------------------------------------------------------


def test_unknown_sku_not_added_to_any_total():
    agg = aggregate_actions_minutes(
        [_item("actions_future_runner_type", gross=42, discount=10, net=32)]
    )
    assert agg.eligible_used_minutes == 0
    assert agg.eligible_overage_minutes == 0
    assert agg.paid_non_included_minutes == 0
    assert agg.skipped_unknown_skus == ("actions_future_runner_type",)


# ---------------------------------------------------------------------------
# zero usage / exactly exhausted / overage
# ---------------------------------------------------------------------------


def test_zero_usage():
    agg = aggregate_actions_minutes([])
    assert agg.eligible_used_minutes == 0
    assert agg.eligible_overage_minutes == 0
    assert agg.paid_non_included_minutes == 0


def test_exactly_exhausted_status():
    status = determine_status(included_minutes=2000, eligible_used_minutes=2000, eligible_overage_minutes=0)
    assert status == "exhausted"


def test_overage_status():
    status = determine_status(included_minutes=2000, eligible_used_minutes=2000, eligible_overage_minutes=50)
    assert status == "overage"


def test_warning_boundary_at_20_percent_remaining():
    # remaining exactly 20% of included_minutes -> warning
    status = determine_status(included_minutes=2000, eligible_used_minutes=1600, eligible_overage_minutes=0)
    assert status == "warning"


def test_just_above_warning_boundary_is_normal():
    status = determine_status(included_minutes=2000, eligible_used_minutes=1599, eligible_overage_minutes=0)
    assert status == "normal"


def test_normal_status_low_usage():
    status = determine_status(included_minutes=2000, eligible_used_minutes=10, eligible_overage_minutes=0)
    assert status == "normal"


# ---------------------------------------------------------------------------
# malformed quantity / NaN / Infinity
# ---------------------------------------------------------------------------


def test_malformed_quantity_string_skipped():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount="oops", net=0)])
    assert agg.eligible_used_minutes == 0


def test_negative_quantity_skipped():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=-5, net=0)])
    assert agg.eligible_used_minutes == 0


def test_nan_quantity_rejected():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=math.nan, net=0)])
    assert agg.eligible_used_minutes == 0


def test_infinity_quantity_rejected():
    agg = aggregate_actions_minutes([_item("actions_linux", gross=100, discount=math.inf, net=0)])
    assert agg.eligible_used_minutes == 0


def test_missing_quantity_field_treated_as_zero_contribution():
    item = _item("actions_linux", gross=100, discount=100, net=0)
    del item["discountQuantity"]
    agg = aggregate_actions_minutes([item])
    assert agg.eligible_used_minutes == 0


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
    assert agg.eligible_used_minutes == 10


# ---------------------------------------------------------------------------
# unexpected product / unitType
# ---------------------------------------------------------------------------


def test_unexpected_product_ignored():
    agg = aggregate_actions_minutes([_item("actions_linux", product="copilot", gross=100, discount=100, net=0)])
    assert agg.eligible_used_minutes == 0


def test_product_match_is_case_insensitive():
    agg = aggregate_actions_minutes([_item("actions_linux", product="Actions", gross=100, discount=100, net=0)])
    assert agg.eligible_used_minutes == 100


def test_unexpected_unit_type_ignored():
    agg = aggregate_actions_minutes(
        [_item("actions_linux", unit_type="requests", gross=100, discount=100, net=0)]
    )
    assert agg.eligible_used_minutes == 0


def test_unit_type_match_is_case_insensitive():
    agg = aggregate_actions_minutes(
        [_item("actions_linux", unit_type="Minutes", gross=100, discount=100, net=0)]
    )
    assert agg.eligible_used_minutes == 100


# ---------------------------------------------------------------------------
# build_billing_report
# ---------------------------------------------------------------------------


def test_build_billing_report_normal():
    report = build_billing_report(
        plan_name="pro",
        usage_items=[_item("actions_linux", gross=125, discount=125, net=0)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.status == "normal"
    assert report.plan_name == "pro"
    assert report.included_minutes == 3000
    assert report.used_included_minutes == 125
    assert report.remaining_minutes == 2875
    assert report.usage_percentage == pytest.approx(125 / 3000 * 100)
    assert report.overage_minutes == 0
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


def test_build_billing_report_overage():
    report = build_billing_report(
        plan_name="free",
        usage_items=[_item("actions_linux", gross=2100, discount=2000, net=100)],
        billing_year=2026,
        billing_month=8,
        now=NOW,
        source="github_billing_api",
    )
    assert report.status == "overage"
    assert report.remaining_minutes == 0
    assert report.overage_minutes == 100


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
    assert report.used_included_minutes == 100
    assert report.paid_non_included_minutes == 60


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
