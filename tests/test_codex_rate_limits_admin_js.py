"""Pure-function tests for the admin dashboard's Codex rate limit refresh error handling.

`codexRateLimitsErrorDisplay(status, body)` (static/app.js) is the only code
path that turns a `POST /api/codex-rate-limits/refresh` response into text
shown in the DOM. These tests feed it adversarial response bodies (token-like
strings, fake tracebacks, JSON-RPC-error shapes, malformed/absent JSON) and
assert the returned `user_message` never contains any of that content —
proving the sanitization holds regardless of what a misbehaving backend,
proxy, or future regression might put in a response body.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"

GENERIC_MESSAGE = "Codex使用枠の取得に失敗しました。しばらく待ってから再度お試しください。"


def run_app_js(expression: str):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


# 1/7: 500(相当)は固定user_messageだけを返す
def test_non_429_status_returns_fixed_generic_message():
    result = run_app_js("app.codexRateLimitsErrorDisplay(500, null)")
    assert result["user_message"] == GENERIC_MESSAGE
    assert result["error_type"] == "unknown_error"
    assert result["retry_after_seconds"] == 0


# 2/3: 500本文にtoken風文字列を含めてもDOM表示用の値へ出ない
def test_token_like_content_in_body_never_surfaces_for_non_429():
    secret_marker = "sk-ant-api03-SHOULD-NEVER-LEAK-INTO-RESPONSE"
    body = {"detail": secret_marker, "error": {"message": secret_marker}}
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(500, {json.dumps(body)})")
    assert secret_marker not in result["user_message"]
    assert result["user_message"] == GENERIC_MESSAGE


# 4/5: 500本文にTracebackを含めてもDOM表示用の値へ出ない
def test_traceback_like_content_in_body_never_surfaces():
    body = {
        "detail": "Traceback (most recent call last):\n  File \"app/main.py\", line 42, in refresh\nRuntimeError: boom"
    }
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(500, {json.dumps(body)})")
    assert "Traceback" not in result["user_message"]
    assert "app/main.py" not in result["user_message"]
    assert result["user_message"] == GENERIC_MESSAGE


# 6: JSON-RPC error風本文もDOM表示用の値へ出ない
def test_json_rpc_error_shaped_body_never_surfaces():
    body = {"id": 2, "error": {"code": -32601, "message": "method not found: account/rateLimits/read"}}
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(500, {json.dumps(body)})")
    assert "account/rateLimits/read" not in result["user_message"]
    assert "-32601" not in json.dumps(result)
    assert result["user_message"] == GENERIC_MESSAGE


# 8: 429では安全なdetail.user_messageを表示する
def test_429_uses_safe_detail_user_message():
    body = {"detail": {"error_type": "cooldown_active", "user_message": "更新の間隔が短すぎます。", "retry_after_seconds": 12}}
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(429, {json.dumps(body)})")
    assert result["user_message"] == "更新の間隔が短すぎます。"
    assert result["error_type"] == "cooldown_active"


# 9: 429のretry_after_secondsが伝播する(countdown開始条件)
def test_429_propagates_retry_after_seconds():
    body = {"detail": {"user_message": "cooldown", "retry_after_seconds": 27}}
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(429, {json.dumps(body)})")
    assert result["retry_after_seconds"] == 27


# 10: 429レスポンスが不正JSON(=bodyがnull)でも固定文言
def test_429_with_null_body_falls_back_to_generic_message():
    result = run_app_js("app.codexRateLimitsErrorDisplay(429, null)")
    assert result["user_message"] == GENERIC_MESSAGE
    assert result["retry_after_seconds"] == 0


def test_429_with_non_object_detail_falls_back_to_generic_message():
    body = {"detail": "just a string, not the documented object shape"}
    result = run_app_js(f"app.codexRateLimitsErrorDisplay(429, {json.dumps(body)})")
    assert result["user_message"] == GENERIC_MESSAGE


# 11: network error相当(statusなし)でも固定文言
def test_no_status_network_error_case_returns_generic_message():
    result = run_app_js("app.codexRateLimitsErrorDisplay(null, null)")
    assert result["user_message"] == GENERIC_MESSAGE
    assert result["error_type"] == "unknown_error"


def _codex_rate_limits_handler_source() -> str:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index('document.querySelector("#codexRateLimitsRefresh").addEventListener')
    end = js.index('document.querySelector(', start + 1)
    return js[start:end]


def test_app_js_never_reads_response_text_for_codex_rate_limits():
    handler_source = _codex_rate_limits_handler_source()
    # the refresh click handler must never call response.text() or build an
    # Error from response content (unlike the pre-existing GitHub handler,
    # which is out of this task's scope and intentionally left untouched)
    assert "response.text()" not in handler_source
    assert "new Error(" not in handler_source


def test_app_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
