import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.claude_desktop_cloud_usage_cache import (
    MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS,
    STALE_THRESHOLD_SECONDS,
    CacheValidationError,
    load_snapshot,
    validate_cache_record,
    write_cache_atomic,
)
from app.main import app

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

SAMPLE_RECORD = {
    "schema_version": 1,
    "source": "claude_desktop_cloud_manual",
    "observed_at": NOW.isoformat(),
    "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
    "seven_day": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"},
}


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


def _base_record(**overrides) -> dict:
    record = json.loads(json.dumps(SAMPLE_RECORD))
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# cache module: load_snapshot / validate_cache_record / write_cache_atomic
# ---------------------------------------------------------------------------


def test_load_snapshot_missing_file_returns_not_observed(tmp_path: Path) -> None:
    snapshot = load_snapshot(now=NOW, path=tmp_path / "missing.json")
    assert snapshot["available"] is False
    assert snapshot["stale"] is False
    assert snapshot["status"] == "not_observed"
    assert snapshot["five_hour"] is None
    assert snapshot["seven_day"] is None


def test_load_snapshot_invalid_json_returns_invalid_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"
    assert snapshot["error_message"]


def test_load_snapshot_non_dict_root_returns_invalid_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["status"] == "invalid_cache"


def test_load_snapshot_valid_cache_returns_available_and_not_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is False
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"]["used_percentage"] == 40.0
    assert snapshot["seven_day"]["used_percentage"] == 20.0


def test_load_snapshot_old_observation_is_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS + 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["status"] == "stale"


def test_load_snapshot_just_under_threshold_is_not_stale(tmp_path: Path) -> None:
    record = _base_record(
        five_hour={**SAMPLE_RECORD["five_hour"], "resets_at": "2999-01-01T00:00:00+00:00"},
        seven_day={**SAMPLE_RECORD["seven_day"], "resets_at": "2999-01-08T00:00:00+00:00"},
    )
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(record, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS - 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["stale"] is False


def test_load_snapshot_stale_when_a_window_reset_time_has_passed(tmp_path: Path) -> None:
    record = _base_record()
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(record, cache_path)
    just_after_reset = datetime(2026, 1, 1, 17, 0, 1, tzinfo=timezone.utc)
    snapshot = load_snapshot(now=just_after_reset, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["status"] == "stale"
    assert snapshot["five_hour"]["used_percentage"] == 40.0


MALFORMED_CACHE_CASES = [
    ("five_hour_is_string", _base_record(five_hour="not-an-object")),
    ("seven_day_is_list", _base_record(seven_day=[1, 2, 3])),
    ("used_percentage_is_string", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": "40"})),
    ("used_percentage_is_bool", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": True})),
    ("used_percentage_out_of_range", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": 150})),
    (
        "remaining_percentage_is_bool",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "remaining_percentage": False}),
    ),
    (
        "remaining_percentage_out_of_range",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "remaining_percentage": -5}),
    ),
    (
        "used_plus_remaining_not_100",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": 40.0, "remaining_percentage": 40.0}),
    ),
    ("resets_at_invalid_string", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "resets_at": "not-a-date"})),
    (
        "resets_at_naive_iso",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "resets_at": "2026-01-01T17:00:00"}),
    ),
    ("observed_at_naive_iso", _base_record(observed_at="2026-01-01T12:00:00")),
    ("schema_version_missing", {k: v for k, v in _base_record().items() if k != "schema_version"}),
    ("schema_version_mismatch", _base_record(schema_version=2)),
    ("source_missing", {k: v for k, v in _base_record().items() if k != "source"}),
    ("source_mismatch", _base_record(source="something_else")),
    # a value from the CLI-auto cache's source string must never validate here
    ("source_is_cli_auto_source", _base_record(source="claude_code_statusline")),
    (
        "observed_at_far_future",
        _base_record(
            observed_at=(NOW + timedelta(seconds=MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS + 3600)).isoformat()
        ),
    ),
    # Unlike the CLI-auto cache, a manual snapshot must always be self-contained —
    # a missing window is rejected outright rather than treated as "not observed
    # for that window" (see the both-windows-required comment in
    # app/claude_desktop_cloud_usage_cache.py::validate_cache_record).
    ("five_hour_missing", _base_record(five_hour=None)),
    ("seven_day_missing", _base_record(seven_day=None)),
]
MALFORMED_CACHE_IDS = [case[0] for case in MALFORMED_CACHE_CASES]


