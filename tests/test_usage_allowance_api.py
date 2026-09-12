"""Tests for the generic Usage Allowance read model (Phase 1).

Two layers are tested separately and are never conflated:

- Generic SCHEMA tests, which pin what the contract is able to represent
  (for example a window with no duration and no reset time). These say
  nothing about whether any current cache can carry such a window.
- CACHE PROJECTION tests, which pin what the caches actually produce. The
  Codex auto-fetch record here is a v1 cache: it carries only the two
  historical windows, so everything else the Codex App Server returns must
  project as null and must not be dressed up as supported.

The v2 multi-bucket path (`rateLimitsByLimitId` ingestion, the `rateLimits`
vs `rateLimitsByLimitId` selection/dedupe rule, and `limitId` / `limitName` /
`planType` / `rateLimitReachedType` extraction) is covered in
`tests/test_codex_rate_limits_multibucket.py`.
"""

import ast
import inspect
import json
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import claude_code_usage_cache
from app import claude_desktop_cloud_usage_cache
from app import codex_rate_limits_cache
from app import codex_usage_cache
from app import schemas
from app import usage_allowance
from app.main import app
from app.usage_allowance import build_usage_allowance_payload, project_window

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

APP_DIR = Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# Cache record builders — the on-disk shape each existing cache validates.
# ---------------------------------------------------------------------------


def _percent_window(used: float, resets_at: datetime, *, duration: int | None = None) -> dict:
    window = {
        "used_percentage": used,
        "remaining_percentage": 100.0 - used,
        "resets_at": resets_at.isoformat(),
    }
    if duration is not None:
        window["window_duration_minutes"] = duration
    return window


def codex_rate_limits_record(observed_at: datetime = NOW) -> dict:
    """A v1 Codex auto-fetch cache (legacy windows only, no canonical buckets)."""
    return {
        "schema_version": codex_rate_limits_cache.LEGACY_SCHEMA_VERSION,
        "source": codex_rate_limits_cache.SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": _percent_window(42.0, NOW + timedelta(hours=3), duration=300),
        "weekly": _percent_window(18.0, NOW + timedelta(days=5), duration=10080),
    }


def codex_manual_record(observed_at: datetime = NOW) -> dict:
    return {
        "schema_version": codex_usage_cache.SCHEMA_VERSION,
        "source": codex_usage_cache.SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": _percent_window(10.0, NOW + timedelta(hours=2)),
        "weekly": _percent_window(20.0, NOW + timedelta(days=4)),
    }


def claude_code_record(observed_at: datetime = NOW) -> dict:
    return {
        "schema_version": claude_code_usage_cache.SCHEMA_VERSION,
        "source": claude_code_usage_cache.SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": _percent_window(33.0, NOW + timedelta(hours=1)),
        "seven_day": _percent_window(55.0, NOW + timedelta(days=3)),
    }


def claude_desktop_record(observed_at: datetime = NOW) -> dict:
    return {
        "schema_version": claude_desktop_cloud_usage_cache.SCHEMA_VERSION,
        "source": claude_desktop_cloud_usage_cache.SOURCE_NAME,
        "observed_at": observed_at.isoformat(),
        "five_hour": _percent_window(5.0, NOW + timedelta(hours=4)),
        "seven_day": _percent_window(6.0, NOW + timedelta(days=2)),
    }


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


