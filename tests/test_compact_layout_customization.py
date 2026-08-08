"""Tests for the /compact dashboard layout customization: section/card
drag-and-drop reordering, per-card visibility, localStorage persistence, and
safe recovery from corrupted saved data.

Scope: presentation only (static/compact.js + static/compact.css +
static/compact.html). No fetch logic, domain judgment, stale rules,
app-schedule/reset-schedule separation, or API shape changes.

Everything that can be tested without a real browser DOM (the
data-transform/validation core: default state, sanitization, JSON
load/save round-trip, index-move arithmetic, and — critically — that each
card's stable `data-card-id` is baked directly into its HTML at generation
time rather than inferred from DOM position) is tested directly via Node.

DOM-only behavior (actual drag-and-drop, edit-mode toggling and re-toggling,
`applyFullLayout()` idempotency across repeated calls on already-wrapped
cards, real element reordering) is out of reach for the Node `require()`
harness used elsewhere in this repo (no jsdom dependency was added — that
would be a new dependency requiring explicit approval first) and is instead
verified via a fixture-based browser preview, per the task instructions.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPACT_JS = ROOT / "static" / "compact.js"
COMPACT_CSS = ROOT / "static" / "compact.css"
COMPACT_HTML = ROOT / "static" / "compact.html"


def run_compact_js(expression: str):
    script = f"""
const compact = require({json.dumps(str(COMPACT_JS))});
const result = ({expression});
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(proc.stdout)


# --- 初期状態は全表示 -------------------------------------------------------------


def test_default_layout_state_shows_everything():
    state = run_compact_js("compact.defaultLayoutState()")
    assert state["hiddenCardIds"] == []
    assert state["version"] == 1
    assert state["sectionOrder"] == run_compact_js("compact.DEFAULT_SECTION_ORDER")


def test_default_section_order_includes_all_known_sections():
    order = run_compact_js("compact.DEFAULT_SECTION_ORDER")
    assert order == [
        "section.dashboard",
        "section.github",
        "section.github-actions",
        "section.claude",
        "section.codex",
    ]


def test_default_card_order_matches_documented_stable_ids():
    meta = run_compact_js("compact.CARD_META_BY_SECTION")
    assert [c["id"] for c in meta["section.github"]] == ["github.core", "github.graphql", "github.search"]
    assert [c["id"] for c in meta["section.github-actions"]] == ["github-actions.billing"]
    assert [c["id"] for c in meta["section.claude"]] == ["claude.five_hour", "claude.seven_day"]
    assert [c["id"] for c in meta["section.codex"]] == ["codex.five_hour", "codex.weekly"]


# --- moveIdToIndex: 上へ/下へボタンとドラッグは同じ関数を経由する ------------------


def test_move_id_to_index_swaps_for_adjacent_move():
    # 上へ/下へボタンは currentIndex ± 1 を渡すだけなので、隣接swapと同じ結果になる。
    assert run_compact_js('compact.moveIdToIndex(["a","b","c"], "b", 0)') == ["b", "a", "c"]
    assert run_compact_js('compact.moveIdToIndex(["a","b","c"], "b", 2)') == ["a", "c", "b"]


def test_move_id_to_index_matches_drag_drop_target_index():
    # ドラッグ&ドロップは「ドロップ先要素の現在index」をtargetIndexとして渡す。
    # 同じ入力・同じtargetIndexならボタン操作と完全に同じ並びになる。
    order = ["a", "b", "c", "d"]
    button_result = run_compact_js(f'compact.moveIdToIndex({json.dumps(order)}, "a", 2)')
    drag_result = run_compact_js(f'compact.moveIdToIndex({json.dumps(order)}, "a", 2)')
    assert button_result == drag_result == ["b", "c", "a", "d"]


def test_move_id_to_index_out_of_range_is_clamped():
    assert run_compact_js('compact.moveIdToIndex(["a","b","c"], "a", 999)') == ["b", "c", "a"]
    assert run_compact_js('compact.moveIdToIndex(["a","b","c"], "c", -50)') == ["c", "a", "b"]


