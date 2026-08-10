"""Pure-function tests for the main dashboard's GitHub GraphQL Consumption
Diagnostics panel (static/app.js).

Non-negotiable meaning this feature must preserve everywhere: this shows a
*temporal correlation* between an Activity and an observed GraphQL used
delta, never *exact attribution*. GitHub does not expose which
client/process/token consumed GraphQL points, so wording must never claim a
specific consumer "used"/"consumed" quota, and when 2+ sessions overlap
there must be no fabricated per-session numeric split.

Mirrors this codebase's established node-subprocess pure-function testing
convention (see test_github_rate_limit_banners_js.py /
test_github_actions_billing_ui_js.py) -- no DOM, no jsdom.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"


def run_app_js(expression: str):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsAttributionLabel: all 6 AttributionStatus values
# ---------------------------------------------------------------------------


def test_attribution_label_all_six_statuses():
    cases = {
        "UNATTRIBUTED": "相関候補なし",
        "SINGLE_ACTIVITY_CORRELATION": "単一Activityと時間的相関",
        "OVERLAPPING_ACTIVITIES": "複数Activityが重複（個別内訳不可）",
        "RESET_BOUNDARY": "reset境界のため差分判定不可",
        "COUNTER_REGRESSION": "カウンタ減少を検出（差分判定不可）",
        "FETCH_FAILED": "取得失敗",
    }
    for status, expected in cases.items():
        assert run_app_js(f'app.githubGraphqlDiagnosticsAttributionLabel("{status}")') == expected


def test_attribution_label_unknown_value_falls_back_safely():
    label = run_app_js('app.githubGraphqlDiagnosticsAttributionLabel("SOMETHING_UNKNOWN")')
    assert label == "不明"


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsRenderPanel: enabled:false
# ---------------------------------------------------------------------------


def test_panel_disabled_shows_message_and_no_start_form():
    html = run_app_js('app.githubGraphqlDiagnosticsRenderPanel({"enabled": false, "sampler_running": false, "sample_seconds": 10, "max_minutes": 15, "active_sessions": [], "last_sample": null})')
    assert "無効" in html
    assert "GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED" in html
    assert "<form" not in html
    assert "undefined" not in html
    assert "NaN" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsRenderPanel: one active session + last_sample
# (same reset window)
# ---------------------------------------------------------------------------


def _session(**overrides):
    base = {
        "id": 1,
        "actor_type": "claude_code",
        "label": "PR #25 review",
        "repository": "owner/repo",
        "pr_number": 25,
        "started_at": "2026-08-10T00:00:00+00:00",
        "ended_at": None,
        "github_login": "octocat",
        "github_user_id": 123,
        "reset_at_start": "2026-08-10T01:00:00+00:00",
        "graphql_used_start": 500,
        "graphql_used_end": None,
        "graphql_delta_total": None,
        "attribution_status": "SINGLE_ACTIVITY_CORRELATION",
        "status": "ACTIVE",
        "stop_reason": None,
    }
    base.update(overrides)
    return base


def _sample(**overrides):
    base = {
        "id": 10,
        "collected_at": "2026-08-10T00:05:00+00:00",
        "core_used": 20,
        "graphql_used": 530,
        "search_used": 0,
        "graphql_limit": 5000,
        "graphql_remaining": 4470,
        "graphql_reset_at": "2026-08-10T01:00:00+00:00",
        "graphql_delta": 30,
        "fetch_status": "ok",
        "attribution_status": "SINGLE_ACTIVITY_CORRELATION",
        "trigger_session_id": None,
    }
    base.update(overrides)
    return base


def _panel_data(**overrides):
    base = {
        "enabled": True,
        "sampler_running": True,
        "sample_seconds": 10,
        "max_minutes": 15,
        "active_sessions": [],
        "last_sample": None,
    }
    base.update(overrides)
    return base


def test_panel_enabled_with_active_session_and_matching_last_sample_shows_delta():
    data = _panel_data(active_sessions=[_session()], last_sample=_sample())
    html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(data)})")
    assert "<form" in html
    assert "530" in html  # last_sample.graphql_used
    assert "単一Activityと時間的相関" in html
    # 生のenum文字列がuser-facing labelとして単独露出していない(data属性としてなら可)
    assert 'data-attribution-status="SINGLE_ACTIVITY_CORRELATION"' in html
    assert "undefined" not in html
    assert "NaN" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsRenderPanel: last_sample crosses a reset boundary
# ---------------------------------------------------------------------------


def test_panel_shows_reset_boundary_message_when_reset_at_differs():
    session = _session(reset_at_start="2026-08-10T01:00:00+00:00")
    sample = _sample(graphql_reset_at="2026-08-10T02:00:00+00:00", graphql_used=999999)
    data = _panel_data(active_sessions=[session], last_sample=sample)
    html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(data)})")
    assert "reset境界を跨いだため判定不可" in html
    # stale/不正確な数値(999999)をそのまま「現在のGraphQL used」として出さない
    assert "999999" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsRenderPanel: last_sample is null
# ---------------------------------------------------------------------------


def test_panel_with_null_last_sample_does_not_crash_or_leak_undefined():
    data = _panel_data(active_sessions=[_session()], last_sample=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(data)})")
    assert "undefined" not in html
    assert "NaN" not in html
    assert "最終観測: 未取得" in html


def test_panel_with_no_active_sessions_and_no_last_sample():
    data = _panel_data(active_sessions=[], last_sample=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(data)})")
    assert "計測中のActivityはありません" in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_panel_with_null_data_shows_not_fetched_state():
    html = run_app_js("app.githubGraphqlDiagnosticsRenderPanel(null)")
    assert "未取得" in html
    assert "undefined" not in html
    assert "NaN" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsRenderPanel: 2 active sessions -- no fabricated split
# ---------------------------------------------------------------------------


def test_panel_with_two_overlapping_sessions_shows_both_and_no_fabricated_split():
    session_a = _session(id=1, label="PR #25 review", attribution_status="OVERLAPPING_ACTIVITIES")
    session_b = _session(id=2, label="Issue triage", attribution_status="OVERLAPPING_ACTIVITIES")
    data = _panel_data(active_sessions=[session_a, session_b], last_sample=_sample(attribution_status="OVERLAPPING_ACTIVITIES"))
    html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(data)})")
    assert "PR #25 review" in html
    assert "Issue triage" in html
    assert html.count('data-session-id="1"') >= 1
    assert html.count('data-session-id="2"') >= 1
    assert "複数Activityが重複（個別内訳不可）" in html
    # 按分/個別内訳を示唆する表現がどこにも出ない
    for forbidden in ("それぞれ", "按分", "50%ずつ", "均等"):
        assert forbidden not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsErrorDisplay: 400/409/502/404-shaped bodies
# ---------------------------------------------------------------------------


def test_error_display_400_diagnostics_disabled_uses_user_message():
    body = {
        "detail": {
            "error_type": "diagnostics_disabled",
            "user_message": "GraphQL消費診断は現在無効です。GITHUB_GRAPHQL_DIAGNOSTICS_ENABLEDを確認してください。",
        }
    }
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(400, {json.dumps(body)})")
    assert resolved["error_type"] == "diagnostics_disabled"
    assert "GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED" in resolved["user_message"]


def test_error_display_409_account_context_changed_uses_user_message():
    body = {
        "detail": {
            "error_type": "account_context_changed",
            "user_message": "現在activeな診断Sessionと異なるGitHubアカウントが検出されました。既存Sessionを終了してから開始してください。",
        }
    }
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(409, {json.dumps(body)})")
    assert resolved["error_type"] == "account_context_changed"
    assert "既存Session" in resolved["user_message"]


def test_error_display_502_identity_fetch_failed_uses_user_message():
    body = {"detail": {"error_type": "identity_fetch_failed", "user_message": "GitHubアカウント情報の取得に失敗しました。"}}
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(502, {json.dumps(body)})")
    assert resolved["error_type"] == "identity_fetch_failed"
    assert resolved["user_message"] == "GitHubアカウント情報の取得に失敗しました。"


def test_error_display_404_session_not_found_uses_user_message():
    body = {"detail": {"error_type": "session_not_found", "user_message": "指定されたSessionが見つかりません。"}}
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(404, {json.dumps(body)})")
    assert resolved["error_type"] == "session_not_found"
    assert resolved["user_message"] == "指定されたSessionが見つかりません。"


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsErrorDisplay: malformed/unexpected error body
# ---------------------------------------------------------------------------


def test_error_display_malformed_body_falls_back_to_generic():
    resolved = run_app_js('app.githubGraphqlDiagnosticsErrorDisplay(500, {"detail": "Internal Server Error"})')
    assert resolved["error_type"] == "unknown_error"
    assert "Internal Server Error" not in resolved["user_message"]
    assert resolved["user_message"]


def test_error_display_null_body_falls_back_to_generic():
    resolved = run_app_js("app.githubGraphqlDiagnosticsErrorDisplay(500, null)")
    assert resolved["error_type"] == "unknown_error"
    assert resolved["user_message"]


def test_error_display_unknown_status_falls_back_even_with_valid_shaped_body():
    # statusが既知の4xx/5xx集合に無い場合は、bodyのshapeが正しくてもgenericへfallbackする。
    body = {"detail": {"error_type": "diagnostics_disabled", "user_message": "何か"}}
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(200, {json.dumps(body)})")
    assert resolved["error_type"] == "unknown_error"


def test_error_display_html_like_body_never_echoed():
    body = {"detail": "<html><body>Traceback (most recent call last)</body></html>"}
    resolved = run_app_js(f"app.githubGraphqlDiagnosticsErrorDisplay(502, {json.dumps(body)})")
    assert "<html" not in resolved["user_message"]
    assert "Traceback" not in resolved["user_message"]


# ---------------------------------------------------------------------------
# Regression guards across all fixtures above
# ---------------------------------------------------------------------------


def test_regression_no_literal_undefined_or_nan_across_all_panel_fixtures():
    fixtures = [
        None,
        {"enabled": False, "sampler_running": False, "sample_seconds": 10, "max_minutes": 15, "active_sessions": [], "last_sample": None},
        _panel_data(active_sessions=[_session()], last_sample=_sample()),
        _panel_data(active_sessions=[_session(), _session(id=2)], last_sample=_sample()),
        _panel_data(active_sessions=[], last_sample=None),
        _panel_data(
            active_sessions=[_session(reset_at_start="2026-08-10T01:00:00+00:00")],
            last_sample=_sample(graphql_reset_at="2026-08-10T05:00:00+00:00"),
        ),
    ]
    for fixture in fixtures:
        html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(fixture)})")
        assert "undefined" not in html, f"leaked literal undefined for fixture: {fixture}"
        assert "NaN" not in html, f"leaked literal NaN for fixture: {fixture}"


def test_regression_never_claims_exact_or_confirmed_consumption():
    # user-facingなレンダリング出力だけを対象にする(開発者向けコメントが英語で
    # "exact attribution"という概念自体を説明することは許容される -- 禁止されて
    # いるのはuser-facingな出力がこれらの語を主張することであり、コード
    # コメントの技術的な説明ではない)。
    fixtures = [
        {"enabled": False, "sampler_running": False, "sample_seconds": 10, "max_minutes": 15, "active_sessions": [], "last_sample": None},
        _panel_data(active_sessions=[], last_sample=None),
        _panel_data(active_sessions=[_session()], last_sample=_sample()),
        _panel_data(active_sessions=[_session(), _session(id=2)], last_sample=_sample(attribution_status="OVERLAPPING_ACTIVITIES")),
        _panel_data(
            active_sessions=[_session(reset_at_start="2026-08-10T01:00:00+00:00")],
            last_sample=_sample(graphql_reset_at="2026-08-10T05:00:00+00:00"),
        ),
    ]
    forbidden_strings = ("が消費しました", "confirmed", "exact")
    for fixture in fixtures:
        html = run_app_js(f"app.githubGraphqlDiagnosticsRenderPanel({json.dumps(fixture)})")
        for forbidden in forbidden_strings:
            assert forbidden not in html, f"'{forbidden}' leaked into rendered panel HTML for fixture: {fixture}"

    for status in (
        "UNATTRIBUTED",
        "SINGLE_ACTIVITY_CORRELATION",
        "OVERLAPPING_ACTIVITIES",
        "RESET_BOUNDARY",
        "COUNTER_REGRESSION",
        "FETCH_FAILED",
    ):
        label = run_app_js(f'app.githubGraphqlDiagnosticsAttributionLabel("{status}")')
        for forbidden in forbidden_strings:
            assert forbidden not in label


# ---------------------------------------------------------------------------
# syntax check
# ---------------------------------------------------------------------------


def test_app_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