@pytest.fixture()
def cache_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every cache loader at an isolated tmp file — never the real one."""
    paths = {
        "codex_rate_limits": tmp_path / "codex-rate-limits.json",
        "codex_manual": tmp_path / "codex-usage.json",
        "claude_code": tmp_path / "claude-code-usage.json",
        "claude_desktop": tmp_path / "claude-desktop-cloud-usage.json",
    }
    monkeypatch.setattr(
        "app.codex_rate_limits_cache.resolve_cache_path", lambda env=None: paths["codex_rate_limits"]
    )
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: paths["codex_manual"])
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: paths["claude_code"])
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: paths["claude_desktop"]
    )
    return paths


@pytest.fixture()
def client(cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)
    with TestClient(app) as test_client:
        yield test_client


def write_all_caches(paths: dict[str, Path]) -> None:
    paths["codex_rate_limits"].write_text(json.dumps(codex_rate_limits_record()), encoding="utf-8")
    paths["codex_manual"].write_text(json.dumps(codex_manual_record()), encoding="utf-8")
    paths["claude_code"].write_text(json.dumps(claude_code_record()), encoding="utf-8")
    paths["claude_desktop"].write_text(json.dumps(claude_desktop_record()), encoding="utf-8")


def bucket_for(payload: dict, product_surface: str, source_kind: str) -> dict:
    matches = [
        bucket
        for bucket in payload["allowances"]
        if bucket["product_surface"] == product_surface and bucket["source_kind"] == source_kind
    ]
    assert len(matches) == 1, f"expected exactly one {product_surface}/{source_kind} bucket"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Generic contract shape
# ---------------------------------------------------------------------------


def test_response_has_expected_top_level_shape(client: TestClient, cache_paths: dict[str, Path]) -> None:
    write_all_caches(cache_paths)
    payload = client.get("/api/usage-allowances").json()

    assert set(payload) == {"generated_at", "allowances", "unavailable"}
    # generated_at describes when the projection ran, not when any number was
    # observed; the two must stay separately named.
    assert payload["generated_at"] == NOW.isoformat()
    assert isinstance(payload["allowances"], list)
    assert isinstance(payload["unavailable"], list)


def test_bucket_and_window_key_shape_is_pinned(client: TestClient, cache_paths: dict[str, Path]) -> None:
    write_all_caches(cache_paths)
    payload = client.get("/api/usage-allowances").json()

    for bucket in payload["allowances"]:
        assert set(bucket) == {
            "provider",
            "product_surface",
            "limit_id",
            "limit_id_origin",
            "display_name",
            "plan_type",
            "rate_limit_reached_type",
            "status",
            "source_kind",
            "provenance",
            "observed_at",
            "windows",
        }
        # windows is an array, never a fixed five_hour/weekly/seven_day key set
        assert isinstance(bucket["windows"], list)
        for window in bucket["windows"]:
            assert set(window) == {
                "source_slot",
                "window_duration_minutes",
                "used_percent",
                "remaining_percent",
                "resets_at",
            }


def test_no_fixed_window_keys_anywhere_in_response(client: TestClient, cache_paths: dict[str, Path]) -> None:
    """The whole point of the generic model: window names are data, not schema."""
    write_all_caches(cache_paths)
    payload = client.get("/api/usage-allowances").json()

    for bucket in payload["allowances"]:
        for fixed_key in ("five_hour", "weekly", "seven_day"):
            assert fixed_key not in bucket


# ---------------------------------------------------------------------------
# 2. Nullable representability — GENERIC SCHEMA ONLY.
#    This is not a claim that any current cache can produce such a window.
# ---------------------------------------------------------------------------


def test_schema_allows_window_with_used_percent_only() -> None:
    window = schemas.UsageAllowanceWindow(used_percent=42.0)

    assert window.used_percent == 42.0
    assert window.window_duration_minutes is None
    assert window.resets_at is None
    assert window.remaining_percent is None
    assert window.source_slot is None


def test_schema_allows_null_duration_and_null_reset_with_a_slot() -> None:
    window = schemas.UsageAllowanceWindow(
        source_slot="primary", window_duration_minutes=None, used_percent=7.5, resets_at=None
    )

    assert window.source_slot == "primary"
    assert window.window_duration_minutes is None
    assert window.resets_at is None


def test_schema_allows_bucket_with_no_windows() -> None:
    bucket = schemas.UsageAllowanceBucket(
        provider="openai", product_surface="work_codex", status="ok", source_kind="OFFICIAL_LOCAL_RUNTIME"
    )

    assert bucket.windows == []


@pytest.mark.parametrize("value", [True, False])
def test_schema_rejects_boolean_percentages(value: bool) -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=value)


@pytest.mark.parametrize("value", [True, False])
def test_schema_rejects_boolean_duration(value: bool) -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=1.0, window_duration_minutes=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_schema_rejects_non_finite_percentages(value: float) -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=value)


@pytest.mark.parametrize("value", [-0.1, 100.1])
def test_schema_rejects_out_of_range_used_percent(value: float) -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=value)


@pytest.mark.parametrize("value", [-0.1, 100.1])
def test_schema_rejects_out_of_range_remaining_percent(value: float) -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=10.0, remaining_percent=value)


@pytest.mark.parametrize("value", [0.0, 100.0])
def test_schema_accepts_percentage_boundaries(value: float) -> None:
    assert schemas.UsageAllowanceWindow(used_percent=value).used_percent == value


def test_schema_accepts_float_percentages() -> None:
    """The generic contract is numeric; a vendor's int32 wire type is that
    vendor's business and must not be pushed onto every source."""
    assert schemas.UsageAllowanceWindow(used_percent=42.5).used_percent == 42.5


# ---------------------------------------------------------------------------
# 3. Open-string forward compatibility
# ---------------------------------------------------------------------------


def test_unknown_product_surface_and_provider_are_representable() -> None:
    bucket = schemas.UsageAllowanceBucket(
        provider="some-future-provider",
        product_surface="some_future_surface",
        status="ok",
        source_kind="SOME_FUTURE_SOURCE_KIND",
    )

    assert bucket.provider == "some-future-provider"
    assert bucket.product_surface == "some_future_surface"
    assert bucket.source_kind == "SOME_FUTURE_SOURCE_KIND"


def test_unknown_plan_type_and_reached_type_are_representable() -> None:
    bucket = schemas.UsageAllowanceBucket(
        provider="openai",
        product_surface="work_codex",
        status="ok",
        source_kind="OFFICIAL_LOCAL_RUNTIME",
        plan_type="some_future_plan",
        rate_limit_reached_type="some_future_reached_type",
    )

    assert bucket.plan_type == "some_future_plan"
    assert bucket.rate_limit_reached_type == "some_future_reached_type"


