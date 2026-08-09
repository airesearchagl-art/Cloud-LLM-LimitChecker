from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.github_actions_billing_cli import GitHubActionsBillingFetchResult
from app.github_actions_billing_state import GitHubActionsBillingController
from app.main import app

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

USAGE_ITEMS_PRO = [
    {
        "product": "Actions",
        "sku": "actions_linux",
        "unitType": "minutes",
        "pricePerUnit": 0.008,
        "grossQuantity": 125,
        "grossAmount": 1.0,
        "discountQuantity": 125,
        "discountAmount": 1.0,
        "netQuantity": 0,
        "netAmount": 0,
    }
]


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


@pytest.fixture()
def billing_client():
    app.state.github_actions_billing_controller = GitHubActionsBillingController()
    with TestClient(app) as client:
        yield client


def fake_success(*, plan_name="pro", usage_items=USAGE_ITEMS_PRO, year=2026, month=8):
    def fetch(*, now=None, **kwargs):
        return GitHubActionsBillingFetchResult(
            success=True,
            plan_name=plan_name,
            usage_items=usage_items,
            billing_year=year,
            billing_month=month,
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    return fetch


def fake_failure(error_type: str, user_message: str, return_code: int | None = 1):
    def fetch(*, now=None, **kwargs):
        return GitHubActionsBillingFetchResult(
            success=False,
            plan_name=None,
            usage_items=None,
            billing_year=None,
            billing_month=None,
            error_type=error_type,
            user_message=user_message,
            return_code=return_code,
            collected_at=now,
        )

    return fetch


def set_now(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr("app.main._current_utc_time", lambda: value)


# 1. GET初期状態でghを呼ばない
def test_get_initial_state_does_not_call_gh(billing_client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("fetch_github_actions_billing must not be called on GET")

    monkeypatch.setattr("app.main.fetch_github_actions_billing", explode)

    response = billing_client.get("/api/github-actions-billing")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is False
    assert body["status"] is None
    assert body["live_validation_required"] is True


# 2. success response
def test_refresh_success_response(billing_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success())
    set_now(monkeypatch, NOW)

    response = billing_client.post("/api/github-actions-billing/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is True
    assert body["status"] == "usage_breakdown_inconclusive"
    assert body["plan_name"] == "pro"
    assert body["included_minutes"] == 3000
    # exact quota consumption is never fabricated from discountQuantity --
    # see app.github_actions_billing's module docstring
    assert body["used_included_minutes"] is None
    assert body["remaining_minutes"] is None
    assert body["usage_percentage"] is None
    assert body["discounted_standard_minutes"] == 125
    assert body["source"] == "github_billing_api"
    assert body["live_validation_required"] is False


# 3. permission_required
def test_refresh_permission_required(billing_client, monkeypatch):
    monkeypatch.setattr(
        "app.main.fetch_github_actions_billing",
        fake_failure(
            "permission_required",
            'The current GitHub credential does not have permission to read Actions billing usage (requires the "Plan: read" permission / "user" scope).',
        ),
    )
    set_now(monkeypatch, NOW)

    response = billing_client.post("/api/github-actions-billing/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is False
    assert body["error"]["error_type"] == "permission_required"
    assert "Plan" in body["error"]["user_message"] or "permission" in body["error"]["user_message"]


# 4. plan_unknown
def test_refresh_plan_unknown(billing_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success(plan_name="team"))
    set_now(monkeypatch, NOW)

    response = billing_client.post("/api/github-actions-billing/refresh")
    body = response.json()

    assert response.status_code == 200
    assert body["fetched"] is True
    assert body["status"] == "plan_unknown"
    assert body["included_minutes"] is None
    assert body["remaining_minutes"] is None


# 5. stale cache: 直近成功から時間が経過するとstale=trueになる
def test_stale_after_cooldown_window_elapses(billing_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success())
    set_now(monkeypatch, NOW)
    first = billing_client.post("/api/github-actions-billing/refresh")
    assert first.status_code == 200
    assert first.json()["stale"] is False

    # GETは取得しないので、経過時間だけ進めてGETのstale判定を確認する
    set_now(monkeypatch, NOW + timedelta(seconds=901))
    response = billing_client.get("/api/github-actions-billing")

    assert response.status_code == 200
    assert response.json()["stale"] is True
    # cacheされた値自体は保持され続ける(古い値として表示可能)
    assert response.json()["fetched"] is True
    assert response.json()["plan_name"] == "pro"


# 6. cooldown中の再実行は429
def test_refresh_immediately_again_is_rejected_with_retry_after(billing_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success())
    set_now(monkeypatch, NOW)
    first = billing_client.post("/api/github-actions-billing/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=5))
    second = billing_client.post("/api/github-actions-billing/refresh")

    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["error_type"] == "cooldown_active"
    assert detail["retry_after_seconds"] > 0


# 7. cooldown経過後は再実行可能
def test_refresh_after_cooldown_elapses_is_allowed(billing_client, monkeypatch):
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success())
    set_now(monkeypatch, NOW)
    first = billing_client.post("/api/github-actions-billing/refresh")
    assert first.status_code == 200

    set_now(monkeypatch, NOW + timedelta(seconds=901))
    second = billing_client.post("/api/github-actions-billing/refresh")

    assert second.status_code == 200


# 8. 更新中の二重実行防止
def test_concurrent_refresh_is_rejected_while_in_progress(billing_client, monkeypatch):
    controller = app.state.github_actions_billing_controller

    def fetch_that_marks_in_progress(*, now=None, **kwargs):
        inner_response = billing_client.post("/api/github-actions-billing/refresh")
        assert inner_response.status_code == 429
        assert inner_response.json()["detail"]["error_type"] == "already_refreshing"
        return GitHubActionsBillingFetchResult(
            success=True,
            plan_name="pro",
            usage_items=USAGE_ITEMS_PRO,
            billing_year=2026,
            billing_month=8,
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    monkeypatch.setattr("app.main.fetch_github_actions_billing", fetch_that_marks_in_progress)
    set_now(monkeypatch, NOW)

    outer_response = billing_client.post("/api/github-actions-billing/refresh")
    assert outer_response.status_code == 200
    assert controller._refreshing is False


# 9. GETは保持状態を返すだけで再取得しない
def test_get_after_refresh_does_not_trigger_another_fetch(billing_client, monkeypatch):
    call_count = {"n": 0}

    def counting_fetch(*, now=None, **kwargs):
        call_count["n"] += 1
        return GitHubActionsBillingFetchResult(
            success=True,
            plan_name="pro",
            usage_items=USAGE_ITEMS_PRO,
            billing_year=2026,
            billing_month=8,
            error_type=None,
            user_message=None,
            return_code=0,
            collected_at=now,
        )

    monkeypatch.setattr("app.main.fetch_github_actions_billing", counting_fetch)
    set_now(monkeypatch, NOW)

    billing_client.post("/api/github-actions-billing/refresh")
    billing_client.get("/api/github-actions-billing")
    billing_client.get("/api/github-actions-billing")

    assert call_count["n"] == 1


# 10. token/stderr風文字列がレスポンスに含まれない
def test_response_never_contains_token_like_string(billing_client, monkeypatch):
    fake_token = "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    monkeypatch.setattr("app.main.fetch_github_actions_billing", fake_success())
    set_now(monkeypatch, NOW)

    response = billing_client.post("/api/github-actions-billing/refresh")
    raw_text = response.text

    assert fake_token not in raw_text
    assert "GH_TOKEN" not in raw_text
    assert "GITHUB_TOKEN" not in raw_text
    assert "stdout" not in raw_text
    assert "stderr" not in raw_text


# 11. billable_standard_minutesが正しく反映される(overageと断定はしない)
def test_refresh_billable_standard_minutes_response(billing_client, monkeypatch):
    billed_items = [
        {
            "product": "Actions",
            "sku": "actions_linux",
            "unitType": "minutes",
            "pricePerUnit": 0.008,
            "grossQuantity": 2100,
            "grossAmount": 5.0,
            "discountQuantity": 2000,
            "discountAmount": 4.0,
            "netQuantity": 100,
            "netAmount": 1.0,
        }
    ]
    monkeypatch.setattr(
        "app.main.fetch_github_actions_billing", fake_success(plan_name="free", usage_items=billed_items)
    )
    set_now(monkeypatch, NOW)

    response = billing_client.post("/api/github-actions-billing/refresh")
    body = response.json()

    assert body["status"] == "usage_breakdown_inconclusive"
    assert body["discounted_standard_minutes"] == 2000
    assert body["billable_standard_minutes"] == 100
    # never fabricated, regardless of billable_standard_minutes being non-zero
    assert body["remaining_minutes"] is None
    assert body["used_included_minutes"] is None


# 12. Basic Auth互換性: 有効時は資格情報が必須で、既存パターン(dashboard等)と同じ挙動
def test_basic_auth_enabled_requires_credentials(monkeypatch):
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "true")
    monkeypatch.setenv("BASIC_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")
    app.state.github_actions_billing_controller = GitHubActionsBillingController()

    with TestClient(app) as client:
        unauthenticated = client.get("/api/github-actions-billing")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"].startswith("Basic")

        authenticated = client.get("/api/github-actions-billing", auth=("admin", "secret"))
        assert authenticated.status_code == 200
