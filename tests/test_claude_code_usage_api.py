import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.claude_code_usage_cache import (
    STALE_THRESHOLD_SECONDS,
    load_snapshot,
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
