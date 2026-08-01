import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.github_rate_limit_state import GitHubRateLimitController
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
COMPACT_HTML = ROOT / "static" / "compact.html"
COMPACT_JS = ROOT / "static" / "compact.js"
COMPACT_CSS = ROOT / "static" / "compact.css"


@pytest.fixture(autouse=True)
def default_basic_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BASIC_AUTH", "false")


def run_compact_js(expression: str):
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
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


SAMPLE_ROW_WITH_MAX = {
    "limit_id": 1,
    "service_id": 1,
    "service_name": "OpenAI GPT",
    "provider": "OpenAI",
    "plan_name": "API",
    "account_type": "api",
    "model_name": "gpt-5",
    "limit_type": "tokens",
    "max_value": 1000.0,
    "used_value": 400.0,
    "remaining_value": 600.0,
    "usage_percent": 40.0,
    "unit": "tokens",
    "next_reset_at": "2999-01-01T00:00:00+00:00",
    "source_type": "manual_required",
    "status": "正常",
    "last_updated_at": "2026-01-01T00:00:00+00:00",
}

SAMPLE_ROW_NO_MAX = {
    **SAMPLE_ROW_WITH_MAX,
    "max_value": None,
    "remaining_value": None,
    "usage_percent": None,
    "status": "手入力待ち",
}

GITHUB_RESOURCE_BASE = {
    "limit": 5000,
    "used": 100,
    "remaining": 4900,
    "usage_percent": 2.0,
    "remaining_percent": 98.0,
    "reset_at_utc": "2999-01-01T00:00:00+00:00",
    "reset_at_local": "2999-01-01T09:00:00+09:00",
    "seconds_until_reset": 3600,
    "error_message": None,
    "status": "Normal",
}


# 1. GET /compact が200
def test_compact_route_returns_200() -> None:
    client = TestClient(app)
    response = client.get("/compact")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# 2. 通常画面/へ影響なし
def test_admin_root_screen_unaffected() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Cloud LLM Limit Checker" in response.text
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    index_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    index_css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
    assert "compact" not in index_html.lower()
    assert "compact" not in index_js.lower()
    assert "compact" not in index_css.lower()


# 3. compact画面に編集フォームなし
def test_compact_html_has_no_edit_or_registration_forms() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "<form" not in html
    assert "PUT" not in js
    assert "/api/limits" not in js
    assert "/api/services" not in js


# 4. Collector UIなし
def test_compact_has_no_collector_ui() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    for marker in ("Collector", "/api/collect"):
        assert marker not in html
        assert marker not in js


# 5. Export UIなし
def test_compact_has_no_export_ui() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    for marker in ("Export", "/api/export"):
        assert marker not in html
        assert marker not in js


# 6. 外部API用POSTなし
def test_compact_js_never_sends_post_requests() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "POST" not in js
    assert "method:" not in js
    assert "/api/github-rate-limit/refresh" not in js


# 7. 30秒GET更新を維持
def test_compact_js_declares_thirty_second_refresh_interval() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "REFRESH_INTERVAL_MS = 30000" in js
    assert "setInterval(loadCompact, REFRESH_INTERVAL_MS)" in js


# 8. 1100px以上で3列相当のCSS
def test_grid_uses_three_columns_at_1100px() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "@media (min-width: 1100px)" in css
    assert "repeat(3, 1fr)" in css


# 9. 700〜1099pxで2列
def test_grid_uses_two_columns_at_700px() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "@media (min-width: 700px)" in css
    assert "repeat(2, 1fr)" in css


# 10. 699px以下で1列
def test_grid_is_single_column_by_default() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "grid-template-columns: 1fr;" in css


# 11. ダーク背景色が設定されている
def test_dark_background_colors_present() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "background: #0f1419" in css
    assert "background: #132028" in css
    assert "color-scheme: dark" in css
    html = COMPACT_HTML.read_text(encoding="utf-8")
    assert 'content="dark"' in html


# 12. primary textの高コントラスト
def test_primary_text_color_is_high_contrast_light() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "color: #e6edf3" in css


# 13. 上限登録済みで残り%表示
def test_dashboard_limit_with_percent_renders_percentages_and_source_label() -> None:
    html = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_WITH_MAX)})")
    assert "OpenAI GPT" in html
    assert "60%" in html
    assert "40%" in html
    assert "手入力" in html