def test_move_id_to_index_unknown_id_is_noop():
    assert run_compact_js('compact.moveIdToIndex(["a","b","c"], "z", 0)') == ["a", "b", "c"]


# --- Providerセクション順変更 / Provider内カード順変更 -----------------------------


def test_sanitize_accepts_reordered_section_order():
    raw = {
        "version": 1,
        "sectionOrder": [
            "section.codex",
            "section.claude",
            "section.github-actions",
            "section.github",
            "section.dashboard",
        ],
        "cardOrderBySection": {},
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["sectionOrder"] == [
        "section.codex",
        "section.claude",
        "section.github-actions",
        "section.github",
        "section.dashboard",
    ]


def test_sanitize_accepts_reordered_card_order_within_section():
    raw = {
        "version": 1,
        "sectionOrder": [],
        "cardOrderBySection": {"section.github": ["github.search", "github.core", "github.graphql"]},
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["cardOrderBySection"]["section.github"] == ["github.search", "github.core", "github.graphql"]
    # 他セクションは既定順のまま(このセクションだけの変更が他へ波及しない)
    assert state["cardOrderBySection"]["section.claude"] == ["claude.five_hour", "claude.seven_day"]


# --- Providerをまたぐカード移動は不可(データレベルでの防止) -----------------------


def test_sanitize_rejects_cross_provider_card_id_in_wrong_section():
    raw = {
        "version": 1,
        "sectionOrder": [],
        "cardOrderBySection": {
            "section.github": ["claude.five_hour", "github.core", "github.graphql", "github.search"],
        },
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    # claude.five_hourはgithubセクションのdefaultOrderに存在しないため無視される
    assert "claude.five_hour" not in state["cardOrderBySection"]["section.github"]
    assert state["cardOrderBySection"]["section.github"] == ["github.core", "github.graphql", "github.search"]
    # claudeセクション側にも紛れ込まない(そもそも保存対象外)
    assert state["cardOrderBySection"]["section.claude"] == ["claude.five_hour", "claude.seven_day"]


def test_sanitize_rejects_cross_provider_id_even_if_it_is_a_known_card_elsewhere():
    # codex.weeklyという「実在する」IDでも、claudeセクションのdefaultOrderには含まれないため無視される。
    raw = {
        "version": 1,
        "sectionOrder": [],
        "cardOrderBySection": {"section.claude": ["codex.weekly", "claude.five_hour"]},
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["cardOrderBySection"]["section.claude"] == ["claude.five_hour", "claude.seven_day"]


# --- 再読み込み後の復元(保存→読み込みのラウンドトリップ) --------------------------


def test_serialize_and_load_round_trip_preserves_custom_layout():
    custom = {
        "version": 1,
        "sectionOrder": [
            "section.codex",
            "section.github",
            "section.github-actions",
            "section.claude",
            "section.dashboard",
        ],
        "cardOrderBySection": {
            "section.github": ["github.graphql", "github.core", "github.search"],
            "section.github-actions": ["github-actions.billing"],
            "section.claude": ["claude.seven_day", "claude.five_hour"],
            "section.codex": ["codex.weekly", "codex.five_hour"],
        },
        "hiddenCardIds": ["github.search"],
    }
    serialized = run_compact_js(f"compact.serializeLayoutState({json.dumps(custom)})")
    restored = run_compact_js(f"compact.loadLayoutStateFromRaw({json.dumps(serialized)})")
    assert restored == custom


# --- カード非表示 / 非表示カードの再表示 -------------------------------------------


def test_sanitize_preserves_hidden_card_ids():
    raw = {"version": 1, "sectionOrder": [], "cardOrderBySection": {}, "hiddenCardIds": ["github.search", "claude.seven_day"]}
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert sorted(state["hiddenCardIds"]) == ["claude.seven_day", "github.search"]


def test_hidden_card_ids_round_trip_through_serialize_and_load():
    custom = {**run_compact_js("compact.defaultLayoutState()"), "hiddenCardIds": ["codex.five_hour"]}
    serialized = run_compact_js(f"compact.serializeLayoutState({json.dumps(custom)})")
    restored = run_compact_js(f"compact.loadLayoutStateFromRaw({json.dumps(serialized)})")
    assert restored["hiddenCardIds"] == ["codex.five_hour"]


def test_all_cards_in_a_section_can_be_hidden():
    all_github_ids = [c["id"] for c in run_compact_js("compact.CARD_META_BY_SECTION")["section.github"]]
    raw = {"version": 1, "sectionOrder": [], "cardOrderBySection": {}, "hiddenCardIds": all_github_ids}
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert sorted(state["hiddenCardIds"]) == sorted(all_github_ids)
    # 非表示にしても、カードのstable ID自体(=編集モードで再表示するための一覧)は
    # CARD_META_BY_SECTIONに残り続ける(sanitizeで削除されるのはhiddenCardIdsだけ)
    assert [c["id"] for c in run_compact_js("compact.CARD_META_BY_SECTION")["section.github"]] == all_github_ids


# --- 初期配置へ戻す ----------------------------------------------------------------


def test_default_layout_state_has_no_hidden_cards_and_default_orders():
    state = run_compact_js("compact.defaultLayoutState()")
    assert state["hiddenCardIds"] == []
    for section_id, cards in run_compact_js("compact.CARD_META_BY_SECTION").items():
        assert state["cardOrderBySection"][section_id] == [c["id"] for c in cards]


# --- 不正な保存値からの安全な復旧 ---------------------------------------------------


def test_broken_json_string_falls_back_to_default():
    default = run_compact_js("compact.defaultLayoutState()")
    assert run_compact_js('compact.loadLayoutStateFromRaw("{not valid json")') == default
    assert run_compact_js("compact.loadLayoutStateFromRaw(null)") == default
    assert run_compact_js('compact.loadLayoutStateFromRaw("")') == default
    assert run_compact_js('compact.loadLayoutStateFromRaw("[]")') == default
    assert run_compact_js('compact.loadLayoutStateFromRaw("42")') == default
    assert run_compact_js('compact.loadLayoutStateFromRaw("\\"just a string\\"")') == default


def test_version_mismatch_falls_back_to_default():
    default = run_compact_js("compact.defaultLayoutState()")
    for bad_version in (0, 2, 999, "1", None):
        raw = {"version": bad_version, "sectionOrder": ["section.codex"], "cardOrderBySection": {}, "hiddenCardIds": []}
        assert run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})") == default


def test_type_invalid_top_level_falls_back_to_default():
    default = run_compact_js("compact.defaultLayoutState()")
    for bad in ("a string", 42, True, None):
        assert run_compact_js(f"compact.sanitizeLayoutState({json.dumps(bad)})") == default
    assert run_compact_js("compact.sanitizeLayoutState([1,2,3])") == default


def test_unknown_ids_are_ignored():
    raw = {
        "version": 1,
        "sectionOrder": ["section.made.up", "section.github"],
        "cardOrderBySection": {"section.claude": ["claude.made.up", "claude.five_hour"]},
        "hiddenCardIds": ["totally.unknown"],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert "section.made.up" not in state["sectionOrder"]
    assert "claude.made.up" not in state["cardOrderBySection"]["section.claude"]
    assert state["hiddenCardIds"] == []


def test_duplicate_ids_are_deduplicated():
    raw = {
        "version": 1,
        "sectionOrder": ["section.github", "section.github", "section.github"],
        "cardOrderBySection": {"section.codex": ["codex.weekly", "codex.weekly"]},
        "hiddenCardIds": ["github.core", "github.core"],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["sectionOrder"].count("section.github") == 1
    assert state["cardOrderBySection"]["section.codex"].count("codex.weekly") == 1
    assert state["hiddenCardIds"] == ["github.core"]


def test_missing_ids_are_appended_at_end_in_default_order():
    raw = {
        "version": 1,
        "sectionOrder": ["section.codex"],
        "cardOrderBySection": {"section.github": ["github.search"]},
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["sectionOrder"] == [
        "section.codex",
        "section.dashboard",
        "section.github",
        "section.github-actions",
        "section.claude",
    ]
    assert state["cardOrderBySection"]["section.github"] == ["github.search", "github.core", "github.graphql"]


def test_non_array_section_order_falls_back_to_default_order():
    raw = {"version": 1, "sectionOrder": "not-an-array", "cardOrderBySection": {}, "hiddenCardIds": []}
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["sectionOrder"] == run_compact_js("compact.DEFAULT_SECTION_ORDER")


def test_non_array_hidden_card_ids_falls_back_to_empty():
    raw = {"version": 1, "sectionOrder": [], "cardOrderBySection": {}, "hiddenCardIds": "not-an-array"}
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["hiddenCardIds"] == []


def test_non_string_and_non_object_entries_are_filtered_out():
    raw = {
        "version": 1,
        "sectionOrder": [123, None, True, {"nested": "object"}, "section.github"],
        "cardOrderBySection": {"section.claude": [123, None, "claude.five_hour"]},
        "hiddenCardIds": [123, None, {"a": 1}, "github.core"],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(raw)})")
    assert state["sectionOrder"][0] == "section.github"
    assert state["cardOrderBySection"]["section.claude"][0] == "claude.five_hour"
    assert state["hiddenCardIds"] == ["github.core"]


# --- 新カード追加時の補完(将来カードを想定したシミュレーション) -------------------


def test_new_card_added_in_future_is_automatically_visible():
    # 「将来カードが追加された」状況を、旧バージョンの保存データ(新カードを知らない)として再現する。
    # 新カードのIDは保存データに存在しないため、hiddenCardIdsには入りようがなく自動的に表示対象になる。
    old_save = {
        "version": 1,
        "sectionOrder": run_compact_js("compact.DEFAULT_SECTION_ORDER"),
        "cardOrderBySection": {"section.codex": ["codex.five_hour"]},  # codex.weeklyを知らない古い保存データ
        "hiddenCardIds": [],
    }
    state = run_compact_js(f"compact.sanitizeLayoutState({json.dumps(old_save)})")
    assert "codex.weekly" in state["cardOrderBySection"]["section.codex"]
    assert "codex.weekly" not in state["hiddenCardIds"]


# --- localStorageに保存しない情報の確認(usage値・reset時刻・account情報を含めない) -


def test_serialized_layout_never_contains_usage_or_account_looking_keys():
    custom = run_compact_js("compact.defaultLayoutState()")
    serialized = run_compact_js(f"compact.serializeLayoutState({json.dumps(custom)})")
    for forbidden in ("used_", "remaining_", "resets_at", "reset_at", "account", "token", "organization"):
        assert forbidden not in serialized


# --- RATE LIMITED等の既存バナー・stale/overdue/emptyのレンダリングは無変更 --------


def test_existing_banner_rendering_unaffected_by_layout_module():
    overall = {"status": "Limited", "reason": "REST API core exhausted"}
    resources = {
        "core": {
            "resource": "core",
            "status": "Exhausted",
            "limit": 5000,
            "used": 100,
            "remaining": 0,
            "usage_percent": 100.0,
            "remaining_percent": 0.0,
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
    }
    html = run_compact_js(f"compact.githubLimitedBannerHtml({json.dumps(overall)}, {json.dumps(resources)}, false)")
    assert "RATE LIMITED" in html
    assert "compact-banner-limited" in html


def test_existing_empty_and_stale_rendering_unaffected_by_layout_module():
    empty_html = run_compact_js("compact.githubSectionHtml({fetched: false, last_known: null})")
    assert "未取得" in empty_html
    assert "compact-provider-github" in empty_html


# --- CSSレベル: 640px/1280x720半画面での崩れ防止・overflow対策 -------------------


def test_css_defines_layout_customization_classes():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for cls in (
        ".compact-edit-bar",
        ".compact-sr-only",
        ".compact-section-edit-bar",
        ".compact-card-editable",
        ".compact-card-edit-controls",
        ".compact-drag-handle",
        ".compact-move-btn",
        ".compact-visibility-toggle",
        ".compact-dragging",
        ".compact-drag-over",
    ):
        assert f"{cls} {{" in css


def test_css_edit_bar_and_section_edit_bar_wrap_to_avoid_overflow():
    css = COMPACT_CSS.read_text(encoding="utf-8")
    for selector in (".compact-edit-bar {", ".compact-section-edit-bar {", ".compact-card-edit-controls {"):
        start = css.index(selector)
        end = css.index("}", start)
        block = css[start:end]
        assert "flex-wrap: wrap" in block


# --- HTMLレベル: stable IDのdata属性、編集トグル、reset、aria-live -----------------


def test_html_declares_section_stable_ids():
    html = COMPACT_HTML.read_text(encoding="utf-8")
    for section_id in run_compact_js("compact.DEFAULT_SECTION_ORDER"):
        assert f'data-section-id="{section_id}"' in html


def test_html_has_layout_edit_toggle_and_reset_button_and_live_region():
    html = COMPACT_HTML.read_text(encoding="utf-8")
    assert 'id="layoutEditToggle"' in html
    assert 'id="layoutResetButton"' in html
    assert 'id="layoutAnnounce"' in html
    assert 'aria-live="polite"' in html
    # 標準button要素であること(キーボード操作可能・新規依存不要)
    assert '<button id="layoutEditToggle"' in html
    assert '<button id="layoutResetButton"' in html


def test_html_still_has_no_form_elements():
    html = COMPACT_HTML.read_text(encoding="utf-8")
    assert "<form" not in html


# --- カード編集操作行のaria-label(操作対象が分かること) ---------------------------


def test_card_edit_controls_html_has_descriptive_aria_labels():
    html = run_compact_js('compact.buildCardEditControlsHtml("github.core", "GitHub REST API", false)')
    assert "GitHub REST APIをドラッグして並べ替え" in html
    assert "GitHub REST APIを上へ移動" in html
    assert "GitHub REST APIを下へ移動" in html
    assert "GitHub REST APIを表示" in html
    assert "<button" in html
    assert 'type="checkbox"' in html
    assert "checked" in html  # 非hidden時はchecked


def test_card_edit_controls_html_reflects_hidden_state():
    html = run_compact_js('compact.buildCardEditControlsHtml("github.core", "GitHub REST API", true)')
    assert "checked" not in html


def test_section_edit_bar_html_has_descriptive_aria_labels():
    html = run_compact_js('compact.buildSectionEditBarHtml("section.github", "GitHub API Rate Limit")')
    assert "GitHub API Rate Limitセクションをドラッグして並べ替え" in html
    assert "GitHub API Rate Limitセクションを上へ移動" in html
    assert "GitHub API Rate Limitセクションを下へ移動" in html


# --- 修正必須2: カードIDはHTML生成時に直接埋め込まれる(DOM位置からの推測ではない) -


def _resource(resource_name, status="Normal"):
    return {
        "resource": resource_name,
        "status": status,
        "limit": 5000,
        "used": 100,
        "remaining": 4900,
        "usage_percent": 2.0,
        "remaining_percent": 98.0,
        "reset_at_utc": "2999-01-01T00:00:00+00:00",
        "reset_at_local": "2999-01-01T09:00:00+09:00",
        "seconds_until_reset": 3600,
        "error_message": None,
    }


def test_github_resource_card_html_embeds_stable_card_id_matching_resource_field():
    for resource_name, expected_id in (("core", "github.core"), ("graphql", "github.graphql"), ("search", "github.search")):
        html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(_resource(resource_name))})")
        assert f'data-card-id="{expected_id}"' in html


def test_github_resource_card_html_embeds_card_id_even_in_error_status():
    html = run_compact_js(f"compact.githubResourceCardHtml({json.dumps(_resource('graphql', 'Error'))})")
    assert 'data-card-id="github.graphql"' in html


def test_claude_window_html_embeds_explicit_card_id_when_provided():
    win = {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"}
    html = run_compact_js(
        f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(win)}, false, "claude.five_hour")'
    )
    assert 'data-card-id="claude.five_hour"' in html


def test_claude_window_html_embeds_card_id_on_unobserved_empty_state_too():
    html = run_compact_js('compact.claudeUsageWindowHtml("Claude 5時間枠", null, false, "claude.five_hour")')
    assert 'data-card-id="claude.five_hour"' in html


def test_claude_window_html_omits_card_id_attribute_when_not_provided():
    # 既存呼び出し(cardId省略)との後方互換: data-card-id自体を出力しない。
    win = {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"}
    html = run_compact_js(f'compact.claudeUsageWindowHtml("Claude 5時間枠", {json.dumps(win)})')
    assert "data-card-id" not in html


def test_codex_window_html_embeds_explicit_card_id_when_provided():
    win = {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"}
    html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(win)}, "自動取得", false, "codex.five_hour")'
    )
    assert 'data-card-id="codex.five_hour"' in html


def test_codex_window_html_embeds_card_id_on_unentered_empty_state_too():
    html = run_compact_js('compact.codexUsageWindowHtml("Codex 5時間枠", null, "自動取得", false, "codex.five_hour")')
    assert 'data-card-id="codex.five_hour"' in html


def test_codex_window_html_embeds_card_id_on_reset_overdue_state_too():
    overdue_win = {"used_percentage": 90.0, "remaining_percentage": 10.0, "resets_at": "2000-01-01T00:00:00+00:00"}
    html = run_compact_js(
        f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(overdue_win)}, "自動取得", false, "codex.weekly")'
    )
    assert 'data-card-id="codex.weekly"' in html


