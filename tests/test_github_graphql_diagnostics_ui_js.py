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
# Finding 8: githubGraphqlDiagnosticsSessionCardHtml's "相関状態" line must
# come from last_sample.attribution_status (under the same reset-boundary-
# safety condition as "現在のGraphQL used"), never from the stale
# session.attribution_status baseline snapshot.
# ---------------------------------------------------------------------------


def test_session_card_uses_last_sample_attribution_when_window_matches():
    # session.attribution_status and lastSample.attribution_status are
    # deliberately different, to prove which one wins.
    session = _session(attribution_status="SINGLE_ACTIVITY_CORRELATION")
    last_sample = _sample(
        attribution_status="OVERLAPPING_ACTIVITIES",
        graphql_reset_at=session["reset_at_start"],
        collected_at="2026-08-10T00:10:00+00:00",
    )
    html = run_app_js(
        f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session)}, {json.dumps(last_sample)})"
    )
    assert "複数Activityが重複（個別内訳不可）" in html
    assert "単一Activityと時間的相関" not in html


def test_session_card_shows_not_yet_observed_when_last_sample_is_null():
    session = _session()
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session)}, null)")
    assert "まだ観測なし" in html
    assert "単一Activityと時間的相関" not in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_session_card_shows_not_yet_observed_when_last_sample_reset_window_differs():
    session = _session(reset_at_start="2026-08-10T01:00:00+00:00")
    last_sample = _sample(graphql_reset_at="2026-08-10T05:00:00+00:00", collected_at="2026-08-10T04:00:00+00:00")
    html = run_app_js(
        f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session)}, {json.dumps(last_sample)})"
    )
    assert "まだ観測なし" in html
    assert "単一Activityと時間的相関" not in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_session_card_shows_not_yet_observed_when_last_sample_collected_before_session_started():
    session = _session(started_at="2026-08-10T00:10:00+00:00", reset_at_start="2026-08-10T01:00:00+00:00")
    # Same reset window, but collected BEFORE the session started -- must not
    # be treated as covering this session.
    last_sample = _sample(
        graphql_reset_at="2026-08-10T01:00:00+00:00",
        collected_at="2026-08-10T00:00:00+00:00",
        attribution_status="OVERLAPPING_ACTIVITIES",
    )
    html = run_app_js(
        f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session)}, {json.dumps(last_sample)})"
    )
    assert "まだ観測なし" in html
    assert "複数Activityが重複（個別内訳不可）" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsSessionComparisonTableHtml (Recent Activity Sessions)
# ---------------------------------------------------------------------------


def _completed_session(**overrides):
    base = _session(
        id=7,
        ended_at="2026-08-10T00:05:00+00:00",
        graphql_used_start=500,
        graphql_used_end=560,
        graphql_delta_total=60,
        attribution_status="SINGLE_ACTIVITY_CORRELATION",
        status="STOPPED",
        stop_reason="USER_STOP",
    )
    base["max_valid_interval_delta"] = 30
    base.update(overrides)
    return base


def test_session_comparison_table_empty_shows_no_history_message():
    html = run_app_js("app.githubGraphqlDiagnosticsSessionComparisonTableHtml([])")
    assert "履歴はありません" in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_session_comparison_table_shows_all_fields_for_completed_session():
    session = _completed_session(
        actor_type="claude_code",
        label="PR #25 review",
        repository="owner/repo",
        pr_number=25,
    )
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session)}])")
    assert "PR #25 review" in html
    assert "owner/repo" in html
    assert "#25" in html
    assert "500" in html  # graphql_used_start
    assert "560" in html  # graphql_used_end
    assert "60" in html  # graphql_delta_total
    assert "30" in html  # max_valid_interval_delta
    assert "終了（手動）" in html  # STOPPED
    assert "ユーザー操作による終了" in html  # USER_STOP
    assert "undefined" not in html
    assert "NaN" not in html


def test_session_comparison_table_null_delta_with_both_endpoints_measured_shows_anomaly_note():
    session = _completed_session(graphql_used_start=500, graphql_used_end=560, graphql_delta_total=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session)}])")
    assert "reset境界または異常検出のため差分判定不可" in html