def test_unknown_source_slot_is_representable() -> None:
    window = schemas.UsageAllowanceWindow(source_slot="tertiary", used_percent=1.0)

    assert window.source_slot == "tertiary"


def test_unknown_window_duration_is_representable() -> None:
    """A window length nobody has seen yet must survive, not be dropped."""
    window = schemas.UsageAllowanceWindow(window_duration_minutes=4320, used_percent=1.0)

    assert window.window_duration_minutes == 4320


# ---------------------------------------------------------------------------
# 4. Unknown FIELD security — values may be unknown, raw fields may not pass through
# ---------------------------------------------------------------------------


def test_schema_forbids_extra_fields_on_every_generic_model() -> None:
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=1.0, raw_payload={"a": 1})
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceBucket(
            provider="openai",
            product_surface="work_codex",
            status="ok",
            source_kind="MANUAL",
            credits={"balance": "9"},
        )
    with pytest.raises(ValidationError):
        schemas.UnavailableUsageAllowance(
            provider="openai", product_surface="chat", status="not_observed", raw="x"
        )
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceResponse(generated_at=NOW.isoformat(), stdout="x")


def test_projection_ignores_unknown_window_fields() -> None:
    """Tests the projection's own allowlist, not the cache validator.

    The end-to-end test below cannot reach this: every cache validator
    already rewrites a window down to its own known keys, so an unknown field
    never arrives at `project_window` from disk. Handing one in directly is
    the only way to prove the allowlist — not the validator — is what keeps
    it out.
    """
    projected = project_window(
        {
            "used_percentage": 1.0,
            "remaining_percentage": 99.0,
            "resets_at": NOW.isoformat(),
            "window_duration_minutes": 300,
            "rateLimitReachedType": "rate_limit_reached",
            "credits": {"balance": "12.34"},
            "leak": "SHOULD-NEVER-APPEAR",
        },
        source_slot=None,
    )

    assert set(projected) == {
        "source_slot",
        "window_duration_minutes",
        "used_percent",
        "remaining_percent",
        "resets_at",
    }
    assert "SHOULD-NEVER-APPEAR" not in json.dumps(projected)


def test_projection_ignores_unknown_snapshot_fields() -> None:
    """Same, one level up: an unknown snapshot key must not become a bucket
    key, and `error_message` must stay off this surface."""
    bucket, missing = usage_allowance.project_cache_snapshot(
        {
            "available": True,
            "stale": False,
            "status": "ok",
            "observed_at": NOW.isoformat(),
            "source": "codex_app_server",
            "five_hour": None,
            "weekly": None,
            "error_message": "SHOULD-NEVER-APPEAR",
            "rateLimitsByLimitId": {"codex": {"limitId": "SHOULD-NEVER-APPEAR"}},
            "raw_payload": {"leak": "SHOULD-NEVER-APPEAR"},
        },
        projection=usage_allowance.CODEX_RATE_LIMITS_PROJECTION,
    )

    assert missing is None
    assert "error_message" not in bucket
    assert "rateLimitsByLimitId" not in bucket
    assert "raw_payload" not in bucket
    assert "SHOULD-NEVER-APPEAR" not in json.dumps(bucket)


