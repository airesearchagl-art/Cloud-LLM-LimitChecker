"""Phase 2: Codex App Server multi-bucket ingestion (adapter -> cache v2 -> projection).

Fixture-only: no real `codex` process is ever started and nothing touches the
network. Every value here is synthetic -- the bucket ids, display names and
plan strings are placeholders, not observed account data, and no model name
is used as an identity.
"""

import ast
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codex_rate_limits_adapter as adapter
from app import codex_rate_limits_cache as cache
from app import usage_allowance
from app.codex_rate_limits_state import CodexRateLimitsController
from app.main import app

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
EPOCH_2026 = 1767225600  # 2026-01-01T00:00:00Z
RESET_SOON = EPOCH_2026 + 15 * 3600  # 2026-01-01T15:00:00Z
RESET_LATER = EPOCH_2026 + 5 * 86400 + 12 * 3600  # 2026-01-06T12:00:00Z
RESET_SOON_ISO = "2026-01-01T15:00:00+00:00"
RESET_LATER_ISO = "2026-01-06T12:00:00+00:00"

INIT_OK = {"id": 1, "result": {"userAgent": "fixture", "codexHome": "fixture"}}

# Synthetic stand-ins for fields that must never be ingested, persisted or
# served. Each carries a unique marker so a leak is detectable as plain text.
ACCOUNT_MARKER = "SYNTHETIC-ACCOUNT-MARKER"
BALANCE_MARKER = "SYNTHETIC-BALANCE-MARKER"
RESET_CREDIT_MARKER = "SYNTHETIC-RESET-CREDIT-MARKER"
UPSELL_MARKER = "SYNTHETIC-UPSELL-MARKER"
SPEND_MARKER = "SYNTHETIC-SPEND-MARKER"
SENSITIVE_MARKERS = (ACCOUNT_MARKER, BALANCE_MARKER, RESET_CREDIT_MARKER, UPSELL_MARKER, SPEND_MARKER)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def window(used=40, duration=300, resets=RESET_SOON) -> dict:
    return {"usedPercent": used, "windowDurationMins": duration, "resetsAt": resets}


def snapshot(*, limit_id=None, limit_name=None, plan_type=None, reached=None, primary=None, secondary=None, **extra) -> dict:
    raw = {
        "limitId": limit_id,
        "limitName": limit_name,
        "planType": plan_type,
        "rateLimitReachedType": reached,
        "primary": primary,
        "secondary": secondary,
    }
    raw.update(extra)
    return raw


def sensitive_snapshot_fields() -> dict:
    return {
        "credits": {"hasCredits": True, "unlimited": False, "balance": BALANCE_MARKER},
        "individualLimit": {"limit": SPEND_MARKER, "used": SPEND_MARKER, "remainingPercent": 50, "resetsAt": RESET_LATER},
        "spendControlReached": True,
    }


def sensitive_top_level_fields() -> dict:
    return {
        "accountId": ACCOUNT_MARKER,
        "rateLimitResetCredits": {
            "availableCount": 1,
            "credits": [{"id": RESET_CREDIT_MARKER, "grantedAt": EPOCH_2026, "resetType": "unknown", "status": "available"}],
        },
        "rateLimitUpsell": {"banner_text": UPSELL_MARKER},
    }


def bucket_a() -> dict:
    """Primary is a WEEKLY window and secondary is null: a slot name implies no duration."""
    return snapshot(
        limit_id="bucket_a",
        plan_type="synthetic_plan",
        primary=window(used=30, duration=10080, resets=RESET_LATER),
        **sensitive_snapshot_fields(),
    )


def bucket_b() -> dict:
    return snapshot(
        limit_id="bucket_b",
        limit_name="Synthetic Bucket B",
        plan_type="synthetic_plan",
        primary=window(used=55, duration=300, resets=RESET_SOON),
        secondary=window(used=20, duration=10080, resets=RESET_LATER),
    )


def multibucket_result() -> dict:
    """`rateLimits` mirrors `bucket_a`; the map lists its keys out of order."""
    return {
        "rateLimits": bucket_a(),
        "rateLimitsByLimitId": {"bucket_b": bucket_b(), "bucket_a": bucket_a()},
        **sensitive_top_level_fields(),
    }


def map_result(entry: dict, *, key: str = "bucket_a") -> dict:
    """A valid legacy view (`rateLimits` == bucket_a) plus a one-entry map under test."""
    return {"rateLimits": bucket_a(), "rateLimitsByLimitId": {key: entry}}


def legacy_result(**rate_limits_overrides) -> dict:
    rate_limits = snapshot(
        primary=window(used=42, duration=300, resets=RESET_SOON),
        secondary=window(used=18, duration=10080, resets=RESET_LATER),
    )
    rate_limits.update(rate_limits_overrides)
    return {"rateLimits": rate_limits}


def make_recv(messages: list[dict | None]):
    queue_ = list(messages)

    def recv(_timeout: float) -> dict | None:
        return queue_.pop(0) if queue_ else None

    return recv


