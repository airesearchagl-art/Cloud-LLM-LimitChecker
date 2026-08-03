"""Coverage for the admin-screen ("/") Claude Desktop Cloud manual usage panel.

DOM-driven behavior (form submit flow, pre-fill on load, success/error
rendering) is out of reach for the Node `require()` harness used elsewhere in
this repo (no jsdom dependency — adding one would need explicit approval) and
is instead verified via fixture-based browser preview, per the established
convention already used for compact.js layout customization
(see tests/test_compact_layout_customization.py). This file covers what IS
reachable without a DOM: the one pure function this panel added, and static
wiring/wording checks against index.html + app.js source text.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"


def run_app_js(expression: str):
    script = f"""
const app = require({json.dumps(str(APP_JS))});
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


def test_confirm_claude_desktop_cloud_usage_save_defaults_true_without_window() -> None:
    # Under plain Node (no `window`), the wrapper must fail open to `true` rather
    # than throw — this is what makes it safely require()-able/testable, and
    # matches the guard already used by the pre-existing confirmLayoutReset
    # pattern in compact.js.
    result = run_app_js("app.confirmClaudeDesktopCloudUsageSave()")
    assert result is True


def test_app_js_exports_confirm_claude_desktop_cloud_usage_save() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    assert "confirmClaudeDesktopCloudUsageSave," in js or "confirmClaudeDesktopCloudUsageSave:" in js


def test_app_js_never_calls_window_confirm_directly_for_this_panel() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("#claudeDesktopCloudUsageForm")
    end = js.index("#codexUsageForm")
    handler_source = js[start:end]
    assert "window.confirm(" not in handler_source
    assert "confirmClaudeDesktopCloudUsageSave()" in handler_source


def test_app_js_claude_desktop_cloud_handler_puts_to_manual_endpoint() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("#claudeDesktopCloudUsageForm")
    end = js.index("#codexUsageForm")
    handler_source = js[start:end]
    assert "/api/claude-code-usage/manual" in handler_source
    assert '"PUT"' in handler_source or "'PUT'" in handler_source


def test_index_html_claude_desktop_cloud_panel_has_expected_field_ids() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for expected_id in (
        "claudeDesktopCloudUsagePanel",
        "claudeDesktopCloudUsageForm",
        "claudeDesktopCloudFiveHourRemaining",
        "claudeDesktopCloudFiveHourResetsAt",
        "claudeDesktopCloudSevenDayRemaining",
        "claudeDesktopCloudSevenDayResetsAt",
        "claudeDesktopCloudUsageSubmit",
        "claudeDesktopCloudUsageResult",
        "claudeDesktopCloudUsageLastConfirmed",
    ):
        assert f'id="{expected_id}"' in html, expected_id


def test_index_html_claude_desktop_cloud_panel_never_implies_automatic_fetch() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="claudeDesktopCloudUsagePanel"')
    end = html.index("</section>", start)
    panel_html = html[start:end]
    assert "自動取得ではありません" in panel_html
    assert "Desktop Cloud 自動取得" not in panel_html
    assert "自動的に反映" not in panel_html


def test_index_html_claude_desktop_cloud_panel_has_no_hardcoded_default_values() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="claudeDesktopCloudUsagePanel"')
    end = html.index("</section>", start)
    panel_html = html[start:end]
    assert 'value="' not in panel_html


def test_index_html_claude_desktop_cloud_panel_fieldsets_close_properly() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="claudeDesktopCloudUsagePanel"')
    end = html.index("</section>", start)
    panel_html = html[start:end]
    assert panel_html.count("<fieldset") == 2
    assert panel_html.count("</fieldset>") == 2
    assert panel_html.count("<form") == 1
    assert panel_html.count("</form>") == 1


def test_index_html_claude_desktop_cloud_panel_requires_all_four_fields() -> None:
    # Both windows are mandatory on save (PUT /api/claude-code-usage/manual rejects a
    # partial snapshot — see app/schemas.py ClaudeDesktopCloudUsageInput), so all four
    # inputs must carry the native `required` attribute as a first line of validation.
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="claudeDesktopCloudUsagePanel"')
    end = html.index("</section>", start)
    panel_html = html[start:end]
    for field_id in (
        "claudeDesktopCloudFiveHourRemaining",
        "claudeDesktopCloudFiveHourResetsAt",
        "claudeDesktopCloudSevenDayRemaining",
        "claudeDesktopCloudSevenDayResetsAt",
    ):
        field_start = panel_html.index(f'id="{field_id}"')
        field_end = panel_html.index("/>", field_start)
        field_tag = panel_html[field_start:field_end]
        assert "required" in field_tag, field_id


def test_index_html_claude_desktop_cloud_result_area_is_aria_live_polite() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index('id="claudeDesktopCloudUsageResult"')
    end = html.index(">", start)
    tag = html[start:end]
    assert 'aria-live="polite"' in tag


def test_parse_datetime_local_to_iso_or_null_valid_value() -> None:
    result = run_app_js('app.parseDatetimeLocalToIsoOrNull("2026-01-01T17:00")')
    assert result is not None
    assert result.startswith("2026-01-01T")


def test_parse_datetime_local_to_iso_or_null_empty_string_is_null() -> None:
    result = run_app_js('app.parseDatetimeLocalToIsoOrNull("")')
    assert result is None


def test_parse_datetime_local_to_iso_or_null_garbage_string_is_null_not_throw() -> None:
    # This is exactly the case that used to reach new Date(value).toISOString()
    # uncaught: an invalid datetime-local string must resolve to null, not throw.
    result = run_app_js('app.parseDatetimeLocalToIsoOrNull("not-a-datetime")')
    assert result is None


def test_app_js_claude_desktop_cloud_handler_validates_datetime_before_iso_conversion() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("#claudeDesktopCloudUsageForm")
    end = js.index("#codexUsageForm")
    handler_source = js[start:end]
    assert "parseDatetimeLocalToIsoOrNull(" in handler_source
    # the raw, unvalidated .toISOString() call this handler used to make must be gone
    assert ".toISOString()" not in handler_source


def test_app_js_claude_desktop_cloud_handler_requires_both_windows() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("#claudeDesktopCloudUsageForm")
    end = js.index("#codexUsageForm")
    handler_source = js[start:end]
    assert "five_hour_remaining_percentage" in handler_source
    assert "seven_day_remaining_percentage" in handler_source
    # both must be checked in the same guard clause (no success path that only checks one)
    assert "fiveHourRemaining === \"\"" in handler_source
    assert "sevenDayRemaining === \"\"" in handler_source


def test_app_js_claude_desktop_cloud_handler_disables_button_during_submit_and_reenables() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("#claudeDesktopCloudUsageForm")
    end = js.index("#codexUsageForm")
    handler_source = js[start:end]
    assert "submitButton.disabled = true" in handler_source
    assert "submitButton.disabled = false" in handler_source
    assert "finally" in handler_source


def test_app_js_passes_node_syntax_check() -> None:
    proc = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