def test_codex_window_html_omits_card_id_attribute_when_not_provided():
    win = {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"}
    html = run_compact_js(f'compact.codexUsageWindowHtml("Codex 5時間枠", {json.dumps(win)})')
    assert "data-card-id" not in html


# セクション全体のHTML(githubSectionHtml/claudeCodeSectionHtml/codexUsageSectionHtml)でも
# 実際に呼び出されるcardIdが正しく渡っていることを統合的に確認する。
def test_github_section_html_cards_all_carry_correct_stable_ids():
    data = {
        "fetched": True,
        "refreshing": False,
        "overall": {"status": "Normal", "reason": "core and graphql are within normal limits"},
        "resources": {"core": _resource("core"), "graphql": _resource("graphql"), "search": _resource("search")},
        "auto_refresh_pending": False,
        "next_auto_refresh_at": None,
        "last_auto_refresh_error": None,
    }
    html = run_compact_js(f"compact.githubSectionHtml({json.dumps(data)})")
    assert 'data-card-id="github.core"' in html
    assert 'data-card-id="github.graphql"' in html
    assert 'data-card-id="github.search"' in html


def test_claude_code_section_html_windows_carry_correct_stable_ids():
    data = {
        "available": True,
        "stale": False,
        "observed_at": "2026-08-02T00:00:00+00:00",
        "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"},
        "seven_day": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2999-01-08T00:00:00+00:00"},
    }
    html = run_compact_js(f"compact.claudeCodeSectionHtml({json.dumps(data)})")
    assert 'data-card-id="claude.five_hour"' in html
    assert 'data-card-id="claude.seven_day"' in html


def test_codex_usage_section_html_windows_carry_correct_stable_ids():
    auto = {
        "available": True,
        "stale": False,
        "observed_at": "2026-08-02T00:00:00+00:00",
        "five_hour": {"used_percentage": 40.0, "remaining_percentage": 60.0, "resets_at": "2999-01-01T00:00:00+00:00"},
        "weekly": {"used_percentage": 20.0, "remaining_percentage": 80.0, "resets_at": "2999-01-08T00:00:00+00:00"},
    }
    html = run_compact_js(f"compact.codexUsageSectionHtml({json.dumps(auto)}, null)")
    assert 'data-card-id="codex.five_hour"' in html
    assert 'data-card-id="codex.weekly"' in html


# --- syntax check ------------------------------------------------------------------


def test_compact_js_passes_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(COMPACT_JS)], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
