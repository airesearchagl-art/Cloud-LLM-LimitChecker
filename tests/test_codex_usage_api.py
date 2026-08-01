import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.codex_usage_cache import (
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
    "source": "codex_manual",
    "observed_at": NOW.isoformat(),
    "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
    "weekly": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"},
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


# 16. cacheなし
def test_load_snapshot_missing_file_returns_not_observed(tmp_path: Path) -> None:
    snapshot = load_snapshot(now=NOW, path=tmp_path / "missing.json")
    assert snapshot["available"] is False
    assert snapshot["stale"] is False
    assert snapshot["status"] == "not_observed"
    assert snapshot["five_hour"] is None
    assert snapshot["weekly"] is None


# 17. cache不正JSON
def test_load_snapshot_invalid_json_returns_invalid_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    snapshot = load_snapshot(now=NOW, path=cache_path)
    assert snapshot["available"] is False
    assert snapshot["status"] == "invalid_cache"
    assert snapshot["error_message"]


# 18. cache構造不正
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
    assert snapshot["weekly"]["used_percentage"] == 20.0


# 25. stale 24時間
def test_load_snapshot_old_observation_is_stale(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS + 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["status"] == "stale"


def test_load_snapshot_just_under_threshold_is_not_stale(tmp_path: Path) -> None:
    # Both windows' resets_at are far in the future here, so only the pure
    # 24h age threshold is under test (not the reset-exceeded rule).
    record = _base_record(
        five_hour={**SAMPLE_RECORD["five_hour"], "resets_at": "2999-01-01T00:00:00+00:00"},
        weekly={**SAMPLE_RECORD["weekly"], "resets_at": "2999-01-08T00:00:00+00:00"},
    )
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(record, cache_path)
    later = NOW + timedelta(seconds=STALE_THRESHOLD_SECONDS - 1)
    snapshot = load_snapshot(now=later, path=cache_path)
    assert snapshot["stale"] is False


# 26. reset超過でstale (24時間未満でも、いずれかの枠のresets_atを過ぎていればstale)
def test_load_snapshot_stale_when_a_window_reset_time_has_passed(tmp_path: Path) -> None:
    record = _base_record()
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(record, cache_path)
    # five_hour resets_at is 2026-01-01T17:00:00+00:00; observe from just after that,
    # well within the 24h stale threshold.
    just_after_reset = datetime(2026, 1, 1, 17, 0, 1, tzinfo=timezone.utc)
    snapshot = load_snapshot(now=just_after_reset, path=cache_path)
    assert snapshot["available"] is True
    assert snapshot["stale"] is True
    assert snapshot["status"] == "stale"
    # the old percentage must still be returned (not hidden), display layer decides how to show it
    assert snapshot["five_hour"]["used_percentage"] == 40.0


# 19. schema_version不一致 / 20. source不一致 / その他不正パターン
MALFORMED_CACHE_CASES = [
    ("five_hour_is_string", _base_record(five_hour="not-an-object")),
    ("weekly_is_list", _base_record(weekly=[1, 2, 3])),
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
        # 21. percentage不整合
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
    assert snapshot["weekly"] is None, label
    assert snapshot["error_message"] == "usage cache could not be read", label


# 13. awareなJST日時をUTCへ正規化
def test_load_snapshot_normalizes_aware_jst_observed_at_to_utc(tmp_path: Path) -> None:
    jst_observed_at = "2026-01-01T21:00:00+09:00"  # == 2026-01-01T12:00:00+00:00 == NOW
    record = _base_record(observed_at=jst_observed_at)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["observed_at"] == NOW.isoformat()


# 2. 5時間枠だけ保存 / 片方window欠落は正常として許容
def test_load_snapshot_allows_one_window_missing(tmp_path: Path) -> None:
    record = _base_record(weekly=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"] is not None
    assert snapshot["weekly"] is None


# 3. 週次枠だけ保存
def test_load_snapshot_allows_only_weekly_window(tmp_path: Path) -> None:
    record = _base_record(five_hour=None)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")

    snapshot = load_snapshot(now=NOW, path=cache_path)

    assert snapshot["available"] is True
    assert snapshot["status"] == "ok"
    assert snapshot["five_hour"] is None
    assert snapshot["weekly"] is not None


def test_validate_cache_record_normal_case_succeeds() -> None:
    validated = validate_cache_record(SAMPLE_RECORD, now=NOW)
    assert validated["five_hour"]["used_percentage"] == 40.0
    assert validated["weekly"]["used_percentage"] == 20.0


def test_validate_cache_record_raises_cache_validation_error_on_bad_input() -> None:
    with pytest.raises(CacheValidationError):
        validate_cache_record({"schema_version": 999}, now=NOW)


# 15. atomic write: a temp file is used and cleaned up, and the final file replaces atomically
def test_write_cache_atomic_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    assert cache_path.exists()
    leftover_tmp_files = list(tmp_path.glob(".tmp-codex-usage-*"))
    assert leftover_tmp_files == []
    assert json.loads(cache_path.read_text(encoding="utf-8"))["source"] == "codex_manual"


# ---------------------------------------------------------------------------
# GET /api/codex-usage
# ---------------------------------------------------------------------------


# 22. GETは外部処理なし
def test_get_codex_usage_endpoint_never_runs_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("GET /api/codex-usage must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(
        "app.codex_usage_cache.resolve_cache_path",
        lambda env=None: tmp_path / "codex-usage.json",
    )

    with TestClient(app) as client:
        response = client.get("/api/codex-usage")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "not_observed"


def test_get_codex_usage_endpoint_returns_cached_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "codex-usage.json"
    write_cache_atomic(SAMPLE_RECORD, cache_path)
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/codex-usage")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["stale"] is False
    assert body["five_hour"]["used_percentage"] == 40.0
    assert body["weekly"]["remaining_percentage"] == 80.0


@pytest.mark.parametrize("label,record", MALFORMED_CACHE_CASES, ids=MALFORMED_CACHE_IDS)
def test_get_codex_usage_endpoint_never_500s_on_malformed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, record: dict
) -> None:
    cache_path = tmp_path / f"cache-{label}.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/codex-usage")

    assert response.status_code == 200, label
    body = response.json()
    assert body["available"] is False, label
    assert body["status"] == "invalid_cache", label


# 24. raw request/token風文字列非表示
def test_get_codex_usage_endpoint_never_leaks_raw_cache_or_token_like_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_marker = "codex-session-TOKEN-SHOULD-NEVER-LEAK"
    record = _base_record(five_hour="not-an-object")
    record["unexpected_debug_field"] = {"token": secret_marker}
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = client.get("/api/codex-usage")

    assert response.status_code == 200
    assert secret_marker not in response.text
    assert "not-an-object" not in response.text
    body = response.json()
    assert body["error_message"] == "usage cache could not be read"


# ---------------------------------------------------------------------------
# PUT /api/codex-usage
# ---------------------------------------------------------------------------


def _put(client: TestClient, payload: dict):
    return client.put("/api/codex-usage", json=payload)


# 1. 正常な5時間枠・週次枠保存
def test_put_codex_usage_saves_both_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "codex-usage.json"
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "weekly": {"remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"},
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["five_hour"]["remaining_percentage"] == 60.0
    assert body["weekly"]["remaining_percentage"] == 80.0
    assert body["observed_at"] == NOW.isoformat()
    assert cache_path.exists()


# 2. 5時間枠だけ保存
def test_put_codex_usage_saves_five_hour_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "codex-usage.json"
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["five_hour"] is not None
    assert body["weekly"] is None


# 3. 週次枠だけ保存
def test_put_codex_usage_saves_weekly_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "codex-usage.json"
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: cache_path)
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(client, {"weekly": {"remaining_percentage": 80.0, "resets_at": "2026-01-08T00:00:00+00:00"}})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["five_hour"] is None
    assert body["weekly"] is not None


# 4. 両方未入力を拒否
def test_put_codex_usage_rejects_both_windows_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(client, {})

    assert response.status_code == 422


# 5. remaining 0 (境界値、正常)
def test_put_codex_usage_accepts_remaining_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 0, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 0.0


# 6. remaining 100 (境界値、正常)
def test_put_codex_usage_accepts_remaining_hundred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 100, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 100.0


# 7. remaining範囲外
@pytest.mark.parametrize("bad_value", [-1, 100.1, 999])
def test_put_codex_usage_rejects_remaining_out_of_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": bad_value, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 422


# 8. remainingがbool
def test_put_codex_usage_rejects_remaining_bool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": True, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 422


# 9. remainingがstring
def test_put_codex_usage_rejects_remaining_non_numeric_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": "not-a-number", "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 422


# strict validation: remaining_percentage accepts only bool-excluded int/float,
# never a numeric string, empty string, null, list, or dict — for both windows.
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
def test_put_codex_usage_five_hour_rejects_non_numeric_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, bad_value: object
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": bad_value, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 422, label


@pytest.mark.parametrize("label,bad_value", STRICT_REJECT_CASES, ids=STRICT_REJECT_IDS)
def test_put_codex_usage_weekly_rejects_non_numeric_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, bad_value: object
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client, {"weekly": {"remaining_percentage": bad_value, "resets_at": "2026-01-08T00:00:00+00:00"}}
        )

    assert response.status_code == 422, label


