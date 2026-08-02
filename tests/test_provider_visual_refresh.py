"""Tests for the compact/admin visual refresh: provider accent colors and the
unified relative-time duration format.

Scope: presentation only. No fetch logic, domain judgment, scheduler, or API
shape changes are covered here (none were made). See static/app.js and
static/compact.js for the shared `fmtDurationJa` / `fmtAbsoluteWithRelative` /
`suppressCountdownIfStale` helpers duplicated across both files so the two
screens render identical duration text.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
APP_CSS = ROOT / "static" / "styles.css"
COMPACT_JS = ROOT / "static" / "compact.js"
COMPACT_CSS = ROOT / "static" / "compact.css"


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


DURATION_MATRIX = [
    (0, "1分未満"),
    (1, "1分未満"),
    (59, "1分未満"),
    (60, "1分"),
    (61, "1分"),
    (3599, "59分"),
    (3600, "1時間"),
    (3660, "1時間 1分"),
    (86399, "23時間 59分"),
    (86400, "1日"),
    (90000, "1日 1時間"),
]


# --- fmtDurationJa: exact bucketing matrix, both screens -----------------------


def test_app_duration_matrix():
    for seconds, expected in DURATION_MATRIX:
        assert run_app_js(f"app.fmtDurationJa({seconds})") == expected, seconds


def test_compact_duration_matrix():
    for seconds, expected in DURATION_MATRIX:
        assert run_compact_js(f"compact.fmtDurationJa({seconds})") == expected, seconds


# 通常画面とcompact画面で同じ表示結果にする(duration部分の完全一致)
def test_duration_matrix_identical_across_both_screens():
    for seconds, _ in DURATION_MATRIX:
        app_result = run_app_js(f"app.fmtDurationJa({seconds})")
        compact_result = run_compact_js(f"compact.fmtDurationJa({seconds})")
        assert app_result == compact_result, seconds


# fmtDurationJaは「あと」を含まない(呼び出し側が付与する)
def test_duration_matrix_never_includes_ato_prefix():
    for seconds, _ in DURATION_MATRIX:
        assert "あと" not in run_app_js(f"app.fmtDurationJa({seconds})")
        assert "あと" not in run_compact_js(f"compact.fmtDurationJa({seconds})")


# --- invalid / overdue: 既存のreset時刻超過・不明表記を維持 --------------------


def test_app_invalid_and_overdue_text_preserved():
    assert run_app_js("app.fmtSecondsUntilReset(null)") == "不明"
    assert run_app_js("app.fmtSecondsUntilReset(-5)") == "リセット時刻超過"


def test_compact_invalid_and_overdue_text_preserved():
    assert run_compact_js("compact.githubSecondsUntilResetText(null)") == "不明"
    assert run_compact_js("compact.githubSecondsUntilResetText(-5)") == "reset時刻超過"
    assert run_compact_js("compact.resetRelativeText(null)") == "未設定"
    assert run_compact_js("compact.resetRelativeText('not-a-date')") == "不明"


# 「あと」プレフィックスは呼び出し側(resetRelativeText等)が付与する
def test_app_wrapper_adds_ato_prefix_for_positive_seconds():
    assert run_app_js("app.fmtSecondsUntilReset(3660)") == "あと1時間 1分"


def test_compact_wrapper_adds_ato_prefix_for_positive_seconds():
    assert run_compact_js("compact.githubSecondsUntilResetText(3660)") == "あと1時間 1分"


# --- fmtAbsoluteWithRelative / suppressCountdownIfStale -------------------------


def test_app_absolute_with_relative_combines():
    result = run_app_js('app.fmtAbsoluteWithRelative("2026/8/2 23:40", "あと2時間 13分")')
    assert result == "2026/8/2 23:40（あと2時間 13分）"


def test_app_absolute_with_relative_falls_back_to_absolute_only():
    assert run_app_js('app.fmtAbsoluteWithRelative("2026/8/2 23:40", "")') == "2026/8/2 23:40"
    assert run_app_js('app.fmtAbsoluteWithRelative("2026/8/2 23:40", "不明")') == "2026/8/2 23:40"


def test_compact_absolute_with_relative_combines():
    result = run_compact_js('compact.fmtAbsoluteWithRelative("2026/8/2 23:40", "あと2時間 13分")')
    assert result == "2026/8/2 23:40（あと2時間 13分）"


def test_compact_absolute_with_relative_falls_back_to_absolute_only():
    assert run_compact_js('compact.fmtAbsoluteWithRelative("2026/8/2 23:40", "")') == "2026/8/2 23:40"
    assert run_compact_js('compact.fmtAbsoluteWithRelative("2026/8/2 23:40", "不明")') == "2026/8/2 23:40"
    assert run_compact_js('compact.fmtAbsoluteWithRelative("2026/8/2 23:40", "未設定")') == "2026/8/2 23:40"


# staleでは「あと...」を抑制するが、reset時刻超過/不明/未設定はそのまま通す
def test_app_suppress_countdown_if_stale_strips_only_ato_text():
    assert run_app_js('app.suppressCountdownIfStale("あと1時間 1分", true)') == ""
    assert run_app_js('app.suppressCountdownIfStale("あと1時間 1分", false)') == "あと1時間 1分"
    assert run_app_js('app.suppressCountdownIfStale("リセット時刻超過", true)') == "リセット時刻超過"
    assert run_app_js('app.suppressCountdownIfStale("不明", true)') == "不明"


def test_compact_suppress_countdown_if_stale_strips_only_ato_text():
    assert run_compact_js('compact.suppressCountdownIfStale("あと1時間 1分", true)') == ""
    assert run_compact_js('compact.suppressCountdownIfStale("あと1時間 1分", false)') == "あと1時間 1分"
    assert run_compact_js('compact.suppressCountdownIfStale("reset時刻超過", true)') == "reset時刻超過"
    assert run_compact_js('compact.suppressCountdownIfStale("不明", true)') == "不明"
    assert run_compact_js('compact.suppressCountdownIfStale("未設定", true)') == "未設定"


# --- Reset overdue / staleでは「あと」を出さない(結合済みカードでの確認) --------


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
        "seconds_until_reset": 3660,
        "error_message": None,
    }
    base.update(overrides)
    return base


# fresh(stale=false)なGitHub resource cardは「あと」付きの相対時間を表示する
def test_compact_github_resource_card_fresh_shows_countdown():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, false)")
    assert "あと1時間 1分" in html


# stale(last_known由来)なGitHub resource cardは絶対時刻のみで、「あと」を出さない
def test_compact_github_resource_card_stale_suppresses_countdown():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, true)")
    assert "あと" not in html
    assert "2999" in html  # 絶対時刻(reset_at_local)は維持される


def test_app_github_resource_card_fresh_shows_countdown():
    html = run_app_js(f"app.githubResourceCardHtml({json.dumps(resource('Normal'))}, false)")
    assert "あと1時間 1分" in html


def test_app_github_resource_card_stale_suppresses_countdown():
    html = run_app_js(f"app.githubResourceCardHtml({json.dumps(resource('Normal'))}, true)")
    assert "あと" not in html
    assert "2999" in html


# --- Claude / Codexウィンドウでも同様にstaleで「あと」を抑制する ----------------


def window(resets_at="2999-01-01T00:00:00+00:00"):
    return {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": resets_at}


def test_compact_claude_window_stale_suppresses_countdown():
    fresh_html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())}, false)')
    stale_html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())}, true)')
    assert "あと" in fresh_html
    assert "あと" not in stale_html
    assert "2999" in stale_html


def test_compact_codex_window_stale_suppresses_countdown():
    fresh_html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())}, "自動取得", false)'
    )
    stale_html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())}, "自動取得", true)'
    )
    assert "あと" in fresh_html
    assert "あと" not in stale_html
    assert "2999" in stale_html


# --- 色だけに依存しない: プロバイダ名を文字ラベルとして維持 ---------------------


def test_compact_cards_include_provider_name_as_text():
    github_html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))})")
    claude_html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())})')
    codex_html = run_compact_js(f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())})')
    assert "GitHub" in github_html
    assert "Claude" in claude_html
    assert "Codex" in codex_html


def test_app_github_card_includes_provider_name_as_text():
    html = run_app_js(f"app.githubResourceCardHtml({json.dumps(resource('Normal'))})")
    assert "GitHub" in html


# --- Provider色は状態色を上書きしない(CSSレベルの分離確認) ----------------------


def test_compact_css_defines_provider_variables_separately_from_status_colors():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "--provider-github:" in css
    assert "--provider-claude:" in css
    assert "--provider-codex:" in css
    # 既存の状態色ルールが変更されず残っている
    for cls in (
        ".compact-status-normal",
        ".compact-status-warning",
        ".compact-status-exhausted",
        ".compact-status-error",
        ".compact-banner-overdue",
        ".compact-banner-stale",
        ".compact-banner-secondary",
    ):
        assert f"{cls} {{" in css
    # providerアクセントはbox-shadowで別チャンネル化されており、状態色のbackground/colorには使われない
    assert "box-shadow: inset 3px 0 0 var(--provider-github)" in css
    assert "box-shadow: inset 3px 0 0 var(--provider-claude)" in css
    assert "box-shadow: inset 3px 0 0 var(--provider-codex)" in css


def test_app_css_defines_provider_variables_separately_from_status_colors():
    css = APP_CSS.read_text(encoding="utf-8")
    assert "--provider-github:" in css
    assert "--provider-claude:" in css
    assert "--provider-codex:" in css
    for cls in (".github-status-normal", ".github-status-warning", ".github-status-exhausted", ".github-status-overdue"):
        assert f"{cls} {{" in css
    assert "box-shadow: inset 3px 0 0 var(--provider-github)" in css
    assert "box-shadow: inset 3px 0 0 var(--provider-codex)" in css


# providerのbox-shadow定義に、既存の状態色banner/badgeクラス名は含まれない
# (バナー自体の色は上書きしない: providerセレクタの対象がカード/パネルに限定されていることを確認)
def test_compact_provider_accent_not_applied_to_status_banners():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    provider_block_start = css.index(".compact-provider-github")
    provider_block_end = css.index(".compact-empty", provider_block_start)
    provider_block = css[provider_block_start:provider_block_end]
    assert "compact-banner" not in provider_block
    assert "compact-github-limited" not in provider_block
    assert "compact-status" not in provider_block


# --- 640px幅でのoverflow安全性(CSSプロパティの存在確認) --------------------------


def test_compact_cards_wrap_long_text_safely():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "overflow-wrap: anywhere" in css
    assert "flex-wrap: wrap" in css


def test_compact_grid_still_single_column_under_700px():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    assert "grid-template-columns: 1fr;" in css


# --- 長い併記reset行でも折り返し可能なテキストであること(実データでの長さ確認) ---


def test_compact_combined_reset_line_is_plain_wrappable_text():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, false)")
    # white-space:nowrapを強制するインラインstyleを reset行 に付けていないこと
    assert 'style="white-space:nowrap"' not in html


# --- 既存のstatusバナー(RATE LIMITED等)の可読性は維持される ---------------------


def test_compact_rate_limited_banner_still_renders_with_existing_classes():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "compact-banner-limited" in html


def test_app_rate_limited_banner_still_renders_with_existing_classes():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_app_js(f"app.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "github-banner-limited" in html


# --- 修正必須1: 未取得/未観測のemptyカードにもProviderアクセントを適用する -----


def test_compact_github_empty_card_has_provider_class_and_name():
    html = run_compact_js("compact.githubSectionHtml({fetched: false, last_known: null})")
    assert "compact-provider-github" in html
    assert "GitHub" in html


def test_compact_claude_empty_card_has_provider_class_and_name():
    html = run_compact_js('compact.claudeCodeSectionHtml({available: false, status: "not_observed"})')
    assert "compact-provider-claude" in html
    assert "Claude" in html

    invalid_html = run_compact_js('compact.claudeCodeSectionHtml({available: false, status: "invalid_cache"})')
    assert "compact-provider-claude" in invalid_html
    assert "Claude" in invalid_html


def test_compact_codex_empty_card_has_provider_class_and_name():
    html = run_compact_js(
        'compact.codexUsageSectionHtml({available: false, status: "not_observed"}, {available: false, status: "not_observed"})'
    )
    assert "compact-provider-codex" in html
    assert "Codex" in html

    invalid_html = run_compact_js(
        'compact.codexUsageSectionHtml({available: false, status: "invalid_cache"}, {available: false, status: "invalid_cache"})'
    )
    assert "compact-provider-codex" in invalid_html
    assert "Codex" in invalid_html


# 個別window単位の未観測/未入力カードにも既にproviderクラスが付いていることを確認する
def test_compact_claude_window_unobserved_has_provider_class():
    html = run_compact_js('compact.claudeUsageWindowHtml("Claude 5時間枠", null)')
    assert "compact-provider-claude" in html
    assert "未観測" in html


def test_compact_codex_window_unentered_has_provider_class():
    html = run_compact_js('compact.codexUsageWindowHtml("Codex 5時間枠", null)')
    assert "compact-provider-codex" in html
    assert "未入力" in html


# empty状態でも既存のstatus/banner色(CSSルール)は変更されていないことの確認
def test_compact_empty_card_class_addition_does_not_touch_status_css():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (".compact-status-unknown", ".compact-empty"):
        assert f"{cls} {{" in css


# --- 修正必須2: アプリ予定用の相対時間をresetまでの相対時間から分離する ---------

APP_SCHEDULE_FUTURE_CASES = [
    # 分の境界ちょうど(例: 7980 = 133*60)は、同一tick内での関数呼び出しにかかる
    # わずかな経過時間だけでfloor()が切り下がりflakyになるため避け、余裕を持たせる。
    (2 * 3600 + 13 * 60 + 5, "あと2時間 13分"),
    (5, "あと1分未満"),
]


def _iso_offset(seconds_from_now: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


def _js_future_expr(offset_seconds: int) -> str:
    # オフセット計算とfmtAppScheduleRelative呼び出しを同一node процессの同一tick内で行い、
    # Python側とnode側のプロセス間クロックずれ(subprocess起動オーバーヘッド)による
    # 分境界での1分ズレ(flaky)を避ける。
    return f"new Date(Date.now() + {offset_seconds} * 1000).toISOString()"


def test_app_schedule_relative_future_app():
    for offset, expected in APP_SCHEDULE_FUTURE_CASES:
        assert run_app_js(f"app.fmtAppScheduleRelative({_js_future_expr(offset)})") == expected


def test_app_schedule_relative_future_compact():
    for offset, expected in APP_SCHEDULE_FUTURE_CASES:
        assert run_compact_js(f"compact.fmtAppScheduleRelative({_js_future_expr(offset)})") == expected


def test_app_schedule_relative_near_now_and_past_app():
    # 現在時刻付近(数秒過去)と、はっきり過去の両方で「再取得待ち」になる
    assert run_app_js(f'app.fmtAppScheduleRelative("{_iso_offset(-1)}")') == "再取得待ち"
    assert run_app_js(f'app.fmtAppScheduleRelative("{_iso_offset(-3600)}")') == "再取得待ち"


def test_app_schedule_relative_near_now_and_past_compact():
    assert run_compact_js(f'compact.fmtAppScheduleRelative("{_iso_offset(-1)}")') == "再取得待ち"
    assert run_compact_js(f'compact.fmtAppScheduleRelative("{_iso_offset(-3600)}")') == "再取得待ち"


def test_app_schedule_relative_invalid_app():
    assert run_app_js("app.fmtAppScheduleRelative(null)") == ""
    assert run_app_js('app.fmtAppScheduleRelative("not-a-date")') == ""


def test_app_schedule_relative_invalid_compact():
    assert run_compact_js("compact.fmtAppScheduleRelative(null)") == ""
    assert run_compact_js('compact.fmtAppScheduleRelative("not-a-date")') == ""


# app.jsとcompact.jsで意味(出力)が完全に一致することを確認する
def test_app_schedule_relative_matches_across_both_screens():
    cases = [_iso_offset(8000), _iso_offset(30), _iso_offset(-1), _iso_offset(-3600)]
    for iso in cases:
        app_result = run_app_js(f'app.fmtAppScheduleRelative("{iso}")')
        compact_result = run_compact_js(f'compact.fmtAppScheduleRelative("{iso}")')
        assert app_result == compact_result, iso
    assert run_app_js("app.fmtAppScheduleRelative(null)") == run_compact_js("compact.fmtAppScheduleRelative(null)")


# 過去に「まもなく」が出ない
def test_app_schedule_relative_never_says_mamonaku():
    for iso in (_iso_offset(-1), _iso_offset(-3600), _iso_offset(-86400)):
        assert "まもなく" not in run_app_js(f'app.fmtAppScheduleRelative("{iso}")')
        assert "まもなく" not in run_compact_js(f'compact.fmtAppScheduleRelative("{iso}")')


# アプリ予定に「reset時刻超過」/「リセット時刻超過」が出ない
def test_app_schedule_relative_never_says_reset_overdue():
    for iso in (_iso_offset(-1), _iso_offset(-3600), _iso_offset(-86400)):
        app_result = run_app_js(f'app.fmtAppScheduleRelative("{iso}")')
        compact_result = run_compact_js(f'compact.fmtAppScheduleRelative("{iso}")')
        assert "時刻超過" not in app_result
        assert "時刻超過" not in compact_result


# --- 統合: 実際の通知HTMLでも「reset時刻超過」「まもなく」が出ないことを確認する ---


def test_compact_github_auto_refresh_notice_past_shows_wait_not_overdue_or_soon():
    html = run_compact_js(
        f'compact.githubAutoRefreshNoticeHtml({{"refreshing": false, "auto_refresh_pending": true, '
        f'"next_auto_refresh_at": "{_iso_offset(-120)}", "last_auto_refresh_error": null}})'
    )
    assert "再取得待ち" in html
    assert "まもなく" not in html
    assert "reset時刻超過" not in html


def test_app_github_auto_refresh_notice_past_shows_wait_not_overdue_or_soon():
    html = run_app_js(
        f'app.githubAutoRefreshNoticeHtml({{"refreshing": false, "auto_refresh_pending": true, '
        f'"next_auto_refresh_at": "{_iso_offset(-120)}", "last_auto_refresh_error": null}})'
    )
    assert "再取得待ち" in html
    assert "まもなく" not in html
    assert "リセット時刻超過" not in html


def test_compact_codex_periodic_notice_past_shows_wait_not_overdue():
    auto = {"auto_refresh_interval_seconds": 600, "next_auto_refresh_at": _iso_offset(-120)}
    html = run_compact_js(f"compact.codexPeriodicRefreshNoticeHtml({json.dumps(auto)})")
    assert "再取得待ち" in html
    assert "reset時刻超過" not in html
    assert "まもなく" not in html


def test_compact_codex_periodic_notice_future_shows_countdown():
    auto = {"auto_refresh_interval_seconds": 600, "next_auto_refresh_at": _iso_offset(400)}
    html = run_compact_js(f"compact.codexPeriodicRefreshNoticeHtml({json.dumps(auto)})")
    assert "あと" in html


# --- 修正推奨: fmtDurationJaの入力防御 -------------------------------------------


def test_app_duration_defensive_against_bad_input():
    assert run_app_js("app.fmtDurationJa(-100)") == "1分未満"
    assert run_app_js("app.fmtDurationJa(NaN)") == "1分未満"
    assert run_app_js("app.fmtDurationJa(Infinity)") == "1分未満"
    assert run_app_js("app.fmtDurationJa(3660)") == "1時間 1分"


def test_compact_duration_defensive_against_bad_input():
    assert run_compact_js("compact.fmtDurationJa(-100)") == "1分未満"
    assert run_compact_js("compact.fmtDurationJa(NaN)") == "1分未満"
    assert run_compact_js("compact.fmtDurationJa(Infinity)") == "1分未満"
    assert run_compact_js("compact.fmtDurationJa(3660)") == "1時間 1分"


# --- syntax check (再確認) -------------------------------------------------------


def test_both_js_files_pass_node_syntax_check():
    for path in (APP_JS, COMPACT_JS):
        proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
