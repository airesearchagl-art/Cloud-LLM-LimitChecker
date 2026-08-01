import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codex_rate_limits_cache
from app import codex_usage_cache
from app.codex_rate_limits_adapter import CodexRateLimitsFetchResult
from app.codex_rate_limits_state import CodexRateLimitsController
from app.main import app

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

FIVE_HOUR_WINDOW = {
    "used_percentage": 42.0,
    "remaining_percentage": 58.0,
    "resets_at": "2026-01-01T17:00:00+00:00",
    "window_duration_minutes": 300,
}
WEEKLY_WINDOW = {
    "used_percentage": 18.0,
    "remaining_percentage": 82.0,
    "resets_at": "2026-01-08T00:00:00+00:00",
    "window_duration_minutes": 10080,
}

SAMPLE_RECORD = {
    "schema_version": 1,
    "source": "codex_app_server",
    "observed_at": NOW.isoformat(),
    "five_hour": FIVE_HOUR_WINDOW,
    "weekly": WEEKLY_WINDOW,
}


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


@pytest.fixture()
def rl_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app.state.codex_rate_limits_controller = CodexRateLimitsController()
    monkeypatch.setattr(
        "app.codex_rate_limits_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-rate-limits.json"
    )
    monkeypatch.setattr(
        "app.codex_usage_cache.resolve_cache_path", lambda env=None: tmp_path / "codex-usage-manual.json"
    )
    with TestClient(app) as client:
        yield client


def fake_success(windows: dict):
    def fetch(*, now=None, **kwargs):
        return CodexRateLimitsFetchResult(
            success=True, windows=windows, error_type=None, user_message=None, collected_at=now
        )

    return fetch


def fake_failure(error_type: str, user_message: str):
    def fetch(*, now=None, **kwargs):
        return CodexRateLimitsFetchResult(
            success=False, windows=None, error_type=error_type, user_message=user_message, collected_at=now
        )

    return fetch


