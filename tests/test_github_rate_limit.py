import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.github_rate_limit import (
    build_resource_rate_limit,
    determine_overall,
    parse_github_rate_limit,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github_rate_limit"


def load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def resource(limit=5000, used=0, remaining=5000, reset=2_000_000_000, **extra) -> dict:
    return {"limit": limit, "used": used, "remaining": remaining, "reset": reset, **extra}


NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())


# 1. core/graphql正常
def test_core_and_graphql_normal():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 3600),
            "graphql": resource(limit=5000, used=200, remaining=4800, reset=NOW_EPOCH + 3600),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Normal"
    assert report.resources["graphql"].status == "Normal"
    assert report.overall.status == "Normal"


# 2. GraphQL枯渇（提示済みfixture）
def test_graphql_exhausted_from_fixture():
    payload = load_fixture("core_normal_graphql_exhausted.json")
    # fixtureのresetより前の時刻を基準にすることで、提示された期待どおり
    # graphqlが「Exhausted」（reset到来前の枯渇）になることを検証する。
    fixture_now = datetime.fromtimestamp(1784949500, tz=timezone.utc)
    report = parse_github_rate_limit(payload, now=fixture_now)

    core = report.resources["core"]
    graphql = report.resources["graphql"]

    assert core.status == "Normal"
    assert graphql.status == "Exhausted"
    assert graphql.usage_percent == pytest.approx(200.66, rel=1e-3)
    assert report.overall.status == "Limited"
    assert report.overall.reason == "GraphQL API exhausted"


# 3. core枯渇
def test_core_exhausted():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=5000, remaining=0, reset=NOW_EPOCH + 60),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Exhausted"
    assert report.overall.status == "Limited"
    assert report.overall.reason == "REST API core exhausted"


# 4. 両方枯渇（同点はcoreを主原因として報告）
def test_both_exhausted_ties_on_core():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=5000, remaining=0, reset=NOW_EPOCH + 60),
            "graphql": resource(limit=5000, used=5000, remaining=0, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Exhausted"
    assert report.resources["graphql"].status == "Exhausted"
    assert report.overall.status == "Limited"
    assert report.overall.reason == "REST API core exhausted"


# 5. Warning境界ちょうど20%
def test_warning_at_exactly_twenty_percent():
    r = build_resource_rate_limit("core", resource(limit=1000, used=800, remaining=200), now=NOW)
    assert r.status == "Warning"


# 6. Normal境界20%超
def test_normal_just_above_twenty_percent():
    r = build_resource_rate_limit("core", resource(limit=1000, used=799, remaining=201), now=NOW)
    assert r.status == "Normal"


# 7. reset時刻前のExhausted
def test_exhausted_before_reset_time():
    r = build_resource_rate_limit(
        "core", resource(remaining=0, reset=NOW_EPOCH + 1), now=NOW
    )
    assert r.status == "Exhausted"


# 8. reset時刻ちょうどのExhausted
def test_exhausted_at_exact_reset_time():
    r = build_resource_rate_limit(
        "core", resource(remaining=0, reset=NOW_EPOCH), now=NOW
    )
    assert r.status == "Exhausted"


# 9. reset超過のReset overdue
def test_reset_overdue_after_reset_time():
    r = build_resource_rate_limit(
        "core", resource(remaining=0, reset=NOW_EPOCH - 1), now=NOW
    )
    assert r.status == "Reset overdue"


# 10. used > limitを許容
def test_used_greater_than_limit_is_allowed():
    r = build_resource_rate_limit(
        "core", resource(limit=5000, used=6000, remaining=0, reset=NOW_EPOCH + 60), now=NOW
    )
    assert r.status == "Exhausted"
    assert r.usage_percent == pytest.approx(120.0)


# 11. remaining > limitを拒否
def test_remaining_greater_than_limit_is_error():
    r = build_resource_rate_limit(
        "core", resource(limit=5000, used=0, remaining=6000), now=NOW
    )
    assert r.status == "Error"
    assert "remaining" in r.error_message


# 12. limit == 0
def test_limit_zero_is_error():
    r = build_resource_rate_limit(
        "core", resource(limit=0, used=0, remaining=0), now=NOW
    )
    assert r.status == "Error"
    assert "limit" in r.error_message


# 13. 負数入力
@pytest.mark.parametrize("field", ["limit", "used", "remaining"])
def test_negative_values_are_error(field):
    raw = resource(limit=5000, used=100, remaining=4900)
    raw[field] = -1
    r = build_resource_rate_limit("core", raw, now=NOW)
    assert r.status == "Error"
    assert field in r.error_message


# 14. core欠落
def test_missing_core_is_error_and_drives_overall():
    payload = {"resources": {"graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60)}}
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Error"
    assert report.resources["core"].error_message == "GitHub rate limit data unavailable"
    assert report.overall.status == "Error"


# 15. graphql欠落
def test_missing_graphql_is_error_and_drives_overall():
    payload = {"resources": {"core": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60)}}
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["graphql"].status == "Error"
    assert report.overall.status == "Error"


# 16. search欠落時の扱い
def test_missing_search_is_error_but_present_in_report():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["search"].status == "Error"
    assert report.overall.status == "Normal"


# 17. 不正JSON構造
@pytest.mark.parametrize(
    "payload",
    [
        "not a dict",
        [],
        {},
        {"resources": "not a dict either"},
        {"resources": ["core", "graphql"]},
    ],
)
def test_malformed_payload_raises(payload):
    with pytest.raises(ValueError):
        parse_github_rate_limit(payload, now=NOW)


# 18. UTC変換
def test_reset_at_utc_conversion():
    reset_epoch = 1784949522
    r = build_resource_rate_limit(
        "core", resource(remaining=100, reset=reset_epoch), now=NOW
    )
    assert r.reset_at_utc == datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
    assert r.reset_at_utc.tzinfo is not None


# 19. 指定timezoneへのlocal変換
def test_reset_at_local_conversion_uses_given_timezone():
    reset_epoch = 1784949522
    tokyo = ZoneInfo("Asia/Tokyo")
    r = build_resource_rate_limit(
        "core", resource(remaining=100, reset=reset_epoch), now=NOW, tz=tokyo
    )
    expected = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).astimezone(tokyo)
    assert r.reset_at_local == expected
    assert r.reset_at_local.utcoffset() == expected.utcoffset()


# 20. seconds_until_reset
def test_seconds_until_reset_can_be_negative_or_positive():
    future = build_resource_rate_limit(
        "core", resource(remaining=100, reset=NOW_EPOCH + 90), now=NOW
    )
    past = build_resource_rate_limit(
        "core", resource(remaining=0, reset=NOW_EPOCH - 90), now=NOW
    )
    assert future.seconds_until_reset == 90
    assert past.seconds_until_reset == -90


# 21. Overall reason
@pytest.mark.parametrize(
    "core_raw,graphql_raw,expected_status,expected_reason",
    [
        (
            resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "Normal",
            "core and graphql are within normal limits",
        ),
        (
            resource(limit=1000, used=850, remaining=150, reset=NOW_EPOCH + 60),
            resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "Warning",
            "REST API core approaching limit",
        ),
        (
            resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            resource(limit=5000, used=5000, remaining=0, reset=NOW_EPOCH - 60),
            "Limited",
            "GraphQL API reset overdue",
        ),
    ],
)
def test_overall_reason_text(core_raw, graphql_raw, expected_status, expected_reason):
    core = build_resource_rate_limit("core", core_raw, now=NOW)
    graphql = build_resource_rate_limit("graphql", graphql_raw, now=NOW)
    overall = determine_overall(core, graphql)
    assert overall.status == expected_status
    assert overall.reason == expected_reason


# 22. Error優先順位
def test_error_outranks_exhausted_and_warning():
    exhausted_core = build_resource_rate_limit(
        "core", resource(limit=5000, used=5000, remaining=0, reset=NOW_EPOCH + 60), now=NOW
    )
    missing_graphql = build_resource_rate_limit("graphql", None, now=NOW)

    overall = determine_overall(exhausted_core, missing_graphql)
    assert overall.status == "Error"
    assert overall.reason == "GitHub rate limit data unavailable"

    warning_core = build_resource_rate_limit(
        "core", resource(limit=1000, used=850, remaining=150, reset=NOW_EPOCH + 60), now=NOW
    )
    overall2 = determine_overall(warning_core, missing_graphql)
    assert overall2.status == "Error"


# 23. searchの状態がOverallへ影響しない
def test_search_status_does_not_affect_overall():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "search": resource(limit=30, used=30, remaining=0, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["search"].status == "Exhausted"
    assert report.overall.status == "Normal"


# 24. 入力fixtureを変更しない純粋性
def test_input_payload_is_not_mutated():
    payload = load_fixture("core_normal_graphql_exhausted.json")
    before = copy.deepcopy(payload)

    parse_github_rate_limit(payload, now=NOW)

    assert payload == before


HUGE_RESET = 999_999_999_999_999


# collected_atはaware nowをUTCへ正規化する
def test_collected_at_is_normalized_to_utc_from_jst_now():
    jst_now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
    r = build_resource_rate_limit(
        "core", resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60), now=jst_now
    )
    assert r.collected_at == NOW
    assert r.collected_at.tzinfo is timezone.utc


def test_resource_collected_at_is_timezone_aware():
    r = build_resource_rate_limit(
        "core", resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60), now=NOW
    )
    assert r.collected_at.tzinfo is not None