def run(result_payload: dict, *, before_response: tuple[dict, ...] = ()):
    sent: list[dict] = []
    recv = make_recv([INIT_OK, *before_response, {"id": 2, "result": result_payload}])
    result = adapter.run_json_rpc_session(send=sent.append, recv=recv, now=NOW)
    return result, sent


def fetch_for(result_payload: dict):
    """A `fetch(now=...)` that runs the real protocol logic on a scripted transport."""

    def fetch(*, now=None, **kwargs):
        recv = make_recv([INIT_OK, {"id": 2, "result": result_payload}])
        return adapter.run_json_rpc_session(send=lambda obj: None, recv=recv, now=now)

    return fetch


def bucket_by_id(buckets: list[dict], limit_id: str) -> dict:
    matches = [bucket for bucket in buckets if bucket["limit_id"] == limit_id]
    assert len(matches) == 1
    return matches[0]


def assert_invalid(result_payload: dict) -> None:
    result, _ = run(result_payload)
    assert result.success is False
    assert result.error_type == "invalid_response"
    assert result.buckets is None


# ---------------------------------------------------------------------------
# A. Protocol
# ---------------------------------------------------------------------------


def test_sends_initialize_initialized_then_read_without_params_key():
    _, sent = run(multibucket_result())
    assert [message["method"] for message in sent] == ["initialize", "initialized", "account/rateLimits/read"]
    assert "id" not in sent[1]
    assert sent[2] == {"method": "account/rateLimits/read", "id": 2}


def test_notifications_and_stray_ids_are_skipped_not_used_as_snapshot():
    decoy = {"rateLimits": snapshot(limit_id="decoy", primary=window(used=99, duration=300))}
    result, _ = run(
        multibucket_result(),
        before_response=(
            {"method": "account/rateLimits/updated", "params": decoy},
            {"id": 99, "result": decoy},
        ),
    )
    assert result.success is True
    assert [bucket["limit_id"] for bucket in result.buckets] == ["bucket_a", "bucket_b"]


def test_adapter_source_only_ever_sends_the_three_read_path_methods():
    tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
    methods = {
        value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "method" and isinstance(value, ast.Constant)
    }
    assert methods == {"initialize", "initialized", "account/rateLimits/read"}


# ---------------------------------------------------------------------------
# B. Multi-bucket canonical view
# ---------------------------------------------------------------------------


def test_non_empty_map_is_the_only_canonical_view_without_legacy_duplicate():
    result, _ = run(multibucket_result())
    assert result.success is True
    # two map entries -> two buckets; `rateLimits` (== bucket_a) is not added a third time
    assert [bucket["limit_id"] for bucket in result.buckets] == ["bucket_a", "bucket_b"]
    assert all(bucket["limit_id_origin"] == "map_key" for bucket in result.buckets)


def test_legacy_windows_still_come_from_rate_limits_only():
    result, _ = run(multibucket_result())
    # rateLimits == bucket_a: a single weekly window in the primary slot
    assert result.windows["five_hour"] is None
    assert result.windows["weekly"]["window_duration_minutes"] == 10080
    assert result.windows["weekly"]["used_percentage"] == 30.0


def test_actual_primary_secondary_slots_are_preserved():
    result, _ = run(multibucket_result())
    b = bucket_by_id(result.buckets, "bucket_b")
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in b["windows"]] == [
        ("primary", 300),
        ("secondary", 10080),
    ]


def test_primary_weekly_window_is_kept_as_primary():
    result, _ = run(multibucket_result())
    a = bucket_by_id(result.buckets, "bucket_a")
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in a["windows"]] == [("primary", 10080)]


def test_bucket_metadata_is_carried_as_open_strings():
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_b"]["planType"] = "some_future_plan"
    payload["rateLimitsByLimitId"]["bucket_b"]["rateLimitReachedType"] = "some_future_reason"
    result, _ = run(payload)
    b = bucket_by_id(result.buckets, "bucket_b")
    assert b["display_name"] == "Synthetic Bucket B"
    assert b["plan_type"] == "some_future_plan"
    assert b["rate_limit_reached_type"] == "some_future_reason"


# ---------------------------------------------------------------------------
# C. Identity
# ---------------------------------------------------------------------------


def test_map_key_equal_to_limit_id_is_accepted():
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": bucket_a()}})
    assert result.buckets[0]["limit_id"] == "bucket_a"
    assert result.buckets[0]["limit_id_origin"] == "map_key"


def test_null_limit_id_in_map_uses_the_map_key():
    entry = bucket_a()
    entry["limitId"] = None
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})
    assert result.buckets[0]["limit_id"] == "bucket_a"
    assert result.buckets[0]["limit_id_origin"] == "map_key"


def test_limit_id_disagreeing_with_map_key_fails_closed():
    entry = bucket_a()
    entry["limitId"] = "something_else"
    assert_invalid({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})


