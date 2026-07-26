from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.github_rate_limit_cli import GitHubRateLimitFetchResult
from app.github_rate_limit_state import GitHubRateLimitController
from app.main import app

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)

VALID_PAYLOAD = {
    "resources": {
        "core": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
        "graphql": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
        "search": {"limit": 30, "used": 1, "remaining": 29, "reset": int(NOW.timestamp()) + 3600},
    }
}

GRAPHQL_EXHAUSTED_PAYLOAD = {
    "resources": {
        "core": {"limit": 5000, "used": 816, "remaining": 4184, "reset": int(NOW.timestamp()) + 3600},
        "graphql": {"limit": 5000, "used": 10033, "remaining": 0, "reset": int(NOW.timestamp()) + 3600},
    }
}


@pytest.fixture()
def gh_client():
    app.state.github_rate_limit_controller = GitHubRateLimitController()
    with TestClient(app) as client:
        yield client


def fake_success(payload: dict):
    def fetch(*, now=None, **kwargs):
        return GitHubRateLimitFetchResult(
            success=True, payload=payload, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    return fetch


def fake_failure(error_type: str, user_message: str, return_code: int | None = 1):
    def fetch(*, now=None, **kwargs):
        return GitHubRateLimitFetchResult(
            success=False,
            payload=None,
            error_type=error_type,
            user_message=user_message,
            return_code=return_code,
            collected_at=now,
        )

    return fetch


def set_now(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr("app.main._current_utc_time", lambda: value)


# 1. GET初期状態でghを呼ばない / 2. 初期状態が未取得
def test_get_initial_state_does_not_call_gh_and_is_unfetched(gh_client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("fetch_github_rate_limit must not be called on GET")

    monkeypatch.setattr("app.main.fetch_github_rate_limit", explode)

    response = gh_client.get("/api/github-rate-limit")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is False
    assert body["refreshing"] is False
    assert body["resources"] is None
    assert body["overall"] == {"status": "Unknown", "reason": "not fetched yet"}
    assert body["last_attempt_at"] is None
    assert body["last_success_at"] is None
    assert body["error"] is None


# 3. POST refresh成功 / 4. 成功時にcore/graphql/searchを返す
def test_refresh_success_returns_all_three_resources(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is True
    assert set(body["resources"].keys()) == {"core", "graphql", "search"}
    assert body["resources"]["core"]["status"] == "Normal"
    assert body["last_success_at"] is not None


# 5. GraphQLのみ枯渇でOverall Limited
def test_graphql_only_exhausted_overall_limited(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(GRAPHQL_EXHAUSTED_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert body["resources"]["core"]["status"] == "Normal"
    assert body["resources"]["graphql"]["status"] == "Exhausted"
    assert body["overall"]["status"] == "Limited"
    assert body["overall"]["reason"] == "GraphQL API exhausted"
    assert "全体が使用不可" not in body["overall"]["reason"]


# 6. refresh失敗時のerror_type
def test_refresh_failure_reports_error_type(gh_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_github_rate_limit", fake_failure("not_authenticated", "GitHub CLI is not authenticated.")
    )
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is False
    assert body["error"]["error_type"] == "not_authenticated"
    assert body["error"]["user_message"] == "GitHub CLI is not authenticated."


# 7. stderr/tokenがレスポンスへ含まれない
def test_response_never_contains_token_like_stderr_content(gh_client, monkeypatch):
    fake_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    monkeypatch.setattr(
        "app.main.fetch_github_rate_limit",
        fake_failure("authentication_expired", "GitHub CLI authentication appears to be expired or invalid."),
    )
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    raw_text = response.text

    assert fake_token not in raw_text
    assert "GH_TOKEN" not in raw_text
    assert "GITHUB_TOKEN" not in raw_text


# 8. refresh直後の再実行をクールダウンで拒否 / 9. retry_after_seconds
def test_refresh_immediately_again_is_rejected_with_retry_after(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)
    first = gh_client.post("/api/github-rate-limit/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=5))
    second = gh_client.post("/api/github-rate-limit/refresh")

    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["error_type"] == "cooldown_active"
    assert detail["retry_after_seconds"] > 0
    assert "user_message" in detail


# 10. cooldown経過後は再実行可能
def test_refresh_after_cooldown_elapses_is_allowed(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)
    first = gh_client.post("/api/github-rate-limit/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=31))
    second = gh_client.post("/api/github-rate-limit/refresh")

    assert second.status_code == 200


# 11. 失敗後もクールダウンが適用される
def test_cooldown_applies_after_a_failed_attempt_too(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_failure("timeout", "Fetching timed out."))
    set_now(monkeypatch, NOW)
    first = gh_client.post("/api/github-rate-limit/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=5))
    second = gh_client.post("/api/github-rate-limit/refresh")

    assert second.status_code == 429
    assert second.json()["detail"]["error_type"] == "cooldown_active"


# 12. 更新中の二重実行防止
def test_concurrent_refresh_is_rejected_while_in_progress(gh_client, monkeypatch):
    controller = app.state.github_rate_limit_controller

    def fetch_that_marks_in_progress(*, now=None, **kwargs):
        inner_response = gh_client.post("/api/github-rate-limit/refresh")
        assert inner_response.status_code == 429
        assert inner_response.json()["detail"]["error_type"] == "already_refreshing"
        return GitHubRateLimitFetchResult(
            success=True, payload=VALID_PAYLOAD, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    monkeypatch.setattr("app.main.fetch_github_rate_limit", fetch_that_marks_in_progress)
    set_now(monkeypatch, NOW)

    outer_response = gh_client.post("/api/github-rate-limit/refresh")
    assert outer_response.status_code == 200
    assert controller._refreshing is False


# 13. GETは保持状態を返すだけで再取得しない
def test_get_after_refresh_does_not_trigger_another_fetch(gh_client, monkeypatch):
    call_count = {"n": 0}

    def counting_fetch(*, now=None, **kwargs):
        call_count["n"] += 1
        return GitHubRateLimitFetchResult(
            success=True, payload=VALID_PAYLOAD, error_type=None, user_message=None, return_code=0, collected_at=now
        )

    monkeypatch.setattr("app.main.fetch_github_rate_limit", counting_fetch)
    set_now(monkeypatch, NOW)

    gh_client.post("/api/github-rate-limit/refresh")
    gh_client.get("/api/github-rate-limit")
    gh_client.get("/api/github-rate-limit")

    assert call_count["n"] == 1


# 14. 古い成功値と最新失敗を混同しない
def test_stale_success_is_not_confused_with_latest_failure(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)
    gh_client.post("/api/github-rate-limit/refresh")

    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_failure("api_error", "The GitHub API returned an error."))
    set_now(monkeypatch, NOW + timedelta(seconds=60))
    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert body["fetched"] is False
    assert body["resources"] is None
    assert body["stale"] is True
    assert body["last_known"] is not None
    assert body["last_known"]["stale"] is True
    assert body["last_known"]["resources"]["core"]["status"] == "Normal"
    assert body["last_success_at"] is not None


# 15. collected_at / last_attempt_at / last_success_atがUTC aware
def test_timestamps_are_utc_aware_iso_strings(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    for field in ("collected_at", "last_attempt_at", "last_success_at"):
        value = body[field]
        assert value is not None
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


# 16. controllerの状態がテスト間で分離される
def test_controller_state_is_isolated_between_tests(gh_client):
    response = gh_client.get("/api/github-rate-limit")
    body = response.json()
    assert body["fetched"] is False
    assert body["last_attempt_at"] is None


# 17. CLI未導入
def test_cli_not_installed_error_type(gh_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_github_rate_limit", fake_failure("cli_not_installed", "GitHub CLI (gh) is not installed.")
    )
    set_now(monkeypatch, NOW)
    response = gh_client.post("/api/github-rate-limit/refresh")
    assert response.json()["error"]["error_type"] == "cli_not_installed"


# 18. 未認証
def test_not_authenticated_error_type(gh_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_github_rate_limit", fake_failure("not_authenticated", "GitHub CLI is not authenticated.")
    )
    set_now(monkeypatch, NOW)
    response = gh_client.post("/api/github-rate-limit/refresh")
    assert response.json()["error"]["error_type"] == "not_authenticated"


# 19. timeout
def test_timeout_error_type(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_failure("timeout", "Fetching timed out."))
    set_now(monkeypatch, NOW)
    response = gh_client.post("/api/github-rate-limit/refresh")
    assert response.json()["error"]["error_type"] == "timeout"


# 20. invalid_json
def test_invalid_json_error_type(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_failure("invalid_json", "Not valid JSON."))
    set_now(monkeypatch, NOW)
    response = gh_client.post("/api/github-rate-limit/refresh")
    assert response.json()["error"]["error_type"] == "invalid_json"


# 21. invalid_response
def test_invalid_response_error_type(gh_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_github_rate_limit", fake_failure("invalid_response", "Unexpected structure.")
    )
    set_now(monkeypatch, NOW)
    response = gh_client.post("/api/github-rate-limit/refresh")
    assert response.json()["error"]["error_type"] == "invalid_response"


# 22. search ErrorでもOverallへ影響しない
def test_search_error_does_not_affect_overall_via_api(gh_client, monkeypatch):
    payload = {
        "resources": {
            "core": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
            "graphql": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
        }
    }
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(payload))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert body["resources"]["search"]["status"] == "Error"
    assert body["overall"]["status"] == "Normal"


# 23. core Errorまたはgraphql ErrorでOverall Error
def test_core_missing_makes_overall_error_via_api(gh_client, monkeypatch):
    payload = {
        "resources": {
            "graphql": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
        }
    }
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(payload))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert body["resources"]["core"]["status"] == "Error"
    assert body["overall"]["status"] == "Error"


# 24. APIレスポンスにstdout/stderrが存在しない
def test_response_never_contains_stdout_or_stderr_keys(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_failure("command_failed", "The GitHub CLI command failed."))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert "stdout" not in body
    assert "stderr" not in body
    assert "stdout" not in body.get("error", {})
    assert "stderr" not in body.get("error", {})


# 25. token風文字列がレスポンスに存在しない
def test_response_never_contains_token_like_string_on_success(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    raw_text = response.text

    assert "ghp_" not in raw_text
    assert "gho_" not in raw_text


# 追加: 自動再取得フィールドがAPIレスポンスへ含まれる（既存フィールドとの後方互換を維持）
def test_response_includes_auto_refresh_fields_when_exhausted(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(GRAPHQL_EXHAUSTED_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["auto_refresh_pending"] is True
    assert body["scheduled_reset_at"] is not None
    assert body["next_auto_refresh_at"] is not None
    assert body["last_auto_refresh_at"] is None
    assert body["last_auto_refresh_error"] is None


# 追加: 予約が発生してもリクエストが即座に完了する(タイマー登録がリクエストをブロックしない)
def test_scheduling_auto_refresh_does_not_block_the_response(gh_client, monkeypatch):
    import time

    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(GRAPHQL_EXHAUSTED_PAYLOAD))
    set_now(monkeypatch, NOW)

    start = time.monotonic()
    response = gh_client.post("/api/github-rate-limit/refresh")
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert elapsed < 2.0


# 追加: Normalではauto_refresh_pendingがFalseのまま(後方互換確認)
def test_response_auto_refresh_pending_false_when_normal(gh_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_rate_limit", fake_success(VALID_PAYLOAD))
    set_now(monkeypatch, NOW)

    response = gh_client.post("/api/github-rate-limit/refresh")
    body = response.json()

    assert body["auto_refresh_pending"] is False
    assert body["scheduled_reset_at"] is None
    assert body["next_auto_refresh_at"] is None