@pytest.mark.parametrize("label,record", MALFORMED_CACHE_CASES, ids=MALFORMED_CACHE_IDS)
def test_load_snapshot_rejects_malformed_cache_as_invalid(tmp_path: Path, label: str, record: dict) -> None:
    cache_path = tmp_path / f"cache-{label}.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is False, label
    assert snapshot["stale"] is False, label
    assert snapshot["status"] == "invalid_cache", label
    assert snapshot["five_hour"] is None, label
    assert snapshot["seven_day"] is None, label
    assert snapshot["error_message"] == "usage cache could not be read", label


def test_load_snapshot_normalizes_aware_jst_observed_at_to_utc(tmp_path: Path) -> None:
    jst_observed_at = "2026-01-01T21:00:00+09:00"  # == 2026-01-01T12:00:00+00:00 == NOW
    record = _base_record(observed_at=jst_observed_at)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["observed_at"] == NOW.isoformat()


# Both windows are required for every manual snapshot (see the both-windows-required
# comment in app/claude_desktop_cloud_usage_cache.py::validate_cache_record) — a
# partial manual snapshot must be treated as invalid_cache, not as a legitimately
# partial observation the way the CLI-auto cache treats a missing window.
def test_load_snapshot_rejects_seven_day_missing(tmp_path: Path) -> None:
    record = _base_record(seven_day=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"
    assert snapshot["five_hour"] is None
    assert snapshot["seven_day"] is None


def test_load_snapshot_rejects_five_hour_missing(tmp_path: Path) -> None:
    record = _base_record(five_hour=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"
    assert snapshot["five_hour"] is None
    assert snapshot["seven_day"] is None


# observed_atが許容skew内の未来なら拒否しない(「observed_at_far_future」の境界を固定する)
def test_load_snapshot_allows_observed_at_within_skew_allowance(tmp_path: Path) -> None:
    record = _base_record(
        observed_at=(NOW + timedelta(seconds=MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS - 1)).isoformat()
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"


def test_load_snapshot_rejects_observed_at_far_future(tmp_path: Path) -> None:
    record = _base_record(
        observed_at=(NOW + timedelta(seconds=MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS + 3600)).isoformat()
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"


def test_validate_cache_record_normal_case_succeeds() -> None:
    validated = validate_cache_record(SAMPLE_RECORD, now=NOW)
    assert validated["five_hour"]["used_percentage"] == 40.0
    assert validated["seven_day"]["used_percentage"] == 20.0


def test_validate_cache_record_raises_cache_validation_error_on_bad_input() -> None:
    with pytest.raises(CacheValidationError):
        validate_cache_record({"schema_version": 999}, now=NOW)


def test_write_cache_atomic_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    assert cache_path.exists()
    leftover_tmp_files = list(tmp_path.glob(".tmp-claude-desktop-cloud-usage-*"))
    assert leftover_tmp_files == []
    assert json.loads(cache_path.read_text(encoding="utf-8"))["source"] == "claude_desktop_cloud_manual"


def test_resolve_cache_path_never_points_inside_repo_or_backups(tmp_path: Path) -> None:
    from app.claude_desktop_cloud_usage_cache import resolve_cache_path

    fake_env = {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
    resolved = resolve_cache_path(env=fake_env)
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in resolved.parents
    assert "backups" not in resolved.parts
    assert resolved.name == "claude-desktop-cloud-usage.json"


# ---------------------------------------------------------------------------
# GET /api/claude-code-usage/manual
# ---------------------------------------------------------------------------


def test_get_claude_desktop_cloud_usage_endpoint_never_runs_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("GET /api/claude-code-usage/manual must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage/manual")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "not_observed"


def test_get_claude_desktop_cloud_usage_endpoint_returns_cached_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "claude-desktop-cloud-usage.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage/manual")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["stale"] is False
    assert body["source"] == "claude_desktop_cloud_manual"
    assert body["five_hour"]["used_percentage"] == 40.0
    assert body["seven_day"]["remaining_percentage"] == 80.0


@pytest.mark.parametrize("label,record", MALFORMED_CACHE_CASES, ids=MALFORMED_CACHE_IDS)
def test_get_claude_desktop_cloud_usage_endpoint_never_500s_on_malformed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, record: dict
) -> None:
    cache_path = tmp_path / f"cache-{label}.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage/manual")

    assert response.status_code == 200, label
    body = response.json()
    assert body["available"] is False, label
    assert body["status"] == "invalid_cache", label


def test_get_claude_desktop_cloud_usage_endpoint_never_leaks_raw_cache_or_token_like_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_marker = "claude-desktop-session-TOKEN-SHOULD-NEVER-LEAK"
    record = _base_record(five_hour="not-an-object")
    record["unexpected_debug_field"] = {"token": secret_marker}
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage/manual")

    assert response.status_code == 200
    assert secret_marker not in response.text
    assert "not-an-object" not in response.text
    body = response.json()
    assert body["error_message"] == "usage cache could not be read"


def test_get_claude_desktop_cloud_usage_endpoint_is_independent_of_cli_auto_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point both caches at distinct, isolated files with different content, then
    # confirm the manual endpoint only ever reflects its own file.
    auto_cache_path = tmp_path / "claude-code-usage.json"
    manual_cache_path = tmp_path / "claude-desktop-cloud-usage.json"
    write_cache_atomic(
        {
            "schema_version": 1,
            "source": "claude_code_statusline",
            "observed_at": NOW.isoformat(),
            "five_hour": {"used_percentage": 5.0, "remaining_percentage": 95.0, "resets_at": "2026-01-01T17:00:00+00:00"},
            "seven_day": None,
        },
        auto_cache_path,
    )
    write_cache_atomic(SAMPLE_RECORD, manual_cache_path)
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: auto_cache_path)
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: manual_cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        auto_response = client.get("/api/claude-code-usage")
        manual_response = client.get("/api/claude-code-usage/manual")

    assert auto_response.json()["five_hour"]["used_percentage"] == 5.0
    assert manual_response.json()["five_hour"]["used_percentage"] == 40.0


# ---------------------------------------------------------------------------
# GET /api/claude-code-usage backward compatibility (existing CLI-auto endpoint)
# ---------------------------------------------------------------------------


def test_get_claude_code_usage_endpoint_unchanged_shape_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_code_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "claude-code-usage.json"
    )

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "available",
        "stale",
        "status",
        "observed_at",
        "source",
        "five_hour",
        "seven_day",
        "error_message",
    }
    assert body["available"] is False
    assert body["status"] == "not_observed"