def test_session_comparison_table_null_start_used_shows_bare_dash_not_anomaly_note():
    session = _completed_session(graphql_used_start=None, graphql_used_end=None, graphql_delta_total=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session)}])")
    assert "reset境界または異常検出のため差分判定不可" not in html
    assert "—" in html


def test_session_comparison_table_never_shows_session_attribution_status():
    session = _completed_session(attribution_status="OVERLAPPING_ACTIVITIES")
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session)}])")
    assert "OVERLAPPING_ACTIVITIES" not in html
    assert "複数Activityが重複（個別内訳不可）" not in html
    assert "単一Activityと時間的相関" not in html


def test_session_comparison_table_active_session_shows_measuring_in_progress():
    session = _session(ended_at=None, status="ACTIVE", stop_reason=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session)}])")
    assert "計測中" in html
    assert "undefined" not in html
    assert "NaN" not in html


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsActiveSessionsAtSample
# ---------------------------------------------------------------------------


def test_active_sessions_at_sample_empty_when_none_cover_instant():
    session = _session(started_at="2026-08-10T01:00:00+00:00", ended_at="2026-08-10T02:00:00+00:00")
    sample = _sample(collected_at="2026-08-10T00:00:00+00:00")
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(sample)}, [{json.dumps(session)}])"
    )
    assert result == []


def test_active_sessions_at_sample_one_session_covers_instant():
    session = _session(id=1, started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    sample = _sample(collected_at="2026-08-10T00:05:00+00:00")
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(sample)}, [{json.dumps(session)}])"
    )
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_active_sessions_at_sample_two_overlapping_sessions_both_returned():
    session_a = _session(id=1, started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    session_b = _session(id=2, started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    sample = _sample(collected_at="2026-08-10T00:05:00+00:00")
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(sample)}, "
        f"[{json.dumps(session_a)}, {json.dumps(session_b)}])"
    )
    assert len(result) == 2


def test_active_sessions_at_sample_boundary_inclusive_at_ended_at():
    session = _session(started_at="2026-08-10T00:00:00+00:00", ended_at="2026-08-10T00:05:00+00:00")
    sample = _sample(collected_at="2026-08-10T00:05:00+00:00")
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(sample)}, [{json.dumps(session)}])"
    )
    assert len(result) == 1


def test_active_sessions_at_sample_boundary_inclusive_at_started_at():
    session = _session(started_at="2026-08-10T00:05:00+00:00", ended_at=None)
    sample = _sample(collected_at="2026-08-10T00:05:00+00:00")
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(sample)}, [{json.dumps(session)}])"
    )
    assert len(result) == 1


def test_active_sessions_at_sample_excludes_session_from_its_own_start_baseline_sample():
    # Regression guard found during this round's manual UI check: a
    # session's own start-baseline sample must NOT list that session as
    # "active", because the backend's attribution_status for that exact
    # sample (Finding 2) was classified using only PRE-EXISTING sessions --
    # showing the just-started session here would contradict a
    # SINGLE_ACTIVITY_CORRELATION/UNATTRIBUTED status stored on the same row.
    session_a = _session(id=1, label="Session A", started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    session_b = _session(id=2, label="Session B", started_at="2026-08-10T00:05:00+00:00", ended_at=None)
    baseline_sample = _sample(
        collected_at="2026-08-10T00:05:00+00:00",
        trigger_session_id=2,
        attribution_status="SINGLE_ACTIVITY_CORRELATION",
    )
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(baseline_sample)}, "
        f"[{json.dumps(session_a)}, {json.dumps(session_b)}])"
    )
    assert [row["id"] for row in result] == [1]


