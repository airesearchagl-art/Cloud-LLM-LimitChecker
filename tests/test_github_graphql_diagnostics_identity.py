import json
import subprocess

from app import github_graphql_diagnostics_identity as identity_module

NOW_STDOUT_PAYLOAD = {
    "login": "octocat",
    "id": 1,
    "email": "octocat@example.com",
    "name": "The Octocat",
    "avatar_url": "https://example.com/avatar.png",
    "bio": "should never leak",
}


def make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=list(identity_module.GH_USER_COMMAND), returncode=returncode, stdout=stdout, stderr=stderr
    )


# 1. successful parse extracts only login + id
def test_successful_fetch_extracts_only_login_and_id(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(NOW_STDOUT_PAYLOAD))
    )
    result = identity_module.fetch_github_identity()
    assert result.success is True
    assert result.login == "octocat"
    assert result.user_id == 1
    assert result.error_type is None
    assert result.user_message is None
    # dataclass has exactly these fields -- no room for extra payload fields.
    assert set(result.__dataclass_fields__.keys()) == {"success", "login", "user_id", "error_type", "user_message"}


# 2. CLI not installed
def test_cli_not_installed(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(identity_module.subprocess, "run", raise_not_found)
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "cli_not_installed"
    assert result.login is None
    assert result.user_id is None


# 3. not authenticated
def test_not_authenticated(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *a, **k: make_completed(returncode=1, stderr="To get started with GitHub CLI, please run: gh auth login"),
    )
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "not_authenticated"


# 4. authentication expired
def test_authentication_expired(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *a, **k: make_completed(returncode=1, stderr="gh: Bad credentials (HTTP 401)"),
    )
    result = identity_module.fetch_github_identity()
    assert result.error_type == "authentication_expired"


# 5. timeout
def test_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(identity_module.GH_USER_COMMAND), timeout=10)

    monkeypatch.setattr(identity_module.subprocess, "run", raise_timeout)
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "timeout"


# 6. malformed JSON
def test_malformed_json(monkeypatch):
    monkeypatch.setattr(identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout="{not valid json"))
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_json"


# 7. non-dict response
def test_non_dict_response_is_invalid(monkeypatch):
    monkeypatch.setattr(identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(["a", "b"])))
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_response"


# 8. missing login field
def test_missing_login_field_is_invalid(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps({"id": 1}))
    )
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_response"


# 9. missing id field
def test_missing_id_field_is_invalid(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps({"login": "octocat"}))
    )
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_response"


# 10. non-int id (string) is invalid
def test_non_integer_id_is_invalid(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *a, **k: make_completed(stdout=json.dumps({"login": "octocat", "id": "1"})),
    )
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_response"


# 11. bool id is rejected (bool is technically an int subclass in Python)
def test_bool_id_is_invalid(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *a, **k: make_completed(stdout=json.dumps({"login": "octocat", "id": True})),
    )
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "invalid_response"


# 12. generic command failure
def test_generic_command_failure(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(returncode=1, stderr="something went wrong")
    )
    result = identity_module.fetch_github_identity()
    assert result.error_type == "command_failed"


# 13. generic API error
def test_generic_api_error(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess,
        "run",
        lambda *a, **k: make_completed(returncode=1, stderr="gh: Internal Server Error (HTTP 500)"),
    )
    result = identity_module.fetch_github_identity()
    assert result.error_type == "api_error"


# 14. unexpected subprocess exception does not escape
def test_unexpected_exception_does_not_escape(monkeypatch):
    def raise_unexpected(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(identity_module.subprocess, "run", raise_unexpected)
    result = identity_module.fetch_github_identity()
    assert result.success is False
    assert result.error_type == "unknown"


# 15. non-leak: injected fake stderr/raw response never reaches user_message
def test_user_message_never_leaks_stderr_or_response_content(monkeypatch):
    fake_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    secret_stderr = f"gh: Bad credentials using token {fake_token} for user internal-employee-12345 (HTTP 401)"
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(returncode=1, stderr=secret_stderr)
    )
    result = identity_module.fetch_github_identity()
    assert fake_token not in (result.user_message or "")
    assert "internal-employee-12345" not in (result.user_message or "")
    assert secret_stderr not in (result.user_message or "")


def test_user_message_never_leaks_raw_response_fields(monkeypatch):
    monkeypatch.setattr(
        identity_module.subprocess, "run", lambda *a, **k: make_completed(stdout=json.dumps(NOW_STDOUT_PAYLOAD))
    )
    result = identity_module.fetch_github_identity()
    assert not hasattr(result, "email")
    assert not hasattr(result, "name")
    assert not hasattr(result, "avatar_url")
    assert not hasattr(result, "raw")


# 16. run is called with an argument list, shell=False
def test_run_is_called_with_argument_list_and_shell_false(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args[0] if args else kwargs.get("args")
        captured["kwargs"] = kwargs
        return make_completed(stdout=json.dumps(NOW_STDOUT_PAYLOAD))

    monkeypatch.setattr(identity_module.subprocess, "run", fake_run)
    identity_module.fetch_github_identity()

    assert captured["args"] == ["gh", "api", "user"]
    assert captured["kwargs"]["shell"] is False


# 17. timeout value passed through
def test_timeout_value_is_passed_through(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return make_completed(stdout=json.dumps(NOW_STDOUT_PAYLOAD))

    monkeypatch.setattr(identity_module.subprocess, "run", fake_run)
    identity_module.fetch_github_identity(timeout=3)

    assert captured["timeout"] == 3


# 18. this module only ever invokes the fixed `gh api user` command tuple
def test_module_only_invokes_gh_api_user_command():
    assert identity_module.GH_USER_COMMAND == ("gh", "api", "user")