def test_unknown_cache_fields_never_reach_the_response(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    """Defense in depth, end to end: a cache file carrying extra
    vendor-shaped fields must not leak them (here the cache validator strips
    them first — the two tests above pin the projection's own allowlist)."""
    record = codex_rate_limits_record()
    record["rateLimitsByLimitId"] = {"codex": {"limitId": "codex"}}
    record["credits"] = {"hasCredits": True, "unlimited": False, "balance": "12.34"}
    record["rateLimitResetCredits"] = {"availableCount": 2}
    record["raw_payload"] = {"leak-marker": "SHOULD-NEVER-APPEAR"}
    record["five_hour"]["rateLimitReachedType"] = "rate_limit_reached"
    record["five_hour"]["leak_marker_window"] = "SHOULD-NEVER-APPEAR"
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")

    body = client.get("/api/usage-allowances").text

    for forbidden in (
        "rateLimitsByLimitId",
        "rateLimitResetCredits",
        "raw_payload",
        "leak_marker_window",
        "SHOULD-NEVER-APPEAR",
        "hasCredits",
        "balance",
        "availableCount",
    ):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# 5. Current cache projection
# ---------------------------------------------------------------------------


def test_all_four_sources_project_when_present(client: TestClient, cache_paths: dict[str, Path]) -> None:
    write_all_caches(cache_paths)
    payload = client.get("/api/usage-allowances").json()

    assert payload["unavailable"] == []
    assert {(b["provider"], b["product_surface"], b["source_kind"]) for b in payload["allowances"]} == {
        ("openai", "work_codex", "OFFICIAL_LOCAL_RUNTIME"),
        ("openai", "work_codex", "MANUAL"),
        ("anthropic", "claude_code", "LOCAL_OBSERVATION"),
        ("anthropic", "claude_desktop_cloud", "MANUAL"),
    }


def test_codex_auto_projection_uses_only_cached_values(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["codex_rate_limits"].write_text(json.dumps(codex_rate_limits_record()), encoding="utf-8")
    payload = client.get("/api/usage-allowances").json()
    bucket = bucket_for(payload, "work_codex", "OFFICIAL_LOCAL_RUNTIME")

    assert bucket["provenance"] == "codex_app_server"
    assert bucket["status"] == "ok"
    assert bucket["observed_at"] == NOW.isoformat()
    assert bucket["windows"] == [
        {
            "source_slot": None,
            "window_duration_minutes": 300,
            "used_percent": 42.0,
            "remaining_percent": 58.0,
            "resets_at": (NOW + timedelta(hours=3)).isoformat(),
        },
        {
            "source_slot": None,
            "window_duration_minutes": 10080,
            "used_percent": 18.0,
            "remaining_percent": 82.0,
            "resets_at": (NOW + timedelta(days=5)).isoformat(),
        },
    ]


def test_codex_manual_projection_uses_only_cached_values(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["codex_manual"].write_text(json.dumps(codex_manual_record()), encoding="utf-8")
    payload = client.get("/api/usage-allowances").json()
    bucket = bucket_for(payload, "work_codex", "MANUAL")

    assert bucket["provenance"] == "codex_manual"
    # This cache stores no duration, so duration is null and the slot name it
    # does carry is what keeps the two windows apart.
    assert [(w["source_slot"], w["window_duration_minutes"]) for w in bucket["windows"]] == [
        ("five_hour", None),
        ("weekly", None),
    ]
    assert [w["used_percent"] for w in bucket["windows"]] == [10.0, 20.0]


def test_claude_code_projection_uses_only_cached_values(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["claude_code"].write_text(json.dumps(claude_code_record()), encoding="utf-8")
    payload = client.get("/api/usage-allowances").json()
    bucket = bucket_for(payload, "claude_code", "LOCAL_OBSERVATION")

    assert bucket["provider"] == "anthropic"
    assert bucket["provenance"] == "claude_code_statusline"
    assert [(w["source_slot"], w["used_percent"], w["window_duration_minutes"]) for w in bucket["windows"]] == [
        ("five_hour", 33.0, None),
        ("seven_day", 55.0, None),
    ]


def test_claude_desktop_cloud_projection_uses_only_cached_values(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["claude_desktop"].write_text(json.dumps(claude_desktop_record()), encoding="utf-8")
    payload = client.get("/api/usage-allowances").json()
    bucket = bucket_for(payload, "claude_desktop_cloud", "MANUAL")

    assert bucket["provider"] == "anthropic"
    assert bucket["provenance"] == "claude_desktop_cloud_manual"
    assert [w["source_slot"] for w in bucket["windows"]] == ["five_hour", "seven_day"]


def test_partial_window_projects_without_inventing_the_other(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    record = codex_rate_limits_record()
    record["weekly"] = None
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")

    bucket = bucket_for(client.get("/api/usage-allowances").json(), "work_codex", "OFFICIAL_LOCAL_RUNTIME")

    assert len(bucket["windows"]) == 1
    assert bucket["windows"][0]["window_duration_minutes"] == 300


def test_missing_cache_is_unavailable_not_a_zeroed_bucket(client: TestClient) -> None:
    payload = client.get("/api/usage-allowances").json()

    assert payload["allowances"] == []
    assert payload["unavailable"] == [
        {
            "provider": "openai",
            "product_surface": "work_codex",
            "status": "not_observed",
            "source_kind": "OFFICIAL_LOCAL_RUNTIME",
        },
        {
            "provider": "openai",
            "product_surface": "work_codex",
            "status": "not_observed",
            "source_kind": "MANUAL",
        },
        {
            "provider": "anthropic",
            "product_surface": "claude_code",
            "status": "not_observed",
            "source_kind": "LOCAL_OBSERVATION",
        },
        {
            "provider": "anthropic",
            "product_surface": "claude_desktop_cloud",
            "status": "not_observed",
            "source_kind": "MANUAL",
        },
    ]


def test_unreadable_cache_is_reported_as_invalid_cache(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["codex_rate_limits"].write_text("{ not json", encoding="utf-8")

    payload = client.get("/api/usage-allowances").json()

    assert payload["allowances"] == []
    statuses = {(entry["product_surface"], entry["source_kind"]): entry["status"] for entry in payload["unavailable"]}
    assert statuses[("work_codex", "OFFICIAL_LOCAL_RUNTIME")] == "invalid_cache"


def test_stale_cache_still_projects_with_stale_status(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    stale_record = codex_rate_limits_record(observed_at=NOW - timedelta(hours=2))
    cache_paths["codex_rate_limits"].write_text(json.dumps(stale_record), encoding="utf-8")

    bucket = bucket_for(client.get("/api/usage-allowances").json(), "work_codex", "OFFICIAL_LOCAL_RUNTIME")

    assert bucket["status"] == "stale"
    assert bucket["observed_at"] == (NOW - timedelta(hours=2)).isoformat()


def test_remaining_percent_is_read_from_the_source_not_recomputed(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    """Uses a pair that does NOT sum to exactly 100 (the caches allow a 0.5
    tolerance), so a `100 - used` reimplementation would return 58.0 and fail
    here. A fixture where remaining == 100 - used could not tell the two
    apart."""
    record = codex_rate_limits_record()
    record["five_hour"]["used_percentage"] = 42.0
    record["five_hour"]["remaining_percentage"] = 58.4
    cache_paths["codex_rate_limits"].write_text(json.dumps(record), encoding="utf-8")

    bucket = bucket_for(client.get("/api/usage-allowances").json(), "work_codex", "OFFICIAL_LOCAL_RUNTIME")

    assert bucket["windows"][0]["used_percent"] == 42.0
    assert bucket["windows"][0]["remaining_percent"] == 58.4


# ---------------------------------------------------------------------------
# 5b. Partial-source failure isolation
#
# The whole point of aggregating four independent sources: one of them being
# broken must cost you that one source, not the endpoint and not the other
# three. Each existing loader already converts its own failure into a status
# instead of raising, so this needs no production code of its own — these
# tests pin that the aggregate keeps that property.
# ---------------------------------------------------------------------------


def test_one_corrupt_source_does_not_hide_the_others(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    cache_paths["codex_rate_limits"].write_text("{ not json", encoding="utf-8")
    cache_paths["claude_code"].write_text(json.dumps(claude_code_record()), encoding="utf-8")
    cache_paths["claude_desktop"].write_text(json.dumps(claude_desktop_record()), encoding="utf-8")

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200

    # The valid sources survive intact, windows and all.
    claude_code_bucket = bucket_for(payload, "claude_code", "LOCAL_OBSERVATION")
    assert [w["used_percent"] for w in claude_code_bucket["windows"]] == [33.0, 55.0]
    assert bucket_for(payload, "claude_desktop_cloud", "MANUAL")["windows"]

    # The broken one is reported as unavailable, not silently dropped and not
    # zeroed.
    unavailable = {(e["product_surface"], e["source_kind"]): e["status"] for e in payload["unavailable"]}
    assert unavailable[("work_codex", "OFFICIAL_LOCAL_RUNTIME")] == "invalid_cache"
    assert unavailable[("work_codex", "MANUAL")] == "not_observed"
    assert all(
        bucket["source_kind"] != "OFFICIAL_LOCAL_RUNTIME" for bucket in payload["allowances"]
    )


def test_a_corrupt_source_leaks_no_file_content_or_error_text(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    """A broken cache must not turn into an error message on the wire."""
    cache_paths["codex_rate_limits"].write_text(
        '{"schema_version": 1, "secret": "SHOULD-NEVER-APPEAR", broken', encoding="utf-8"
    )
    cache_paths["claude_code"].write_text(json.dumps(claude_code_record()), encoding="utf-8")

    body = client.get("/api/usage-allowances").text

    for forbidden in ("SHOULD-NEVER-APPEAR", "Expecting", "JSONDecodeError", "Traceback", "usage cache"):
        assert forbidden not in body


@pytest.mark.parametrize(
    "broken_source, surviving_source, surviving_surface, surviving_kind",
    [
        ("codex_rate_limits", "claude_code", "claude_code", "LOCAL_OBSERVATION"),
        ("claude_code", "codex_rate_limits", "work_codex", "OFFICIAL_LOCAL_RUNTIME"),
        ("codex_manual", "claude_desktop", "claude_desktop_cloud", "MANUAL"),
        ("claude_desktop", "codex_manual", "work_codex", "MANUAL"),
    ],
)
def test_any_single_broken_source_leaves_the_rest_readable(
    client: TestClient,
    cache_paths: dict[str, Path],
    broken_source: str,
    surviving_source: str,
    surviving_surface: str,
    surviving_kind: str,
) -> None:
    """Isolation holds whichever source is the broken one, not just the first."""
    write_all_caches(cache_paths)
    cache_paths[broken_source].write_text("{ not json", encoding="utf-8")

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200
    assert bucket_for(payload, surviving_surface, surviving_kind)["windows"]
    assert len(payload["allowances"]) == 3
    assert len(payload["unavailable"]) == 1
    assert payload["unavailable"][0]["status"] == "invalid_cache"


def test_all_sources_broken_still_answers_without_an_error_payload(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    for path in cache_paths.values():
        path.write_text("{ not json", encoding="utf-8")

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200
    assert payload["allowances"] == []
    assert [entry["status"] for entry in payload["unavailable"]] == ["invalid_cache"] * 4


# ---------------------------------------------------------------------------
# 5c. Loader-level exception isolation
#
# The tests above break the cache *file*, which every loader already converts
# into a status. These break the loader *call* itself — the case a loader
# cannot absorb, because it can raise before reaching its own try block (path
# resolution is outside it). Four sources share one response here, so that
# must cost one source, not the endpoint.
#
# Patch targets are the names the route actually resolves: the module
# attribute for three of them, and `app.main`'s own global for the Claude Code
# loader, which `app/main.py` binds directly via `from ... import ... as ...`.
# Patching the defining module for that one would not be seen by the route.
# ---------------------------------------------------------------------------

SECRET_MARKER = "SECRET-MARKER"

LOADER_PATCH_TARGETS = {
    "codex_rate_limits": ("app.codex_rate_limits_cache.load_snapshot", "work_codex", "OFFICIAL_LOCAL_RUNTIME"),
    "codex_manual": ("app.codex_usage_cache.load_snapshot", "work_codex", "MANUAL"),
    "claude_code": ("app.main.load_claude_code_usage_snapshot", "claude_code", "LOCAL_OBSERVATION"),
    "claude_desktop": (
        "app.claude_desktop_cloud_usage_cache.load_snapshot",
        "claude_desktop_cloud",
        "MANUAL",
    ),
}


def _raising_loader(**kwargs):
    raise RuntimeError(SECRET_MARKER)


def test_a_raising_loader_does_not_take_down_the_other_sources(
    client: TestClient, cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_all_caches(cache_paths)
    monkeypatch.setattr("app.codex_rate_limits_cache.load_snapshot", _raising_loader)

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200

    # The three healthy sources keep their windows.
    assert bucket_for(payload, "claude_code", "LOCAL_OBSERVATION")["windows"]
    assert bucket_for(payload, "claude_desktop_cloud", "MANUAL")["windows"]
    assert bucket_for(payload, "work_codex", "MANUAL")["windows"]

    # The failing one degrades to an unavailable entry with a status from the
    # existing vocabulary — not a new status, and not a zeroed bucket.
    assert payload["unavailable"] == [
        {
            "provider": "openai",
            "product_surface": "work_codex",
            "status": "invalid_cache",
            "source_kind": "OFFICIAL_LOCAL_RUNTIME",
        }
    ]
    assert all(b["source_kind"] != "OFFICIAL_LOCAL_RUNTIME" for b in payload["allowances"])

    # Nothing about the failure reaches the wire.
    for forbidden in (SECRET_MARKER, "RuntimeError", "Traceback", "usage cache"):
        assert forbidden not in response.text


@pytest.mark.parametrize("failing_source", sorted(LOADER_PATCH_TARGETS))
def test_any_single_raising_loader_leaves_the_other_three_readable(
    client: TestClient,
    cache_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failing_source: str,
) -> None:
    target, expected_surface, expected_kind = LOADER_PATCH_TARGETS[failing_source]
    write_all_caches(cache_paths)
    monkeypatch.setattr(target, _raising_loader)

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200
    assert len(payload["allowances"]) == 3
    assert all(bucket["windows"] for bucket in payload["allowances"])
    assert payload["unavailable"] == [
        {
            "provider": "anthropic" if expected_surface.startswith("claude") else "openai",
            "product_surface": expected_surface,
            "status": "invalid_cache",
            "source_kind": expected_kind,
        }
    ]
    assert SECRET_MARKER not in response.text


def test_every_loader_raising_still_answers_without_error_text(
    client: TestClient, cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_all_caches(cache_paths)
    for target, _surface, _kind in LOADER_PATCH_TARGETS.values():
        monkeypatch.setattr(target, _raising_loader)

    response = client.get("/api/usage-allowances")
    payload = response.json()

    assert response.status_code == 200
    assert payload["allowances"] == []
    assert len(payload["unavailable"]) == 4
    assert {entry["status"] for entry in payload["unavailable"]} == {"invalid_cache"}
    for forbidden in (SECRET_MARKER, "RuntimeError", "Traceback"):
        assert forbidden not in response.text


def test_the_guard_does_not_swallow_a_bug_in_our_own_projection(
    client: TestClient, cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is around the loader call only.

    A failure in the projection is our bug, not a source failure, and must
    still surface — otherwise this endpoint would quietly report four
    unreadable sources whenever its own code was broken.
    """
    write_all_caches(cache_paths)

    def explode(**kwargs):
        raise RuntimeError("projection bug must not be reported as an unreadable source")

    monkeypatch.setattr("app.main.build_usage_allowance_payload", explode)

    with pytest.raises(RuntimeError):
        client.get("/api/usage-allowances")


def test_sanitized_unavailable_snapshot_is_built_from_nothing() -> None:
    snapshot = usage_allowance.sanitized_unavailable_snapshot()

    assert snapshot == {
        "available": False,
        "stale": False,
        "status": "invalid_cache",
        "observed_at": None,
        "source": None,
        "error_message": None,
    }
    # Reuses the existing cache vocabulary rather than adding a status.
    assert snapshot["status"] == codex_rate_limits_cache.STATUS_INVALID_CACHE
    # It takes no arguments, so no failure detail can be threaded into it.
    assert inspect.signature(usage_allowance.sanitized_unavailable_snapshot).parameters == {}


# ---------------------------------------------------------------------------
# 6. No fabrication
# ---------------------------------------------------------------------------


def test_codex_auto_never_guesses_primary_or_secondary_slot(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    """The adapter dropped the App Server's own slot before writing the cache,
    so this projection must not re-invent one."""
    cache_paths["codex_rate_limits"].write_text(json.dumps(codex_rate_limits_record()), encoding="utf-8")

    body = client.get("/api/usage-allowances")
    bucket = bucket_for(body.json(), "work_codex", "OFFICIAL_LOCAL_RUNTIME")

    assert all(window["source_slot"] is None for window in bucket["windows"])
    assert "primary" not in body.text
    assert "secondary" not in body.text


def test_fields_no_cache_carries_are_null_for_every_source(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    write_all_caches(cache_paths)
    payload = client.get("/api/usage-allowances").json()

    for bucket in payload["allowances"]:
        assert bucket["limit_id"] is None
        assert bucket["limit_id_origin"] is None
        assert bucket["display_name"] is None
        assert bucket["plan_type"] is None
        assert bucket["rate_limit_reached_type"] is None


def test_project_window_returns_none_for_a_missing_window() -> None:
    assert project_window(None, source_slot="five_hour") is None


def test_projection_emits_null_for_optional_fields_a_source_omits() -> None:
    """The nullable window contract at the PROJECTION layer, not just in the
    schema: a source that reports only a used percentage yields nulls, not a
    KeyError and not a back-computed remaining."""
    assert project_window({"used_percentage": 12.0}, source_slot="primary") == {
        "source_slot": "primary",
        "window_duration_minutes": None,
        "used_percent": 12.0,
        "remaining_percent": None,
        "resets_at": None,
    }


def test_available_snapshot_with_no_windows_projects_an_empty_list() -> None:
    """A source that is readable but currently reports no window is a bucket
    with no windows — never an unavailable entry, and never a zeroed window."""
    bucket, missing = usage_allowance.project_cache_snapshot(
        {
            "available": True,
            "stale": False,
            "status": "ok",
            "observed_at": NOW.isoformat(),
            "source": "codex_manual",
            "five_hour": None,
            "weekly": None,
            "error_message": None,
        },
        projection=usage_allowance.CODEX_MANUAL_PROJECTION,
    )

    assert missing is None
    assert bucket["windows"] == []
    assert bucket["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. reached semantics
# ---------------------------------------------------------------------------


def test_window_model_has_no_reached_field() -> None:
    assert "reached" not in schemas.UsageAllowanceWindow.model_fields
    with pytest.raises(ValidationError):
        schemas.UsageAllowanceWindow(used_percent=1.0, reached=True)


def test_rate_limit_reached_type_lives_on_the_bucket() -> None:
    assert "rate_limit_reached_type" in schemas.UsageAllowanceBucket.model_fields
    assert "rate_limit_reached_type" not in schemas.UsageAllowanceWindow.model_fields


# ---------------------------------------------------------------------------
# 8-9. Vocabulary and forbidden methods
# ---------------------------------------------------------------------------


def test_estimated_is_never_defined_as_a_value_anywhere_in_the_app() -> None:
    """This project never estimates a usage value, so `ESTIMATED` must never
    exist as a string literal that could be emitted. Prose explaining why it
    is absent is fine — an emittable value is not."""
    offenders = [
        path.relative_to(APP_DIR).as_posix()
        for path in APP_DIR.rglob("*.py")
        for literal in ('"ESTIMATED"', "'ESTIMATED'")
        if literal in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_known_source_kinds_do_not_include_estimated_or_unavailable() -> None:
    assert "ESTIMATED" not in usage_allowance.KNOWN_SOURCE_KINDS
    # Unavailability is an availability axis, expressed by the `unavailable`
    # list — never a source kind.
    assert "UNAVAILABLE" not in usage_allowance.KNOWN_SOURCE_KINDS


def test_read_model_names_no_write_or_credit_consuming_method() -> None:
    source = (APP_DIR / "usage_allowance.py").read_text(encoding="utf-8")
    for forbidden in (
        "rateLimitResetCredit/consume",
        "account/rateLimitResetCredit",
        "account/rateLimits/updated",
    ):
        assert forbidden not in source


def test_read_model_imports_nothing_that_could_perform_io() -> None:
    """Pins the import list itself rather than grepping for words: a text
    scan both misses transports it did not think of and trips over prose that
    merely names one."""
    tree = ast.parse((APP_DIR / "usage_allowance.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {"dataclasses", "datetime"}


# ---------------------------------------------------------------------------
# 10. Secret leakage
# ---------------------------------------------------------------------------


def test_response_never_carries_credential_or_process_material(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    write_all_caches(cache_paths)
    body = client.get("/api/usage-allowances").text

    for forbidden in (
        "token",
        "auth",
        "credential",
        "email",
        "session_id",
        "stdout",
        "stderr",
        "error_message",
        "traceback",
        "spendControl",
        "individualLimit",
    ):
        assert forbidden not in body


# ---------------------------------------------------------------------------
# 11. Existing endpoint shape regression
# ---------------------------------------------------------------------------


def test_existing_provider_endpoints_keep_their_shape(
    client: TestClient, cache_paths: dict[str, Path]
) -> None:
    write_all_caches(cache_paths)

    codex_rate_limits = client.get("/api/codex-rate-limits").json()
    assert {
        "fetched",
        "available",
        "stale",
        "status",
        "observed_at",
        "source",
        "five_hour",
        "weekly",
        "error_type",
        "user_message",
    } <= set(codex_rate_limits)
    assert codex_rate_limits["five_hour"]["window_duration_minutes"] == 300

    codex_usage = client.get("/api/codex-usage").json()
    assert set(codex_usage) == {
        "available",
        "stale",
        "status",
        "observed_at",
        "source",
        "five_hour",
        "weekly",
        "error_message",
    }

    claude_code = client.get("/api/claude-code-usage").json()
    assert set(claude_code) == {
        "available",
        "stale",
        "status",
        "observed_at",
        "source",
        "five_hour",
        "seven_day",
        "error_message",
    }

    claude_manual = client.get("/api/claude-code-usage/manual").json()
    assert set(claude_manual) == set(claude_code)


# ---------------------------------------------------------------------------
# 12. I/O boundary
# ---------------------------------------------------------------------------


def _snapshot_from(record: dict, window_keys: tuple[str, ...]) -> dict:
    snapshot = {
        "available": True,
        "stale": False,
        "status": "ok",
        "observed_at": record["observed_at"],
        "source": record["source"],
        "error_message": None,
    }
    for key in window_keys:
        snapshot[key] = record[key]
    return snapshot


def test_pure_projection_touches_no_filesystem_subprocess_network_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The projector is a pure function of its arguments — prove it by making
    every I/O primitive it could reach explode."""

    def explode(*args, **kwargs):
        raise AssertionError("the pure projection must not perform I/O")

    monkeypatch.setattr("builtins.open", explode)
    monkeypatch.setattr(Path, "open", explode)
    monkeypatch.setattr(Path, "exists", explode)
    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(urllib.request, "urlopen", explode)
    monkeypatch.setattr("app.database.SessionLocal", explode)
    monkeypatch.setattr("app.main.SessionLocal", explode)

    payload = build_usage_allowance_payload(
        generated_at=NOW,
        codex_rate_limits=_snapshot_from(codex_rate_limits_record(), ("five_hour", "weekly")),
        codex_manual=_snapshot_from(codex_manual_record(), ("five_hour", "weekly")),
        claude_code=_snapshot_from(claude_code_record(), ("five_hour", "seven_day")),
        claude_desktop_cloud=_snapshot_from(claude_desktop_record(), ("five_hour", "seven_day")),
    )

    assert len(payload["allowances"]) == 4
    assert payload["unavailable"] == []


def test_endpoint_runs_no_subprocess_no_network_and_no_database(
    client: TestClient, cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_all_caches(cache_paths)

    def explode(*args, **kwargs):
        raise AssertionError("this endpoint must not spawn, call out, or open a session")

    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(urllib.request, "urlopen", explode)
    # Patch the names as `app.main` bound them: it does `from app.database
    # import SessionLocal` and `from app.codex_rate_limits_adapter import
    # fetch_codex_rate_limits`, so patching the defining module would leave
    # `app.main`'s own globals — the ones a route body actually resolves —
    # untouched, and the guard would silently never fire.
    monkeypatch.setattr("app.main.SessionLocal", explode)
    monkeypatch.setattr("app.main.fetch_codex_rate_limits", explode)
    monkeypatch.setattr("app.database.SessionLocal", explode)

    assert client.get("/api/usage-allowances").status_code == 200


def test_endpoint_never_writes_a_cache(
    client: TestClient, cache_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_all_caches(cache_paths)
    before = {name: path.read_bytes() for name, path in cache_paths.items()}

    def explode(*args, **kwargs):
        raise AssertionError("this endpoint is read-only")

    for module in (
        "app.codex_rate_limits_cache",
        "app.codex_usage_cache",
        "app.claude_code_usage_cache",
        "app.claude_desktop_cloud_usage_cache",
    ):
        monkeypatch.setattr(f"{module}.write_cache_atomic", explode)

    assert client.get("/api/usage-allowances").status_code == 200
    assert {name: path.read_bytes() for name, path in cache_paths.items()} == before
