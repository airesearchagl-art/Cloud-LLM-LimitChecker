import copy
import json
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import github_rate_limit_cli as cli

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)

VALID_PAYLOAD = {
    "resources": {
        "core": {"limit": 5000, "used": 100, "remaining": 4900, "reset": 2_000_000_000},
        "graphql": {"limit": 5000, "used": 5000, "remaining": 0, "reset": 1_000_000_000},
    }
}


def make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=list(cli.GH_COMMAND), returncode=returncode, stdout=stdout, stderr=stderr)


# 1. 正常JSON取得
def test_successful_fetch_returns_payload(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(VALID_PAYLOAD)))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.success is True
    assert result.payload == VALID_PAYLOAD
    assert result.error_type is None
    assert result.user_message is None
    assert result.collected_at == NOW


# 2. 正常取得結果をフェーズAreportへ変換
def test_successful_fetch_converts_to_phase_a_report(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(VALID_PAYLOAD)))
    result = cli.fetch_github_rate_limit(now=NOW)
    report = cli.build_github_rate_limit_report(result)
    assert report is not None
    assert report.resources["core"].status == "Normal"
    assert report.resources["graphql"].status in ("Exhausted", "Reset overdue")
    assert report.overall.status == "Limited"


# 3. gh未導入
def test_gh_not_installed(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(cli.subprocess, "run", raise_not_found)
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.success is False
    assert result.error_type == "cli_not_installed"
    assert result.payload is None


# 4. timeout
def test_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(cli.GH_COMMAND), timeout=10)

    monkeypatch.setattr(cli.subprocess, "run", raise_timeout)
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.success is False
    assert result.error_type == "timeout"


# 5. returncode非ゼロ（一般的な失敗）
def test_nonzero_returncode_generic_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: make_completed(returncode=1, stderr="something went wrong")
    )
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.success is False
    assert result.error_type == "command_failed"
    assert result.return_code == 1


# 6. 未認証に相当するstderr
def test_not_authenticated_stderr(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: make_completed(
            returncode=1, stderr="To get started with GitHub CLI, please run: gh auth login"
        ),
    )
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "not_authenticated"


# 7. 認証期限切れに相当するstderr
def test_authentication_expired_stderr(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: make_completed(returncode=1, stderr="gh: Bad credentials (HTTP 401)")
    )
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "authentication_expired"


# 8. 一般的なAPI失敗
def test_generic_api_failure(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: make_completed(returncode=1, stderr="gh: Internal Server Error (HTTP 500)"),
    )
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "api_error"


# 9. stdoutが空
def test_empty_stdout_is_invalid_json(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=""))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "invalid_json"


# 10. 不正JSON
def test_malformed_json(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout="{not valid json"))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "invalid_json"


# 11. top-levelがlist
def test_top_level_list_is_invalid_response(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(["core", "graphql"])))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "invalid_response"


# 12. resources欠落
def test_missing_resources_key_is_invalid_response(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps({"rate": {}})))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.error_type == "invalid_response"


# 13. stderrにtoken風文字列があってもuser_messageへ含まれない
def test_token_like_stderr_never_reaches_user_message(monkeypatch):
    fake_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: make_completed(
            returncode=1, stderr=f"gh: Bad credentials using token {fake_token} (HTTP 401)"
        ),
    )
    result = cli.fetch_github_rate_limit(now=NOW)
    assert fake_token not in (result.user_message or "")


# 14. shell=False相当の引数配列で呼ばれる
def test_run_is_called_with_argument_list_and_shell_false(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args[0] if args else kwargs.get("args")
        captured["kwargs"] = kwargs
        return make_completed(stdout=json.dumps(VALID_PAYLOAD))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.fetch_github_rate_limit(now=NOW)

    assert captured["args"] == ["gh", "api", "rate_limit"]
    assert captured["kwargs"]["shell"] is False


# 15. timeout値が渡される
def test_timeout_value_is_passed_through(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return make_completed(stdout=json.dumps(VALID_PAYLOAD))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.fetch_github_rate_limit(timeout=3, now=NOW)

    assert captured["timeout"] == 3


# 16. subprocess例外が外へ漏れない
def test_unexpected_subprocess_exception_does_not_escape(monkeypatch):
    def raise_unexpected(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.subprocess, "run", raise_unexpected)
    result = cli.fetch_github_rate_limit(now=NOW)
    assert result.success is False
    assert result.error_type == "unknown"


# 17. 入力・mock結果を変更しない
def test_payload_is_not_mutated_by_report_conversion(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(VALID_PAYLOAD)))
    result = cli.fetch_github_rate_limit(now=NOW)
    before = copy.deepcopy(result.payload)

    cli.build_github_rate_limit_report(result)

    assert result.payload == before


# 18. collected_atがtimezone-aware UTC
def test_collected_at_is_timezone_aware_utc(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(VALID_PAYLOAD)))
    jst_now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
    result = cli.fetch_github_rate_limit(now=jst_now)
    assert result.collected_at.tzinfo is timezone.utc
    assert result.collected_at == NOW


# 19. 正常payload内のcore/graphql状態判定
def test_core_graphql_status_from_real_shaped_payload(monkeypatch):
    payload = {
        "resources": {
            "core": {"limit": 5000, "used": 816, "remaining": 4184, "reset": int(NOW.timestamp()) + 3600},
            "graphql": {"limit": 5000, "used": 10033, "remaining": 0, "reset": int(NOW.timestamp()) + 3600},
        }
    }
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(payload)))
    result = cli.fetch_github_rate_limit(now=NOW)
    report = cli.build_github_rate_limit_report(result)
    assert report.resources["core"].status == "Normal"
    assert report.resources["graphql"].status == "Exhausted"
    assert report.overall.status == "Limited"


# 20. search欠落時はフェーズA仕様どおり扱う
def test_missing_search_resource_becomes_error_without_affecting_overall(monkeypatch):
    payload = {
        "resources": {
            "core": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
            "graphql": {"limit": 5000, "used": 100, "remaining": 4900, "reset": int(NOW.timestamp()) + 3600},
        }
    }
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(payload)))
    result = cli.fetch_github_rate_limit(now=NOW)
    report = cli.build_github_rate_limit_report(result)
    assert report.resources["search"].status == "Error"
    assert report.overall.status == "Normal"


def test_failed_fetch_produces_no_report(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: make_completed(returncode=1, stderr="boom"))
    result = cli.fetch_github_rate_limit(now=NOW)
    assert cli.build_github_rate_limit_report(result) is None