# int 0 / int 100 / float 42.5 are all accepted (bool-excluded int or float only)
def test_put_codex_usage_accepts_plain_int_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(client, {"five_hour": {"remaining_percentage": 0, "resets_at": "2026-01-01T17:00:00+00:00"}})

    assert response.status_code == 200, response.text


def test_put_codex_usage_accepts_plain_int_hundred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 100, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text


def test_put_codex_usage_accepts_float(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 42.5, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["remaining_percentage"] == 42.5


# rootの未定義field / token風fieldを422
def test_put_codex_usage_rejects_unknown_root_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "source": "codex_manual",
            },
        )

    assert response.status_code == 422


def test_put_codex_usage_rejects_token_like_root_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "token": "sk-ant-api03-SHOULD-NEVER-BE-ACCEPTED",
            },
        )

    assert response.status_code == 422


# token風値をレスポンスへ出さない(未定義fieldの値としてtoken風文字列を送っても本文へ反映しない)
def test_put_codex_usage_error_response_never_echoes_token_like_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"},
                "token": secret_marker,
            },
        )

    assert response.status_code == 422
    assert secret_marker not in response.text


def test_put_codex_usage_error_response_never_echoes_rejected_window_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": secret_marker, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 422
    assert secret_marker not in response.text


# 10. usedをサーバー側で計算
def test_put_codex_usage_computes_used_percentage_server_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 63.0, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["five_hour"]["used_percentage"] == 37.0

    # confirm the raw request body has no used_percentage field accepted at all
    schema_fields = set(response.json()["five_hour"].keys())
    assert schema_fields == {"used_percentage", "remaining_percentage", "resets_at"}


