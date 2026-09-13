"""Coverage for the application-level navigation and the new Overview /
Limits (usage allowance) views layered over the existing GET
/api/usage-allowances read model.

Mirrors the source-text + `node --check` convention already used by
tests/test_codex_rate_limits_admin_js.py and
tests/test_claude_desktop_cloud_usage_admin_js.py: no jsdom dependency is
added, so DOM-driven behavior is not exercised here — only the pure
formatting/grouping functions (via Node `require()`) and static wiring/
wording checks against index.html + app.js + styles.css source text.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLES_CSS = ROOT / "static" / "styles.css"


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


# ---------------------------------------------------------------------------
# Navigation markup (index.html)
# ---------------------------------------------------------------------------


def test_index_html_nav_has_expected_buttons_and_label():
    html = INDEX_HTML.read_text(encoding="utf-8")
    header_end = html.index("</header>")
    nav_start = html.index("<nav", header_end)
    nav_end = html.index("</nav>", nav_start)
    nav_html = html[nav_start:nav_end]

    assert 'aria-label="ビュー切り替え"' in nav_html

    for button_id, label in (
        ("navOverview", "概要"),
        ("navLimits", "上限"),
        ("navDiagnostics", "診断"),
        ("navHistory", "履歴"),
    ):
        id_marker = f'id="{button_id}"'
        assert id_marker in nav_html, button_id
        id_idx = nav_html.index(id_marker)
        button_tag_start = nav_html.rindex("<button", 0, id_idx)
        button_close_idx = nav_html.index("</button>", button_tag_start)
        button_block = nav_html[button_tag_start:button_close_idx]
        assert 'type="button"' in button_block, button_id
        assert label in button_block, button_id


def test_index_html_new_views_are_placed_immediately_after_nav():
    html = INDEX_HTML.read_text(encoding="utf-8")
    nav_end = html.index("</nav>")

    assert 'id="overviewView"' in html
    assert 'id="limitsAllowanceView"' in html

    overview_id_idx = html.index('id="overviewView"')
    overview_tag_start = html.rindex("<section", 0, overview_id_idx)
    overview_tag_end = html.index(">", overview_id_idx)
    overview_tag = html[overview_tag_start:overview_tag_end]
    assert 'data-view="overview"' in overview_tag

    limits_id_idx = html.index('id="limitsAllowanceView"')
    limits_tag_start = html.rindex("<section", 0, limits_id_idx)
    limits_tag_end = html.index(">", limits_id_idx)
    limits_tag = html[limits_tag_start:limits_tag_end]
    assert 'data-view="limits"' in limits_tag

    # both new sections come after the nav, overview before limits, and
    # nothing existing (e.g. the toolbar) is allowed to sit between them.
    assert nav_end < overview_tag_start < limits_tag_start
    between = html[overview_tag_end:limits_tag_start]
    assert 'class="toolbar"' not in between


# ---------------------------------------------------------------------------
# Regression guard: every existing panel id from the spec's mapping survives
# ---------------------------------------------------------------------------

EXISTING_MAPPED_PANEL_IDS = (
    "cards",
    "collectorForm",
    "collectorRuns",
    "githubRateLimitPanel",
    "githubActionsBillingPanel",
    "githubGraphqlDiagnosticsPanel",
    "claudeDesktopCloudUsagePanel",
    "codexRateLimitsPanel",
    "codexUsagePanel",
    "serviceForm",
    "limitForm",
    "usageForm",
    "alerts",
    "history",
)


def test_index_html_existing_panel_ids_still_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for panel_id in EXISTING_MAPPED_PANEL_IDS:
        assert f'id="{panel_id}"' in html, panel_id
    assert 'class="toolbar"' in html


def test_index_html_data_view_mapping_counts_match_spec():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # limits: toolbar, #cards section, claudeDesktopCloudUsagePanel section,
    # codexRateLimitsPanel section, codexUsagePanel section, the
    # serviceForm/limitForm/usageForm grid, plus the new limitsAllowanceView.
    assert html.count('data-view="limits"') == 7
    # diagnostics: collectorForm/collectorRuns grid, githubRateLimitPanel,
    # githubActionsBillingPanel, githubGraphqlDiagnosticsPanel.
    assert html.count('data-view="diagnostics"') == 4
    # history: the alerts/history grid.
    assert html.count('data-view="history"') == 1
    # overview: only the new overview section.
    assert html.count('data-view="overview"') == 1


# ---------------------------------------------------------------------------
# Data fetching (app.js)
# ---------------------------------------------------------------------------


def test_app_js_requests_usage_allowances_endpoint_exactly_once():
    js = APP_JS.read_text(encoding="utf-8")
    # the path may also appear in an explanatory comment (as other endpoints'
    # paths do elsewhere in this file); what must be exactly one is the
    # actual fetch call site.
    assert js.count('fetch("/api/usage-allowances")') == 1


def test_app_js_uses_non_throwing_fetch_allowances_helper_not_the_throwing_api_helper():
    js = APP_JS.read_text(encoding="utf-8")
    assert "async function fetchAllowancesSafe()" in js
    assert "fetchAllowancesSafe()" in js
    # the throwing api() helper must never be used for this optional endpoint
    # (a single failing endpoint must not break the rest of loadAll()).
    assert 'api("/api/usage-allowances")' not in js


def test_fetch_allowances_safe_is_exported_for_testing():
    js = APP_JS.read_text(encoding="utf-8")
    assert "fetchAllowancesSafe," in js or "fetchAllowancesSafe:" in js


# ---------------------------------------------------------------------------
# Provenance (source_kind) labels
# ---------------------------------------------------------------------------

USAGE_ALLOWANCE_SOURCE_KIND_LABELS = {
    "OFFICIAL_API": "公式API",
    "OFFICIAL_LOCAL_RUNTIME": "公式ローカル取得",
    "LOCAL_OBSERVATION": "ローカル観測",
    "MANUAL": "手動入力",
    "TEMPORAL_CORRELATION": "時間相関推定",
}


def test_source_kind_labels_are_all_mapped():
    for source_kind, expected_label in USAGE_ALLOWANCE_SOURCE_KIND_LABELS.items():
        result = run_app_js(f'app.usageAllowanceSourceKindLabel("{source_kind}")')
        assert result == expected_label, source_kind


def test_source_kind_label_unknown_value_falls_back_to_raw_string():
    result = run_app_js('app.usageAllowanceSourceKindLabel("SOME_NEW_KIND")')
    assert result == "SOME_NEW_KIND"


def test_source_kind_label_null_never_crashes_or_blank():
    result = run_app_js("app.usageAllowanceSourceKindLabel(null)")
    assert result
    assert isinstance(result, str)
    assert result != ""


def test_source_badge_html_escapes_unknown_source_kind():
    html = run_app_js('app.usageAllowanceSourceBadgeHtml("<script>alert(1)</script>")')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Window duration label mapping
# ---------------------------------------------------------------------------


def test_window_duration_label_mapping():
    assert run_app_js("app.usageAllowanceWindowLabel(300)") == "5時間枠"
    assert run_app_js("app.usageAllowanceWindowLabel(10080)") == "週次枠"
    assert run_app_js("app.usageAllowanceWindowLabel(null)") == "期間不明"
    # fallback branch: any other integer n -> `${n}分枠`
    assert run_app_js("app.usageAllowanceWindowLabel(60)") == "60分枠"


def test_window_duration_label_never_references_model_or_plan_fields():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function usageAllowanceWindowLabel")
    end = js.index("\n}\n", start)
    fn_source = js[start:end]
    assert "display_name" not in fn_source
    assert "plan_type" not in fn_source
    assert "limit_id" not in fn_source


# ---------------------------------------------------------------------------
# Unavailable rendering (both views)
# ---------------------------------------------------------------------------


def test_unavailable_card_never_uses_a_meter_or_a_percentage():
    item = {
        "provider": "openai",
        "product_surface": "chatgpt_web",
        "status": "not_observed",
        "source_kind": "MANUAL",
    }
    html = run_app_js(f"app.usageAllowanceUnavailableCardHtml({json.dumps(item)})")
    assert "取得不能" in html
    assert item["status"] in html
    assert item["provider"] in html
    assert item["product_surface"] in html
    assert 'class="meter"' not in html
    assert "%" not in html


def test_overview_status_line_never_uses_a_meter_or_a_percentage():
    # `not_observed`, not `stale`: this renderer is for genuinely unavailable
    # surfaces. A stale surface has a real last-known value, so it appears in
    # the value list with an inline 最終取得値 badge instead (see below).
    item = {
        "provider": "anthropic",
        "product_surface": "claude_desktop_cloud",
        "status": "not_observed",
        "source_kind": "MANUAL",
    }
    html = run_app_js(f"app.usageAllowanceOverviewStatusLineHtml({json.dumps(item)})")
    assert "取得不能" in html
    assert 'class="meter"' not in html
    assert "%" not in html


# ---------------------------------------------------------------------------
# Bucket title fallback
# ---------------------------------------------------------------------------


def test_bucket_title_falls_back_display_name_then_limit_id_then_surface():
    bucket_with_display_name = {"display_name": "My Plan", "limit_id": "x", "product_surface": "api"}
    assert run_app_js(f"app.usageAllowanceBucketTitle({json.dumps(bucket_with_display_name)})") == "My Plan"

    bucket_with_limit_id = {"display_name": None, "limit_id": "tier-1-limit", "product_surface": "api"}
    assert run_app_js(f"app.usageAllowanceBucketTitle({json.dumps(bucket_with_limit_id)})") == "tier-1-limit"

    bucket_with_only_surface = {"display_name": None, "limit_id": None, "product_surface": "chatgpt_web"}
    assert run_app_js(f"app.usageAllowanceBucketTitle({json.dumps(bucket_with_only_surface)})") == "chatgpt_web"


# ---------------------------------------------------------------------------
# No hard-coded model names anywhere in the new (or existing) app.js
# ---------------------------------------------------------------------------


def test_app_js_never_hardcodes_a_model_name():
    js = APP_JS.read_text(encoding="utf-8")
    for forbidden in ("GPT-", "Astra", "Claude 3", "gpt-4", "o3"):
        assert forbidden not in js, forbidden


# ---------------------------------------------------------------------------
# Accessibility (styles.css)
# ---------------------------------------------------------------------------


def test_styles_css_has_focus_visible_rules():
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css


# ---------------------------------------------------------------------------
# Stale is "old but real", never "unavailable"
# ---------------------------------------------------------------------------


def test_overview_unavailable_block_carries_only_genuinely_unavailable_surfaces():
    # A stale bucket is a value: it belongs in the value list (with an inline
    # 最終取得値 badge), and must not ALSO appear in the block reserved for
    # surfaces that returned nothing — one surface, one entry.
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function renderOverviewView")
    end = js.index("\n}\n", start)
    fn_source = js[start:end]

    assert "payload.unavailable" in fn_source
    assert 'bucket.status === "stale"' not in fn_source
    assert "usageAllowanceOverviewStaleLineHtml" not in js


# ---------------------------------------------------------------------------
# Missing values must stay readable (no "未取得%" / "—%")
# ---------------------------------------------------------------------------


def test_used_percent_text_stays_readable_when_the_value_is_missing():
    assert run_app_js("app.usageAllowanceUsedPercentText(42)") == "42%"
    assert run_app_js("app.usageAllowanceUsedPercentText(null)") == "使用率不明"


def test_window_html_shows_a_dash_without_a_percent_sign_when_remaining_is_null():
    window = {
        "source_slot": "primary",
        "window_duration_minutes": 300,
        "used_percent": 40,
        "remaining_percent": None,
        "resets_at": None,
    }
    html = run_app_js(f"app.usageAllowanceWindowHtml({json.dumps(window)}, false)")

    assert "—%" not in html
    assert "—" in html
    # a null reset must not be turned into a guessed countdown
    assert "あと" not in html
    assert "未設定" in html


# ---------------------------------------------------------------------------
# One row per bucket: (provider, product_surface) is not a unique key
# ---------------------------------------------------------------------------


def test_overview_keeps_both_buckets_that_share_a_provider_and_surface():
    # app/usage_allowance.py maps the Codex auto cache and the Codex manual
    # cache to the same openai/work_codex pair; collapsing them would hide one
    # reading entirely.
    payload = {
        "generated_at": "2026-09-13T00:00:00+00:00",
        "allowances": [
            {
                "provider": "openai",
                "product_surface": "work_codex",
                "display_name": None,
                "limit_id": "codex",
                "plan_type": None,
                "rate_limit_reached_type": None,
                "status": "ok",
                "source_kind": "OFFICIAL_LOCAL_RUNTIME",
                "provenance": "codex_app_server",
                "observed_at": "2026-09-13T00:00:00+00:00",
                "windows": [
                    {
                        "source_slot": "primary",
                        "window_duration_minutes": 300,
                        "used_percent": 10,
                        "remaining_percent": 90,
                        "resets_at": None,
                    }
                ],
            },
            {
                "provider": "openai",
                "product_surface": "work_codex",
                "display_name": None,
                "limit_id": None,
                "plan_type": None,
                "rate_limit_reached_type": None,
                "status": "stale",
                "source_kind": "MANUAL",
                "provenance": "codex_manual",
                "observed_at": "2026-08-01T00:00:00+00:00",
                "windows": [
                    {
                        "source_slot": "five_hour",
                        "window_duration_minutes": 300,
                        "used_percent": 90,
                        "remaining_percent": 10,
                        "resets_at": None,
                    }
                ],
            },
        ],
        "unavailable": [],
    }
    rows = run_app_js(f"app.usageAllowanceOverviewRows({json.dumps(payload)})")

    assert len(rows) == 2, rows
    used = sorted(row["window"]["used_percent"] for row in rows)
    assert used == [10, 90]


def test_overview_row_marks_a_stale_bucket_inline():
    entry = {
        "provider": "openai",
        "surface": "work_codex",
        "window": {
            "source_slot": "five_hour",
            "window_duration_minutes": 300,
            "used_percent": 90,
            "remaining_percent": 10,
            "resets_at": None,
        },
        "bucket": {
            "provider": "openai",
            "product_surface": "work_codex",
            "display_name": None,
            "limit_id": None,
            "status": "stale",
            "source_kind": "MANUAL",
        },
    }
    html = run_app_js(f"app.usageAllowanceOverviewRowHtml({json.dumps(entry)})")

    # a stale value must not read as a live one
    assert "最終取得値" in html
    assert "90%" in html


def test_overview_row_names_the_bucket_when_it_differs_from_the_surface():
    entry = {
        "provider": "openai",
        "surface": "work_codex",
        "window": None,
        "bucket": {
            "provider": "openai",
            "product_surface": "work_codex",
            "display_name": None,
            "limit_id": "codex_secondary",
            "status": "ok",
            "source_kind": "OFFICIAL_LOCAL_RUNTIME",
        },
    }
    html = run_app_js(f"app.usageAllowanceOverviewRowHtml({json.dumps(entry)})")
    assert "codex_secondary" in html


# ---------------------------------------------------------------------------
# The UI states what the payload states, and nothing more
# ---------------------------------------------------------------------------


def test_full_usage_is_never_declared_as_limit_reached():
    # "reached" is a claim the payload makes separately via
    # rate_limit_reached_type; a rounded 100% must not assert it.
    assert run_app_js("app.usageAllowanceWindowStatus(100)") == "危険"
    assert run_app_js("app.usageAllowanceWindowStatus(120)") == "危険"
    assert run_app_js("app.usageAllowanceWindowStatus(86)") == "危険"
    assert run_app_js("app.usageAllowanceWindowStatus(70)") == "注意"
    assert run_app_js("app.usageAllowanceWindowStatus(0)") == "正常"

    # Scoped to this function on purpose: the dashboard's own limit rows do
    # carry a real 上限到達 status (statusClass/meterClass map it), and that
    # existing vocabulary is out of scope here. What must not happen is the
    # allowance view inventing that claim from a percentage.
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function usageAllowanceWindowStatus")
    end = js.index("\n}\n", start)
    assert "上限到達" not in js[start:end]


def test_unparseable_reset_never_reaches_the_screen_as_invalid_date():
    text = run_app_js('app.usageAllowanceWindowResetText("not-a-timestamp", false)')
    assert "Invalid Date" not in text
    assert text == "未設定"


# ---------------------------------------------------------------------------
# Navigation must not trap the browser's Back button
# ---------------------------------------------------------------------------


def test_hash_is_only_written_when_it_would_actually_change():
    assert run_app_js('app.shouldWriteAppViewHash("#limits", "limits")') is False
    assert run_app_js('app.shouldWriteAppViewHash("#overview", "limits")') is True
    assert run_app_js('app.shouldWriteAppViewHash("", "overview")') is True


def test_set_active_view_toggles_sections_and_marks_the_nav_button():
    # Behavioural, against a minimal fake DOM: the source-text checks below
    # cannot tell whether the wiring actually works.
    result = run_app_js(
        """(() => {
          const sections = [
            { dataset: { view: "overview" }, hidden: false },
            { dataset: { view: "limits" }, hidden: false },
            { dataset: { view: "diagnostics" }, hidden: false },
          ];
          const buttons = {
            "#navOverview": { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
            "#navLimits": { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
            "#navDiagnostics": { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
            "#navHistory": { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } },
          };
          global.document = {
            querySelectorAll: () => sections,
            querySelector: (sel) => buttons[sel] || null,
          };
          global.location = { hash: "" };
          app.setActiveView("limits");
          return {
            hidden: sections.map((s) => s.hidden),
            current: Object.entries(buttons).filter(([, b]) => b.attrs["aria-current"]).map(([sel]) => sel),
            hash: global.location.hash,
          };
        })()"""
    )

    assert result["hidden"] == [True, False, True]
    assert result["current"] == ["#navLimits"]
    assert result["hash"] == "limits"


def test_set_active_view_does_not_write_the_hash_when_asked_not_to():
    # This is what the hashchange handler does; writing here is what trapped
    # the Back button.
    result = run_app_js(
        """(() => {
          global.document = {
            querySelectorAll: () => [{ dataset: { view: "limits" }, hidden: false }],
            querySelector: () => null,
          };
          global.location = { hash: "#overview" };
          app.setActiveView("limits", { updateHash: false });
          return global.location.hash;
        })()"""
    )

    assert result == "#overview"


def test_hashchange_handler_never_writes_the_hash_back():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index('window.addEventListener("hashchange"')
    end = js.index("\n", start)
    handler = js[start:end]
    assert "updateHash: false" in handler
    # the initial sync must use replaceState so an entry is not pushed
    assert "history.replaceState" in js


# ---------------------------------------------------------------------------
# Source hygiene
# ---------------------------------------------------------------------------


def test_app_js_contains_no_control_characters_that_hide_code_from_tooling():
    # A literal NUL makes grep treat the file as binary from that offset on,
    # silently excluding everything after it from source-text checks.
    raw = APP_JS.read_bytes()
    assert b"\x00" not in raw
    text = raw.decode("utf-8")
    forbidden = {ch for ch in text if ord(ch) < 32 and ch not in "\n\r\t"}
    assert not forbidden, sorted(hex(ord(ch)) for ch in forbidden)


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------


def test_app_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