def test_active_sessions_at_sample_includes_stopping_session_on_its_own_final_sample():
    # The stop case is intentionally NOT excluded: the backend includes the
    # stopping session in active_session_count for its own final sample
    # (it was active for the whole interval leading up to that instant), so
    # the frontend's label list must agree and still show it.
    session = _session(
        id=1,
        label="Session A",
        started_at="2026-08-10T00:00:00+00:00",
        ended_at="2026-08-10T00:10:00+00:00",
    )
    final_sample = _sample(collected_at="2026-08-10T00:10:00+00:00", trigger_session_id=1)
    result = run_app_js(
        f"app.githubGraphqlDiagnosticsActiveSessionsAtSample({json.dumps(final_sample)}, [{json.dumps(session)}])"
    )
    assert [row["id"] for row in result] == [1]


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsTimelineTableHtml / githubGraphqlDiagnosticsTimelineRowHtml
# ---------------------------------------------------------------------------


def test_timeline_table_null_delta_shows_dash_not_zero_or_undefined_or_nan():
    sample = _sample(graphql_delta=None)
    html = run_app_js(f"app.githubGraphqlDiagnosticsTimelineTableHtml([{json.dumps(sample)}], [])")
    assert "—" in html
    assert "undefined" not in html
    assert "NaN" not in html
    # graphql_delta was explicitly null -- it must not be displayed as 0.
    assert ">0<" not in html


def test_timeline_table_two_active_sessions_shows_both_labels_no_numeric_split():
    session_a = _session(id=1, label="PR #25 review", started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    session_b = _session(id=2, label="Issue triage", started_at="2026-08-10T00:00:00+00:00", ended_at=None)
    sample = _sample(collected_at="2026-08-10T00:05:00+00:00")
    html = run_app_js(
        f"app.githubGraphqlDiagnosticsTimelineTableHtml([{json.dumps(sample)}], "
        f"[{json.dumps(session_a)}, {json.dumps(session_b)}])"
    )
    assert "PR #25 review" in html
    assert "Issue triage" in html
    for forbidden in ("それぞれ", "按分", "50%ずつ", "均等"):
        assert forbidden not in html


def test_timeline_table_empty_samples_shows_no_samples_message():
    html = run_app_js("app.githubGraphqlDiagnosticsTimelineTableHtml([], [])")
    assert "サンプルはありません" in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_timeline_row_html_handles_all_null_delta_fetch_statuses_safely():
    for fetch_status in ("reset_boundary", "counter_regression", "fetch_failed", "no_previous"):
        sample = _sample(fetch_status=fetch_status, graphql_delta=None)
        html = run_app_js(f"app.githubGraphqlDiagnosticsTimelineRowHtml({json.dumps(sample)}, [])")
        assert "undefined" not in html
        assert "NaN" not in html
        assert "—" in html


# ---------------------------------------------------------------------------
# Regression guard across all new rendering functions
# ---------------------------------------------------------------------------


def test_regression_new_functions_never_leak_undefined_nan_or_forbidden_claims():
    session_active = _session()
    session_completed = _completed_session()
    sample_with_delta = _sample()
    sample_null_delta = _sample(graphql_delta=None, fetch_status="reset_boundary")

    htmls = [
        run_app_js(
            f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session_active)}, {json.dumps(sample_with_delta)})"
        ),
        run_app_js(f"app.githubGraphqlDiagnosticsSessionCardHtml({json.dumps(session_active)}, null)"),
        run_app_js(
            f"app.githubGraphqlDiagnosticsSessionComparisonTableHtml([{json.dumps(session_active)}, {json.dumps(session_completed)}])"
        ),
        run_app_js("app.githubGraphqlDiagnosticsSessionComparisonTableHtml([])"),
        run_app_js(
            f"app.githubGraphqlDiagnosticsTimelineTableHtml([{json.dumps(sample_with_delta)}, {json.dumps(sample_null_delta)}], "
            f"[{json.dumps(session_active)}])"
        ),
        run_app_js("app.githubGraphqlDiagnosticsTimelineTableHtml([], [])"),
    ]
    forbidden_strings = ("undefined", "NaN", "が消費しました", "confirmed", "exact")
    for html in htmls:
        for forbidden in forbidden_strings:
            assert forbidden not in html, f"'{forbidden}' leaked into rendered HTML: {html}"


# ---------------------------------------------------------------------------
# syntax check
# ---------------------------------------------------------------------------


def test_app_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
