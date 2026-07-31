import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.claude_code_usage_cache import (
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
    "source": "claude_code_statusline",
    "observed_at": NOW.isoformat(),
    "five_hour": {"used_percentage": 42.0, "remaining_percentage": 58.0, "resets_at": "2026-01-01T17:00:00+00:00"},
    "seven_day": {"used_percentage": 18.0, "remaining_percentage": 82.0, "resets_at": "2026-01-07T12:00:00+00:00"},
}


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


# 18. cacheなし
def test_load_snapshot_missing_file_returns_not_observed(tmp_path: Path) -> None:
    snapshot = load_snapshot(now=NOW, path=tmp_path / "missing.json")
    assert snapshot["available"] is False
    assert snapshot["stale"] is False
    assert snapshot["status"] == "not_observed"
    assert snapshot["five_hour"] is None
    assert snapshot["seven_day"] is None


# 19. cache不正
def test_load_snapshot_invalid_json_returns_invalid_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"
    assert snapshot["error_message"]


# 20. cache正常
def test_load_snapshot_valid_cache_returns_available_and_not_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is False
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"]["used_percentage"] == 42.0
    assert snapshot["seven_day"]["used_percentage"] == 18.0


# 21. stale判定
def test_load_snapshot_old_observation_is_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS + 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["status"] == "stale"
    # five_hour resets_at (17:00) has already passed by `later`, but with no new
    # observation the cache must stay "stale", not silently look fresh again.
    assert snapshot["five_hour"]["used_percentage"] == 42.0


def test_load_snapshot_just_under_threshold_is_not_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS - 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["stale"] is False


# 23. GET APIが外部処理を実行しない
def test_get_claude_code_usage_endpoint_never_runs_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("GET /api/claude-code-usage must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(
        "app.claude_code_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "claude-code-usage.json",
    )

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "not_observed"


def test_get_claude_code_usage_endpoint_returns_cached_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "claude-code-usage.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["stale"] is False
    assert body["five_hour"]["used_percentage"] == 42.0
    assert body["seven_day"]["remaining_percentage"] == 82.0


def _base_record(**overrides) -> dict:
    record = json.loads(json.dumps(SAMPLE_RECORD))
    record.update(overrides)
    return record


MALFORMED_CACHE_CASES = [
    ("five_hour_is_string", _base_record(five_hour="not-an-object")),
    ("seven_day_is_list", _base_record(seven_day=[1, 2, 3])),
    ("used_percentage_is_string", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": "42"})),
    ("used_percentage_is_bool", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": True})),
    ("used_percentage_out_of_range", _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": 150})),
    (
        "remaining_percentage_is_string",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "remaining_percentage": "58"}),
    ),
    (
        "remaining_percentage_out_of_range",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "remaining_percentage": -5}),
    ),
    (
        "used_plus_remaining_not_100",
        _base_record(five_hour={**SAMPLE_RECORD["five_hour"], "used_percentage": 42.0, "remaining_percentage": 40.0}),
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
    (
        "observed_at_far_future",
        _base_record(
            observed_at=(NOW + timedelta(seconds=MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS + 3600)).isoformat()
        ),
    ),
]
MALFORMED_CACHE_IDS = [case[0] for case in MALFORMED_CACHE_CASES]


# 1-16 (各不正パターン) / 17. 上記すべてでload_snapshot()がinvalid_cache
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


# 18. 上記すべてでGET /api/claude-code-usageが500にならない
@pytest.mark.parametrize("label,record", MALFORMED_CACHE_CASES, ids=MALFORMED_CACHE_IDS)
def test_get_claude_code_usage_endpoint_never_500s_on_malformed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, record: dict
) -> None:
    cache_path = tmp_path / f"cache-{label}.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage")

    assert response.status_code == 200, label
    body = response.json()
    assert body["available"] is False, label
    assert body["status"] == "invalid_cache", label


# 19. APIレスポンスにcache原文・token風文字列が含まれない
def test_get_claude_code_usage_endpoint_never_leaks_raw_cache_or_token_like_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"
    record = _base_record(five_hour="not-an-object")
    record["unexpected_debug_field"] = {"token": secret_marker}
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.claude_code_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/claude-code-usage")

    assert response.status_code == 200
    assert secret_marker not in response.text
    assert "not-an-object" not in response.text
    body = response.json()
    assert body["error_message"] == "usage cache could not be read"


# 20. awareなJST日時をUTCへ正規化
def test_load_snapshot_normalizes_aware_jst_observed_at_to_utc(tmp_path: Path) -> None:
    jst_observed_at = "2026-01-01T21:00:00+09:00"  # == 2026-01-01T12:00:00+00:00 == NOW
    record = _base_record(observed_at=jst_observed_at)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["observed_at"] == NOW.isoformat()


# observed_atが許容skew内の未来なら拒否しない(「16. 大きく未来のobserved_at」の境界を固定する)
def test_load_snapshot_allows_observed_at_within_skew_allowance(tmp_path: Path) -> None:
    record = _base_record(
        observed_at=(NOW + timedelta(seconds=MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS - 1)).isoformat()
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"


# 22. 片方window欠落は正常として許容
def test_load_snapshot_allows_one_window_missing(tmp_path: Path) -> None:
    record = _base_record(seven_day=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"] is not None
    assert snapshot["seven_day"] is None


# 23. rate_limits未観測cacheは仕様どおり扱う(最初のAPI応答前に書かれたcache: 両方null)
def test_load_snapshot_allows_both_windows_null_before_first_response(tmp_path: Path) -> None:
    record = _base_record(five_hour=None, seven_day=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"] is None
    assert snapshot["seven_day"] is None


def test_validate_cache_record_normal_case_succeeds() -> None:
    validated = validate_cache_record(SAMPLE_RECORD, now=NOW)
    assert validated["five_hour"]["used_percentage"] == 42.0
    assert validated["seven_day"]["used_percentage"] == 18.0


def test_validate_cache_record_raises_cache_validation_error_on_bad_input() -> None:
    with pytest.raises(CacheValidationError):
        validate_cache_record({"schema_version": 999}, now=NOW)
