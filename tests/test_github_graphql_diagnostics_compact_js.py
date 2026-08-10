"""Tests for the /compact GraphQL消費診断 card (static/compact.js):
  - githubGraphqlDiagnosticsCompactCardHtml renders a minimal card with a
    stable data-card-id (v0.1 explicitly excludes timeline/per-session
    detail/start-stop buttons from the compact view -- see the section.github
    CARD_META_BY_SECTION entry).
  - the new card is wired into the same partial-failure-isolation contract
    as every other compact card: a simulated fetch failure for ONLY the
    diagnostics endpoint must not affect any other card's rendering
    (github rate limit, actions billing, claude, codex, dashboard).

Modeled directly on tests/test_compact_partial_failure.py, which is read
first to match its exact fixture/assertion style.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPACT_JS = ROOT / "static" / "compact.js"


def run_compact_js(expression: str):
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


DASHBOARD_ROWS = [
    {
        "limit_id": 1,
        "service_name": "OpenAI",
        "provider": "OpenAI",
        "account_type": "api",
        "model_name": "gpt-test",
        "limit_type": "requests",
        "status": "normal",
        "used_value": 1,
        "max_value": 100,
        "unit": "requests",
        "usage_percent": 1.0,
        "next_reset_at": None,
        "last_updated_at": None,
        "source_type": "manual",
    }
]

GITHUB_OK = {"fetched": False, "resources": None, "overall": None, "last_known": None, "error": None}

GITHUB_ACTIONS_BILLING_OK = {
    "fetched": True,
    "status": "usage_breakdown_inconclusive",
    "plan_name": "pro",
    "included_minutes": 3000,
    "used_included_minutes": None,
    "remaining_minutes": None,
    "usage_percentage": None,
    "discounted_standard_minutes": 10,
    "billable_standard_minutes": 0,
    "paid_non_included_minutes": 0,
    "billing_year": 2026,
    "billing_month": 8,
    "source": "github_billing_api",
    "collected_at": "2026-08-08T12:00:00+00:00",
    "skipped_unknown_skus": [],
    "error": None,
}

GITHUB_GRAPHQL_DIAGNOSTICS_OK = {
    "enabled": True,
    "sampler_running": True,
    "sample_seconds": 10,
    "max_minutes": 15,
    "active_sessions": [
        {
            "id": 1,
            "actor_type": "claude_code",
            "label": "PR #25 review",
            "repository": "owner/repo",
            "pr_number": 25,
            "started_at": "2026-08-10T00:00:00+00:00",
            "ended_at": None,
            "github_login": "octocat",
            "github_user_id": 123,
            "reset_at_start": "2026-08-10T01:00:00+00:00",
            "graphql_used_start": 500,
            "graphql_used_end": None,
            "graphql_delta_total": None,
            "attribution_status": "SINGLE_ACTIVITY_CORRELATION",
            "status": "ACTIVE",
            "stop_reason": None,
        }
    ],
    "last_sample": None,
}

CLAUDE_AUTO_OK = {"available": False, "status": "not_observed"}
CLAUDE_MANUAL_OK = {"available": False, "status": "not_observed"}
CODEX_RATE_LIMITS_OK = {"available": False}
CODEX_USAGE_OK = {"available": False}


def _ok(data):
    return {"ok": True, "data": data}


def _fail():
    return {"ok": False}


def _all_ok_results(**overrides):
    results = {
        "dashboard": _ok(DASHBOARD_ROWS),
        "github": _ok(GITHUB_OK),
        "githubActionsBilling": _ok(GITHUB_ACTIONS_BILLING_OK),
        "githubGraphqlDiagnostics": _ok(GITHUB_GRAPHQL_DIAGNOSTICS_OK),
        "claudeAuto": _ok(CLAUDE_AUTO_OK),
        "claudeManual": _ok(CLAUDE_MANUAL_OK),
        "codexRateLimits": _ok(CODEX_RATE_LIMITS_OK),
        "codexUsage": _ok(CODEX_USAGE_OK),
    }
    results.update(overrides)
    return results


def _plan(results):
    return run_compact_js(f"compact.buildCompactRenderPlan({json.dumps(results)})")


# ---------------------------------------------------------------------------
# CARD_META_BY_SECTION: card registered under the existing section.github group
# ---------------------------------------------------------------------------


def test_card_registered_in_existing_github_section_not_a_new_section():
    meta = run_compact_js("compact.CARD_META_BY_SECTION")
    github_ids = [c["id"] for c in meta["section.github"]]
    assert "github.graphql-diagnostics" in github_ids
    assert "section.github-graphql-diagnostics" not in meta
    section_meta = run_compact_js("compact.SECTION_META")
    section_ids = [s["id"] for s in section_meta]
    assert "section.github" in section_ids
    assert "section.github-graphql-diagnostics" not in section_ids
    assert len(section_ids) == 5  # 既存5セクションのまま(新セクションを増やしていない)


# ---------------------------------------------------------------------------
# githubGraphqlDiagnosticsCompactCardHtml: minimal card, stable data-card-id
# ---------------------------------------------------------------------------


def test_card_html_has_stable_card_id():
    html = run_compact_js(
        f'compact.githubGraphqlDiagnosticsCompactCardHtml({json.dumps(GITHUB_GRAPHQL_DIAGNOSTICS_OK)}, "github.graphql-diagnostics")'
    )
    assert 'data-card-id="github.graphql-diagnostics"' in html


def test_card_html_shows_enabled_and_active_count():
    html = run_compact_js(
        f'compact.githubGraphqlDiagnosticsCompactCardHtml({json.dumps(GITHUB_GRAPHQL_DIAGNOSTICS_OK)}, "github.graphql-diagnostics")'
    )
    assert "有効" in html
    assert "計測中" in html
    assert "1" in html


def test_card_html_shows_disabled_without_active_count():
    data = {**GITHUB_GRAPHQL_DIAGNOSTICS_OK, "enabled": False, "active_sessions": []}
    html = run_compact_js(f'compact.githubGraphqlDiagnosticsCompactCardHtml({json.dumps(data)}, "github.graphql-diagnostics")')
    assert "無効" in html
    assert "計測中" not in html


def test_card_html_zero_active_sessions_omits_active_count_line():
    data = {**GITHUB_GRAPHQL_DIAGNOSTICS_OK, "active_sessions": []}
    html = run_compact_js(f'compact.githubGraphqlDiagnosticsCompactCardHtml({json.dumps(data)}, "github.graphql-diagnostics")')
    assert "計測中" not in html


def test_card_html_null_data_does_not_crash():
    html = run_compact_js('compact.githubGraphqlDiagnosticsCompactCardHtml(null, "github.graphql-diagnostics")')
    assert 'data-card-id="github.graphql-diagnostics"' in html
    assert "undefined" not in html
    assert "NaN" not in html


def test_card_html_v01_excludes_timeline_and_start_stop_buttons():
    html = run_compact_js(
        f'compact.githubGraphqlDiagnosticsCompactCardHtml({json.dumps(GITHUB_GRAPHQL_DIAGNOSTICS_OK)}, "github.graphql-diagnostics")'
    )
    assert "<button" not in html
    assert "<form" not in html
    # per-sessionの詳細(label/repository等)はv0.1のcompactカードには出さない
    assert "PR #25 review" not in html
    assert "owner/repo" not in html


# ---------------------------------------------------------------------------
# Critical regression test: partial-failure isolation
# ---------------------------------------------------------------------------


def test_diagnostics_only_failure_does_not_affect_other_cards():
    plan = _plan(_all_ok_results(githubGraphqlDiagnostics=_fail()))

    # githubCards(section.githubのgrid全体)は他の3カード(core/graphql/search状態)を
    # 引き続き含み、diagnostics providerの固定失敗メッセージだけが追加で現れる。
    assert "GraphQL消費診断の取得に失敗しました" in plan["githubCards"]
    # 他のproviderのcardには一切漏れない
    assert "GraphQL消費診断の取得に失敗しました" not in plan["githubActionsCards"]
    assert "GraphQL消費診断の取得に失敗しました" not in plan["claudeCodeUsageCards"]
    assert "GraphQL消費診断の取得に失敗しました" not in plan["codexUsageCards"]
    assert "GraphQL消費診断の取得に失敗しました" not in plan["limitCards"]
    # dashboard/claude/codex/actions billingは正常時と同じく失敗表示にならない
    assert "取得に失敗しました" not in plan["githubActionsCards"]
    assert "取得に失敗しました" not in plan["claudeCodeUsageCards"]
    assert "取得に失敗しました" not in plan["codexUsageCards"]
    assert "取得に失敗しました" not in plan["limitCards"]


def test_diagnostics_only_failure_other_github_resource_cards_still_render():
    ok_with_fetched_github = _all_ok_results(
        githubGraphqlDiagnostics=_fail(),
        github=_ok(
            {
                "fetched": True,
                "refreshing": False,
                "overall": {"status": "Normal", "reason": "within limits"},
                "resources": {
                    "core": {
                        "resource": "core",
                        "status": "Normal",
                        "limit": 5000,
                        "used": 100,
                        "remaining": 4900,
                        "usage_percent": 2.0,
                        "remaining_percent": 98.0,
                        "reset_at_utc": "2999-01-01T00:00:00+00:00",
                        "reset_at_local": "2999-01-01T09:00:00+09:00",
                        "seconds_until_reset": 3600,
                        "error_message": None,
                    },
                    "graphql": {
                        "resource": "graphql",
                        "status": "Normal",
                        "limit": 5000,
                        "used": 100,
                        "remaining": 4900,
                        "usage_percent": 2.0,
                        "remaining_percent": 98.0,
                        "reset_at_utc": "2999-01-01T00:00:00+00:00",
                        "reset_at_local": "2999-01-01T09:00:00+09:00",
                        "seconds_until_reset": 3600,
                        "error_message": None,
                    },
                    "search": None,
                },
                "auto_refresh_pending": False,
                "next_auto_refresh_at": None,
                "last_auto_refresh_error": None,
            }
        ),
    )
    plan = _plan(ok_with_fetched_github)
    assert 'data-card-id="github.core"' in plan["githubCards"]
    assert 'data-card-id="github.graphql"' in plan["githubCards"]
    assert "GraphQL消費診断の取得に失敗しました" in plan["githubCards"]


def test_all_other_providers_unaffected_by_diagnostics_failure_full_sweep():
    # test_compact_partial_failure.pyのtest_all_success_renders_normally_without_error_messages
    # と同じ形式: diagnostics以外が全て成功しているとき、diagnostics失敗の固定文言が
    # 他providerのkeyへ一切現れないことを網羅的に確認する。
    plan = _plan(_all_ok_results(githubGraphqlDiagnostics=_fail()))
    for key in ("githubActionsCards", "claudeCodeUsageCards", "codexUsageCards", "limitCards"):
        assert "取得に失敗しました" not in plan[key], f"{key} unexpectedly shows a failure message"


def test_missing_diagnostics_key_backward_compatible_not_treated_as_failure():
    # 呼び出し元がまだこのproviderを知らない(=resultsにkey自体が無い)場合は、
    # "取得失敗"扱いにはしない(fetchJsonSafeの{ok:false}という明示的な失敗と、
    # そもそも呼ばれていない状態を区別する)。
    results = _all_ok_results()
    del results["githubGraphqlDiagnostics"]
    plan = _plan(results)
    assert "GraphQL消費診断の取得に失敗しました" not in plan["githubCards"]


def test_all_success_including_diagnostics_renders_without_error_messages():
    plan = _plan(_all_ok_results())
    for key, html in plan.items():
        assert "取得に失敗しました" not in html, f"{key} unexpectedly shows a failure message"


def test_diagnostics_failure_carries_no_body_so_nothing_can_leak():
    plan = _plan(_all_ok_results(githubGraphqlDiagnostics=_fail()))
    html = plan["githubCards"]
    assert '{"detail"' not in html
    assert "Not Found" not in html
    assert "SECRET_MARKER" not in html
    assert "Traceback" not in html
    assert "<html" not in html.lower()
    assert "undefined" not in html
    assert "NaN" not in html


# ---------------------------------------------------------------------------
# syntax check
# ---------------------------------------------------------------------------


def test_compact_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(COMPACT_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