def set_now(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr("app.main._current_utc_time", lambda: value)


# ---------------------------------------------------------------------------
# 29/30: GETはsubprocessなし・POSTだけがadapterを呼ぶ
# ---------------------------------------------------------------------------


def test_get_never_calls_fetch(rl_client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("GET /api/codex-rate-limits must never call the adapter")

    monkeypatch.setattr("app.main.fetch_codex_rate_limits", explode)

    response = rl_client.get("/api/codex-rate-limits")

    assert response.status_code == 200
    assert response.json()["fetched"] is False


def test_get_initial_state_not_observed(rl_client):
    response = rl_client.get("/api/codex-rate-limits")
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "not_observed"
    assert body["five_hour"] is None
    assert body["weekly"] is None
    assert body["refresh_in_progress"] is False
    assert body["cooldown_remaining_seconds"] == 0


# ---------------------------------------------------------------------------
# 26/27: 成功時のみcache更新 / 失敗時に既存cache維持
# ---------------------------------------------------------------------------


def test_refresh_success_writes_cache_and_get_reflects_it(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)

    response = rl_client.post("/api/codex-rate-limits/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["available"] is True
    assert body["status"] == "ok"
    assert body["source"] == "codex_app_server"
    assert body["five_hour"]["used_percentage"] == 42.0
    assert body["weekly"]["used_percentage"] == 18.0

    get_response = rl_client.get("/api/codex-rate-limits")
    assert get_response.json()["five_hour"]["used_percentage"] == 42.0


def test_refresh_failure_does_not_overwrite_existing_cache(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)
    first = rl_client.post("/api/codex-rate-limits/refresh")
    assert first.status_code == 200

    monkeypatch.setattr("app.main.fetch_codex_rate_limits", fake_failure("rate_limits_timeout", "timed out"))
    set_now(monkeypatch, NOW + timedelta(seconds=60))
    second = rl_client.post("/api/codex-rate-limits/refresh")
    body = second.json()

    assert body["available"] is True
    assert body["five_hour"]["used_percentage"] == 42.0  # unchanged from the successful attempt
    assert body["error_type"] == "rate_limits_timeout"


def test_refresh_failure_reports_error_type_and_message(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_failure("executable_not_found", "Codex CLI (codex) is not installed or not on PATH.")
    )
    set_now(monkeypatch, NOW)

    response = rl_client.post("/api/codex-rate-limits/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["available"] is False
    assert body["error_type"] == "executable_not_found"
    assert body["user_message"] == "Codex CLI (codex) is not installed or not on PATH."


# ---------------------------------------------------------------------------
# 31-33: cooldown成功時/失敗時、同時実行拒否
# ---------------------------------------------------------------------------


def test_refresh_immediately_again_is_rejected_with_cooldown(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)
    first = rl_client.post("/api/codex-rate-limits/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=5))
    second = rl_client.post("/api/codex-rate-limits/refresh")

    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["error_type"] == "cooldown_active"
    assert detail["retry_after_seconds"] > 0


def test_cooldown_applies_after_failed_attempt_too(rl_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_codex_rate_limits", fake_failure("rate_limits_timeout", "timed out"))
    set_now(monkeypatch, NOW)
    first = rl_client.post("/api/codex-rate-limits/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=5))
    second = rl_client.post("/api/codex-rate-limits/refresh")
    assert second.status_code == 429


def test_refresh_after_cooldown_elapses_is_allowed(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)
    first = rl_client.post("/api/codex-rate-limits/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=31))
    second = rl_client.post("/api/codex-rate-limits/refresh")
    assert second.status_code == 200


def test_concurrent_refresh_is_rejected_while_in_progress(rl_client, monkeypatch):
    controller = app.state.codex_rate_limits_controller

    def fetch_that_marks_in_progress(*, now=None, **kwargs):
        inner_response = rl_client.post("/api/codex-rate-limits/refresh")
        assert inner_response.status_code == 429
        assert inner_response.json()["detail"]["error_type"] == "already_refreshing"
        return CodexRateLimitsFetchResult(
            success=True,
            windows={"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW},
            error_type=None,
            user_message=None,
            collected_at=now,
        )

    monkeypatch.setattr("app.main.fetch_codex_rate_limits", fetch_that_marks_in_progress)
    set_now(monkeypatch, NOW)

    outer_response = rl_client.post("/api/codex-rate-limits/refresh")
    assert outer_response.status_code == 200
    assert controller._refreshing is False


# ---------------------------------------------------------------------------
# 34-37: 自動cache優先 / stale自動cache表示 / 手動fallback / 両cacheなし
# ---------------------------------------------------------------------------


def test_fallback_available_true_when_manual_snapshot_exists(rl_client, monkeypatch, tmp_path):
    manual_path = tmp_path / "codex-usage-manual.json"
    manual_record = {
        "schema_version": 1,
        "source": "codex_manual",
        "observed_at": NOW.isoformat(),
        "five_hour": {"used_percentage": 10.0, "remaining_percentage": 90.0, "resets_at": "2026-01-01T17:00:00+00:00"},
        "weekly": None,
    }
    codex_usage_cache.write_cache_atomic(manual_record, manual_path)
    set_now(monkeypatch, NOW)

    response = rl_client.get("/api/codex-rate-limits")
    body = response.json()

    assert body["available"] is False  # auto cache itself is still empty
    assert body["fallback_available"] is True
    assert body["fallback_source"] == "codex_manual"


def test_fallback_unavailable_when_neither_cache_exists(rl_client, monkeypatch):
    set_now(monkeypatch, NOW)
    response = rl_client.get("/api/codex-rate-limits")
    body = response.json()
    assert body["available"] is False
    assert body["fallback_available"] is False
    assert body["fallback_source"] is None


def test_stale_auto_cache_still_reports_available_true(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits",
        fake_success(
            {
                "five_hour": {**FIVE_HOUR_WINDOW, "resets_at": "2999-01-01T00:00:00+00:00"},
                "weekly": {**WEEKLY_WINDOW, "resets_at": "2999-01-08T00:00:00+00:00"},
            }
        ),
    )
    set_now(monkeypatch, NOW)
    rl_client.post("/api/codex-rate-limits/refresh")

    later = NOW + timedelta(seconds=codex_rate_limits_cache.STALE_THRESHOLD_SECONDS + 1)
    set_now(monkeypatch, later)
    response = rl_client.get("/api/codex-rate-limits")
    body = response.json()

    assert body["available"] is True
    assert body["stale"] is True
    assert body["status"] == "stale"
    assert body["five_hour"]["used_percentage"] == 42.0  # last known value preserved


# ---------------------------------------------------------------------------
# 手動cache非変更 / 認証情報field非表示 / credits非表示
# ---------------------------------------------------------------------------


def test_manual_cache_is_never_touched_by_auto_refresh(rl_client, monkeypatch, tmp_path):
    manual_path = tmp_path / "codex-usage-manual.json"
    manual_record = {
        "schema_version": 1,
        "source": "codex_manual",
        "observed_at": NOW.isoformat(),
        "five_hour": {"used_percentage": 10.0, "remaining_percentage": 90.0, "resets_at": "2026-01-01T17:00:00+00:00"},
        "weekly": None,
    }
    codex_usage_cache.write_cache_atomic(manual_record, manual_path)
    before = manual_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)
    rl_client.post("/api/codex-rate-limits/refresh")

    after = manual_path.read_text(encoding="utf-8")
    assert before == after


def test_response_never_contains_credits_or_reset_credit_fields(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)

    response = rl_client.post("/api/codex-rate-limits/refresh")
    raw_text = response.text.lower()

    for marker in ("credit", "balance", "rate_limit_reset_credit", "consume"):
        assert marker not in raw_text


def test_response_never_contains_account_identifiers(rl_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_codex_rate_limits", fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW})
    )
    set_now(monkeypatch, NOW)

    response = rl_client.post("/api/codex-rate-limits/refresh")
    raw_text = response.text.lower()

    for marker in ("email", "organization", "session_id", "thread_id", "token", "auth"):
        assert marker not in raw_text


def test_response_never_contains_stdout_or_stderr_keys(rl_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_codex_rate_limits", fake_failure("protocol_error", "Codex App Server returned an unexpected protocol response."))
    set_now(monkeypatch, NOW)

    response = rl_client.post("/api/codex-rate-limits/refresh")
    body = response.json()

    assert "stdout" not in body
    assert "stderr" not in body


# ---------------------------------------------------------------------------
# invalid cache handling — GET never 500s
# ---------------------------------------------------------------------------


def test_get_never_500s_on_malformed_cache_file(rl_client, tmp_path, monkeypatch):
    cache_path = tmp_path / "codex-rate-limits.json"
    cache_path.write_text("{not valid json", encoding="utf-8")
    set_now(monkeypatch, NOW)

    response = rl_client.get("/api/codex-rate-limits")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "invalid_cache"


# ---------------------------------------------------------------------------
# 45: backups/変更なし (adapter/cache modules never resolve a path under backups/)
# ---------------------------------------------------------------------------


def test_resolve_cache_path_never_points_inside_repo_or_backups(tmp_path):
    fake_env = {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
    resolved = codex_rate_limits_cache.resolve_cache_path(env=fake_env)
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in resolved.parents
    assert "backups" not in resolved.parts
    assert resolved.name == "codex-rate-limits.json"


# ---------------------------------------------------------------------------
# state controller unit tests (no HTTP layer)
# ---------------------------------------------------------------------------


def test_controller_status_cooldown_remaining_counts_down(tmp_path):
    controller = CodexRateLimitsController()
    cache_path = tmp_path / "cache.json"
    controller.refresh(now=NOW, fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW}), cache_path=cache_path)

    just_after = controller.status(now=NOW + timedelta(seconds=1))
    near_end = controller.status(now=NOW + timedelta(seconds=29))
    after_cooldown = controller.status(now=NOW + timedelta(seconds=31))

    assert just_after["cooldown_remaining_seconds"] > after_cooldown["cooldown_remaining_seconds"]
    assert near_end["cooldown_remaining_seconds"] > 0
    assert after_cooldown["cooldown_remaining_seconds"] == 0


def test_controller_isolated_between_instances(tmp_path):
    controller_a = CodexRateLimitsController()
    controller_b = CodexRateLimitsController()
    cache_path = tmp_path / "cache.json"

    controller_a.refresh(now=NOW, fetch=fake_success({"five_hour": FIVE_HOUR_WINDOW, "weekly": WEEKLY_WINDOW}), cache_path=cache_path)

    status_a = controller_a.status(now=NOW)
    status_b = controller_b.status(now=NOW)

    assert status_a["last_success_at"] is not None
    assert status_b["last_success_at"] is None