def test_non_string_limit_id_in_map_fails_closed():
    entry = bucket_a()
    entry["limitId"] = 7
    assert_invalid({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})


def test_empty_map_key_fails_closed():
    entry = bucket_a()
    entry["limitId"] = None
    assert_invalid({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"": entry}})


def test_legacy_fallback_uses_snapshot_limit_id_when_present():
    result, _ = run(legacy_result(limitId="legacy_bucket"))
    assert result.buckets[0]["limit_id"] == "legacy_bucket"
    assert result.buckets[0]["limit_id_origin"] == "snapshot_field"


def test_legacy_fallback_never_synthesizes_a_limit_id():
    result, _ = run(legacy_result())
    assert result.buckets[0]["limit_id"] is None
    assert result.buckets[0]["limit_id_origin"] is None


@pytest.mark.parametrize("limit_id", ["", 7, True, {"id": "x"}])
def test_legacy_only_malformed_limit_id_is_dropped_not_fatal(limit_id):
    # The legacy view never read metadata, so it must not start failing on it.
    result, _ = run(legacy_result(limitId=limit_id))
    assert result.success is True
    assert result.buckets[0]["limit_id"] is None
    assert result.buckets[0]["limit_id_origin"] is None


@pytest.mark.parametrize("field", ["limitName", "planType", "rateLimitReachedType"])
def test_legacy_only_malformed_metadata_is_dropped_not_fatal(field):
    result, _ = run(legacy_result(**{field: 12}))
    assert result.success is True
    key = {"limitName": "display_name", "planType": "plan_type", "rateLimitReachedType": "rate_limit_reached_type"}[field]
    assert result.buckets[0][key] is None


# ---------------------------------------------------------------------------
# D. Nullable windows
# ---------------------------------------------------------------------------


def test_null_primary_keeps_secondary_in_its_own_slot():
    entry = snapshot(limit_id="bucket_a", secondary=window(used=10, duration=10080, resets=RESET_LATER))
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})
    assert [w["source_slot"] for w in result.buckets[0]["windows"]] == ["secondary"]


def test_null_secondary_keeps_primary_only():
    result, _ = run(multibucket_result())
    assert [w["source_slot"] for w in bucket_by_id(result.buckets, "bucket_a")["windows"]] == ["primary"]


def test_both_windows_null_is_a_valid_metadata_only_bucket():
    metadata_only = snapshot(limit_id="bucket_meta", limit_name="Synthetic Metadata Only", plan_type="synthetic_plan")
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_meta"] = metadata_only
    result, _ = run(payload)
    assert result.success is True
    meta = bucket_by_id(result.buckets, "bucket_meta")
    assert meta["windows"] == []
    assert meta["display_name"] == "Synthetic Metadata Only"


def test_null_duration_is_kept_as_null():
    entry = snapshot(limit_id="bucket_a", primary=window(used=10, duration=None, resets=RESET_LATER))
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})
    assert result.buckets[0]["windows"][0]["window_duration_minutes"] is None


def test_null_resets_at_is_kept_as_null():
    entry = snapshot(limit_id="bucket_a", primary=window(used=10, duration=300, resets=None))
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})
    assert result.buckets[0]["windows"][0]["resets_at"] is None


