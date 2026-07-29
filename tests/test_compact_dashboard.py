import json
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


# 1. GET /compact が200
def test_compact_route_returns_200() -> None:
    client = TestClient(app)
    response = client.get("/compact")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# 2. compact画面に編集フォームがない
def test_compact_html_has_no_edit_or_registration_forms() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "<form" not in html
    assert "PUT" not in js
    assert "/api/limits" not in js
    assert "/api/services" not in js


# 3. compact画面にCollector実行UIがない
def test_compact_has_no_collector_ui() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    for marker in ("Collector", "/api/collect"):
        assert marker not in html
        assert marker not in js


# 4. compact画面にExport UIがない
def test_compact_has_no_export_ui() -> None:
    html = COMPACT_HTML.read_text(encoding="utf-8")
    js = COMPACT_JS.read_text(encoding="utf-8")
    for marker in ("Export", "/api/export"):
        assert marker not in html
        assert marker not in js


# 5. dashboard APIからlimitを表示できる
def test_dashboard_limit_with_percent_renders_percentages_and_source_label() -> None:
    html = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_WITH_MAX)})")
    assert "OpenAI GPT" in html
    assert "60%" in html
    assert "40%" in html
    assert "手入力" in html


# 6. GitHub Rate Limit未取得表示
def test_github_section_unfetched_shows_not_fetched_message() -> None:
    html = run_compact_js("compact.githubSectionHtml({fetched: false, last_known: null})")
    assert "未取得" in html


# 7. GitHub REST/GraphQL/Search個別表示
def test_github_resource_card_shows_resource_labels() -> None:
    base = {
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
    core_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps({**base, 'resource': 'core'})})")
    graphql_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps({**base, 'resource': 'graphql'})})")
    search_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps({**base, 'resource': 'search'})})")
    assert "REST" in core_html
    assert "GraphQL" in graphql_html
    assert "Search" in search_html


# 8. GraphQL Exhaustedで赤表示
def test_github_resource_card_exhausted_status_uses_exhausted_class() -> None:
    resource = {
        "resource": "graphql",
        "status": "Exhausted",
        "limit": 5000,
        "used": 5000,
        "remaining": 0,
        "usage_percent": 100.0,
        "remaining_percent": 0.0,
        "reset_at_utc": "2999-01-01T00:00:00+00:00",
        "reset_at_local": "2999-01-01T09:00:00+09:00",
        "seconds_until_reset": 3600,
        "error_message": None,
    }
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource)})")
    assert "compact-status-exhausted" in html


# 9. max_valueなしで上限未登録
def test_dashboard_limit_without_max_value_shows_not_registered() -> None:
    html = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_NO_MAX)})")
    assert "上限未登録" in html
    assert "compact-percent-value" not in html


# 10. remaining_percentがある場合のみ%表示
def test_percent_value_only_rendered_when_percent_present() -> None:
    with_percent = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_WITH_MAX)})")
    without_percent = run_compact_js(f"compact.limitCardHtml({json.dumps(SAMPLE_ROW_NO_MAX)})")
    assert "compact-percent-value" in with_percent
    assert "compact-percent-value" not in without_percent


# 11. reset超過で負数を表示しない
def test_reset_relative_text_handles_overdue_without_negative_number() -> None:
    result = run_compact_js("compact.resetRelativeText('2000-01-01T00:00:00Z')")
    assert result == "reset時刻超過"
    assert "-" not in result

    seconds_result = run_compact_js("compact.githubSecondsUntilResetText(-42)")
    assert seconds_result == "reset時刻超過"
    assert "-" not in seconds_result


# 12. source_type表示
def test_source_type_label_maps_known_values() -> None:
    assert run_compact_js('compact.sourceTypeLabel("manual_required")') == "手入力"
    assert run_compact_js('compact.sourceTypeLabel("manual")') == "手入力"
    assert run_compact_js('compact.sourceTypeLabel("api_claude_management")') == "Claude API"


# 13. last_updated_at表示
def test_fmt_date_or_unknown_formats_valid_date_and_missing_value() -> None:
    formatted = run_compact_js('compact.fmtDateOrUnknown("2026-01-01T00:00:00+09:00")')
    assert formatted not in ("未更新", "不明")
    assert run_compact_js("compact.fmtDateOrUnknown(null)") == "未更新"


# 14. Invalid Dateで不明
def test_fmt_date_or_unknown_invalid_string_is_unknown() -> None:
    assert run_compact_js('compact.fmtDateOrUnknown("not-a-date")') == "不明"


# 15. 30秒ローカル更新 (setIntervalの呼び出し自体はブラウザ確認で検証。ここではコード上に間隔値が存在することのみ検証)
def test_compact_js_declares_thirty_second_refresh_interval() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "REFRESH_INTERVAL_MS = 30000" in js
    assert "setInterval(loadCompact, REFRESH_INTERVAL_MS)" in js


# 16. ページロードでPOSTなし
def test_compact_js_never_sends_post_requests() -> None:
    js = COMPACT_JS.read_text(encoding="utf-8")
    assert "POST" not in js
    assert "method:" not in js


# 17. ページロードでgh実行なし
def test_compact_page_load_endpoints_never_call_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("fetch_github_rate_limit must not be called by page-load GET endpoints")

    monkeypatch.setattr("app.main.fetch_github_rate_limit", explode)
    app.state.github_rate_limit_controller = GitHubRateLimitController()

    with TestClient(app) as client:
        assert client.get("/compact").status_code == 200
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/github-rate-limit").status_code == 200


# 18. token/stdout/stderr非表示
def test_compact_files_do_not_leak_process_output_markers() -> None:
    for path in (COMPACT_HTML, COMPACT_JS, COMPACT_CSS):
        text = path.read_text(encoding="utf-8")
        for marker in ("stdout", "stderr", "token"):
            assert marker not in text.lower()
