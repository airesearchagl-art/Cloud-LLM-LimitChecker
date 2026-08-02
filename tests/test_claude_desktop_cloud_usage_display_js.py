import json
import re
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
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


AUTO_FRESH = {
    "available": True,
    "stale": False,
    "status": "ok",
    "observed_at": "2026-01-01T12:00:00+00:00",
    "source": "claude_code_statusline",
    "five_hour": {"used_percentage": 42.0, "remaining_percentage": 58.0, "resets_at": "2999-01-01T00:00:00+00:00"},
    "seven_day": {"used_percentage": 18.0, "remaining_percentage": 82.0, "resets_at": "2999-01-07T00:00:00+00:00"},
    "error_message": None,
}

MANUAL_FRESH = {
    "available": True,
    "stale": False,
    "status": "ok",
    "observed_at": "2026-01-01T10:00:00+00:00",
    "source": "claude_desktop_cloud_manual",
    "five_hour": {"used_percentage": 30.0, "remaining_percentage": 70.0, "resets_at": "2999-01-01T00:00:00+00:00"},
    "seven_day": {"used_percentage": 10.0, "remaining_percentage": 90.0, "resets_at": "2999-01-07T00:00:00+00:00"},
    "error_message": None,
}

NOT_OBSERVED = {"available": False, "status": "not_observed"}
INVALID_CACHE = {"available": False, "status": "invalid_cache"}


# ---------------------------------------------------------------------------
# resolveClaudeCodeUsageDisplay: snapshot selection
# ---------------------------------------------------------------------------


def test_resolve_claude_usage_display_auto_only() -> None:
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(AUTO_FRESH)}, {json.dumps(NOT_OBSERVED)})")
    assert result["available"] is True
    assert result["source"] == "claude_code_statusline"
    assert result["source_label"] == "CLI自動取得"
    assert result["stale"] is False
    assert result["five_hour"]["used_percentage"] == 42.0


def test_resolve_claude_usage_display_manual_only() -> None:
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(MANUAL_FRESH)})")
    assert result["available"] is True
    assert result["source"] == "claude_desktop_cloud_manual"
    assert result["source_label"] == "Desktop Cloud 手動確認値"
    assert result["stale"] is False
    assert result["five_hour"]["used_percentage"] == 30.0


def test_resolve_claude_usage_display_auto_newer_wins() -> None:
    auto = {**AUTO_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T10:00:00+00:00"}
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)})")
    assert result["source"] == "claude_code_statusline"


def test_resolve_claude_usage_display_manual_newer_wins() -> None:
    auto = {**AUTO_FRESH, "observed_at": "2026-01-01T10:00:00+00:00"}
    manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)})")
    assert result["source"] == "claude_desktop_cloud_manual"


def test_resolved_manual_newer_shows_both_windows_fully() -> None:
    # Manual snapshots are now required (PUT /api/claude-code-usage/manual) to always
    # include both windows, so a manual-wins render must always show both fully —
    # never a partial "未観測" gap next to a manual value.
    auto = {**AUTO_FRESH, "observed_at": "2026-01-01T10:00:00+00:00"}
    manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    html = run_compact_js(
        f"compact.claudeCodeSectionHtml(compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)}))"
    )
    assert "未観測" not in html
    assert "70%" in html  # manual five_hour remaining
    assert "90%" in html  # manual seven_day remaining
    assert html.count("Desktop Cloud 手動確認値") == 2


def test_resolve_claude_usage_display_same_timestamp_prefers_auto() -> None:
    same_time = "2026-01-01T12:00:00+00:00"
    auto = {**AUTO_FRESH, "observed_at": same_time}
    manual = {**MANUAL_FRESH, "observed_at": same_time}
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)})")
    assert result["source"] == "claude_code_statusline"


def test_resolve_claude_usage_display_invalid_auto_with_valid_manual_uses_manual() -> None:
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(INVALID_CACHE)}, {json.dumps(MANUAL_FRESH)})"
    )
    assert result["source"] == "claude_desktop_cloud_manual"