def test_absent_duration_and_reset_keys_are_null():
    entry = snapshot(limit_id="bucket_a", primary={"usedPercent": 10})
    result, _ = run({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": entry}})
    projected = result.buckets[0]["windows"][0]
    assert projected["window_duration_minutes"] is None
    assert projected["resets_at"] is None


# ---------------------------------------------------------------------------
# E. Generic durations
# ---------------------------------------------------------------------------


# The schema sets no minimum for `windowDurationMins`, so 0 and negatives are
# kept verbatim in a multi-bucket entry rather than rejected or dropped.
@pytest.mark.parametrize("duration", [60, 1440, 43200, 0, -5])
def test_non_legacy_durations_are_kept_in_map_buckets(duration):
    entry = snapshot(limit_id="bucket_a", primary=window(used=5, duration=duration, resets=RESET_LATER))
    result, _ = run(map_result(entry))
    assert result.success is True
    assert [w["window_duration_minutes"] for w in result.buckets[0]["windows"]] == [duration]


@pytest.mark.parametrize("duration", [True, "300", 300.5, 300.0, float("nan"), 2**63, -(2**63) - 1, 10**30])
def test_invalid_duration_in_map_fails_closed(duration):
    entry = snapshot(limit_id="bucket_a", primary=window(used=5, duration=duration, resets=RESET_LATER))
    assert_invalid(map_result(entry))


def test_legacy_only_unknown_duration_is_dropped_from_both_views():
    result, _ = run(legacy_result(secondary=window(used=5, duration=60, resets=RESET_LATER)))
    assert result.success is True
    assert result.windows["weekly"] is None
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in result.buckets[0]["windows"]] == [("primary", 300)]


# ---------------------------------------------------------------------------
# F. Timestamp
# ---------------------------------------------------------------------------


def test_unix_seconds_become_aware_utc_iso():
    result, _ = run(multibucket_result())
    b = bucket_by_id(result.buckets, "bucket_b")
    assert [w["resets_at"] for w in b["windows"]] == [RESET_SOON_ISO, RESET_LATER_ISO]


def test_map_integer_reset_is_accepted():
    entry = snapshot(limit_id="bucket_a", primary=window(used=42, duration=300, resets=RESET_SOON))
    result, _ = run(map_result(entry))
    assert result.success is True
    assert result.buckets[0]["windows"][0]["resets_at"] == RESET_SOON_ISO


def test_map_integral_float_reset_is_rejected():
    entry = snapshot(limit_id="bucket_a", primary=window(used=42, duration=300, resets=float(RESET_SOON)))
    assert_invalid(map_result(entry))


def test_legacy_only_keeps_its_float_reset_tolerance():
    result, _ = run(legacy_result(primary=window(used=42, duration=300, resets=float(RESET_SOON))))
    assert result.success is True
    assert result.windows["five_hour"]["resets_at"] == RESET_SOON_ISO
    assert result.buckets[0]["windows"][0]["resets_at"] == RESET_SOON_ISO


@pytest.mark.parametrize(
    "resets",
    [True, "2026-01-01T15:00:00Z", 10**20, -1, 1.5, float(RESET_SOON), float("nan"), float("inf"), float("-inf")],
)
def test_invalid_or_overflowing_reset_fails_closed(resets):
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_b"]["primary"]["resetsAt"] = resets
    assert_invalid(payload)


# ---------------------------------------------------------------------------
# G. Percent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("used", [True, False, "40", 101, -1, 42.5, 42.0, None])
def test_invalid_used_percent_fails_closed_without_dropping_the_window(used):
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_b"]["secondary"]["usedPercent"] = used
    assert_invalid(payload)


def test_remaining_is_a_pure_derivation_from_the_same_window():
    result, _ = run(multibucket_result())
    b = bucket_by_id(result.buckets, "bucket_b")
    assert [(w["used_percentage"], w["remaining_percentage"]) for w in b["windows"]] == [(55.0, 45.0), (20.0, 80.0)]


@pytest.mark.parametrize("used", [0, 42, 100])
def test_map_integer_used_percent_is_accepted_and_stored_as_float(used):
    entry = snapshot(limit_id="bucket_a", primary=window(used=used, duration=300, resets=RESET_SOON))
    result, _ = run(map_result(entry))
    assert result.success is True
    projected = result.buckets[0]["windows"][0]
    assert projected["used_percentage"] == float(used)
    assert isinstance(projected["used_percentage"], float)
    assert projected["remaining_percentage"] == 100.0 - float(used)


@pytest.mark.parametrize("used", [42.0, 42.5])
def test_legacy_only_keeps_its_float_used_percent_tolerance(used):
    result, _ = run(legacy_result(primary=window(used=used, duration=300, resets=RESET_SOON)))
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == used
    assert result.buckets[0]["windows"][0]["used_percentage"] == used


# ---------------------------------------------------------------------------
# H. Canonical selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("by_limit_id", ["absent", None, {}])
def test_null_missing_or_empty_map_uses_rate_limits_as_the_single_bucket(by_limit_id):
    payload = legacy_result(limitId="legacy_bucket")
    if by_limit_id != "absent":
        payload["rateLimitsByLimitId"] = by_limit_id
    result, _ = run(payload)
    assert result.success is True
    assert [bucket["limit_id"] for bucket in result.buckets] == ["legacy_bucket"]
    assert [w["source_slot"] for w in result.buckets[0]["windows"]] == ["primary", "secondary"]


def test_invalid_non_empty_map_never_falls_back_to_rate_limits():
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_b"]["primary"] = "not-a-window"
    # rateLimits itself is perfectly valid, yet the fetch must fail closed
    assert_invalid(payload)


@pytest.mark.parametrize("by_limit_id", [["bucket_a"], "bucket_a", 3, True])
def test_non_object_map_fails_closed(by_limit_id):
    payload = legacy_result()
    payload["rateLimitsByLimitId"] = by_limit_id
    assert_invalid(payload)


def test_non_object_map_entry_fails_closed():
    assert_invalid({"rateLimits": bucket_a(), "rateLimitsByLimitId": {"bucket_a": None}})


@pytest.mark.parametrize("field", ["limitName", "planType", "rateLimitReachedType"])
def test_non_string_metadata_fails_closed(field):
    payload = multibucket_result()
    payload["rateLimitsByLimitId"]["bucket_b"][field] = 12
    assert_invalid(payload)


def test_legacy_duplicate_duration_is_still_ambiguous():
    result, _ = run(legacy_result(secondary=window(used=5, duration=300, resets=RESET_SOON)))
    assert result.success is False
    assert result.error_type == "ambiguous_response"


# ---------------------------------------------------------------------------
# Legacy-only path keeps current-main semantics; compatibility guard
# ---------------------------------------------------------------------------


def test_legacy_only_invalid_primary_is_dropped_and_valid_300_secondary_succeeds():
    payload = legacy_result(
        primary={"usedPercent": "bad", "windowDurationMins": 10080, "resetsAt": RESET_LATER},
        secondary=window(used=42, duration=300, resets=RESET_SOON),
    )
    result, _ = run(payload)
    assert result.success is True
    assert result.windows["five_hour"]["used_percentage"] == 42.0
    assert result.windows["weekly"] is None
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in result.buckets[0]["windows"]] == [
        ("secondary", 300)
    ]


