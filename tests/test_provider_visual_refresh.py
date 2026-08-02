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


# --- syntax check (再確認) -------------------------------------------------------


def test_both_js_files_pass_node_syntax_check():
    for path in (APP_JS, COMPACT_JS):
        proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