def test_resolve_claude_usage_display_valid_auto_with_invalid_manual_uses_auto() -> None:
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(AUTO_FRESH)}, {json.dumps(INVALID_CACHE)})"
    )
    assert result["source"] == "claude_code_statusline"


def test_resolve_claude_usage_display_both_invalid_reports_invalid_cache() -> None:
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(INVALID_CACHE)}, {json.dumps(INVALID_CACHE)})"
    )
    assert result["available"] is False
    assert result["source"] is None
    assert result["status"] == "invalid_cache"


def test_resolve_claude_usage_display_both_not_observed() -> None:
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(NOT_OBSERVED)})"
    )
    assert result["available"] is False
    assert result["source"] is None
    assert result["status"] == "not_observed"


def test_resolve_claude_usage_display_stale_auto_fresh_manual_prefers_newer_manual() -> None:
    stale_auto = {**AUTO_FRESH, "stale": True, "status": "stale", "observed_at": "2026-01-01T08:00:00+00:00"}
    fresh_manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(stale_auto)}, {json.dumps(fresh_manual)})"
    )
    assert result["source"] == "claude_desktop_cloud_manual"
    assert result["stale"] is False


def test_resolve_claude_usage_display_fresh_auto_stale_manual_prefers_newer_auto() -> None:
    fresh_auto = {**AUTO_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    stale_manual = {**MANUAL_FRESH, "stale": True, "status": "stale", "observed_at": "2026-01-01T08:00:00+00:00"}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(fresh_auto)}, {json.dumps(stale_manual)})"
    )
    assert result["source"] == "claude_code_statusline"
    assert result["stale"] is False


def test_resolve_claude_usage_display_stale_auto_only_keeps_origin_label() -> None:
    # source_label must always identify the origin (CLI自動取得/Desktop Cloud 手動確認値),
    # even when stale — staleness itself is carried separately via `stale`/`status`,
    # never by overwriting source_label with a generic "最終観測値" placeholder.
    stale_auto = {**AUTO_FRESH, "stale": True, "status": "stale"}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(stale_auto)}, {json.dumps(NOT_OBSERVED)})"
    )
    assert result["source"] == "claude_code_statusline"
    assert result["source_label"] == "CLI自動取得"
    assert result["stale"] is True


def test_resolve_claude_usage_display_stale_manual_only_keeps_origin_label() -> None:
    stale_manual = {**MANUAL_FRESH, "stale": True, "status": "stale"}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(stale_manual)})"
    )
    assert result["source"] == "claude_desktop_cloud_manual"
    assert result["source_label"] == "Desktop Cloud 手動確認値"
    assert result["stale"] is True


def test_resolve_claude_usage_display_partial_window_auto_passes_through() -> None:
    auto_five_hour_only = {**AUTO_FRESH, "seven_day": None}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto_five_hour_only)}, {json.dumps(NOT_OBSERVED)})"
    )
    assert result["five_hour"] is not None
    assert result["seven_day"] is None


def test_resolve_claude_usage_display_partial_manual_is_treated_as_invalid_even_if_newer() -> None:
    # A partial manual snapshot (missing seven_day) must never win over a complete
    # auto snapshot just because it's newer — PUT /api/claude-code-usage/manual and
    # the cache validator both reject partial saves now, but this locks in the
    # defense-in-depth check in resolveClaudeCodeUsageDisplay itself: a manual
    # snapshot missing either window is never treated as `available` for selection
    # purposes, so the complete, older auto snapshot is kept instead of silently
    # hiding its seven_day window behind an incomplete "newer" manual one.
    auto = {**AUTO_FRESH, "observed_at": "2026-01-01T09:00:00+00:00"}
    manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T12:00:00+00:00", "seven_day": None}
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)})")
    assert result["source"] == "claude_code_statusline"
    assert result["five_hour"]["used_percentage"] == 42.0
    assert result["seven_day"] is not None


