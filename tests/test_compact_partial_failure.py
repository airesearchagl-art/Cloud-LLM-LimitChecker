"""Pure-function tests for /compact's endpoint-level partial-failure resilience.

Regression coverage for a real-world incident: when the frontend requested
GET /api/github-actions-billing against a stale backend process that did not
have that route, the whole `/compact` page collapsed into a single top-level
"取得に失敗しました: {"detail":"Not Found"}" message and every other card
(GitHub Rate Limit, Claude Code Usage, Codex Usage) disappeared, even though
their own endpoints were healthy. Root cause: static/compact.js's loadCompact()
used Promise.all() over throwing fetchJson() calls, so one endpoint's failure
rejected the whole batch, and the raw response body was interpolated directly
into the DOM via error.message.

These tests cover the pure orchestration function (`buildCompactRenderPlan`)
that decides what to render per provider from a set of already-fetched
{ok, data} results -- the actual network fetching (`fetchJsonSafe`) is
intentionally excluded from this pure-function test surface, matching this
codebase's existing testing convention (see test_compact_dashboard.py).
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPACT_JS = ROOT / "static" / "compact.js"


def run_compact_js(expression: str):
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


DASHBOARD_ROWS = [
    {
        "limit_id": 1,
        "service_name": "OpenAI",
        "provider": "OpenAI",
        "account_type": "api",
        "model_name": "gpt-test",
        "limit_type": "requests",
        "status": "normal",
        "used_value": 1,
        "max_value": 100,
        "unit": "requests",
        "usage_percent": 1.0,
        "next_reset_at": None,
        "last_updated_at": None,
        "source_type": "manual",
    }
]

GITHUB_OK = {"fetched": False, "resources": None, "overall": None, "last_known": None, "error": None}

GITHUB_ACTIONS_BILLING_OK = {
    "fetched": True,
    "status": "usage_breakdown_inconclusive",
    "plan_name": "pro",
    "included_minutes": 3000,
    "used_included_minutes": None,
    "remaining_minutes": None,
    "usage_percentage": None,
    "discounted_standard_minutes": 10,
    "billable_standard_minutes": 0,
    "paid_non_included_minutes": 0,
    "billing_year": 2026,
    "billing_month": 8,
    "source": "github_billing_api",
    "collected_at": "2026-08-08T12:00:00+00:00",
    "skipped_unknown_skus": [],
    "error": None,
}

CLAUDE_AUTO_OK = {"available": False, "status": "not_observed"}
CLAUDE_MANUAL_OK = {"available": False, "status": "not_observed"}
CODEX_RATE_LIMITS_OK = {"available": False}
CODEX_USAGE_OK = {"available": False}


def _ok(data):
    return {"ok": True, "data": data}


def _fail():
    return {"ok": False}


def _all_ok_results(**overrides):
    results = {
        "dashboard": _ok(DASHBOARD_ROWS),
        "github": _ok(GITHUB_OK),
        "githubActionsBilling": _ok(GITHUB_ACTIONS_BILLING_OK),
        "claudeAuto": _ok(CLAUDE_AUTO_OK),
        "claudeManual": _ok(CLAUDE_MANUAL_OK),
        "codexRateLimits": _ok(CODEX_RATE_LIMITS_OK),
        "codexUsage": _ok(CODEX_USAGE_OK),
    }
    results.update(overrides)
    return results


def _plan(results):
    return run_compact_js(f"compact.buildCompactRenderPlan({json.dumps(results)})")


# ---------------------------------------------------------------------------
# 1. GitHub Actions endpointだけ失敗
# ---------------------------------------------------------------------------


def test_github_actions_billing_failure_isolated():
    plan = _plan(_all_ok_results(githubActionsBilling=_fail()))

    assert "GitHub Actions情報の取得に失敗しました" in plan["githubActionsCards"]
    # 他providerは影響を受けない(失敗providerのmessageが混入しない)
    assert "GitHub Actions情報の取得に失敗しました" not in plan["githubCards"]
    assert "GitHub Actions情報の取得に失敗しました" not in plan["claudeCodeUsageCards"]
    assert "GitHub Actions情報の取得に失敗しました" not in plan["codexUsageCards"]
    assert "GitHub Actions情報の取得に失敗しました" not in plan["limitCards"]
    # 正常なdashboard cardは引き続き描画される
    assert "表示できる制限項目がありません" not in plan["limitCards"]


# ---------------------------------------------------------------------------
# 2. Claude endpointだけ失敗 (auto/manualいずれかが失敗)
# ---------------------------------------------------------------------------


def test_claude_auto_failure_isolated():
    plan = _plan(_all_ok_results(claudeAuto=_fail()))

    assert "Claude Code Usageの取得に失敗しました" in plan["claudeCodeUsageCards"]
    assert "Claude Code Usageの取得に失敗しました" not in plan["githubCards"]
    assert "Claude Code Usageの取得に失敗しました" not in plan["githubActionsCards"]
    assert "Claude Code Usageの取得に失敗しました" not in plan["codexUsageCards"]


def test_claude_manual_failure_isolated():
    plan = _plan(_all_ok_results(claudeManual=_fail()))

    assert "Claude Code Usageの取得に失敗しました" in plan["claudeCodeUsageCards"]
    assert "Claude Code Usageの取得に失敗しました" not in plan["githubActionsCards"]


# ---------------------------------------------------------------------------
# 3. Codex endpointだけ失敗 (rate limits/usageいずれかが失敗)
# ---------------------------------------------------------------------------


def test_codex_rate_limits_failure_isolated():
    plan = _plan(_all_ok_results(codexRateLimits=_fail()))

    assert "Codex Usageの取得に失敗しました" in plan["codexUsageCards"]
    assert "Codex Usageの取得に失敗しました" not in plan["githubCards"]
    assert "Codex Usageの取得に失敗しました" not in plan["githubActionsCards"]
    assert "Codex Usageの取得に失敗しました" not in plan["claudeCodeUsageCards"]


def test_codex_usage_failure_isolated():
    plan = _plan(_all_ok_results(codexUsage=_fail()))

    assert "Codex Usageの取得に失敗しました" in plan["codexUsageCards"]


# ---------------------------------------------------------------------------
# 4. dashboard endpointだけ失敗
# ---------------------------------------------------------------------------


def test_dashboard_failure_isolated_other_providers_still_render():
    plan = _plan(_all_ok_results(dashboard=_fail()))

    assert "Dashboard情報の取得に失敗しました" in plan["limitCards"]
    # 他providerはそれぞれ正常表示(githubActionsCardsは "取得に失敗" ではなくfetchedデータの描画)
    assert "取得に失敗しました" not in plan["githubActionsCards"]
    assert "取得に失敗しました" not in plan["claudeCodeUsageCards"]
    assert "取得に失敗しました" not in plan["codexUsageCards"]


# ---------------------------------------------------------------------------
# github (rate limit)単独失敗
# ---------------------------------------------------------------------------


def test_github_rate_limit_failure_isolated():
    plan = _plan(_all_ok_results(github=_fail()))

    assert "GitHub API Rate Limitの取得に失敗しました" in plan["githubCards"]
    assert "GitHub API Rate Limitの取得に失敗しました" not in plan["githubActionsCards"]


# ---------------------------------------------------------------------------
# 全endpoint成功時は通常どおり描画される(regressionでないことの確認)
# ---------------------------------------------------------------------------


def test_all_success_renders_normally_without_error_messages():
    plan = _plan(_all_ok_results())

    for key, html in plan.items():
        assert "取得に失敗しました" not in html, f"{key} unexpectedly shows a failure message"


# ---------------------------------------------------------------------------
# Safe error: raw response bodyがDOMへ出ない
# ---------------------------------------------------------------------------


def test_failure_result_carries_no_body_so_nothing_can_leak():
    # fetchJsonSafeの{ok:false}は本文を一切保持しない設計そのものが、生bodyの
    # 漏洩を構造的に防ぐ -- {ok:false}だけを渡してもリークしようがないことを確認する。
    plan = _plan(_all_ok_results(githubActionsBilling=_fail()))
    html = plan["githubActionsCards"]

    assert '{"detail"' not in html
    assert "Not Found" not in html
    assert "SECRET_MARKER" not in html
    assert "Traceback" not in html
    assert "<html" not in html.lower()
    assert "undefined" not in html
    assert "NaN" not in html


def test_all_providers_failing_never_leak_any_body_text():
    results = {
        "dashboard": _fail(),
        "github": _fail(),
        "githubActionsBilling": _fail(),
        "claudeAuto": _fail(),
        "claudeManual": _fail(),
        "codexRateLimits": _fail(),
        "codexUsage": _fail(),
    }
    plan = _plan(results)

    for key, html in plan.items():
        assert '{"detail"' not in html, f"{key} leaked raw JSON body"
        assert "SECRET_MARKER" not in html, f"{key} leaked a secret marker"
        assert "undefined" not in html, f"{key} rendered literal undefined"
        assert "NaN" not in html, f"{key} rendered literal NaN"
        assert "取得に失敗しました" in html


# ---------------------------------------------------------------------------
# compactProviderErrorMessage / compactProviderErrorHtml: pure helpers
# ---------------------------------------------------------------------------


def test_provider_error_message_fixed_per_provider():
    assert run_compact_js('compact.compactProviderErrorMessage("dashboard")') == "Dashboard情報の取得に失敗しました"
    assert run_compact_js('compact.compactProviderErrorMessage("github")') == "GitHub API Rate Limitの取得に失敗しました"
    assert (
        run_compact_js('compact.compactProviderErrorMessage("githubActionsBilling")')
        == "GitHub Actions情報の取得に失敗しました"
    )
    assert run_compact_js('compact.compactProviderErrorMessage("claudeCodeUsage")') == "Claude Code Usageの取得に失敗しました"
    assert run_compact_js('compact.compactProviderErrorMessage("codexUsage")') == "Codex Usageの取得に失敗しました"


def test_provider_error_message_unknown_key_falls_back_to_generic():
    assert run_compact_js('compact.compactProviderErrorMessage("something_unexpected")') == "取得に失敗しました"


def test_provider_error_html_escapes_and_wraps_in_empty_card():
    html = run_compact_js('compact.compactProviderErrorHtml("dashboard")')
    assert "compact-card" in html
    assert "compact-empty" in html
    assert "Dashboard情報の取得に失敗しました" in html


# ---------------------------------------------------------------------------
# 未取得 vs 取得失敗の区別: fetchが成功しflag=falseの"未取得"状態は、
# provider errorメッセージへ置き換わらない(既存契約を壊さない)
# ---------------------------------------------------------------------------


def test_fetched_false_success_response_is_not_treated_as_fetch_failure():
    plan = _plan(_all_ok_results(githubActionsBilling=_ok({**GITHUB_ACTIONS_BILLING_OK, "fetched": False, "error": None})))
    assert "GitHub Actions情報の取得に失敗しました" not in plan["githubActionsCards"]
    assert "未取得" in plan["githubActionsCards"]
