import json
import subprocess
from datetime import datetime, timezone

from app import github_actions_billing_cli as cli

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

USER_PAYLOAD_PRO = {"login": "octocat", "plan": {"name": "pro", "space": 976562499, "collaborators": 0, "private_repos": 9999}}
USER_PAYLOAD_NO_PLAN = {"login": "octocat", "plan": None}

BILLING_PAYLOAD = {
    "usageItems": [
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
}


def make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh", "api", "user"], returncode=returncode, stdout=stdout, stderr=stderr)


def dispatcher(*, user_result=None, billing_result=None):
    """Route subprocess.run calls: the "user" call is args=["gh","api","user"];
    the billing call's path starts with "/users/"."""

    def run(args, **kwargs):
        path = args[2]
        if path == "user":
            return user_result if user_result is not None else make_completed(stdout=json.dumps(USER_PAYLOAD_PRO))
        return billing_result if billing_result is not None else make_completed(stdout=json.dumps(BILLING_PAYLOAD))

    return run


# 1. 正常取得(user + billing)
def test_successful_fetch_returns_plan_and_usage_items(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", dispatcher())
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is True
    assert result.plan_name == "pro"
    assert result.usage_items == BILLING_PAYLOAD["usageItems"]
    assert result.billing_year == 2026
    assert result.billing_month == 8
    assert result.error_type is None


# 2. 正常取得結果をdomain reportへ変換
def test_successful_fetch_converts_to_report(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", dispatcher())
    result = cli.fetch_github_actions_billing(now=NOW)
    report = cli.build_github_actions_billing_report(result)
    assert report is not None
    assert report.plan_name == "pro"
    assert report.included_minutes == 3000
    assert report.used_included_minutes == 125


# 3. plan.nameが取得できなくてもfetchはsuccess(plan_unknownはdomain層の責務)
def test_plan_null_is_still_a_successful_fetch(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(user_result=make_completed(stdout=json.dumps(USER_PAYLOAD_NO_PLAN))),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is True
    assert result.plan_name is None
    report = cli.build_github_actions_billing_report(result)
    assert report.status == "plan_unknown"


# 4. gh未導入
def test_gh_not_installed(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(cli.subprocess, "run", raise_not_found)
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "cli_not_installed"


# 5. 未認証
def test_not_authenticated(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(
            user_result=make_completed(
                returncode=1, stderr="To get started with GitHub CLI, please run: gh auth login"
            )
        ),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "not_authenticated"


# 6. timeout(userコール)
def test_timeout_on_user_call(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["gh", "api", "user"], timeout=10)

    monkeypatch.setattr(cli.subprocess, "run", raise_timeout)
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "timeout"


# 7. timeout(billingコール)
def test_timeout_on_billing_call(monkeypatch):
    def run(args, **kwargs):
        if args[2] == "user":
            return make_completed(stdout=json.dumps(USER_PAYLOAD_PRO))
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "timeout"


# 8. 403 permission required
def test_403_permission_required(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(
            billing_result=make_completed(
                returncode=1, stderr='gh: Forbidden (HTTP 403)\ngh: Resource not accessible by personal access token'
            )
        ),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "permission_required"


# 9. 404だがscope不足のhintを伴う場合はpermission_requiredとして分類する
# (このapp自身のgh CLI OAuth tokenで実観測した挙動: HTTP 404 + "needs the ... scope")
def test_404_with_scope_hint_is_permission_required(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(
            billing_result=make_completed(
                returncode=1,
                stderr=(
                    "gh: Not Found (HTTP 404)\n"
                    'gh: This API operation needs the "user" scope. '
                    "To request it, run:  gh auth refresh -h github.com -s user"
                ),
            )
        ),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "permission_required"


# 10. 404で純粋にAPI availabilityの問題(scope hintなし)はapi_unavailable
def test_plain_404_is_api_unavailable(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(billing_result=make_completed(returncode=1, stderr="gh: Not Found (HTTP 404)")),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "api_unavailable"


# 11. malformed JSON(userコール)
def test_malformed_json_on_user_call(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run", dispatcher(user_result=make_completed(stdout="not json"))
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_json"


# 12. malformed JSON(billingコール)
def test_malformed_json_on_billing_call(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run", dispatcher(billing_result=make_completed(stdout="not json"))
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_json"


# 13. usageItemsが欠落した構造 -> invalid_response
def test_missing_usage_items_is_invalid_response(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(billing_result=make_completed(stdout=json.dumps({"user": "octocat"}))),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_response"


# 14. loginが欠落 -> invalid_response(billingコールは実行されない)
def test_missing_login_is_invalid_response(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(user_result=make_completed(stdout=json.dumps({"plan": {"name": "pro"}}))),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.success is False
    assert result.error_type == "invalid_response"


# 15. secondary rate limit
def test_secondary_rate_limit(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(billing_result=make_completed(returncode=1, stderr="You have exceeded a secondary rate limit")),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.error_type == "secondary_rate_limit"


# 16. raw stderr/token風文字列がuser_messageへ含まれない
def test_user_message_never_contains_raw_stderr_or_token(monkeypatch):
    fake_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(
            billing_result=make_completed(
                returncode=1, stderr=f"gh: Forbidden (HTTP 403) token={fake_token} secret-detail-xyz"
            )
        ),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert result.user_message is not None
    assert fake_token not in result.user_message
    assert "secret-detail-xyz" not in result.user_message


# 17. billingパスにyear/month/productクエリが含まれる
def test_billing_request_path_includes_expected_query(monkeypatch):
    captured_paths = []

    def run(args, **kwargs):
        path = args[2]
        captured_paths.append(path)
        if path == "user":
            return make_completed(stdout=json.dumps(USER_PAYLOAD_PRO))
        return make_completed(stdout=json.dumps(BILLING_PAYLOAD))

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.fetch_github_actions_billing(now=NOW)

    billing_path = captured_paths[1]
    assert billing_path.startswith("/users/octocat/settings/billing/usage/summary")
    assert "year=2026" in billing_path
    assert "month=8" in billing_path
    assert "product=actions" in billing_path


# 18. shell=Falseで実行される(subprocess injectionを避ける)
def test_invoked_with_shell_false(monkeypatch):
    captured_kwargs = []

    def run(args, **kwargs):
        captured_kwargs.append(kwargs)
        if args[2] == "user":
            return make_completed(stdout=json.dumps(USER_PAYLOAD_PRO))
        return make_completed(stdout=json.dumps(BILLING_PAYLOAD))

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.fetch_github_actions_billing(now=NOW)

    assert all(kwargs.get("shell") is False for kwargs in captured_kwargs)


# 19. 失敗fetchはbuild_github_actions_billing_reportがNoneを返す
def test_failed_fetch_builds_no_report(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        dispatcher(billing_result=make_completed(returncode=1, stderr="gh: Not Found (HTTP 404)")),
    )
    result = cli.fetch_github_actions_billing(now=NOW)
    assert cli.build_github_actions_billing_report(result) is None