def test_resolve_claude_usage_display_partial_manual_only_is_not_observed() -> None:
    manual = {**MANUAL_FRESH, "five_hour": None}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(manual)})"
    )
    assert result["available"] is False
    assert result["source"] is None
    assert result["status"] == "not_observed"


def test_resolve_claude_usage_display_never_mixes_windows_across_complete_snapshots() -> None:
    # Even though manual is newer, the resolver must select manual's complete
    # snapshot as a whole and never take e.g. five_hour from auto and seven_day
    # from manual.
    auto = {**AUTO_FRESH, "observed_at": "2026-01-01T09:00:00+00:00"}
    manual = {**MANUAL_FRESH, "observed_at": "2026-01-01T12:00:00+00:00"}
    result = run_compact_js(f"compact.resolveClaudeCodeUsageDisplay({json.dumps(auto)}, {json.dumps(manual)})")
    assert result["source"] == "claude_desktop_cloud_manual"
    assert result["five_hour"]["used_percentage"] == 30.0
    assert result["seven_day"]["used_percentage"] == 10.0


def test_resolve_claude_usage_display_future_rejected_manual_falls_back_to_auto() -> None:
    # A manual cache file with an observed_at far in the future is rejected by
    # validate_cache_record before it ever reaches this resolver — the API surfaces
    # it as available=False (mirroring what GET /api/claude-code-usage/manual would
    # return for such a file). The resolver must fall back to a valid auto snapshot.
    future_rejected_manual = {"available": False, "status": "invalid_cache"}
    result = run_compact_js(
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(AUTO_FRESH)}, {json.dumps(future_rejected_manual)})"
    )
    assert result["source"] == "claude_code_statusline"
    assert result["available"] is True


# ---------------------------------------------------------------------------
# end-to-end contract: resolveClaudeCodeUsageDisplay's output must be directly
# renderable by claudeCodeSectionHtml (this is exactly how renderClaudeCodeUsage
# wires them together in static/compact.js) — a resolved, available snapshot must
# never render as the "not observed" placeholder.
# ---------------------------------------------------------------------------


def test_resolved_auto_snapshot_renders_as_available_not_placeholder() -> None:
    html = run_compact_js(
        "compact.claudeCodeSectionHtml("
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(AUTO_FRESH)}, {json.dumps(NOT_OBSERVED)})"
        ")"
    )
    assert "Claude Code実行後に取得" not in html
    assert "58%" in html
    assert "CLI自動取得" in html


def test_resolved_manual_snapshot_renders_as_available_not_placeholder() -> None:
    html = run_compact_js(
        "compact.claudeCodeSectionHtml("
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(MANUAL_FRESH)})"
        ")"
    )
    assert "Claude Code実行後に取得" not in html
    assert "70%" in html
    assert "Desktop Cloud 手動確認値" in html


def test_resolved_both_unavailable_renders_placeholder() -> None:
    html = run_compact_js(
        "compact.claudeCodeSectionHtml("
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(NOT_OBSERVED)}, {json.dumps(NOT_OBSERVED)})"
        ")"
    )
    assert "Claude Code実行後に取得" in html


# ---------------------------------------------------------------------------
# source badge rendering (claudeUsageWindowHtml / claudeCodeSectionHtml)
# ---------------------------------------------------------------------------


def test_claude_usage_window_html_renders_badge_when_label_present() -> None:
    window = AUTO_FRESH["five_hour"]
    html = run_compact_js(
        f'compact.claudeUsageWindowHtml({json.dumps("Claude 5時間枠")}, {json.dumps(window)}, false, null, {json.dumps("CLI自動取得")})'
    )
    assert 'class="compact-source-badge"' in html
    assert "CLI自動取得" in html


