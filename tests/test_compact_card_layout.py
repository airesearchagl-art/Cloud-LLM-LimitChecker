"""Tests for the /compact GitHub/Claude/Codex card layout refinement: a
right-side RESET block (absolute time + a prominent countdown/status value)
replacing the old bottom meta-row, and the height/overflow behavior that
comes with it.

The RESET block's remaining-value label is chosen by content, not fixed to
"残り" (that label is already used by the left-side quota percentage, and
reusing it for the time-remaining value on the right made the two collide
within the same card):
  - countdown ("あと..." text)              -> label "リセットまで", value with "あと" stripped
  - reset時刻超過 / 不明 / 未設定 (a fact, not a countdown) -> label "状態", value as-is
  - stale-suppressed (empty relativeText)    -> no remaining row at all, absolute time only

Scope: presentation only (static/compact.js + static/compact.css). No fetch
logic, domain judgment, stale rules, or app-schedule/reset-schedule
separation were changed — those are exercised by
tests/test_provider_visual_refresh.py and remain green.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPACT_JS = ROOT / "static" / "compact.js"
COMPACT_CSS = ROOT / "static" / "compact.css"


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
        "remaining_percent": 98.0,
        "reset_at_utc": "2999-01-01T00:00:00+00:00",
        "reset_at_local": "2999-01-01T09:00:00+09:00",
        "seconds_until_reset": 8130,  # 2h 15m
        "error_message": None,
    }
    base.update(overrides)
    return base


def window(**overrides):
    base = {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"}
    base.update(overrides)
    return base


# --- resetBlockHtml: shared right-side block pure function ----------------------


def test_reset_block_countdown_uses_dedicated_label_and_strips_ato():
    html = run_compact_js('compact.resetBlockHtml("2999/1/1 9:00:00", "あと2時間 15分")')
    assert "compact-reset-block" in html
    assert "RESET" in html
    assert "2999/1/1 9:00:00" in html
    assert "リセットまで" in html
    assert "2時間 15分" in html
    assert "あと2時間" not in html
    assert "compact-reset-remaining-value" in html


def test_reset_block_omits_remaining_row_when_relative_empty():
    html = run_compact_js('compact.resetBlockHtml("2999/1/1 9:00:00", "")')
    assert "2999/1/1 9:00:00" in html
    assert "compact-reset-remaining-value" not in html
    assert "リセットまで" not in html
    assert "状態" not in html


def test_reset_block_overdue_uses_status_label():
    html = run_compact_js('compact.resetBlockHtml("2000/1/1 9:00:00", "reset時刻超過")')
    assert "compact-reset-remaining-value" in html
    assert "状態" in html
    assert "reset時刻超過" in html
    assert "リセットまで" not in html


def test_reset_block_unknown_uses_status_label():
    html = run_compact_js('compact.resetBlockHtml("不明", "不明")')
    assert "状態" in html
    assert "リセットまで" not in html


# --- Layout structure present on all three provider cards -----------------------


def test_github_resource_card_uses_split_layout():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, false)")
    assert "compact-card-body" in html
    assert "compact-card-left" in html
    assert "compact-reset-block" in html
    assert "RESET" in html
    # 旧: 下端の小さなmeta-row形式の "reset: " 表記は廃止された
    assert "reset: " not in html


def test_claude_window_uses_split_layout():
    html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())}, false)')
    assert "compact-card-body" in html
    assert "compact-card-left" in html
    assert "compact-reset-block" in html
    assert "reset: " not in html


def test_codex_window_uses_split_layout():
    html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())}, "自動取得", false)'
    )
    assert "compact-card-body" in html
    assert "compact-card-left" in html
    assert "compact-reset-block" in html
    assert "reset: " not in html


# empty/エラー状態にはcard-bodyを持たせない(percent/meter自体が無いため)が、
# 既存のprovider識別(class + テキスト)は維持する
def test_github_error_card_has_no_split_layout_but_keeps_provider_identity():
    err = resource("Error", error_message="boom")
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(err)}, false)")
    assert "compact-card-body" not in html
    assert "compact-provider-github" in html
    assert "boom" in html


def test_claude_unobserved_window_has_no_split_layout():
    html = run_compact_js('compact.claudeUsageWindowHtml("Claude 5時間枠", null)')
    assert "compact-card-body" not in html
    assert "未観測" in html


def test_codex_unentered_window_has_no_split_layout():
    html = run_compact_js('compact.codexUsageWindowHtml("Codex 5時間枠", null)')
    assert "compact-card-body" not in html
    assert "未入力" in html


# --- 修正必須1: quota残量の「残り」とreset側ラベルを区別する(GitHub/Claude/Codex共通) ---


def test_github_fresh_distinguishes_quota_remaining_from_reset_countdown():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, false)")
    # 左: quota残量は従来どおり「残り 98%」
    assert "compact-percent-label\">残り<" in html
    # 右: reset側は「リセットまで」であり、あとプレフィックスを含まない値
    assert "リセットまで" in html
    assert "compact-reset-remaining-value\">2時間 15分<" in html
    assert "compact-reset-remaining-value\">あと2時間" not in html


def test_claude_fresh_distinguishes_quota_remaining_from_reset_countdown():
    html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())}, false)')
    assert "compact-percent-label\">残り<" in html
    assert "リセットまで" in html


def test_codex_fresh_distinguishes_quota_remaining_from_reset_countdown():
    html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())}, "自動取得", false)'
    )
    assert "compact-percent-label\">残り<" in html
    assert "リセットまで" in html


def test_github_stale_shows_absolute_only_no_remaining_row():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(resource('Normal'))}, true)")
    assert "compact-reset-remaining-value" not in html
    assert "リセットまで" not in html
    assert "状態" not in html
    assert "あと" not in html
    assert "2999" in html  # 絶対時刻は維持される


def test_claude_stale_shows_absolute_only_no_remaining_row():
    html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(window())}, true)')
    assert "compact-reset-remaining-value" not in html
    assert "リセットまで" not in html
    assert "2999" in html


def test_codex_stale_shows_absolute_only_no_remaining_row():
    html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(window())}, "自動取得", true)'
    )
    assert "compact-reset-remaining-value" not in html
    assert "リセットまで" not in html
    assert "2999" in html


def test_github_reset_overdue_uses_status_label_not_countdown_label():
    overdue = resource("Reset overdue", seconds_until_reset=-100)
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(overdue)}, false)")
    assert "状態" in html
    assert "reset時刻超過" in html
    assert "リセットまで" not in html


# --- 修正必須2: Codex reset超過で「reset時刻超過」が1回だけ表示される -------------


def test_codex_overdue_reset_time_exceeded_appears_exactly_once():
    overdue_window = window(resets_at="2000-01-01T00:00:00+00:00")
    html = run_compact_js(f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(overdue_window)})')
    assert html.count("reset時刻超過") == 1
    assert "compact-percent-value" not in html
    assert "40%" not in html
    assert "compact-reset-remaining-value" in html
    assert "状態" in html


def test_codex_overdue_left_column_has_no_duplicate_text():
    overdue_window = window(resets_at="2000-01-01T00:00:00+00:00")
    html = run_compact_js(f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(overdue_window)})')
    assert "compact-no-limit" not in html


# --- RATE LIMITEDバナー等は今回の対象外で、影響を受けない ------------------------


def test_rate_limited_banner_unaffected_by_layout_change():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")}
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "compact-banner-limited" in html
    assert "compact-card-body" not in html


def test_github_section_html_full_integration_keeps_banner_and_new_card_layout():
    data = {
        "fetched": True,
        "refreshing": False,
        "overall": {"status": "Limited", "reason": "REST API core exhausted"},
        "resources": {"core": resource("Exhausted"), "graphql": resource("Normal", resource="graphql")},
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert "RATE LIMITED" in html
    assert "compact-card-body" in html
    assert "compact-reset-block" in html


# --- CSSレベルの検証: 高さ/overflow/レスポンシブ -----------------------------------


def test_css_defines_split_layout_classes():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (
        ".compact-card-body",
        ".compact-card-left",
        ".compact-reset-block",
        ".compact-reset-label",
        ".compact-reset-absolute",
        ".compact-reset-remaining-label",
        ".compact-reset-remaining-value",
    ):
        assert f"{cls} {{" in css


def test_css_card_body_wraps_to_stack_on_narrow_cards():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    body_start = css.index(".compact-card-body {")
    body_end = css.index("}", body_start)
    body_block = css[body_start:body_end]
    assert "flex-wrap: wrap" in body_block


def test_css_reset_text_wraps_safely_not_nowrap():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (".compact-reset-absolute", ".compact-reset-remaining-value"):
        start = css.index(f"{cls} {{")
        end = css.index("}", start)
        block = css[start:end]
        assert "white-space: nowrap" not in block
        assert "overflow-wrap: anywhere" in block


# 残り時間の値は、絶対時刻(compact-reset-absolute)より目立つ(フォントサイズが大きい)ことを確認する
def test_remaining_value_is_more_prominent_than_absolute_time():
    import re

    css = COMPACT_CSS.read_text(encoding="utf-8")

    def font_size_of(selector: str) -> int:
        start = css.index(f"{selector} {{")
        end = css.index("}", start)
        block = css[start:end]
        match = re.search(r"font-size:\s*(\d+)px", block)
        assert match, f"font-size not found for {selector}"
        return int(match.group(1))

    absolute_size = font_size_of(".compact-reset-absolute")
    remaining_size = font_size_of(".compact-reset-remaining-value")
    assert remaining_size > absolute_size


# --- 既存の状態色/banner定義に影響していないことの再確認(前フェーズのテストと重複あえて残す) ---


def test_existing_status_and_provider_css_untouched():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (
        ".compact-status-normal",
        ".compact-status-warning",
        ".compact-status-exhausted",
        ".compact-banner-limited",
        ".compact-banner-stale",
        ".compact-provider-github",
        ".compact-provider-claude",
        ".compact-provider-codex",
    ):
        assert f"{cls} {{" in css


# --- syntax check ----------------------------------------------------------------


def test_compact_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(COMPACT_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