# 14. 上限未登録で%非表示
def test_dashboard_limit_without_max_value_shows_not_registered() -> None:
    html = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_NO_MAX)})")
    assert "上限未登録" in html
    assert "compact-percent-value" not in html


# 15. 上限未登録カードが大きな案内ボックスを持たない
def test_no_limit_card_stays_compact_without_duplicate_text() -> None:
    html = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_NO_MAX)})")
    assert html.count("上限未登録") == 1
    assert "compact-usage-line" not in html
    assert "compact-meter" not in html
    # reset/最終更新/取得元は上限未登録でも表示され続ける
    assert "reset:" in html
    assert "更新:" in html
    assert "手入力" in html


# 16. status優先ソート
def test_sort_dashboard_rows_orders_by_status_priority() -> None:
    rows = [
        {**SAMPLE_ROW_WITH_MAX, "service_name": "Z", "status": "正常"},
        {**SAMPLE_ROW_WITH_MAX, "service_name": "A", "status": "危険"},
        {**SAMPLE_ROW_WITH_MAX, "service_name": "M", "status": "注意"},
        {**SAMPLE_ROW_NO_MAX, "service_name": "B", "status": "手入力待ち"},
    ]
    sorted_names = run_compact_js(f"compact.sortDashboardRows({json.dumps(rows)}).map(r => r.service_name)")
    assert sorted_names == ["A", "M", "Z", "B"]


# 17. 同順位で安定ソート
def test_sort_dashboard_rows_is_deterministic_within_same_status() -> None:
    rows = [
        {**SAMPLE_ROW_WITH_MAX, "service_name": "Beta", "model_name": "m1", "status": "正常"},
        {**SAMPLE_ROW_WITH_MAX, "service_name": "Alpha", "model_name": "m1", "status": "正常"},
        {**SAMPLE_ROW_WITH_MAX, "service_name": "Alpha", "model_name": "a-model", "status": "正常"},
    ]
    sorted_rows = run_compact_js(f"compact.sortDashboardRows({json.dumps(rows)})")
    names = [f"{r['service_name']} {r['model_name']}" for r in sorted_rows]

    rows_reordered = list(reversed(rows))
    sorted_again = run_compact_js(f"compact.sortDashboardRows({json.dumps(rows_reordered)})")
    names_again = [f"{r['service_name']} {r['model_name']}" for r in sorted_again]

    assert names == names_again


# 18. GitHub Overall表示
def test_github_overall_html_shows_status_and_reason() -> None:
    html = run_compact_js('compact.githubOverallHtml({status: "Normal", reason: "all clear"})')
    assert "Normal" in html
    assert "all clear" in html
    assert "compact-status-normal" in html


# 19. GitHub REST/GraphQL/Search個別表示
def test_github_resource_card_shows_resource_labels() -> None:
    core_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps({**GITHUB_RESOURCE_BASE, 'resource': 'core'})})")
    graphql_html = run_compact_js(
        f"compact.githubResourceCardHtml({json.dumps({**GITHUB_RESOURCE_BASE, 'resource': 'graphql'})})"
    )
    search_html = run_compact_js(
        f"compact.githubResourceCardHtml({json.dumps({**GITHUB_RESOURCE_BASE, 'resource': 'search'})})"
    )
    assert "REST" in core_html
    assert "GraphQL" in graphql_html
    assert "Search" in search_html


# 20. GraphQL ExhaustedでOverall Limited
def test_graphql_exhausted_and_overall_limited_use_exhausted_class() -> None:
    resource = {
        **GITHUB_RESOURCE_BASE,
        "resource": "graphql",
        "status": "Exhausted",
        "used": 5000,
        "remaining": 0,
        "usage_percent": 100.0,
        "remaining_percent": 0.0,
    }
    resource_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource)})")
    overall_html = run_compact_js('compact.githubOverallHtml({status: "Limited", reason: "GraphQL API exhausted"})')
    assert "compact-status-exhausted" in resource_html
    assert "compact-status-exhausted" in overall_html
    assert "Limited" in overall_html