def test_claude_usage_window_html_omits_badge_when_label_absent() -> None:
    window = AUTO_FRESH["five_hour"]
    html = run_compact_js(f'compact.claudeUsageWindowHtml({json.dumps("Claude 5時間枠")}, {json.dumps(window)})')
    assert 'class="compact-source-badge"' not in html


def test_claude_usage_window_html_renders_badge_on_unobserved_branch() -> None:
    html = run_compact_js(
        f'compact.claudeUsageWindowHtml({json.dumps("Claude 7日枠")}, null, false, null, {json.dumps("最終観測値")})'
    )
    assert "未観測" in html
    assert "最終観測値" in html
    assert 'class="compact-source-badge"' in html


def test_claude_code_section_html_threads_source_label_into_cards() -> None:
    data = {**AUTO_FRESH, "source_label": "CLI自動取得"}
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert html.count("CLI自動取得") == 2  # once per window card


def test_claude_code_section_html_manual_badge_never_says_cli_auto() -> None:
    data = {**MANUAL_FRESH, "source_label": "Desktop Cloud 手動確認値"}
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert "Desktop Cloud 手動確認値" in html
    assert "CLI自動取得" not in html
    # this specific mislabeling is explicitly forbidden regardless of source
    assert "Desktop Cloud 自動取得" not in html


def test_claude_code_section_html_stale_notice_includes_source_label() -> None:
    data = {**AUTO_FRESH, "stale": True, "status": "stale", "source_label": "CLI自動取得"}
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert "CLI自動取得" in html
    assert "最終観測値" in html
    assert "古い可能性があります" in html


def test_claude_code_section_html_stale_notice_never_repeats_last_observed_wording() -> None:
    # Regression test: source_label must never itself be "最終観測値" (that used to make
    # the stale notice read "最終観測値・最終観測値(古い可能性があります)"). The phrase
    # "最終観測値" must appear exactly once in the stale banner line.
    for source_label in ("CLI自動取得", "Desktop Cloud 手動確認値"):
        data = {**AUTO_FRESH, "stale": True, "status": "stale", "source_label": source_label}
        html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
        stale_notice_line = next(line for line in html.splitlines() if "古い可能性があります" in line)
        assert stale_notice_line.count("最終観測値") == 1, source_label
        assert source_label in stale_notice_line


def test_resolved_stale_auto_renders_origin_and_staleness_without_duplication() -> None:
    # End-to-end: resolveClaudeCodeUsageDisplay -> claudeCodeSectionHtml for a stale
    # auto-only snapshot must show the CLI自動取得 origin and mention staleness exactly once.
    stale_auto = {**AUTO_FRESH, "stale": True, "status": "stale"}
    html = run_compact_js(
        "compact.claudeCodeSectionHtml("
        f"compact.resolveClaudeCodeUsageDisplay({json.dumps(stale_auto)}, {json.dumps(NOT_OBSERVED)})"
        ")"
    )
    assert "CLI自動取得" in html
    stale_notice_line = next(line for line in html.splitlines() if "古い可能性があります" in line)
    assert stale_notice_line.count("最終観測値") == 1


def test_claude_code_section_html_without_source_label_omits_badge_backward_compat() -> None:
    # No `source_label` key at all (pre-existing call-site shape) — badge must not render,
    # and the section must still show the underlying data normally.
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(AUTO_FRESH)})")
    assert 'class="compact-source-badge"' not in html
    assert "58%" in html


def test_claude_usage_window_reset_overdue_no_negative_with_badge() -> None:
    window = {"used_percentage": 90.0, "remaining_percentage": 10.0, "resets_at": "2000-01-01T00:00:00+00:00"}
    html = run_compact_js(
        f'compact.claudeUsageWindowHtml({json.dumps("Claude 5時間枠")}, {json.dumps(window)}, false, null, {json.dumps("Desktop Cloud 手動確認値")})'
    )
    assert "reset時刻超過" in html
    assert not re.search(r"-\d", html)
    assert "Desktop Cloud 手動確認値" in html