def test_legacy_only_unknown_duration_primary_and_valid_weekly_secondary_succeeds():
    payload = legacy_result(
        primary=window(used=5, duration=60, resets=RESET_SOON),
        secondary=window(used=18, duration=10080, resets=RESET_LATER),
    )
    result, _ = run(payload)
    assert result.success is True
    assert result.windows["five_hour"] is None
    assert result.windows["weekly"]["used_percentage"] == 18.0
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in result.buckets[0]["windows"]] == [
        ("secondary", 10080)
    ]


@pytest.mark.parametrize(
    "primary, secondary",
    [
        ({"usedPercent": True, "windowDurationMins": 300, "resetsAt": RESET_SOON}, window(duration=60)),
        ("garbage", {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": "not-a-timestamp"}),
        (None, None),
    ],
)
def test_legacy_only_without_any_usable_window_fails(primary, secondary):
    assert_invalid(legacy_result(primary=primary, secondary=secondary))


def test_valid_map_with_no_legacy_300_or_10080_window_fails_and_keeps_prior_cache(tmp_path):
    payload = multibucket_result()
    payload["rateLimits"] = snapshot(limit_id="bucket_a", primary=window(used=30, duration=60, resets=RESET_LATER))

    # The guard lives in the adapter: the fetch itself fails, not just the
    # later cache validation.
    assert_invalid(payload)

    path = tmp_path / "c.json"
    controller = CodexRateLimitsController()
    controller.refresh(now=NOW, fetch=fetch_for(multibucket_result()), cache_path=path)
    before = path.read_bytes()
    status = controller.refresh(now=NOW + timedelta(minutes=5), fetch=fetch_for(payload), cache_path=path)
    assert status["success"] is False
    assert status["last_error"]["error_type"] == "invalid_response"
    assert status["last_success_at"] == NOW.isoformat()
    assert path.read_bytes() == before


def test_valid_map_with_one_valid_and_one_malformed_legacy_window_succeeds(tmp_path):
    payload = multibucket_result()
    payload["rateLimits"] = snapshot(
        limit_id="bucket_a",
        primary=window(used=42, duration=300, resets=RESET_SOON),
        secondary="garbage",
    )
    path = tmp_path / "c.json"
    status = CodexRateLimitsController().refresh(now=NOW, fetch=fetch_for(payload), cache_path=path)
    assert status["success"] is True
    record = json.loads(path.read_text(encoding="utf-8"))
    assert [bucket["limit_id"] for bucket in record["buckets"]] == ["bucket_a", "bucket_b"]
    assert record["five_hour"]["used_percentage"] == 42.0
    assert record["weekly"] is None


# ---------------------------------------------------------------------------
# I. Cache v1 / v2
# ---------------------------------------------------------------------------

LEGACY_FIVE_HOUR = {
    "used_percentage": 42.0,
    "remaining_percentage": 58.0,
    "resets_at": RESET_SOON_ISO,
    "window_duration_minutes": 300,
}
LEGACY_WEEKLY = {
    "used_percentage": 18.0,
    "remaining_percentage": 82.0,
    "resets_at": RESET_LATER_ISO,
    "window_duration_minutes": 10080,
}


def v1_record() -> dict:
    return {
        "schema_version": 1,
        "source": "codex_app_server",
        "observed_at": NOW.isoformat(),
        "five_hour": LEGACY_FIVE_HOUR,
        "weekly": LEGACY_WEEKLY,
    }


def v2_record() -> dict:
    return {
        **v1_record(),
        "schema_version": 2,
        "buckets": [
            {
                "limit_id": "bucket_a",
                "limit_id_origin": "map_key",
                "display_name": None,
                "plan_type": "synthetic_plan",
                "rate_limit_reached_type": None,
                "windows": [{"source_slot": "primary", **LEGACY_WEEKLY}],
            },
            {
                "limit_id": "bucket_b",
                "limit_id_origin": "map_key",
                "display_name": "Synthetic Bucket B",
                "plan_type": "synthetic_plan",
                "rate_limit_reached_type": None,
                "windows": [
                    {"source_slot": "primary", **LEGACY_FIVE_HOUR},
                    {
                        "source_slot": "secondary",
                        "used_percentage": 20.0,
                        "remaining_percentage": 80.0,
                        "resets_at": None,
                        "window_duration_minutes": None,
                    },
                ],
            },
        ],
    }


def test_writer_schema_version_is_2_and_reader_accepts_1_and_2():
    assert cache.SCHEMA_VERSION == 2
    assert cache.SUPPORTED_SCHEMA_VERSIONS == (1, 2)


def test_float_legacy_schema_version_is_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({**v1_record(), "schema_version": 1.0}), encoding="utf-8")
    assert cache.load_snapshot(now=NOW, path=path)["status"] == "invalid_cache"


def test_single_legacy_view_bucket_is_valid_in_a_v2_cache(tmp_path):
    record = v2_record()
    record["buckets"] = [dict(record["buckets"][0], limit_id="legacy_bucket", limit_id_origin="snapshot_field")]
    path = tmp_path / "c.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    snapshot_ = cache.load_snapshot(now=NOW, path=path)
    assert [(b["limit_id"], b["limit_id_origin"]) for b in snapshot_["buckets"]] == [("legacy_bucket", "snapshot_field")]


def test_v1_cache_still_loads_with_its_exact_historical_shape(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(v1_record()), encoding="utf-8")
    snapshot_ = cache.load_snapshot(now=NOW, path=path)
    assert snapshot_ == {
        "available": True,
        "stale": False,
        "status": "ok",
        "observed_at": NOW.isoformat(),
        "source": "codex_app_server",
        "five_hour": LEGACY_FIVE_HOUR,
        "weekly": LEGACY_WEEKLY,
        "error_message": None,
    }


def test_v2_cache_loads_buckets_with_nullable_windows(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(v2_record()), encoding="utf-8")
    snapshot_ = cache.load_snapshot(now=NOW, path=path)
    assert snapshot_["five_hour"] == LEGACY_FIVE_HOUR
    assert [bucket["limit_id"] for bucket in snapshot_["buckets"]] == ["bucket_a", "bucket_b"]
    b = snapshot_["buckets"][1]
    assert b["status"] == "ok"
    assert b["windows"][1] == {
        "source_slot": "secondary",
        "used_percentage": 20.0,
        "remaining_percentage": 80.0,
        "resets_at": None,
        "window_duration_minutes": None,
    }


def test_refresh_writes_v2_from_a_real_protocol_run_without_raw_fields(tmp_path):
    path = tmp_path / "c.json"
    controller = CodexRateLimitsController()
    status = controller.refresh(now=NOW, fetch=fetch_for(multibucket_result()), cache_path=path)
    assert status["success"] is True

    text = path.read_text(encoding="utf-8")
    for marker in SENSITIVE_MARKERS:
        assert marker not in text
    record = json.loads(text)
    assert set(record) == {"schema_version", "source", "observed_at", "five_hour", "weekly", "buckets"}
    assert record["schema_version"] == 2
    for bucket in record["buckets"]:
        assert set(bucket) == {
            "limit_id",
            "limit_id_origin",
            "display_name",
            "plan_type",
            "rate_limit_reached_type",
            "windows",
        }
        for projected in bucket["windows"]:
            assert set(projected) == {
                "source_slot",
                "used_percentage",
                "remaining_percentage",
                "resets_at",
                "window_duration_minutes",
            }


def test_fail_closed_fetch_does_not_overwrite_an_existing_v2_cache(tmp_path):
    path = tmp_path / "c.json"
    controller = CodexRateLimitsController()
    controller.refresh(now=NOW, fetch=fetch_for(multibucket_result()), cache_path=path)
    before = path.read_bytes()

    broken = multibucket_result()
    broken["rateLimitsByLimitId"]["bucket_b"]["limitId"] = "mismatch"
    status = controller.refresh(now=NOW + timedelta(minutes=5), fetch=fetch_for(broken), cache_path=path)
    assert status["success"] is False
    assert status["last_error"]["error_type"] == "invalid_response"
    assert path.read_bytes() == before


def test_success_without_canonical_buckets_is_never_written(tmp_path):
    path = tmp_path / "c.json"
    legacy_only = adapter.CodexRateLimitsFetchResult(
        success=True,
        windows={"five_hour": LEGACY_FIVE_HOUR, "weekly": None},
        error_type=None,
        user_message=None,
        collected_at=NOW,
    )
    status = CodexRateLimitsController().refresh(now=NOW, fetch=lambda now: legacy_only, cache_path=path)
    assert status["success"] is False
    assert not path.exists()


def _mutate(record: dict, mutation) -> dict:
    record = copy.deepcopy(record)
    mutation(record)
    return record


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.pop("buckets"),
        lambda r: r.__setitem__("buckets", []),
        lambda r: r.__setitem__("buckets", {"bucket_a": {}}),
        lambda r: r.__setitem__("schema_version", True),
        lambda r: r.__setitem__("schema_version", 2.0),
        lambda r: r.__setitem__("schema_version", 3),
        # several buckets may only come from the multi-bucket view (all map-keyed)
        lambda r: r["buckets"][1].__setitem__("limit_id_origin", "snapshot_field"),
        lambda r: [bucket.update(limit_id=None, limit_id_origin=None) for bucket in r["buckets"]],
        lambda r: r["buckets"][0]["windows"][0].__setitem__("source_slot", "tertiary"),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("source_slot", None),
        lambda r: r["buckets"][1]["windows"][1].__setitem__("source_slot", "primary"),
        lambda r: r["buckets"][1].__setitem__("limit_id", "bucket_a"),
        lambda r: r["buckets"][0].__setitem__("limit_id", None),
        lambda r: r["buckets"][0].__setitem__("limit_id", ""),
        lambda r: r["buckets"][0].__setitem__("limit_id_origin", "invented"),
        lambda r: r["buckets"][0].__setitem__("plan_type", 5),
        lambda r: r["buckets"][0].__setitem__("windows", None),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("used_percentage", True),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("remaining_percentage", 1.0),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("resets_at", "2026-01-06T12:00:00"),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("window_duration_minutes", "300"),
        lambda r: r["buckets"][0]["windows"][0].__setitem__("window_duration_minutes", True),
    ],
)
def test_malformed_v2_cache_is_invalid_cache(tmp_path, mutation):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(_mutate(v2_record(), mutation)), encoding="utf-8")
    assert cache.load_snapshot(now=NOW, path=path)["status"] == "invalid_cache"