# used_percentage passed by the client must simply be ignored/rejected as an unknown field,
# never trusted over the server-computed value.
def test_put_codex_usage_rejects_client_supplied_used_percentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(
            client,
            {
                "five_hour": {
                    "remaining_percentage": 63.0,
                    "used_percentage": 1.0,  # attacker-controlled value, must be rejected outright
                    "resets_at": "2026-01-01T17:00:00+00:00",
                }
            },
        )

    assert response.status_code == 422


# 11. resetがnaive
def test_put_codex_usage_rejects_naive_resets_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00"}})

    assert response.status_code == 422


# 12. resetが不正文字列
def test_put_codex_usage_rejects_invalid_resets_at_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "not-a-date"}})

    assert response.status_code == 422


# 13. aware JSTをUTC正規化
def test_put_codex_usage_normalizes_aware_jst_resets_at_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-02T02:00:00+09:00"}}
        )

    assert response.status_code == 200, response.text
    assert response.json()["five_hour"]["resets_at"] == "2026-01-01T17:00:00+00:00"


# 14. observed_atがUTC aware
def test_put_codex_usage_sets_observed_at_to_current_utc_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text
    observed_at = response.json()["observed_at"]
    assert observed_at == NOW.isoformat()
    assert datetime.fromisoformat(observed_at).tzinfo is not None


# 23. PUTはCodexを実行しない
def test_put_codex_usage_never_runs_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("PUT /api/codex-usage must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")
    monkeypatch.setattr("app.main._current_utc_time", lambda: NOW)

    with TestClient(app) as client:
        response = _put(
            client, {"five_hour": {"remaining_percentage": 60.0, "resets_at": "2026-01-01T17:00:00+00:00"}}
        )

    assert response.status_code == 200, response.text


# PUT validation errors are structured pydantic errors, never a raw traceback or internal file path
def test_put_codex_usage_validation_error_has_no_traceback_or_internal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage.json")

    with TestClient(app) as client:
        response = _put(client, {"five_hour": {"remaining_percentage": "not-a-number", "resets_at": "not-a-date"}})

    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert "codex_usage_cache.py" not in response.text
    assert "site-packages" not in response.text


# 39. backups/を変更しない (cache解決は常にLOCALAPPDATA配下のみで、repo/backups配下を返さない)
def test_resolve_cache_path_never_points_inside_repo_or_backups(tmp_path: Path) -> None:
    from app.codex_usage_cache import resolve_cache_path

    fake_env = {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
    resolved = resolve_cache_path(env=fake_env)
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in resolved.parents
    assert "backups" not in resolved.parts
    assert resolved.name == "codex-usage.json"
