"""Pure-function tests for the GitHub Actions monthly billing card.

Covers static/app.js (main dashboard) and static/compact.js (/compact).
Both must render distinctly from the existing GitHub API Rate Limit card,
must never show a fabricated 0 for plan_unknown/permission_required states
(a literal "—" instead), and must never leak "undefined" into the rendered
HTML for any of these states.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
COMPACT_JS = ROOT / "static" / "compact.js"


def run_app_js(expression: str) -> str:
    script = f"""
const app = require({json.dumps(str(APP_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


def run_compact_js(expression: str) -> str:
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


NORMAL_DATA = {
    "fetched": True,
    "refreshing": False,
    "stale": False,
    "live_validation_required": False,
    "status": "normal",
    "plan_name": "pro",
    "included_minutes": 3000,
    "used_included_minutes": 125,
    "remaining_minutes": 2875,
    "usage_percentage": 4.1666666,
    "overage_minutes": 0,
    "paid_non_included_minutes": 0,
    "billing_year": 2026,
    "billing_month": 8,
    "collected_at": "2026-08-08T12:00:00+00:00",
    "source": "github_billing_api",
    "skipped_unknown_skus": [],
    "error": None,
    "last_attempt_at": "2026-08-08T12:00:00+00:00",
    "last_success_at": "2026-08-08T12:00:00+00:00",
    "retry_after_seconds": 0,
}


def with_status(status, **overrides):
    data = dict(NORMAL_DATA)
    data["status"] = status
    data.update(overrides)
    return data


PLAN_UNKNOWN_DATA = with_status(
    "plan_unknown",
    plan_name="team",
    included_minutes=None,
    used_included_minutes=None,
    remaining_minutes=None,
    usage_percentage=None,
    overage_minutes=None,
)

PERMISSION_REQUIRED_DATA = {
    "fetched": False,
    "refreshing": False,
    "stale": False,
    "live_validation_required": True,
    "error": {
        "error_type": "permission_required",
        "user_message": (
            "The current GitHub credential does not have permission to read Actions billing usage "
            '(requires the "Plan: read" permission / "user" scope).'
        ),
    },
    "last_known": None,
}


# --- app.js: githubActionsBillingHtml ---------------------------------------------


def test_app_not_fetched_shows_未取得():
    html = run_app_js("app.githubActionsBillingHtml(null)")
    assert "未取得" in html


def test_app_normal_shows_used_and_remaining():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(NORMAL_DATA)})")
    assert "125" in html
    assert "3,000" in html or "3000" in html
    assert "2,875" in html or "2875" in html
    assert "undefined" not in html


def test_app_plan_unknown_shows_dash_not_zero():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(PLAN_UNKNOWN_DATA)})")
    assert "—" in html
    assert ">0<" not in html
    assert "undefined" not in html


def test_app_permission_required_shows_error_message_not_raw():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(PERMISSION_REQUIRED_DATA)})")
    assert "Plan" in html or "permission" in html
    assert "undefined" not in html


def test_app_overage_shows_overage_line():
    data = with_status("overage", overage_minutes=100, remaining_minutes=0, used_included_minutes=2000, included_minutes=2000)
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(data)})")
    assert "Overage" in html
    assert "100" in html


def test_app_paid_non_included_minutes_shown_separately_from_included():
    data = with_status("normal", paid_non_included_minutes=60)
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(data)})")
    assert "Non-included" in html
    assert "60" in html


def test_app_status_class_mapping():
    assert run_app_js('app.githubActionsBillingStatusClass("normal")') == "github-status-normal"
    assert run_app_js('app.githubActionsBillingStatusClass("warning")') == "github-status-warning"
    assert run_app_js('app.githubActionsBillingStatusClass("exhausted")') == "github-status-exhausted"
    assert run_app_js('app.githubActionsBillingStatusClass("overage")') == "github-status-exhausted"
    assert run_app_js('app.githubActionsBillingStatusClass("plan_unknown")') == "github-status-error"


def test_app_stale_last_known_notice_shown():
    data = {
        "fetched": False,
        "error": {"error_type": "timeout", "user_message": "Fetching GitHub Actions billing usage timed out."},
        "last_known": {**NORMAL_DATA, "stale": True},
    }
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(data)})")
    assert "古い情報" in html
    assert "undefined" not in html


# --- compact.js: githubActionsBillingCardHtml / githubActionsBillingSectionHtml ---


def test_compact_not_fetched_card_has_card_id():
    html = run_compact_js('compact.githubActionsBillingCardHtml(null, "github-actions.billing")')
    assert 'data-card-id="github-actions.billing"' in html
    assert "undefined" not in html


def test_compact_normal_card_renders_percentage_and_minutes():
    html = run_compact_js(f'compact.githubActionsBillingCardHtml({json.dumps(NORMAL_DATA)}, "github-actions.billing")')
    assert 'data-card-id="github-actions.billing"' in html
    assert "125" in html
    assert "undefined" not in html


def test_compact_plan_unknown_card_shows_dash():
    html = run_compact_js(f'compact.githubActionsBillingCardHtml({json.dumps(PLAN_UNKNOWN_DATA)}, "github-actions.billing")')
    assert "—" in html
    assert "undefined" not in html


def test_compact_section_html_wraps_grid_inner():
    html = run_compact_js(f"compact.githubActionsBillingSectionHtml({json.dumps(NORMAL_DATA)})")
    assert "compact-github-grid-inner" in html
    assert 'data-card-id="github-actions.billing"' in html


def test_compact_status_class_mapping():
    assert run_compact_js('compact.githubActionsBillingStatusClass("normal")') == "compact-status-normal"
    assert run_compact_js('compact.githubActionsBillingStatusClass("warning")') == "compact-status-warning"
    assert run_compact_js('compact.githubActionsBillingStatusClass("exhausted")') == "compact-status-exhausted"
    assert run_compact_js('compact.githubActionsBillingStatusClass("overage")') == "compact-status-exhausted"
    assert run_compact_js('compact.githubActionsBillingStatusClass("plan_unknown")') == "compact-status-error"


# --- percentage boundaries ---------------------------------------------------------


def test_compact_usage_percentage_clamped_at_100_for_meter_width():
    # overage can push usage_percentage slightly above 100 conceptually; the
    # meter fill width must still be clamped to 100 so the bar never overflows.
    data = with_status("overage", usage_percentage=105.0, overage_minutes=60, remaining_minutes=0)
    html = run_compact_js(f'compact.githubActionsBillingCardHtml({json.dumps(data)}, "github-actions.billing")')
    assert "width:100%" in html


def test_compact_usage_percentage_zero_renders_without_error():
    data = with_status("normal", usage_percentage=0.0, used_included_minutes=0, remaining_minutes=3000)
    html = run_compact_js(f'compact.githubActionsBillingCardHtml({json.dumps(data)}, "github-actions.billing")')
    assert "width:0%" in html
    assert "undefined" not in html


def test_app_and_compact_never_conflate_with_rate_limit_labels():
    html_app = run_app_js(f"app.githubActionsBillingHtml({json.dumps(NORMAL_DATA)})")
    html_compact = run_compact_js(
        f'compact.githubActionsBillingCardHtml({json.dumps(NORMAL_DATA)}, "github-actions.billing")'
    )
    for html in (html_app, html_compact):
        assert "REST API core" not in html
        assert "GraphQL API" not in html