def test_unknown_keys_in_a_v2_cache_never_survive_loading(tmp_path):
    record = v2_record()
    record["accountId"] = ACCOUNT_MARKER
    record["buckets"][0]["credits"] = {"balance": BALANCE_MARKER}
    record["buckets"][0]["windows"][0]["rateLimitUpsell"] = UPSELL_MARKER
    path = tmp_path / "c.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    loaded = json.dumps(cache.load_snapshot(now=NOW, path=path))
    for marker in SENSITIVE_MARKERS:
        assert marker not in loaded


def test_bucket_status_follows_its_own_resets_only(tmp_path):
    record = v2_record()
    # only bucket_b's secondary has passed its reset; the legacy windows have not
    record["buckets"][1]["windows"][1]["resets_at"] = (NOW - timedelta(minutes=1)).isoformat()
    path = tmp_path / "c.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    snapshot_ = cache.load_snapshot(now=NOW, path=path)
    assert snapshot_["status"] == "ok"  # legacy meaning unchanged
    assert [bucket["status"] for bucket in snapshot_["buckets"]] == ["ok", "stale"]


def test_old_observation_makes_every_bucket_stale(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(v2_record()), encoding="utf-8")
    snapshot_ = cache.load_snapshot(now=NOW + timedelta(minutes=16), path=path)
    assert snapshot_["status"] == "stale"
    assert {bucket["status"] for bucket in snapshot_["buckets"]} == {"stale"}