# 21. Normal/Warning/Exhausted/Error/Unknown配色
def test_status_color_classes_defined_for_all_states() -> None:
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (
        "compact-status-normal",
        "compact-status-warning",
        "compact-status-exhausted",
        "compact-status-error",
        "compact-status-unknown",
    ):
        assert f".{cls} {{" in css

    assert run_compact_js('compact.githubStatusClass("Normal")') == "compact-status-normal"
    assert run_compact_js('compact.githubStatusClass("Warning")') == "compact-status-warning"
    assert run_compact_js('compact.githubStatusClass("Exhausted")') == "compact-status-exhausted"
    assert run_compact_js('compact.githubStatusClass("Error")') == "compact-status-error"
    assert run_compact_js('compact.githubStatusClass("Unknown")') == "compact-status-unknown"


# 22. reset超過で負数非表示
def test_reset_relative_text_handles_overdue_without_negative_number() -> None:
    result = run_compact_js("compact.resetRelativeText('2000-01-01T00:00:00Z')")
    assert result == "reset時刻超過"
    assert "-" not in result

    seconds_result = run_compact_js("compact.githubSecondsUntilResetText(-42)")
    assert seconds_result == "reset時刻超過"
    assert "-" not in seconds_result


# 23. Invalid Dateで不明
def test_fmt_date_or_unknown_invalid_string_is_unknown() -> None:
    assert run_compact_js('compact.fmtDateOrUnknown("not-a-date")') == "不明"
    formatted = run_compact_js('compact.fmtDateOrUnknown("2026-01-01T00:00:00+09:00")')
    assert formatted not in ("未更新", "不明")
    assert run_compact_js("compact.fmtDateOrUnknown(null)") == "未更新"


# 24. source_type表示
def test_source_type_label_maps_known_values() -> None:
    assert run_compact_js('compact.sourceTypeLabel("manual_required")') == "手入力"
    assert run_compact_js('compact.sourceTypeLabel("manual")') == "手入力"
    assert run_compact_js('compact.sourceTypeLabel("api_claude_management")') == "Claude API"


# 25. token/stdout/stderr非表示
def test_compact_files_do_not_leak_process_output_markers() -> None:
    for path in (COMPACT_HTML, COMPACT_JS, COMPACT_CSS):
        text = path.read_text(encoding="utf-8")
        for marker in ("stdout", "stderr", "token"):
            assert marker not in text.lower()


# 26. node --check static/compact.js success
def test_compact_js_passes_node_syntax_check() -> None:
    proc = subprocess.run(["node", "--check", str(COMPACT_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr


# ページロードでgh実行なし・外部API未実行(30秒GET更新の対象エンドポイントも含めて確認)
def test_compact_page_load_endpoints_never_call_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("fetch_github_rate_limit must not be called by page-load GET endpoints")

    monkeypatch.setattr("app.main.fetch_github_rate_limit", explode)
    app.state.github_rate_limit_controller = GitHubRateLimitController()

    with TestClient(app) as client:
        assert client.get("/compact").status_code == 200
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/github-rate-limit").status_code == 200
        assert client.get("/api/claude-code-usage").status_code == 200
        assert client.get("/api/codex-rate-limits").status_code == 200


# GitHub未取得表示
def test_github_section_unfetched_shows_not_fetched_message() -> None:
    html = run_compact_js("compact.githubSectionHtml({fetched: false, last_known: null})")
    assert "未取得" in html


CLAUDE_USAGE_AVAILABLE = {
    "available": True,
    "stale": False,
    "status": "ok",
    "observed_at": "2026-01-01T12:00:00+00:00",
    "source": "claude_code_statusline",
    "five_hour": {"used_percentage": 42.0, "remaining_percentage": 58.0, "resets_at": "2999-01-01T00:00:00+00:00"},
    "seven_day": {"used_percentage": 18.0, "remaining_percentage": 82.0, "resets_at": "2999-01-07T00:00:00+00:00"},
    "error_message": None,
}


# 24. /compactで5時間・7日を表示
def test_claude_code_section_shows_five_hour_and_seven_day() -> None:
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(CLAUDE_USAGE_AVAILABLE)})")
    assert "5時間枠" in html
    assert "7日枠" in html
    assert "58%" in html  # 5h remaining
    assert "42%" in html  # 5h used
    assert "82%" in html  # 7d remaining
    assert "18%" in html  # 7d used


# 24b. 片方欠落時はその枠だけ「未観測」
def test_claude_code_section_shows_partial_window_as_unobserved() -> None:
    data = {**CLAUDE_USAGE_AVAILABLE, "seven_day": None}
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert "5時間枠" in html
    assert "42%" in html
    assert "未観測" in html


# 25. stale表示
def test_claude_code_section_shows_stale_notice() -> None:
    data = {**CLAUDE_USAGE_AVAILABLE, "stale": True, "status": "stale"}
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert "最終観測" in html


# 26. 未観測表示
def test_claude_code_section_not_observed_shows_guidance_message() -> None:
    html = run_compact_js(
        'compact.claudeCodeSectionHtml({available: false, status: "not_observed"})'
    )
    assert "Claude Code実行後に取得" in html

    invalid_html = run_compact_js('compact.claudeCodeSectionHtml({available: false, status: "invalid_cache"})')
    assert "取得不可" in invalid_html


# 27. reset超過で負数を出さない
def test_claude_usage_window_reset_overdue_shows_no_negative_number() -> None:
    window = {"used_percentage": 90.0, "remaining_percentage": 10.0, "resets_at": "2000-01-01T00:00:00+00:00"}
    html = run_compact_js(f'compact.claudeUsageWindowHtml({json.dumps("5時間枠")}, {json.dumps(window)})')
    assert "reset時刻超過" in html
    assert not re.search(r"-\d", html)


CODEX_USAGE_AVAILABLE = {
    "available": True,
    "stale": False,
    "status": "ok",
    "observed_at": "2026-01-01T12:00:00+00:00",
    "source": "codex_manual",
    "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"},
    "weekly": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2999-01-08T00:00:00+00:00"},
    "error_message": None,
}


