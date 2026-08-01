"""Pure-function tests for the GitHub Rate Limited visibility banners.

Covers static/app.js (normal admin screen) and static/compact.js (/compact),
which must render RATE LIMITED / RESET OVERDUE / LAST KNOWN: ... / SECONDARY
RATE LIMIT distinctly, and must never label auth failure, network failure,
malformed JSON, or plain stale-but-Normal data as rate limited.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
COMPACT_JS = ROOT / "static" / "compact.js"


def run_app_js(expression: str):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


def run_compact_js(expression: str):
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


def resource(status, **overrides):
    base = {
        "resource": "core",
        "status": status,
        "limit": 5000,
        "used": 100,
        "remaining": 4900,
        "usage_percent": 2.0,
        "reset_at_utc": "2999-01-01T00:00:00+00:00",
        "reset_at_local": "2999-01-01T09:00:00+09:00",
        "seconds_until_reset": 3600,
        "error_message": None,
    }
    base.update(overrides)
    return base


# --- app.js: githubLimitedCause -------------------------------------------------


# 1. core Reset overdueが原因として判定される
def test_app_limited_cause_core_reset_overdue():
    resources = {"core": resource("Reset overdue"), "graphql": resource("Normal", resource="graphql")}
    cause = run_app_js(f"app.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "core", "variant": "reset_overdue"}


# 2. graphqlのみExhaustedが原因として判定される
def test_app_limited_cause_graphql_exhausted_only():
    resources = {"core": resource("Normal"), "graphql": resource("Exhausted", resource="graphql")}
    cause = run_app_js(f"app.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "graphql", "variant": "rate_limited"}


# 3. core/graphqlどちらもExhaustedならcoreを優先する（バックエンドのtie-breakと一致）
def test_app_limited_cause_ties_on_core():
    resources = {"core": resource("Exhausted"), "graphql": resource("Exhausted", resource="graphql")}
    cause = run_app_js(f"app.githubLimitedCause({json.dumps(resources)})")
    assert cause["resource"] == "core"


# 3b. core=Exhausted, graphql=Reset overdue → Exhaustedを優先してRATE LIMITED / core
def test_app_limited_cause_exhausted_beats_reset_overdue_core_exhausted():
    resources = {"core": resource("Exhausted"), "graphql": resource("Reset overdue", resource="graphql")}
    cause = run_app_js(f"app.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "core", "variant": "rate_limited"}


# 3c. core=Reset overdue, graphql=Exhausted → Exhaustedを優先してRATE LIMITED / graphql
def test_app_limited_cause_exhausted_beats_reset_overdue_graphql_exhausted():
    resources = {"core": resource("Reset overdue"), "graphql": resource("Exhausted", resource="graphql")}
    cause = run_app_js(f"app.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "graphql", "variant": "rate_limited"}


# 3d. compact.js: core=Exhausted, graphql=Reset overdue → RATE LIMITED / core
def test_compact_limited_cause_exhausted_beats_reset_overdue_core_exhausted():
    resources = {"core": resource("Exhausted"), "graphql": resource("Reset overdue", resource="graphql")}
    cause = run_compact_js(f"compact.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "core", "variant": "rate_limited"}


# 3e. compact.js: core=Reset overdue, graphql=Exhausted → RATE LIMITED / graphql
def test_compact_limited_cause_exhausted_beats_reset_overdue_graphql_exhausted():
    resources = {"core": resource("Reset overdue"), "graphql": resource("Exhausted", resource="graphql")}
    cause = run_compact_js(f"compact.githubLimitedCause({json.dumps(resources)})")
    assert cause == {"resource": "graphql", "variant": "rate_limited"}


# 3f. app.js: バナー全体でも混在時にRATE LIMITEDが優先表示され、原因resourceラベルが一致する
def test_app_banner_mixed_status_shows_rate_limited_with_correct_cause():
    overall = {"status": "Limited", "reason": "GraphQL API reset overdue"}
    resources = {"core": resource("Reset overdue"), "graphql": resource("Exhausted", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "RESET OVERDUE" not in html
    assert "GitHub GraphQL API" in html


# 3g. compact.js: 同上
def test_compact_banner_mixed_status_shows_rate_limited_with_correct_cause():
    overall = {"status": "Limited", "reason": "GraphQL API reset overdue"}
    resources = {"core": resource("Reset overdue"), "graphql": resource("Exhausted", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "RESET OVERDUE" not in html


# --- app.js: githubLimitedBannerHtml --------------------------------------------


# 4. fresh Exhausted: RATE LIMITED（LAST KNOWNではない）
def test_app_banner_fresh_exhausted_is_rate_limited():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "LAST KNOWN" not in html
    assert "github-banner-limited" in html
    assert "github-banner-stale" not in html


# 5. fresh Reset overdue: RESET OVERDUE
def test_app_banner_fresh_reset_overdue():
    overall = {"status": "Limited", "reason": "REST API core reset overdue"}
    resources = {"core": resource("Reset overdue"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RESET OVERDUE" in html
    assert "github-banner-overdue" in html


# 6. stale Exhausted: LAST KNOWN: RATE LIMITED（現在の制限中と区別する）
def test_app_banner_stale_exhausted_is_last_known():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, true)")
    assert "LAST KNOWN: RATE LIMITED" in html
    assert "github-banner-stale" in html
    assert "最終確認時点では制限中でした（現在の状態ではありません）" in html
    assert "reset時刻を経過" not in html


# 6b. stale Reset overdue: subtextがExhausted用の文言と異なる専用文言になる
def test_app_banner_stale_reset_overdue_uses_distinct_subtext():
    overall = {"status": "Limited", "reason": "REST API core reset overdue"}
    resources = {"core": resource("Reset overdue"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, true)")
    assert "LAST KNOWN: RESET OVERDUE" in html
    assert "最終確認時点でreset時刻を経過していましたが、新しい値は未取得です（現在の状態ではありません）" in html
    assert "最終確認時点では制限中でした（現在の状態ではありません）" not in html


# 7. Overallが Limited でなければバナーは出さない
def test_app_banner_absent_when_not_limited():
    overall = {"status": "Normal", "reason": "core and graphql are within normal limits"}
    resources = {"core": resource("Normal"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert html == ""


# --- app.js: githubSecondaryRateLimitBannerHtml ---------------------------------


# 8. secondary_rate_limitで専用バナーを表示する
def test_app_secondary_rate_limit_banner_shown():
    data = {"error": {"error_type": "secondary_rate_limit", "user_message": "GitHub secondary rate limit reached."}}
    html = run_app_js(f"app.githubSecondaryRateLimitBannerHtml({json.dumps(data)})")
    assert "SECONDARY RATE LIMIT" in html
    assert "github-banner-secondary" in html


# 9. 他のerror_typeでは出さない
def test_app_secondary_rate_limit_banner_absent_for_other_errors():
    data = {"error": {"error_type": "not_authenticated", "user_message": "GitHub CLI is not authenticated."}}
    html = run_app_js(f"app.githubSecondaryRateLimitBannerHtml({json.dumps(data)})")
    assert html == ""


# --- app.js: githubRateLimitHtml integration ------------------------------------


# 10. 認証失敗はRATE LIMITEDと誤表示しない
def test_app_full_html_auth_failure_never_shows_rate_limited():
    data = {
        "fetched": False,
        "refreshing": False,
        "error": {"error_type": "not_authenticated", "user_message": "GitHub CLI is not authenticated."},
        "last_known": None,
        "overall": None,
        "resources": None,
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubRateLimitHtml({json.dumps(data)})")
    assert "RATE LIMITED" not in html
    assert "GitHub CLI is not authenticated." in html


# 11. 通信失敗(timeout)もRATE LIMITEDと誤表示しない
def test_app_full_html_network_failure_never_shows_rate_limited():
    data = {
        "fetched": False,
        "refreshing": False,
        "error": {"error_type": "timeout", "user_message": "Fetching the GitHub rate limit timed out."},
        "last_known": None,
        "overall": None,
        "resources": None,
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubRateLimitHtml({json.dumps(data)})")
    assert "RATE LIMITED" not in html


# 12. 不正JSONもRATE LIMITEDと誤表示しない
def test_app_full_html_invalid_json_never_shows_rate_limited():
    data = {
        "fetched": False,
        "refreshing": False,
        "error": {"error_type": "invalid_json", "user_message": "GitHub CLI returned output that is not valid JSON."},
        "last_known": None,
        "overall": None,
        "resources": None,
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubRateLimitHtml({json.dumps(data)})")
    assert "RATE LIMITED" not in html


# 13. staleデータはLAST KNOWNとしてのみRATE LIMITEDを含み、freshのRATE LIMITED表記(LAST KNOWN無し)は出さない
def test_app_full_html_stale_limited_is_last_known_only():
    data = {
        "fetched": False,
        "refreshing": False,
        "error": {"error_type": "timeout", "user_message": "Fetching the GitHub rate limit timed out."},
        "last_known": {
            "resources": {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")},
            "overall": {"status": "Limited", "reason": "REST API core exhausted"},
            "collected_at": "2026-01-01T00:00:00+00:00",
            "stale": True,
        },
        "overall": None,
        "resources": None,
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubRateLimitHtml({json.dumps(data)})")
    assert "LAST KNOWN: RATE LIMITED" in html
    assert "github-limited-banner github-banner-limited github-banner-stale" in html


# 14. secondary_rate_limitでは汎用エラー表示と重複させない
def test_app_full_html_secondary_rate_limit_suppresses_generic_error_box():
    data = {
        "fetched": False,
        "refreshing": False,
        "error": {"error_type": "secondary_rate_limit", "user_message": "GitHub secondary rate limit reached."},
        "last_known": None,
        "overall": None,
        "resources": None,
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubRateLimitHtml({json.dumps(data)})")
    assert "SECONDARY RATE LIMIT" in html
    assert "github-error" not in html


# --- app.js: auto-refresh wording -------------------------------------------------


# 15. 次回更新予定は「アプリの次回取得予定」であり「制限解除予定」ではない
def test_app_auto_refresh_notice_is_apps_own_next_fetch_not_reset_time():
    data = {
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
    }
    html = run_app_js(f"app.githubAutoRefreshNoticeHtml({json.dumps(data)})")
    assert "アプリの次回取得予定" in html
    assert "制限解除予定" not in html


# --- compact.js: githubLimitedBannerHtml / githubSecondaryRateLimitBannerHtml ---


# 16. compact: fresh Exhausted
def test_compact_banner_fresh_exhausted_is_rate_limited():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "LAST KNOWN" not in html
    assert "compact-banner-limited" in html


# 17. compact: stale Exhausted
def test_compact_banner_stale_exhausted_is_last_known():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, true)")
    assert "LAST KNOWN: RATE LIMITED" in html
    assert "compact-banner-stale" in html


# 18. compact: Reset overdue
def test_compact_banner_reset_overdue():
    overall = {"status": "Limited", "reason": "REST API core reset overdue"}
    resources = {"core": resource("Reset overdue"), "graphql": resource("Normal", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RESET OVERDUE" in html
    assert "compact-banner-overdue" in html


# 19. compact: secondary rate limit
def test_compact_secondary_rate_limit_banner_shown():
    data = {"error": {"error_type": "secondary_rate_limit", "user_message": "GitHub secondary rate limit reached."}}
    html = run_compact_js(f"compact.githubSecondaryRateLimitBannerHtml({json.dumps(data)})")
    assert "SECONDARY RATE LIMIT" in html
    assert "compact-banner-secondary" in html


# 20. compact: githubSectionHtml統合 — staleはLAST KNOWNとしてのみ表示される
def test_compact_section_html_stale_limited_is_last_known_only():
    data = {
        "fetched": False,
        "last_known": {
            "resources": {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")},
            "overall": {"status": "Limited", "reason": "REST API core exhausted"},
            "collected_at": "2026-01-01T00:00:00+00:00",
        },
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert "LAST KNOWN: RATE LIMITED" in html


# --- compact.js: githubAutoRefreshNoticeHtml (アプリの次回取得予定) ------------------


# 22. pending + next日時: 「アプリの次回取得予定」を表示する
def test_compact_auto_refresh_notice_shows_next_fetch_time():
    data = {
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
    }
    html = run_compact_js(f"compact.githubAutoRefreshNoticeHtml({json.dumps(data)})")
    assert "アプリの次回取得予定" in html


# 23. 「制限解除予定」という表現は使わない
def test_compact_auto_refresh_notice_never_says_reset_release_schedule():
    data = {
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
    }
    html = run_compact_js(f"compact.githubAutoRefreshNoticeHtml({json.dumps(data)})")
    assert "制限解除予定" not in html


# 24. pendingでない場合は表示しない
def test_compact_auto_refresh_notice_absent_when_not_pending():
    data = {"refreshing": False, "auto_refresh_pending": False, "next_auto_refresh_at": None, "last_auto_refresh_error": None}
    html = run_compact_js(f"compact.githubAutoRefreshNoticeHtml({json.dumps(data)})")
    assert html == ""


# 25. githubSectionHtml統合: staleとの併存(直近取得失敗の補助表示と次回取得予定が両方出る)
def test_compact_section_html_auto_refresh_notice_coexists_with_stale():
    data = {
        "fetched": False,
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
        "last_known": {
            "resources": {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")},
            "overall": {"status": "Limited", "reason": "REST API core exhausted"},
            "collected_at": "2026-01-01T00:00:00+00:00",
        },
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert "LAST KNOWN: RATE LIMITED" in html
    assert "直近の取得は失敗しました" in html
    assert "アプリの次回取得予定" in html


# 26. githubSectionHtml統合: secondary rate limitとの併存
def test_compact_section_html_auto_refresh_notice_coexists_with_secondary():
    data = {
        "fetched": False,
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
        "last_known": None,
        "error": {"error_type": "secondary_rate_limit", "user_message": "GitHub secondary rate limit reached."},
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert "SECONDARY RATE LIMIT" in html
    assert "アプリの次回取得予定" in html


# 27. githubSectionHtml統合: fetched成功時にもpendingなら次回取得予定が出る
def test_compact_section_html_auto_refresh_notice_shows_on_fresh_fetch():
    data = {
        "fetched": True,
        "refreshing": False,
        "auto_refresh_pending": True,
        "next_auto_refresh_at": "2999-01-01T00:00:00+00:00",
        "last_auto_refresh_error": None,
        "overall": {"status": "Limited", "reason": "REST API core exhausted"},
        "resources": {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")},
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert "アプリの次回取得予定" in html
    assert "RATE LIMITED" in html


# 21. compact/app.jsの両方でsyntaxチェックが通ることを再確認（node --check）
def test_both_js_files_pass_node_syntax_check():
    for path in (APP_JS, COMPACT_JS):
        proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