# ---------------------------------------------------------------------------
# PUT /api/claude-code-usage/manual
# ---------------------------------------------------------------------------


def _put(client: TestClient, payload: dict):
    return client.put("/api/claude-code-usage/manual", json=payload)


def test_put_claude_desktop_cloud_usage_saves_both_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "claude-desktop-cloud-usage.json"
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": {"remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"},
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["source"] == "claude_desktop_cloud_manual"
    assert body["five_hour"]["remaining_percentage"] == 60.0
    assert body["seven_day"]["remaining_percentage"] == 80.0
    assert body["observed_at"] == NOW.isoformat()
    assert cache_path.exists()


DEFAULT_SEVEN_DAY = {"remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"}
DEFAULT_FIVE_HOUR = {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"}


def test_put_claude_desktop_cloud_usage_rejects_five_hour_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Snapshot selection (resolveClaudeCodeUsageDisplay in static/compact.js) always
    # picks one snapshot as a whole unit by observed_at — never mixing windows across
    # auto/manual. A partial manual snapshot that's newer than a complete auto snapshot
    # would therefore silently hide a window the user could previously see, so both
    # windows are required on every manual save.
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(client, {"five_hour": DEFAULT_FIVE_HOUR})

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_rejects_seven_day_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(client, {"seven_day": DEFAULT_SEVEN_DAY})

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_rejects_both_windows_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(client, {})

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_accepts_remaining_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 0.0


def test_put_claude_desktop_cloud_usage_accepts_remaining_hundred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 100, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 100.0


@pytest.mark.parametrize("bad_value", [-1, 100.1, 999])
def test_put_claude_desktop_cloud_usage_rejects_remaining_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": bad_value, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422


STRICT_REJECT_CASES = [
    ("numeric_string_int", "42"),
    ("numeric_string_float", "42.0"),
    ("empty_string", ""),
    ("null", None),
    ("bool_true", True),
    ("bool_false", False),
    ("list", [42]),
    ("dict", {"value": 42}),
]
STRICT_REJECT_IDS = [case[0] for case in STRICT_REJECT_CASES]


@pytest.mark.parametrize("label,bad_value", STRICT_REJECT_CASES, ids=STRICT_REJECT_IDS)
def test_put_claude_desktop_cloud_usage_five_hour_rejects_non_numeric_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, bad_value: object
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": bad_value, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422, label


@pytest.mark.parametrize("label,bad_value", STRICT_REJECT_CASES, ids=STRICT_REJECT_IDS)
def test_put_claude_desktop_cloud_usage_seven_day_rejects_non_numeric_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, bad_value: object
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": DEFAULT_FIVE_HOUR,
                "seven_day": {"remaining_percentage": bad_value, "resets_at": "2026-01-08T00:00:00+00:00"},
            },
        )

    assert response.status_code == 422, label