# 27. /compactで5時間枠表示 / 28. /compactで週次枠表示 / 29.「手動確認値」表示
def test_codex_usage_section_shows_five_hour_and_weekly_with_manual_badge() -> None:
    html = run_compact_js(f"compact.codexUsageSectionHtml(null, {json.dumps(CODEX_USAGE_AVAILABLE)})")
    assert "5時間枠" in html
    assert "週次枠" in html
    assert "60%" in html  # 5h remaining
    assert "40%" in html  # 5h used
    assert "80%" in html  # weekly remaining
    assert "20%" in html  # weekly used
    assert html.count("手動確認値") == 2


# 30. 未入力表示
def test_codex_usage_section_not_observed_shows_manual_input_guidance() -> None:
    html = run_compact_js(
        'compact.codexUsageSectionHtml({available: false, status: "not_observed"}, {available: false, status: "not_observed"})'
    )
    assert "自動取得または手動入力してください" in html


# 31. stale表示
def test_codex_usage_section_shows_stale_notice() -> None:
    data = {**CODEX_USAGE_AVAILABLE, "stale": True, "status": "stale"}
    html = run_compact_js(f"compact.codexUsageSectionHtml(null, {json.dumps(data)})")
    assert "手動確認値" in html
    assert "古い可能性があります" in html


# 32. invalid cache表示
def test_codex_usage_section_invalid_cache_shows_unavailable() -> None:
    html = run_compact_js(
        'compact.codexUsageSectionHtml({available: false, status: "invalid_cache"}, {available: false, status: "invalid_cache"})'
    )
    assert "取得不可" in html


# 33. reset超過で負数を出さず、古いpercentageを強調表示しない
def test_codex_usage_window_reset_overdue_hides_percentage_and_shows_no_negative_number() -> None:
    window = {"used_percentage": 90.0, "remaining_percentage": 10.0, "resets_at": "2000-01-01T00:00:00+00:00"}
    html = run_compact_js(f'compact.codexUsageWindowHtml({json.dumps("5時間枠")}, {json.dumps(window)})')
    assert "reset時刻超過" in html
    assert not re.search(r"-\d", html)
    assert "90%" not in html
    assert "compact-percent-value" not in html


# 週次だけ未入力の場合はその枠だけ「未入力」
def test_codex_usage_section_shows_partial_window_as_unentered() -> None:
    data = {**CODEX_USAGE_AVAILABLE, "weekly": None}
    html = run_compact_js(f"compact.codexUsageSectionHtml(null, {json.dumps(data)})")
    assert "5時間枠" in html
    assert "40%" in html
    assert "未入力" in html