# ---------------------------------------------------------------------------
# J / K / L. Endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "codex_rate_limits": tmp_path / "codex-rate-limits.json",
        "codex_manual": tmp_path / "codex-usage.json",
        "claude_code": tmp_path / "claude-code-usage.json",
        "claude_desktop": tmp_path / "claude-desktop-cloud-usage.json",
    }
    monkeypatch.setattr("app.codex_rate_limits_cache.resolve_cache_path", lambda env=None: paths["codex_rate_limits"])
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: paths["codex_manual"])
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: paths["claude_code"])
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: paths["claude_desktop"]
    )
    return paths


@pytest.fixture()
def client(cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)
    monkeypatch.setattr(app.state, "codex_rate_limits_controller", CodexRateLimitsController())
    with TestClient(app) as test_client:
        yield test_client


def codex_auto_allowances(payload: dict) -> list[dict]:
    return [
        bucket
        for bucket in payload["allowances"]
        if bucket["product_surface"] == "work_codex" and bucket["source_kind"] == "OFFICIAL_LOCAL_RUNTIME"
    ]


def test_legacy_endpoint_response_is_identical_for_v1_and_v2_caches(client, cache_paths):
    cache_paths["codex_rate_limits"].write_text(json.dumps(v1_record()), encoding="utf-8")
    from_v1 = client.get("/api/codex-rate-limits").json()
    cache_paths["codex_rate_limits"].write_text(json.dumps(v2_record()), encoding="utf-8")
    from_v2 = client.get("/api/codex-rate-limits").json()
    assert from_v2 == from_v1
    assert "buckets" not in from_v2


def test_generic_endpoint_keeps_phase_1_projection_for_a_v1_cache(client, cache_paths):
    cache_paths["codex_rate_limits"].write_text(json.dumps(v1_record()), encoding="utf-8")
    (bucket,) = codex_auto_allowances(client.get("/api/usage-allowances").json())
    assert bucket["limit_id"] is None
    assert bucket["plan_type"] is None
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in bucket["windows"]] == [(None, 300), (None, 10080)]