def test_put_claude_desktop_cloud_usage_accepts_plain_int_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text


def test_put_claude_desktop_cloud_usage_accepts_plain_int_hundred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 100, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text


def test_put_claude_desktop_cloud_usage_accepts_float(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 42.5, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 42.5


def test_put_claude_desktop_cloud_usage_rejects_unknown_root_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": DEFAULT_FIVE_HOUR,
                "seven_day": DEFAULT_SEVEN_DAY,
                "source": "claude_desktop_cloud_manual",
            },
        )

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_rejects_token_like_root_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": DEFAULT_FIVE_HOUR,
                "seven_day": DEFAULT_SEVEN_DAY,
                "token": "sk-ant-api03-SHOULD-NEVER-BE-ACCEPTED",
            },
        )

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_error_response_never_echoes_token_like_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": DEFAULT_FIVE_HOUR,
                "seven_day": DEFAULT_SEVEN_DAY,
                "token": secret_marker,
            },
        )

    assert response.status_code == 422
    assert secret_marker not in response.text


def test_put_claude_desktop_cloud_usage_error_response_never_echoes_rejected_window_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": secret_marker, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422
    assert secret_marker not in response.text


def test_put_claude_desktop_cloud_usage_computes_used_percentage_server_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 63.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["five_hour"]["used_percentage"] == 37.0

    schema_fields = set(response.json()["five_hour"].keys())
    assert schema_fields == {"used_percentage", "remaining_percentage", "resets_at"}


def test_put_claude_desktop_cloud_usage_rejects_client_supplied_used_percentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {
                    "remaining_percentage": 63.0,
                    "used_percentage": 1.0,
                    "resets_at": "2026-01-01T17:00:00+00:00",
                },
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_rejects_naive_resets_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_rejects_invalid_resets_at_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "not-a-date"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 422


def test_put_claude_desktop_cloud_usage_normalizes_aware_jst_resets_at_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-02T02:00:00+09:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["resets_at"] == "2026-01-01T17:00:00+00:00"


def test_put_claude_desktop_cloud_usage_sets_observed_at_to_current_utc_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    observed_at = response.json()["observed_at"]
    assert observed_at == NOW.isoformat()
    assert datetime.fromisoformat(observed_at).tzinfo is not None


def test_put_claude_desktop_cloud_usage_never_runs_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("PUT /api/claude-code-usage/manual must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text


def test_put_claude_desktop_cloud_usage_validation_error_has_no_traceback_or_internal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.claude_desktop_cloud_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-desktop-cloud-usage.json",
    )

    with TestClient(app) as client:
        response = _put(client, {"five_hour": {"remaining_percentage": "not-a-number", "resets_at": "not-a-date"}})

    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert "claude_desktop_cloud_usage_cache.py" not in response.text
    assert "site-packages" not in response.text


def test_put_claude_desktop_cloud_usage_does_not_affect_cli_auto_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Writing a manual snapshot must never touch the separate CLI-auto cache file.
    auto_cache_path = tmp_path / "claude-code-usage.json"
    manual_cache_path = tmp_path / "claude-desktop-cloud-usage.json"
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: auto_cache_path)
    monkeypatch.setattr("app.claude_desktop_cloud_usage_cache.resolve_cache_path", lambda env=None: manual_cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "seven_day": DEFAULT_SEVEN_DAY,
            },
        )

    assert response.status_code == 200, response.text
    assert not auto_cache_path.exists()
    assert manual_cache_path.exists()