# 34/35. ページロード時にPUTなし・GETのみ(codex-usageも含めてsubprocess未実行)
def test_compact_page_load_never_calls_subprocess_for_codex_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("/compact page-load endpoints must never invoke a subprocess for Codex usage")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    with TestClient(app) as client:
        assert client.get("/compact").status_code == 200
        assert client.get("/api/codex-usage").status_code == 200
        assert client.get("/api/codex-rate-limits").status_code == 200


def test_compact_js_never_invokes_codex_or_a_child_process() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    for marker in ("codex exec", "child_process", "spawn(", "execSync", "codex.exe"):
        assert marker not in js


def test_compact_js_never_calls_codex_rate_limits_refresh() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "/api/codex-rate-limits/refresh" not in js


# 39/40 (Codex App Server自動取得): ページロードでは/api/codex-rate-limitsのGETのみ、POSTは行わない
def test_compact_page_load_never_starts_codex_app_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("Codex App Server must never be started by page-load GET endpoints")

    monkeypatch.setattr("app.main.fetch_codex_rate_limits", explode)

    with TestClient(app) as client:
        assert client.get("/compact").status_code == 200
        assert client.get("/api/codex-rate-limits").status_code == 200


AUTO_RATE_LIMITS_FRESH = {
    "fetched": True,
    "available": True,
    "stale": False,
    "status": "ok",
    "observed_at": "2026-01-01T12:00:00+00:00",
    "source": "codex_app_server",
    "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00", "window_duration_minutes": 300},
    "weekly": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2999-01-08T00:00:00+00:00", "window_duration_minutes": 10080},
    "error_type": None,
    "user_message": None,
    "refresh_in_progress": False,
    "cooldown_remaining_seconds": 0,
    "fallback_available": True,
    "fallback_source": "codex_manual",
}


# 38: compactのsourceバッジ — 自動取得cacheが新鮮なら「自動取得」を優先表示する
def test_resolve_codex_display_prefers_fresh_auto_cache_over_manual() -> None:
    result = run_compact_js(
        f"compact.resolveCodexDisplay({json.dumps(AUTO_RATE_LIMITS_FRESH)}, {json.dumps(CODEX_USAGE_AVAILABLE)})"
    )
    assert result["source"] == "codex_app_server"
    assert result["badgeLabel"] == "自動取得"


# 34: 自動cache優先 — staleでも手動snapshotより自動cacheを優先する
def test_resolve_codex_display_prefers_stale_auto_cache_over_manual() -> None:
    stale_auto = {**AUTO_RATE_LIMITS_FRESH, "stale": True, "status": "stale"}
    result = run_compact_js(f"compact.resolveCodexDisplay({json.dumps(stale_auto)}, {json.dumps(CODEX_USAGE_AVAILABLE)})")
    assert result["source"] == "codex_app_server"
    assert result["badgeLabel"] == "最終自動取得値"


# 36: 手動fallback — 自動cacheが未取得の場合のみ手動snapshotを表示する
def test_resolve_codex_display_falls_back_to_manual_when_auto_unavailable() -> None:
    unavailable_auto = {"available": False, "status": "not_observed"}
    result = run_compact_js(
        f"compact.resolveCodexDisplay({json.dumps(unavailable_auto)}, {json.dumps(CODEX_USAGE_AVAILABLE)})"
    )
    assert result["source"] == "codex_manual"
    assert result["badgeLabel"] == "手動確認値"


# 37: 両方なし
def test_resolve_codex_display_both_unavailable() -> None:
    unavailable = {"available": False, "status": "not_observed"}
    result = run_compact_js(f"compact.resolveCodexDisplay({json.dumps(unavailable)}, {json.dumps(unavailable)})")
    assert result["source"] is None
    assert result["status"] == "not_observed"


def test_codex_usage_section_shows_auto_source_badge() -> None:
    html = run_compact_js(f"compact.codexUsageSectionHtml({json.dumps(AUTO_RATE_LIMITS_FRESH)}, null)")
    assert html.count("自動取得") >= 2
    assert "40%" in html
    assert "20%" in html