def test_report_collected_at_is_timezone_aware_utc():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
        }
    }
    jst_now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
    report = parse_github_rate_limit(payload, now=jst_now)
    assert report.collected_at == NOW
    assert report.collected_at.tzinfo is timezone.utc


def test_naive_now_is_still_rejected():
    naive_now = datetime(2026, 7, 26, 12, 0, 0)
    with pytest.raises(ValueError):
        build_resource_rate_limit(
            "core", resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60), now=naive_now
        )
    with pytest.raises(ValueError):
        parse_github_rate_limit(
            {"resources": {"core": resource(), "graphql": resource()}}, now=naive_now
        )


def test_huge_reset_value_makes_resource_error():
    r = build_resource_rate_limit(
        "core", resource(limit=5000, used=100, remaining=4900, reset=HUGE_RESET), now=NOW
    )
    assert r.status == "Error"
    assert "token" not in r.error_message.lower()
    assert "reset timestamp is out of range" == r.error_message


def test_huge_reset_on_one_resource_does_not_affect_the_other():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=HUGE_RESET),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Error"
    assert report.resources["graphql"].status == "Normal"


def test_huge_reset_on_primary_resource_makes_overall_error():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=HUGE_RESET),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=NOW_EPOCH + 60),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.overall.status == "Error"


def test_reset_datetime_conversion_errors_never_escape_parse():
    payload = {
        "resources": {
            "core": resource(limit=5000, used=100, remaining=4900, reset=HUGE_RESET),
            "graphql": resource(limit=5000, used=100, remaining=4900, reset=-HUGE_RESET),
            "search": resource(limit=30, used=1, remaining=29, reset=HUGE_RESET),
        }
    }
    report = parse_github_rate_limit(payload, now=NOW)
    assert report.resources["core"].status == "Error"
    assert report.resources["graphql"].status == "Error"
    assert report.resources["search"].status == "Error"
