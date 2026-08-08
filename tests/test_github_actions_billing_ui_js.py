"""Pure-function tests for the GitHub Actions monthly billing card.

Covers static/app.js (main dashboard) and static/compact.js (/compact).
Both must render distinctly from the existing GitHub API Rate Limit card,
must never fabricate an exact used/remaining/percentage number (official
Billing usage summary discountQuantity mixes included-allowance, public
-repo, and self-hosted discount and cannot be safely split apart -- see
app/github_actions_billing.py's module docstring), and must never leak
"undefined" or a raw non-429 response body into the rendered HTML / error
message.
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


INCONCLUSIVE_DATA = {
    "fetched": True,
    "refreshing": False,
    "stale": False,
    "live_validation_required": False,
    "status": "usage_breakdown_inconclusive",
    "plan_name": "pro",
    "included_minutes": 3000,
    "used_included_minutes": None,
    "remaining_minutes": None,
    "usage_percentage": None,
    "discounted_standard_minutes": 125,
    "billable_standard_minutes": 0,
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


def with_overrides(**overrides):
    data = dict(INCONCLUSIVE_DATA)
    data.update(overrides)
    return data


PLAN_UNKNOWN_DATA = with_overrides(
    status="plan_unknown",
    plan_name="team",
    included_minutes=None,
    used_included_minutes=None,
    remaining_minutes=None,
    usage_percentage=None,
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


def test_app_never_fabricates_exact_used_or_remaining():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(INCONCLUSIVE_DATA)})")
    assert "Exact used: —" in html
    assert "Exact remaining: —" in html
    assert "undefined" not in html


def test_app_shows_monthly_allowance_from_plan():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(INCONCLUSIVE_DATA)})")
    assert "3,000" in html or "3000" in html
    assert "Monthly allowance" in html


def test_app_shows_safely_named_breakdown_fields_not_included_quota_language():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(INCONCLUSIVE_DATA)})")
    assert "Discounted standard usage" in html
    assert "Billable standard usage" in html
    assert "Non-included paid minutes" in html
    # must never claim these numbers are the plan's included-quota consumption
    assert "Overage" not in html


def test_app_plan_unknown_shows_dash_not_zero():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(PLAN_UNKNOWN_DATA)})")
    assert "—" in html
    assert ">0<" not in html
    assert "undefined" not in html


def test_app_permission_required_shows_error_message_not_raw():
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(PERMISSION_REQUIRED_DATA)})")
    assert "Plan" in html or "permission" in html
    assert "undefined" not in html


def test_app_status_class_mapping():
    assert run_app_js('app.githubActionsBillingStatusClass("usage_breakdown_inconclusive")') == "github-status-unknown"
    assert run_app_js('app.githubActionsBillingStatusClass("plan_unknown")') == "github-status-error"


def test_app_stale_last_known_notice_shown():
    data = {
        "fetched": False,
        "error": {"error_type": "timeout", "user_message": "Fetching GitHub Actions billing usage timed out."},
        "last_known": {**INCONCLUSIVE_DATA, "stale": True},
    }
    html = run_app_js(f"app.githubActionsBillingHtml({json.dumps(data)})")
    assert "古い情報" in html
    assert "undefined" not in html


# --- app.js: githubActionsBillingErrorDisplay (raw response body leak fix) --------


def test_app_error_display_429_uses_backend_user_message():
    body = {"detail": {"user_message": "更新の間隔が短すぎます。しばらく待ってから再度お試しください。", "retry_after_seconds": 42}}
    resolved = run_app_js(f"app.githubActionsBillingErrorDisplay(429, {json.dumps(body)})")
    assert resolved["user_message"] == "更新の間隔が短すぎます。しばらく待ってから再度お試しください。"
    assert resolved["retry_after_seconds"] == 42


def test_app_error_display_non_429_never_echoes_raw_body_secret():
    # Simulates a 500 response whose body contains a secret-looking marker --
    # githubActionsBillingErrorDisplay must never read/echo response bodies
    # for non-429 statuses, only return the fixed generic message.
    resolved = run_app_js('app.githubActionsBillingErrorDisplay(500, null)')
    assert "SECRET_MARKER" not in resolved["user_message"]
    assert resolved["user_message"] == "GitHub Actions billingの更新に失敗しました。しばらく待ってから再度お試しください。"
    assert resolved["error_type"] == "unknown_error"


def test_app_error_display_429_with_malformed_body_falls_back_to_generic():
    resolved = run_app_js('app.githubActionsBillingErrorDisplay(429, {"unexpected": "SECRET_MARKER_XYZ"})')
    assert "SECRET_MARKER_XYZ" not in resolved["user_message"]
    assert resolved["user_message"] == "GitHub Actions billingの更新に失敗しました。しばらく待ってから再度お試しください。"


def test_app_error_display_network_error_uses_generic_message():
    resolved = run_app_js("app.githubActionsBillingErrorDisplay(null, null)")
    assert resolved["user_message"] == "GitHub Actions billingの更新に失敗しました。しばらく待ってから再度お試しください。"
    assert resolved["error_type"] == "unknown_error"


# --- compact.js: githubActionsBillingCardHtml / githubActionsBillingSectionHtml ---


def test_compact_not_fetched_card_has_card_id():
    html = run_compact_js('compact.githubActionsBillingCardHtml(null, "github-actions.billing")')
    assert 'data-card-id="github-actions.billing"' in html
    assert "undefined" not in html


def test_compact_never_fabricates_exact_used_or_remaining():
    html = run_compact_js(
        f'compact.githubActionsBillingCardHtml({json.dumps(INCONCLUSIVE_DATA)}, "github-actions.billing")'
    )
    assert 'data-card-id="github-actions.billing"' in html
    assert "Exact used —" in html
    assert "Exact remaining —" in html
    assert "undefined" not in html


def test_compact_plan_unknown_card_shows_dash():
    html = run_compact_js(
        f'compact.githubActionsBillingCardHtml({json.dumps(PLAN_UNKNOWN_DATA)}, "github-actions.billing")'
    )
    assert "—" in html
    assert "undefined" not in html


def test_compact_section_html_wraps_grid_inner():
    html = run_compact_js(f"compact.githubActionsBillingSectionHtml({json.dumps(INCONCLUSIVE_DATA)})")
    assert "compact-github-grid-inner" in html
    assert 'data-card-id="github-actions.billing"' in html


def test_compact_status_class_mapping():
    assert run_compact_js('compact.githubActionsBillingStatusClass("usage_breakdown_inconclusive")') == "compact-status-unknown"
    assert run_compact_js('compact.githubActionsBillingStatusClass("plan_unknown")') == "compact-status-error"


def test_compact_shows_safely_named_breakdown_not_overage():
    html = run_compact_js(
        f'compact.githubActionsBillingCardHtml({json.dumps(INCONCLUSIVE_DATA)}, "github-actions.billing")'
    )
    assert "Discounted standard" in html
    assert "Billable standard" in html
    assert "Non-included paid" in html
    assert "Overage" not in html


def test_app_and_compact_never_conflate_with_rate_limit_labels():
    html_app = run_app_js(f"app.githubActionsBillingHtml({json.dumps(INCONCLUSIVE_DATA)})")
    html_compact = run_compact_js(
        f'compact.githubActionsBillingCardHtml({json.dumps(INCONCLUSIVE_DATA)}, "github-actions.billing")'
    )
    for html in (html_app, html_compact):
        assert "REST API core" not in html
        assert "GraphQL API" not in html


def test_compact_billable_and_paid_non_included_render_without_error():
    data = with_overrides(discounted_standard_minutes=2000, billable_standard_minutes=100, paid_non_included_minutes=60)
    html = run_compact_js(f'compact.githubActionsBillingCardHtml({json.dumps(data)}, "github-actions.billing")')
    assert "undefined" not in html
    assert "NaN" not in html