def test_generic_endpoint_projects_every_v2_bucket_and_no_legacy_duplicate(client, cache_paths):
    cache_paths["codex_rate_limits"].write_text(json.dumps(v2_record()), encoding="utf-8")
    allowances = codex_auto_allowances(client.get("/api/usage-allowances").json())
    assert [bucket["limit_id"] for bucket in allowances] == ["bucket_a", "bucket_b"]
    b = allowances[1]
    assert b["limit_id_origin"] == "map_key"
    assert b["display_name"] == "Synthetic Bucket B"
    assert b["plan_type"] == "synthetic_plan"
    assert b["status"] == "ok"
    assert b["provenance"] == "codex_app_server"
    assert b["observed_at"] == NOW.isoformat()
    assert b["windows"] == [
        {
            "source_slot": "primary",
            "window_duration_minutes": 300,
            "used_percent": 42.0,
            "remaining_percent": 58.0,
            "resets_at": RESET_SOON_ISO,
        },
        {
            "source_slot": "secondary",
            "window_duration_minutes": None,
            "used_percent": 20.0,
            "remaining_percent": 80.0,
            "resets_at": None,
        },
    ]


def test_generic_endpoint_emits_a_metadata_only_bucket_with_no_windows(client, cache_paths):
    record = v2_record()
    record["buckets"].append(
        {
            "limit_id": "bucket_meta",
            "limit_id_origin": "map_key",
            "display_name": None,
            "plan_type": None,
            "rate_limit_reached_type": "some_future_reason",
            "windows": [],
        }
    )
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")
    meta = codex_auto_allowances(client.get("/api/usage-allowances").json())[-1]
    assert meta["limit_id"] == "bucket_meta"
    assert meta["rate_limit_reached_type"] == "some_future_reason"
    assert meta["windows"] == []


def test_only_the_codex_auto_source_is_ever_projected_through_buckets():
    stray = {
        "limit_id": "stray",
        "limit_id_origin": "map_key",
        "display_name": None,
        "plan_type": None,
        "rate_limit_reached_type": None,
        "status": "ok",
        "windows": [],
    }
    other = {
        "available": True,
        "status": "ok",
        "observed_at": NOW.isoformat(),
        "source": "claude_code_statusline",
        "five_hour": {"used_percentage": 1.0, "remaining_percentage": 99.0, "resets_at": RESET_SOON_ISO},
        "seven_day": None,
        "buckets": [stray],
    }
    unavailable = {"available": False, "status": "not_observed"}
    payload = usage_allowance.build_usage_allowance_payload(
        generated_at=NOW,
        codex_rate_limits=unavailable,
        codex_manual=unavailable,
        claude_code=other,
        claude_desktop_cloud=unavailable,
    )
    (claude_bucket,) = payload["allowances"]
    assert claude_bucket["product_surface"] == "claude_code"
    assert claude_bucket["limit_id"] is None
    assert len(claude_bucket["windows"]) == 1


def test_generic_endpoint_status_is_per_bucket(client, cache_paths):
    record = v2_record()
    record["buckets"][1]["windows"][0]["resets_at"] = (NOW - timedelta(minutes=1)).isoformat()
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")
    allowances = codex_auto_allowances(client.get("/api/usage-allowances").json())
    assert [(bucket["limit_id"], bucket["status"]) for bucket in allowances] == [("bucket_a", "ok"), ("bucket_b", "stale")]


def test_hand_edited_v2_cache_with_unknown_keys_leaks_nothing_through_either_endpoint(client, cache_paths):
    record = v2_record()
    record["accountId"] = ACCOUNT_MARKER
    record["rateLimitUpsell"] = UPSELL_MARKER
    record["buckets"][0]["credits"] = {"balance": BALANCE_MARKER}
    record["buckets"][0]["individualLimit"] = SPEND_MARKER
    record["buckets"][1]["windows"][0]["rateLimitResetCredits"] = RESET_CREDIT_MARKER
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")
    generic = client.get("/api/usage-allowances")
    legacy = client.get("/api/codex-rate-limits")
    assert generic.status_code == 200 and legacy.status_code == 200
    assert len(codex_auto_allowances(generic.json())) == 2
    for text in (generic.text, legacy.text):
        for marker in SENSITIVE_MARKERS:
            assert marker not in text


def test_refresh_then_read_end_to_end_leaks_nothing(client, cache_paths, monkeypatch):
    monkeypatch.setattr("app.main.fetch_codex_rate_limits", fetch_for(multibucket_result()))
    refreshed = client.post("/api/codex-rate-limits/refresh")
    assert refreshed.status_code == 200

    legacy = client.get("/api/codex-rate-limits")
    generic = client.get("/api/usage-allowances")
    assert [bucket["limit_id"] for bucket in codex_auto_allowances(generic.json())] == ["bucket_a", "bucket_b"]
    assert legacy.json()["weekly"]["window_duration_minutes"] == 10080
    for text in (refreshed.text, legacy.text, generic.text, cache_paths["codex_rate_limits"].read_text(encoding="utf-8")):
        for marker in SENSITIVE_MARKERS:
            assert marker not in text
