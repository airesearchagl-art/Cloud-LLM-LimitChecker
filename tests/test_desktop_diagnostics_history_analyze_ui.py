"""Desktop UX Phase 2: Diagnostics / History / Analyze のフロントエンド契約テスト。

このリポジトリの既存フロントエンドテストと同じ方針で書いている。

- 静的ファイル(static/index.html, static/app.js, static/styles.css)のソーステキストに
  対する検査
- `node --check` による構文検査
- `node` から static/app.js を require し、export された純粋関数を実際に実行して
  戻り値を検査する(ブラウザを起動しない)
- DOMが要る挙動は、最小限の偽DOMをグローバルへ注入してから関数を呼ぶ
  (require時点では `document` が未定義なので initApp() は走らない)

このPhaseで最も重要なのは「Analysis Packに出してはいけない値が本当に出ないこと」なので、
禁止語だけでなく合成payloadへ埋めたsentinel VALUEでも検査している。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"

APP_JS_TEXT = APP_JS.read_text(encoding="utf-8")
INDEX_HTML_TEXT = INDEX_HTML.read_text(encoding="utf-8")
STYLES_CSS_TEXT = STYLES_CSS.read_text(encoding="utf-8")

APP_JS_REQUIRE_PATH = APP_JS.as_posix()

# Analysis Packへ絶対に出してはいけない値。禁止「語」ではなく、合成payloadへ実際に
# 埋め込む sentinel VALUE として使う(キー名の検査だけでは、値が別のキーから漏れる
# ケースを捕まえられないため)。
SENTINELS = {
    "label": "LABEL_SENTINEL",
    "repository": "PRIVATE_REPOSITORY_SENTINEL",
    "pr_number": "424242",
    "github_login": "ACCOUNT_LOGIN_SENTINEL",
    "error_message": "SECRET_ERROR_SENTINEL",
    "limit_id": "LIMIT_ID_SENTINEL",
    "note": "NOTE_SENTINEL",
    "path": "C:/Users/PATH_SENTINEL/AppData/Local/app.json",
    "source": "SOURCE_PATH_SENTINEL",
    "github_user_id": "987650321",
    "token": "TOKEN_VALUE_SENTINEL",
    "credential": "CREDENTIAL_SENTINEL",
    "raw_exception": "RAW_EXCEPTION_SENTINEL",
    "raw_payload": "RAW_PAYLOAD_SENTINEL",
}


def _run_node(tmp_path: Path, body: str) -> str:
    script = f'const app = require("{APP_JS_REQUIRE_PATH}");\n{body}\n'
    script_path = tmp_path / "probe.js"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def node_json(tmp_path: Path, body: str):
    return json.loads(_run_node(tmp_path, body))


_LINE_COMMENT = re.compile(r"//.*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_js_comments(source: str) -> str:
    """コメントを取り除いたコード部分だけを返す。

    ここでの検査は「実装が何をしているか」を見るためのもので、「何をしないと
    書いてあるか」を見るためのものではない。たとえばAnalysis Packの実装コメントには
    『localStorage・DB・DOM属性のいずれにも保存しない』と書いてあり、既存のapp.jsには
    『「Xが消費しました」とは言わない』という注意書きがある。コメントを含めたまま
    単語検索すると、正しい実装ほど誤検出する。
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def source_region(start_marker: str, end_marker: str) -> str:
    start = APP_JS_TEXT.index(start_marker)
    end = APP_JS_TEXT.index(end_marker, start)
    return APP_JS_TEXT[start:end]


# セクション見出しのコメントは行末で終わるので、改行まで含めて一意にする
# (同じ語を含む行内コメントが別の場所にもあるため)。
ANALYSIS_PACK_SOURCE = source_region("// 分析用データ(Analysis Pack)\n", "function applyFiltersAndSort")
DIAGNOSTICS_SUMMARY_SOURCE = source_region(
    "// 診断サマリー(Diagnostics Summary)\n", "// 分析用データ(Analysis Pack)\n"
)
HISTORY_SOURCE = source_region("const HISTORY_PAGE_SIZE = 30;", "function renderCollectorRuns")

ANALYSIS_PACK_CODE = strip_js_comments(ANALYSIS_PACK_SOURCE)
DIAGNOSTICS_SUMMARY_CODE = strip_js_comments(DIAGNOSTICS_SUMMARY_SOURCE)
HISTORY_CODE = strip_js_comments(HISTORY_SOURCE)
APP_JS_CODE = strip_js_comments(APP_JS_TEXT)

# 全テストで使い回す合成データ。禁止値をすべて含んでいる。
SOURCES_JS = """
const sessions = [
  {
    id: 7,
    actor_type: "claude_code",
    label: "LABEL_SENTINEL",
    repository: "PRIVATE_REPOSITORY_SENTINEL",
    pr_number: 424242,
    started_at: "2026-09-13T00:00:00+00:00",
    ended_at: "2026-09-13T00:20:00+00:00",
    github_login: "ACCOUNT_LOGIN_SENTINEL",
    github_user_id: 987650321,
    reset_at_start: "2026-09-13T01:00:00+00:00",
    graphql_used_start: 10,
    graphql_used_end: 40,
    graphql_delta_total: 30,
    attribution_status: "SINGLE_ACTIVITY_CORRELATION",
    status: "STOPPED",
    stop_reason: "USER_STOP",
    max_valid_interval_delta: 12,
    // allowlist外の値。どれか1つでも出力されればsentinel検査が落ちる。
    note: "NOTE_SENTINEL",
    token: "TOKEN_VALUE_SENTINEL",
    raw_payload: { body: "RAW_PAYLOAD_SENTINEL" }
  },
  {
    id: 9,
    actor_type: "codex",
    label: "LABEL_SENTINEL",
    repository: "PRIVATE_REPOSITORY_SENTINEL",
    pr_number: 424242,
    started_at: "2026-09-13T00:05:00+00:00",
    ended_at: null,
    github_login: "ACCOUNT_LOGIN_SENTINEL",
    github_user_id: 987650321,
    graphql_used_start: 20,
    graphql_used_end: null,
    graphql_delta_total: null,
    attribution_status: "OVERLAPPING_ACTIVITIES",
    status: "ACTIVE",
    stop_reason: null,
    max_valid_interval_delta: null
  }
];

const samples = [
  {
    id: 1,
    collected_at: "2026-09-13T00:10:00+00:00",
    graphql_used: 30,
    graphql_limit: 5000,
    graphql_remaining: 4970,
    graphql_reset_at: "2026-09-13T01:00:00+00:00",
    graphql_delta: 20,
    fetch_status: "ok",
    attribution_status: "OVERLAPPING_ACTIVITIES",
    trigger_session_id: 7,
    raw_response: "RAW_PAYLOAD_SENTINEL"
  },
  {
    id: 2,
    collected_at: "2026-09-13T00:15:00+00:00",
    graphql_used: null,
    graphql_delta: null,
    fetch_status: "fetch_failed",
    attribution_status: "FETCH_FAILED",
    trigger_session_id: null
  },
  {
    id: 3,
    collected_at: "2026-09-13T00:18:00+00:00",
    graphql_used: 35,
    graphql_delta: null,
    fetch_status: "some_future_status",
    attribution_status: "SOME_FUTURE_ATTRIBUTION",
    trigger_session_id: null
  }
];

const sources = {
  usageAllowances: {
    generated_at: "2026-09-13T00:20:00+00:00",
    allowances: [
      {
        provider: "openai",
        product_surface: "work_codex",
        limit_id: "LIMIT_ID_SENTINEL",
        limit_id_origin: "map_key",
        display_name: null,
        plan_type: "plus",
        rate_limit_reached_type: null,
        status: "ok",
        source_kind: "OFFICIAL_LOCAL_RUNTIME",
        provenance: "codex_app_server",
        observed_at: "2026-09-13T00:19:00+00:00",
        windows: [
          {
            source_slot: "primary",
            window_duration_minutes: 300,
            used_percent: 37,
            remaining_percent: 63,
            resets_at: "2026-09-19T08:52:29+00:00"
          }
        ]
      },
      {
        provider: "anthropic",
        product_surface: "claude_code",
        limit_id: "LIMIT_ID_SENTINEL",
        status: "stale",
        source_kind: "LOCAL_OBSERVATION",
        observed_at: "2026-09-13T00:00:00+00:00",
        windows: []
      }
    ],
    unavailable: [
      {
        provider: "anthropic",
        product_surface: "claude_desktop_cloud",
        status: "not_observed",
        source_kind: "MANUAL"
      }
    ]
  },
  githubRateLimit: {
    fetched: true,
    resources: {
      core: {
        resource: "core",
        status: "Normal",
        limit: 5000,
        used: 100,
        remaining: 4900,
        usage_percent: 2,
        remaining_percent: 98,
        reset_at_utc: "2026-09-13T01:00:00+00:00",
        reset_at_local: "2026-09-13T10:00:00+09:00",
        seconds_until_reset: 1800,
        error_message: null
      },
      graphql: {
        resource: "graphql",
        status: "Error",
        limit: null,
        used: null,
        remaining: null,
        usage_percent: null,
        remaining_percent: null,
        reset_at_utc: null,
        seconds_until_reset: null,
        error_message: "SECRET_ERROR_SENTINEL"
      },
      search: {
        resource: "search",
        status: "Normal",
        limit: 30,
        used: 1,
        remaining: 29,
        usage_percent: 3,
        remaining_percent: 97,
        reset_at_utc: "2026-09-13T00:30:00+00:00",
        seconds_until_reset: 300
      }
    },
    // 実際のdetermine_overall(app/github_rate_limit.py:216-217)は、overallがErrorの
    // ときreasonへworst.error_messageをそのまま入れる。payloadもその形にしないと、
    // reason経由の漏洩を検出できない。
    overall: { status: "Error", reason: "SECRET_ERROR_SENTINEL" },
    error: { error_type: "api_error", user_message: "SECRET_ERROR_SENTINEL" },
    collected_at: "2026-09-13T00:19:30+00:00",
    raw_exception: "Traceback (most recent call last): RAW_EXCEPTION_SENTINEL",
    last_known: null
  },
  githubActionsBilling: {
    fetched: true,
    error: null,
    status: "usage_breakdown_inconclusive",
    plan_name: "free",
    included_minutes: 2000,
    used_included_minutes: null,
    remaining_minutes: null,
    usage_percentage: null,
    discounted_standard_minutes: 120,
    billable_standard_minutes: 0,
    paid_non_included_minutes: 0,
    billing_year: 2026,
    billing_month: 9,
    collected_at: "2026-09-13T00:00:00+00:00",
    source: "SOURCE_PATH_SENTINEL",
    cache_path: "C:/Users/PATH_SENTINEL/AppData/Local/app.json",
    credential: "CREDENTIAL_SENTINEL",
    stale: true,
    skipped_unknown_skus: ["actions_future_sku"]
  },
  diagnosticsSessions: sessions,
  diagnosticsSamples: samples,
  diagnosticsHistoryStatus: "ok",
  // Packの入力ではない領域。buildAnalysisPackが将来これらを読み始めたら検出する。
  history: [{ note: "NOTE_SENTINEL", limit_id: "LIMIT_ID_SENTINEL", source_type: "manual" }],
  collectorRuns: [{ error_message: "SECRET_ERROR_SENTINEL" }]
};

const pack = app.buildAnalysisPack(sources, { now: new Date("2026-09-13T00:20:00Z") });
"""

FAKE_DOM_JS = """
function makeElement(id) {
  return {
    id,
    innerHTML: "",
    textContent: "",
    value: "",
    hidden: false,
    dataset: {},
    _attributes: {},
    setAttribute(name, value) {
      this._attributes[name] = value;
    },
    removeAttribute(name) {
      delete this._attributes[name];
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attributes, name) ? this._attributes[name] : null;
    },
    addEventListener() {}
  };
}

function makeDocument(ids, viewSections) {
  const elements = new Map();
  ids.forEach((id) => elements.set(id, makeElement(id)));
  const sections = (viewSections || []).map((view) => {
    const element = makeElement(`section-${view}`);
    element.dataset.view = view;
    return element;
  });
  return {
    elements,
    sections,
    querySelector(selector) {
      if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-view]") return sections;
      return [];
    }
  };
}
"""


# ---------------------------------------------------------------------------
# 構文・文字コードの健全性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [APP_JS, STATIC_DIR / "compact.js"])
def test_javascript_files_still_parse(path: Path) -> None:
    completed = subprocess.run(
        ["node", "--check", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("path", [APP_JS, INDEX_HTML, STYLES_CSS])
def test_no_control_characters_that_hide_code_from_tooling(path: Path) -> None:
    """NULや制御文字が混ざるとgrep/diffがバイナリ扱いになり、レビューから内容が消える。"""
    raw = path.read_bytes()
    suspicious = [index for index, byte in enumerate(raw) if byte < 9 or 13 < byte < 32]
    assert suspicious == [], f"{path.name} に制御文字がある: {suspicious[:5]}"


# ---------------------------------------------------------------------------
# A. Analysis Pack の漏洩防止
# ---------------------------------------------------------------------------


def test_analysis_pack_never_contains_any_forbidden_sentinel_value(tmp_path: Path) -> None:
    leaked = node_json(
        tmp_path,
        SOURCES_JS
        + """
const sentinels = %s;
console.log(JSON.stringify(sentinels.filter((value) => pack.includes(value))));
"""
        % json.dumps(sorted(SENTINELS.values())),
    )
    assert leaked == []


def test_analysis_pack_never_contains_forbidden_key_names(tmp_path: Path) -> None:
    """値が空でもキー名自体が出れば、そのフィールドを載せる実装になってしまっている。"""
    forbidden_keys = [
        "github_login",
        "github_user_id",
        "pr_number",
        "repository",
        "error_message",
        "note",
        "cache_path",
        "trigger_session_id",
        "limit_id:",
    ]
    leaked = node_json(
        tmp_path,
        SOURCES_JS
        + """
const keys = %s;
console.log(JSON.stringify(keys.filter((key) => pack.includes(key))));
"""
        % json.dumps(forbidden_keys),
    )
    assert leaked == []


def test_analysis_pack_source_reads_only_an_explicit_allowlist(tmp_path: Path) -> None:
    """未知のキーがpayloadへ増えても自動では出力されないこと。"""
    extra = node_json(
        tmp_path,
        """
const bucket = {
  provider: "openai",
  product_surface: "work_codex",
  status: "ok",
  source_kind: "MANUAL",
  future_field: "FUTURE_FIELD_SENTINEL",
  windows: []
};
const pack = app.buildAnalysisPack(
  { usageAllowances: { allowances: [bucket], unavailable: [] } },
  { now: new Date("2026-09-13T00:20:00Z") }
);
console.log(JSON.stringify({ leaked: pack.includes("FUTURE_FIELD_SENTINEL"), provider: pack.includes("openai") }));
""",
    )
    assert extra == {"leaked": False, "provider": True}


def test_error_overall_reason_is_not_forwarded_to_the_pack(tmp_path: Path) -> None:
    """overall.reasonは許可リストに載っているが、Error時だけ中身が別物になる。

    determine_overall(app/github_rate_limit.py:216-217)はoverallがErrorのとき
    reasonへresourceのerror_messageをそのまま入れる。error_message自体は
    許可リストに無いので、reasonという別名で出てもいけない。
    """
    result = node_json(
        tmp_path,
        """
const errorPack = app.buildAnalysisPack(
  {
    githubRateLimit: {
      fetched: true,
      overall: { status: "Error", reason: "SECRET_ERROR_SENTINEL" },
      resources: { core: { resource: "core", status: "Error", error_message: "SECRET_ERROR_SENTINEL" } }
    }
  },
  { now: new Date("2026-09-13T00:20:00Z") }
);
const normalPack = app.buildAnalysisPack(
  {
    githubRateLimit: {
      fetched: true,
      overall: { status: "Warning", reason: "GraphQL API approaching limit" },
      resources: {}
    }
  },
  { now: new Date("2026-09-13T00:20:00Z") }
);
console.log(JSON.stringify({
  leaked: errorPack.includes("SECRET_ERROR_SENTINEL"),
  statusKept: errorPack.includes("overall.status: Error"),
  normalReasonKept: normalPack.includes("overall.reason: GraphQL API approaching limit")
}));
""",
    )
    assert result == {"leaked": False, "statusKept": True, "normalReasonKept": True}


def test_analysis_pack_never_expands_nested_objects_it_does_not_understand(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
console.log(JSON.stringify({
  nested: app.analysisPackFormatValue({ secret: "NESTED_SENTINEL" }),
  nestedInArray: app.analysisPackFormatValue([{ secret: "NESTED_SENTINEL" }, "safe"]),
  scalar: app.analysisPackFormatValue(12)
}));
""",
    )
    assert result == {"nested": "", "nestedInArray": "safe", "scalar": "12"}


# ---------------------------------------------------------------------------
# B. pseudonymization
# ---------------------------------------------------------------------------


def test_sessions_appear_only_as_pack_local_sequential_pseudonyms(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
console.log(JSON.stringify({
  activity1: pack.includes("activity_1"),
  activity2: pack.includes("activity_2"),
  rawLabel: pack.includes("LABEL_SENTINEL"),
  rawRepository: pack.includes("PRIVATE_REPOSITORY_SENTINEL"),
  rawPrNumber: pack.includes("424242")
}));
""",
    )
    assert result == {
        "activity1": True,
        "activity2": True,
        "rawLabel": False,
        "rawRepository": False,
        "rawPrNumber": False,
    }


def test_same_session_keeps_one_pseudonym_and_different_sessions_differ(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const sessions = [{ id: 3 }, { id: 8 }, { id: 3 }];
const map = app.analysisPackSessionPseudonyms(sessions);
console.log(JSON.stringify({
  size: map.size,
  first: app.analysisPackPseudonymFor(map, 3),
  firstAgain: app.analysisPackPseudonymFor(map, 3),
  second: app.analysisPackPseudonymFor(map, 8),
  unknown: app.analysisPackPseudonymFor(map, 99)
}));
""",
    )
    assert result["size"] == 2
    assert result["first"] == result["firstAgain"] == "activity_1"
    assert result["second"] == "activity_2"
    assert result["first"] != result["second"]
    assert result["unknown"] == "activity_unknown"


def test_pseudonym_is_not_derived_from_the_raw_value(tmp_path: Path) -> None:
    """内容から作った安定fingerprintであってはならない。

    ラベル等を丸ごと入れ替えても番号が変わらないなら、番号は内容ではなく
    出現順から来ている。逆に、内容が同一でも別sessionなら別番号になる。
    """
    result = node_json(
        tmp_path,
        """
const a = app.analysisPackSessionPseudonyms([
  { id: 1, label: "alpha", repository: "owner/one" },
  { id: 2, label: "beta", repository: "owner/two" }
]);
const b = app.analysisPackSessionPseudonyms([
  { id: 1, label: "COMPLETELY DIFFERENT", repository: "other/repo" },
  { id: 2, label: "ALSO DIFFERENT", repository: "other/repo" }
]);
const identical = app.analysisPackSessionPseudonyms([
  { id: 4, label: "same", repository: "same/repo" },
  { id: 5, label: "same", repository: "same/repo" }
]);
console.log(JSON.stringify({
  unchangedByContent: app.analysisPackPseudonymFor(a, 1) === app.analysisPackPseudonymFor(b, 1),
  sameContentStillDistinct:
    app.analysisPackPseudonymFor(identical, 4) !== app.analysisPackPseudonymFor(identical, 5)
}));
""",
    )
    assert result == {"unchangedByContent": True, "sameContentStillDistinct": True}


def test_pseudonym_mapping_is_never_persisted_or_hashed() -> None:
    """別名は保存もせず、raw valueから導出もしないこと(コード部分だけを見る)。"""
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "crypto",
        "charCodeAt",
        "btoa",
        "setAttribute",
        "dataset",
    ):
        assert forbidden not in ANALYSIS_PACK_CODE, f"Analysis Packのコードが{forbidden}を使っている"


def test_timeline_active_activities_are_pseudonymized_too(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const timelineSection = pack.slice(pack.indexOf("## Sample Timeline"));
console.log(JSON.stringify({
  hasActive: timelineSection.includes("active_activities: activity_1"),
  rawLabel: timelineSection.includes("LABEL_SENTINEL")
}));
""",
    )
    assert result == {"hasActive": True, "rawLabel": False}


# ---------------------------------------------------------------------------
# C. preamble
# ---------------------------------------------------------------------------


def test_pack_always_starts_with_the_non_attribution_preamble(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const empty = app.buildAnalysisPack({}, { now: new Date("2026-09-13T00:20:00Z") });
console.log(JSON.stringify({
  preamble: app.ANALYSIS_PACK_PREAMBLE,
  full: pack.startsWith(app.ANALYSIS_PACK_PREAMBLE),
  emptyToo: empty.startsWith(app.ANALYSIS_PACK_PREAMBLE)
}));
""",
    )
    assert result["full"] is True
    assert result["emptyToo"] is True
    assert "exact consumer attribution" in result["preamble"]
    assert "consumer別のGraphQL消費内訳を返しません" in result["preamble"]


def test_pack_repeats_that_overlapping_activities_cannot_be_split(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS + 'console.log(JSON.stringify({ tail: pack.includes("按分することはできません") }));',
    )
    assert result == {"tail": True}


# ---------------------------------------------------------------------------
# D. 未知値・失敗値でも描画が壊れない
# ---------------------------------------------------------------------------


def test_fetch_status_including_fetch_failed_is_mapped(tmp_path: Path) -> None:
    labels = node_json(
        tmp_path,
        """
const values = ["ok", "no_previous", "reset_boundary", "counter_regression", "fetch_failed", "zzz_unknown"];
console.log(JSON.stringify(values.map(app.githubGraphqlDiagnosticsFetchStatusLabel)));
""",
    )
    assert labels[:5] == ["取得成功", "直前サンプルなし", "reset境界（差分判定不可）", "カウンタ減少（差分判定不可）", "取得失敗"]
    assert labels[5] == "不明"


def test_timeline_renders_unknown_status_without_leaking_raw_enum(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const html = app.githubGraphqlDiagnosticsTimelineTableHtml(samples, sessions);
console.log(JSON.stringify({
  rendered: html.includes("取得状態"),
  fetchFailed: html.includes("取得失敗"),
  rawFetchStatus: html.includes("some_future_status"),
  rawAttribution: html.includes("SOME_FUTURE_ATTRIBUTION"),
  unknownLabel: html.includes("不明")
}));
""",
    )
    assert result == {
        "rendered": True,
        "fetchFailed": True,
        "rawFetchStatus": False,
        "rawAttribution": False,
        "unknownLabel": True,
    }


def test_diagnostics_summary_handles_missing_and_unknown_payloads(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const items = app.diagnosticsSummaryItems({});
const unknownBilling = app.diagnosticsSummaryActionsBillingItem({ fetched: true, status: "zzz_future" });
console.log(JSON.stringify({
  count: items.length,
  allUnknown: items.every((item) => item.statusText === "未取得"),
  unknownBilling: unknownBilling.statusText,
  html: app.diagnosticsSummaryHtml(items).includes("未取得")
}));
""",
    )
    assert result["count"] == 4
    assert result["allUnknown"] is True
    assert result["unknownBilling"] == "未取得"
    assert result["html"] is True


# ---------------------------------------------------------------------------
# E. 常時nullのbilling派生値
# ---------------------------------------------------------------------------


def test_constant_null_billing_fields_are_omitted_entirely_not_zero(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const billingSection = pack.slice(
  pack.indexOf("## GitHub Actions"),
  pack.indexOf("## GitHub GraphQL Diagnostic Sessions")
);
console.log(JSON.stringify({
  used: billingSection.includes("used_included_minutes"),
  remaining: billingSection.includes("remaining_minutes"),
  percentage: billingSection.includes("usage_percentage"),
  included: billingSection.includes("included_minutes: 2000")
}));
""",
    )
    assert result == {"used": False, "remaining": False, "percentage": False, "included": True}


def test_unavailable_allowance_is_never_written_as_zero(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const allowanceSection = pack.slice(
  pack.indexOf("## 使用枠"),
  pack.indexOf("## GitHub API Rate Limit")
);
console.log(JSON.stringify({
  unavailableMarked: allowanceSection.includes("取得不能。0%ではありません"),
  emptyWindowsMarked: allowanceSection.includes("枠情報なし（0%ではありません）"),
  fabricatedZero: /used_percent: 0\\b/.test(allowanceSection)
}));
""",
    )
    assert result == {"unavailableMarked": True, "emptyWindowsMarked": True, "fabricatedZero": False}


# ---------------------------------------------------------------------------
# F / G. clipboard
# ---------------------------------------------------------------------------


def test_copy_without_clipboard_api_reports_manual_copy_instead_of_throwing(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
(async () => {
  const missing = await app.copyAnalysisPackText("body", undefined);
  const noWriteText = await app.copyAnalysisPackText("body", {});
  console.log(JSON.stringify({ missing, noWriteText }));
})();
""",
    )
    assert result["missing"]["ok"] is False
    assert "手動でコピー" in result["missing"]["message"]
    assert result["noWriteText"]["ok"] is False


def test_copy_rejection_keeps_the_page_working_and_hides_the_reason(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
(async () => {
  const rejected = await app.copyAnalysisPackText("body", {
    writeText: () => Promise.reject(new Error("SECRET_ERROR_SENTINEL"))
  });
  const threw = await app.copyAnalysisPackText("body", {
    writeText: () => {
      throw new Error("SECRET_ERROR_SENTINEL");
    }
  });
  console.log(JSON.stringify({ rejected, threw }));
})();
""",
    )
    for outcome in (result["rejected"], result["threw"]):
        assert outcome["ok"] is False
        assert "SECRET_ERROR_SENTINEL" not in outcome["message"]
        assert "手動でコピー" in outcome["message"]


def test_copy_before_generate_asks_the_user_to_generate_first(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
(async () => {
  console.log(JSON.stringify(await app.copyAnalysisPackText("", { writeText: () => Promise.resolve() })));
})();
""",
    )
    assert result["ok"] is False
    assert "生成" in result["message"]


def test_generate_never_touches_the_clipboard_and_copy_writes_once(tmp_path: Path) -> None:
    """生成しただけでは自動コピーしない。画面のGenerateと同じgenerateAnalysisPack()で見る。

    グローバルのnavigator.clipboardへspyを置くので、generate内部でclipboardへ触れれば
    (try/catchで握りつぶしていても)呼び出し回数に現れる。
    """
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
let calls = 0;
Object.defineProperty(globalThis, "navigator", {
  value: {
    clipboard: {
      writeText: () => {
        calls += 1;
        return Promise.resolve();
      }
    }
  },
  configurable: true,
  writable: true
});
global.document = makeDocument(["analysisPackOutput", "analysisPackStatus"], []);
(async () => {
  const generated = app.generateAnalysisPack();
  const afterGenerate = calls;
  const outcome = await app.copyAnalysisPackText(generated, globalThis.navigator.clipboard);
  console.log(JSON.stringify({ afterGenerate, afterCopy: calls, ok: outcome.ok, generated: generated.length > 0 }));
})();
""",
    )
    assert result == {"afterGenerate": 0, "afterCopy": 1, "ok": True, "generated": True}


# ---------------------------------------------------------------------------
# H. 新しいfetchを発生させない
# ---------------------------------------------------------------------------


def test_generate_and_copy_do_not_issue_any_backend_request(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
let fetchCalls = 0;
global.fetch = () => {
  fetchCalls += 1;
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
global.document = makeDocument(["analysisPackOutput", "analysisPackStatus"], []);

(async () => {
  const text = app.generateAnalysisPack();
  const afterGenerate = fetchCalls;
  await app.copyAnalysisPackText(text, { writeText: () => Promise.resolve() });
  console.log(JSON.stringify({
    afterGenerate,
    afterCopy: fetchCalls,
    startsWithPreamble: global.document.querySelector("#analysisPackOutput").value.startsWith(app.ANALYSIS_PACK_PREAMBLE)
  }));
})();
""",
    )
    assert result["afterGenerate"] == 0
    assert result["afterCopy"] == 0
    assert result["startsWithPreamble"] is True


def test_analysis_pack_and_summary_source_contains_no_request_call() -> None:
    for region_name, region in (
        ("analysis pack", ANALYSIS_PACK_CODE),
        ("diagnostics summary", DIAGNOSTICS_SUMMARY_CODE),
        ("history", HISTORY_CODE),
    ):
        assert "fetch(" not in region, f"{region_name} が新しい取得を行っている"
        assert "XMLHttpRequest" not in region
        assert re.search(r"\bapi\(", region) is None, f"{region_name} が api() を呼んでいる"


# ---------------------------------------------------------------------------
# I / J. History
# ---------------------------------------------------------------------------


def test_period_filter_only_narrows_the_already_fetched_array(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const now = new Date("2026-09-13T12:00:00Z").getTime();
const rows = [
  { recorded_at: "2026-09-13T09:00:00Z", tag: "today" },
  { recorded_at: "2026-09-10T09:00:00Z", tag: "within7" },
  { recorded_at: "2026-08-20T09:00:00Z", tag: "within30" },
  { recorded_at: "2026-01-01T09:00:00Z", tag: "old" },
  { recorded_at: "not-a-date", tag: "broken" }
];
const pick = (period) => app.historyRowsWithinPeriod(rows, period, now).map((row) => row.tag);
console.log(JSON.stringify({
  all: pick("all"),
  today: pick("today"),
  week: pick("7d"),
  month: pick("30d"),
  allStart: app.historyPeriodStartMs("all", now)
}));
""",
    )
    assert result["all"] == ["today", "within7", "within30", "old", "broken"]
    assert result["today"] == ["today"]
    assert result["week"] == ["today", "within7"]
    assert result["month"] == ["today", "within7", "within30"]
    assert result["allStart"] is None


def test_source_filter_still_works_and_is_pure(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const rows = [
  { source_type: "manual" },
  { source_type: "manual_adjustment" },
  { source_type: "api_openai_management" },
  { source_type: "api_claude_management" }
];
const count = (mode) => app.historySourceFilteredRows(rows, mode).length;
console.log(JSON.stringify({
  all: count("all"),
  manual: count("manual"),
  adjust: count("adjust"),
  openai: count("openai"),
  api: count("api"),
  originalUntouched: rows.length
}));
""",
    )
    assert result == {
        "all": 4,
        "manual": 1,
        "adjust": 1,
        "openai": 1,
        "api": 2,
        "originalUntouched": 4,
    }


def test_api_derived_rows_are_not_presented_as_events(tmp_path: Path) -> None:
    labels = node_json(
        tmp_path,
        """
console.log(JSON.stringify({
  manual: app.historyRecordedAtLabel("manual"),
  adjustment: app.historyRecordedAtLabel("manual_adjustment"),
  openai: app.historyRecordedAtLabel("api_openai_management"),
  gemini: app.historyRecordedAtLabel("api_gemini_management"),
  claude: app.historyRecordedAtLabel("api_claude_management")
}));
""",
    )
    assert labels["manual"] == "記録"
    assert labels["adjustment"] == "記録"
    # API由来行のrecorded_atは、行が書かれた時刻ではなく取り込んだ利用期間の終わり
    # (app/collectors/importer.py:272-280)。usage_recordsには行自体の更新時刻を持つ列が
    # 無いので、「最終更新」と呼ぶとデータに無い意味を主張することになる。
    assert labels["openai"] == labels["gemini"] == labels["claude"] == "対象期間の終了"
    # 「最終更新」という語自体は概要ビューでpayloadのgenerated_atに対して正しく
    # 使われている(そこでは本当に最終更新時刻)。禁じたいのは履歴行に対する主張なので、
    # Historyの領域だけを見る。
    assert "最終更新" not in HISTORY_CODE, "履歴行が存在しない『最終更新』時刻を主張している"


def test_history_panel_explains_the_api_derived_semantics() -> None:
    assert "取り込んだ利用期間の終わり" in INDEX_HTML_TEXT
    assert "発生した出来事の履歴としては読めません" in INDEX_HTML_TEXT
    assert "追加のサーバー取得は発生しません" in INDEX_HTML_TEXT
    assert "最終更新された値" not in INDEX_HTML_TEXT


def test_history_period_control_exists_with_an_accessible_name() -> None:
    assert 'id="historyPeriod"' in INDEX_HTML_TEXT
    assert 'aria-label="使用履歴の期間"' in INDEX_HTML_TEXT
    for value in ("today", "7d", "30d"):
        assert f'value="{value}"' in INDEX_HTML_TEXT


def test_history_uses_staged_display_instead_of_a_silent_cap() -> None:
    """従来は先頭30件で黙って打ち切っていた。件数表示と「もっと見る」を必須にする。"""
    assert 'id="historyMore"' in INDEX_HTML_TEXT
    assert 'id="historyCount"' in INDEX_HTML_TEXT
    assert "件中 ${visible.length}件を表示" in APP_JS_TEXT
    assert ".slice(0, 30)" not in APP_JS_TEXT


def test_history_rendering_is_driven_by_the_shared_page_size() -> None:
    assert "const HISTORY_PAGE_SIZE = 30;" in APP_JS_TEXT
    assert "resetHistoryPaging" in APP_JS_TEXT


# ---------------------------------------------------------------------------
# K / L. 相関の扱い
# ---------------------------------------------------------------------------


def test_recent_sessions_table_never_shows_baseline_attribution_status(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const html = app.githubGraphqlDiagnosticsSessionComparisonTableHtml(sessions);
const guidance = "相関状態(の推移)はSample Timelineで確認できます。";
console.log(JSON.stringify({
  rendered: html.includes("Recent Activity Sessions"),
  correlationField: html.includes("相関状態:"),
  guidanceOnce: html.split(guidance).length - 1 === 1,
  correlationOutsideGuidance: html.split(guidance).join("").includes("相関"),
  rawAttribution: html.includes("SINGLE_ACTIVITY_CORRELATION"),
  pointsToTimeline: html.includes("Sample Timeline")
}));
""",
    )
    assert result["rendered"] is True
    # 表の見出しには「相関状態はSample Timelineで確認できる」という案内文があるので、
    # 語そのものではなく、行のフィールドとして出ていないこと(「相関状態:」)を見る。
    assert result["correlationField"] is False
    # 「相関状態:」以外の書き方(例:「相関(baseline):」)でも行フィールドとして出さない。
    # 案内文を1回だけ取り除いた残りに「相関」という語が残っていないことを見る。
    assert result["guidanceOnce"] is True
    assert result["correlationOutsideGuidance"] is False
    assert result["rawAttribution"] is False
    assert result["pointsToTimeline"] is True


def test_timeline_is_the_correlation_source_and_uses_sample_level_values(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const html = app.githubGraphqlDiagnosticsTimelineTableHtml(samples, sessions);
console.log(JSON.stringify({
  correlation: html.includes("相関状態"),
  overlapping: html.includes("複数Activityが重複（個別内訳不可）"),
  fetchFailedAttribution: html.includes("取得失敗"),
  labels: {
    fetchFailed: app.githubGraphqlDiagnosticsAttributionLabel("FETCH_FAILED"),
    resetBoundary: app.githubGraphqlDiagnosticsAttributionLabel("RESET_BOUNDARY"),
    counterRegression: app.githubGraphqlDiagnosticsAttributionLabel("COUNTER_REGRESSION")
  }
}));
""",
    )
    # 「取得失敗」はfetch_statusのラベルとしても描画されるため、html側の検査だけでは
    # FETCH_FAILEDの相関ラベルが何に写っているかを区別できない。ラベルは値で固定する。
    assert result == {
        "correlation": True,
        "overlapping": True,
        "fetchFailedAttribution": True,
        "labels": {
            "fetchFailed": "取得失敗",
            "resetBoundary": "reset境界のため差分判定不可",
            "counterRegression": "カウンタ減少を検出（差分判定不可）",
        },
    }


def test_no_exact_attribution_wording_is_introduced(tmp_path: Path) -> None:
    """断定表現を「実装が出す文字列」として持ち込まない。

    既存app.jsのコメントには『「Xが消費しました」「confirmed」「exact」という表現は
    出力しない』という注意書きがあるため、コメント込みの検索では必ず誤検出する。
    ここではコメントを除いたコードと、実際に描画される文字列の両方を見る。
    なお "exact" 自体は禁止語にしない — Pack先頭の
    「exact consumer attributionではありません」は、まさに断定を否定する文言だから。
    """
    for banned in ("が消費しました", "が消費した", "が原因です", "按分して"):
        assert banned not in APP_JS_CODE, f"断定的な表現がコードに入っている: {banned}"
        assert banned not in INDEX_HTML_TEXT, f"断定的な表現がmarkupに入っている: {banned}"

    rendered = node_json(
        tmp_path,
        SOURCES_JS
        + """
const timeline = app.githubGraphqlDiagnosticsTimelineTableHtml(samples, sessions);
const comparison = app.githubGraphqlDiagnosticsSessionComparisonTableHtml(sessions);
const summary = app.diagnosticsSummaryHtml(app.diagnosticsSummaryItems({}));
const all = [pack, timeline, comparison, summary].join("\\n");
console.log(JSON.stringify({
  consumed: /が消費し/.test(all),
  confirmed: all.toLowerCase().includes("confirmed"),
  disclaimer: pack.includes("按分することはできません")
}));
""",
    )
    assert rendered == {"consumed": False, "confirmed": False, "disclaimer": True}


# ---------------------------------------------------------------------------
# 診断サマリー
# ---------------------------------------------------------------------------


def test_summary_never_invents_a_normal_state_for_actions_billing(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const inconclusive = app.diagnosticsSummaryActionsBillingItem({
  fetched: true,
  status: "usage_breakdown_inconclusive"
});
const planUnknown = app.diagnosticsSummaryActionsBillingItem({ fetched: true, status: "plan_unknown" });
const failed = app.diagnosticsSummaryActionsBillingItem({ error: { error_type: "api_error" } });
console.log(JSON.stringify({
  inconclusive: inconclusive.statusText,
  planUnknown: planUnknown.statusText,
  failed: failed.statusText
}));
""",
    )
    assert result["inconclusive"] == "内訳判定不可"
    assert result["planUnknown"] == "Plan不明"
    assert result["failed"] == "取得失敗"
    # 公式データ側に「正常」に相当するstatusが無いので、UI側でも作らない。
    assert all("正常" not in text for text in result.values())


def test_summary_reports_the_backend_overall_without_adding_search(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const item = app.diagnosticsSummaryGithubRateLimitItem({
  fetched: true,
  overall: { status: "Warning", reason: "GraphQL API approaching limit" },
  resources: { search: { resource: "search", status: "Exhausted" } }
});
const lastKnown = app.diagnosticsSummaryGithubRateLimitItem({
  fetched: false,
  last_known: { overall: { status: "Normal", reason: "core and graphql are within normal limits" } }
});
console.log(JSON.stringify({
  status: item.statusText,
  reason: item.detail,
  mentionsSearch: item.detail.toLowerCase().includes("search"),
  lastKnownStale: lastKnown.stale,
  html: app.diagnosticsSummaryHtml([item]).includes("searchは含みません")
}));
""",
    )
    assert result["status"] == "Warning"
    assert result["reason"] == "GraphQL API approaching limit"
    assert result["mentionsSearch"] is False
    assert result["lastKnownStale"] is True
    assert result["html"] is True


def test_summary_hides_the_raw_codex_error_token(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const item = app.diagnosticsSummaryCodexItem({
  auto_refresh_enabled: true,
  last_auto_refresh_error_type: "authentication_unavailable"
});
console.log(JSON.stringify({ statusText: item.statusText, detail: item.detail }));
""",
    )
    assert "authentication_unavailable" not in json.dumps(result, ensure_ascii=False)
    assert result["statusText"] == "直近の自動更新に失敗"


def test_summary_renders_into_the_dom_without_any_fetch(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
let fetchCalls = 0;
global.fetch = () => {
  fetchCalls += 1;
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
global.document = makeDocument(["diagnosticsSummary"], []);
app.renderDiagnosticsSummary();
const html = global.document.querySelector("#diagnosticsSummary").innerHTML;
console.log(JSON.stringify({ fetchCalls, rows: (html.match(/diagnostics-summary-row/g) || []).length }));
""",
    )
    assert result == {"fetchCalls": 0, "rows": 4}


# ---------------------------------------------------------------------------
# M. 既存ビュー / ナビゲーションのregression
# ---------------------------------------------------------------------------


def test_view_sections_and_nav_buttons_match_the_information_architecture() -> None:
    counts: dict[str, int] = {}
    for view in re.findall(r'data-view="([a-z]+)"', INDEX_HTML_TEXT):
        counts[view] = counts.get(view, 0) + 1
    assert counts == {"overview": 1, "limits": 7, "diagnostics": 5, "history": 1, "analyze": 1}
    assert re.findall(r'<button id="(nav[A-Za-z]+)"', INDEX_HTML_TEXT) == [
        "navOverview",
        "navLimits",
        "navDiagnostics",
        "navHistory",
        "navAnalyze",
    ]


def test_existing_panels_are_all_still_present() -> None:
    for element_id in (
        "overviewView",
        "limitsAllowanceView",
        "cards",
        "collectorForm",
        "collectorRuns",
        "githubRateLimitPanel",
        "githubRateLimitResult",
        "githubActionsBillingPanel",
        "githubActionsBillingResult",
        "githubGraphqlDiagnosticsPanel",
        "githubGraphqlDiagnosticsResult",
        "githubGraphqlDiagnosticsSessionsResult",
        "githubGraphqlDiagnosticsTimelineResult",
        "claudeDesktopCloudUsagePanel",
        "codexRateLimitsPanel",
        "codexUsagePanel",
        "alerts",
        "history",
        "historyFilter",
        "exportUsageCsv",
    ):
        assert f'id="{element_id}"' in INDEX_HTML_TEXT, f"既存要素 {element_id} が消えている"


def test_diagnostics_sections_follow_the_agreed_order() -> None:
    order = [
        'id="diagnosticsSummaryPanel"',
        'id="githubRateLimitPanel"',
        'id="githubActionsBillingPanel"',
        'id="githubGraphqlDiagnosticsPanel"',
        'id="collectorForm"',
    ]
    positions = [INDEX_HTML_TEXT.index(marker) for marker in order]
    assert positions == sorted(positions), "診断セクションの並び順が仕様と違う"


def test_analyze_is_a_first_class_view(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
console.log(JSON.stringify({
  fromHash: app.normalizeAppView("#analyze"),
  unknown: app.normalizeAppView("#nope"),
  writes: app.shouldWriteAppViewHash("#history", "analyze"),
  skips: app.shouldWriteAppViewHash("#analyze", "analyze")
}));
""",
    )
    assert result == {"fromHash": "analyze", "unknown": "overview", "writes": True, "skips": False}


def test_switching_to_analyze_hides_every_other_view(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
global.location = { hash: "" };
global.document = makeDocument(
  ["navOverview", "navLimits", "navDiagnostics", "navHistory", "navAnalyze"],
  ["overview", "limits", "diagnostics", "history", "analyze"]
);
app.setActiveView("analyze");
const visible = global.document.sections.filter((section) => !section.hidden).map((s) => s.dataset.view);
const current = ["navOverview", "navLimits", "navDiagnostics", "navHistory", "navAnalyze"].filter(
  (id) => global.document.querySelector(`#${id}`).getAttribute("aria-current") === "page"
);
console.log(JSON.stringify({ visible, current, hash: global.location.hash }));
""",
    )
    # 偽のlocationはただのオブジェクトなので、ブラウザのように"#"を前置しない。
    # ここで見るのは「hashへビュー名が書かれたか」まで。"#analyze"という最終形と
    # 書き込みの要否そのものは normalizeAppView / shouldWriteAppViewHash 側で検査する。
    assert result == {"visible": ["analyze"], "current": ["navAnalyze"], "hash": "analyze"}


# ---------------------------------------------------------------------------
# スタイル / アクセシビリティ
# ---------------------------------------------------------------------------


def test_previously_unstyled_diagnostics_classes_now_have_rules() -> None:
    """app.jsはこれらのクラスでDOMを組み立てていたのに、CSS規則が1つも無かった。"""
    for selector in (
        ".github-graphql-diagnostics-session",
        ".github-graphql-diagnostics-history-row",
        ".github-graphql-diagnostics-timeline-row",
        ".github-graphql-diagnostics-meta",
        ".diagnostics-summary-row",
        ".analysis-pack-output",
    ):
        assert selector in STYLES_CSS_TEXT, f"{selector} のCSS規則が無い"


def test_analysis_pack_textarea_keeps_a_visible_focus_ring() -> None:
    """共通の input/select/button 規則は textarea を対象にしていない。

    セレクタの存在だけを見ると、宣言部が `outline: none` でも通ってしまう。
    規則の中身を取り出して、実際に見えるoutlineを引いていることまで見る。
    """
    match = re.search(r"\.analysis-pack-output:focus-visible\s*\{([^}]*)\}", STYLES_CSS_TEXT)
    assert match is not None, "textarea用の:focus-visible規則が無い"
    body = match.group(1)
    outline = re.search(r"outline:\s*([^;]+);", body)
    assert outline is not None, "outlineを宣言していない"
    assert "none" not in outline.group(1), f"focusリングを消している: {outline.group(1)}"
    assert "outline-offset" in body


def test_live_regions_are_only_where_a_short_notice_appears() -> None:
    """live regionを入れ子にせず、読み込みのたびの連続読み上げも作らない。

    要約はloadAll()の中で各パネルのrenderから4回再描画されるため、sectionごと
    live regionにすると同じ4行が最大4回読み上げられる。実際に短い通知が出るのは
    #analysisPackStatus だけなので、live regionもそこだけに置く。

    概要/上限ビューとClaude Desktop入力欄のlive regionはこのPhase以前からある。
    このPhaseで増やしてよいのは #analysisPackStatus の1つだけなので、属性の順序や
    位置が変わっても検出できるよう、aria-liveを持つタグを全部数えて固定する。
    """
    live_tags = re.findall(r"<[a-z]+\b[^>]*\saria-live=\"[^\"]*\"[^>]*>", INDEX_HTML_TEXT)
    live_ids = [re.search(r'\sid="([^"]+)"', tag).group(1) for tag in live_tags]
    assert live_ids == [
        "overviewView",
        "limitsAllowanceView",
        "claudeDesktopCloudUsageResult",
        "analysisPackStatus",
    ]
    assert 'id="analysisPackStatus" class="muted" aria-live="polite"' in INDEX_HTML_TEXT
    assert 'id="diagnosticsSummarySection" data-view="diagnostics">' in INDEX_HTML_TEXT
    assert 'id="analyzeView" data-view="analyze">' in INDEX_HTML_TEXT


def test_analysis_pack_textarea_is_labelled_and_read_only() -> None:
    match = re.search(r"<textarea[^>]*id=\"analysisPackOutput\"[^>]*>", INDEX_HTML_TEXT)
    assert match is not None, "Analysis Packのtextareaが無い"
    tag = match.group(0)
    assert 'aria-label="Analysis Pack本文"' in tag
    assert "readonly" in tag


def _code_lines_with_urls(source: str) -> list[str]:
    """行全体がコメントである行を除き、URLを含む行を返す。

    strip_js_comments() の `//.*` は文字列リテラル内の "https://..." まで消してしまうため、
    外部URLの検査には使えない。ここでは生テキストを行単位で見て、行頭がコメントの
    場合だけを除外する(コードの後ろに続く行末コメント内のURLは、検出する側に倒す)。
    """
    return [
        line.strip()
        for line in source.splitlines()
        if re.search(r"https?://", line) and not line.lstrip().startswith(("//", "*", "/*"))
    ]


def test_no_remote_asset_or_telemetry_is_introduced() -> None:
    for banned in ("http://", "https://", "googleapis", "unpkg", "jsdelivr"):
        assert banned not in STYLES_CSS_TEXT, f"styles.css が外部参照を含む: {banned}"
        assert banned not in INDEX_HTML_TEXT, f"index.html が外部参照を含む: {banned}"
    # 検出器自体が、文字列リテラル内のURLを見逃さず、コメント行だけを除外すること。
    assert _code_lines_with_urls('const X = "https://example.invalid/collect";') != []
    assert _code_lines_with_urls("  // see https://docs.github.com/rest") == []
    # 「外部へ送信しない」がAnalysis Pack最大の主張なので、app.jsのコードも見る。
    assert _code_lines_with_urls(APP_JS_TEXT) == [], "app.js のコードが外部URLを含む"


def test_all_six_attribution_values_are_mapped(tmp_path: Path) -> None:
    labels = node_json(
        tmp_path,
        """
const values = [
  "UNATTRIBUTED",
  "SINGLE_ACTIVITY_CORRELATION",
  "OVERLAPPING_ACTIVITIES",
  "RESET_BOUNDARY",
  "COUNTER_REGRESSION",
  "FETCH_FAILED",
  "ZZZ_FUTURE_VALUE"
];
console.log(JSON.stringify(values.map(app.githubGraphqlDiagnosticsAttributionLabel)));
""",
    )
    assert len(set(labels[:6])) == 6, "6値が別々のラベルへ写っていない"
    assert all(label != "不明" for label in labels[:6])
    assert labels[6] == "不明"
    assert all("消費" not in label for label in labels), "相関ラベルが消費を断定している"


def test_timeline_never_attaches_a_number_to_a_single_activity(tmp_path: Path) -> None:
    """按分しない、を出力側で確認する。

    Activity名の隣に数値が並ぶと、貼り付け先のAIがper-activityの内訳として
    読んでしまう。active_activitiesは名前の列挙だけであること。
    """
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const timelineSection = pack.slice(pack.indexOf("## Sample Timeline"), pack.indexOf("## 読み方の注意"));
const activityLines = timelineSection.split("\\n").filter((line) => line.includes("active_activities"));
console.log(JSON.stringify({
  lines: activityLines,
  numberedActivity: /activity_\\d+\\s*[:=]\\s*-?\\d/.test(timelineSection),
  warnsAboutSumming: pack.includes("合算しないでください")
}));
""",
    )
    assert result["numberedActivity"] is False
    assert result["warnsAboutSumming"] is True
    for line in result["lines"]:
        assert re.search(r"activity_\d+\s*[:=]", line) is None


def test_textarea_survives_a_clipboard_failure(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
global.document = makeDocument(["analysisPackOutput", "analysisPackStatus"], []);
(async () => {
  const text = app.generateAnalysisPack();
  const before = global.document.querySelector("#analysisPackOutput").value;
  const outcome = await app.copyAnalysisPackText(text, {
    writeText: () => Promise.reject(new Error("SECRET_ERROR_SENTINEL"))
  });
  const after = global.document.querySelector("#analysisPackOutput").value;
  console.log(JSON.stringify({
    preserved: before === after && after.length > 0,
    ok: outcome.ok,
    leaksReason: outcome.message.includes("SECRET_ERROR_SENTINEL")
  }));
})();
""",
    )
    assert result == {"preserved": True, "ok": False, "leaksReason": False}


def test_render_history_issues_no_request(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
let fetchCalls = 0;
global.fetch = () => {
  fetchCalls += 1;
  return Promise.resolve({ ok: true, json: () => Promise.resolve([]) });
};
global.document = makeDocument(["historyFilter", "historyPeriod", "history", "historyCount", "historyMore"], []);
app.renderHistory();
global.document.querySelector("#historyPeriod").value = "7d";
app.renderHistory();
console.log(JSON.stringify({
  fetchCalls,
  emptyMessage: global.document.querySelector("#history").innerHTML.includes("使用履歴はありません")
}));
""",
    )
    assert result == {"fetchCalls": 0, "emptyMessage": True}


# ---------------------------------------------------------------------------
# 最終検証で補強した契約
# ---------------------------------------------------------------------------


def test_analysis_pack_reads_state_only_through_the_allowlisted_sources(tmp_path: Path) -> None:
    """Generateの入力はここで集めるsourcesだけ。state.history(note列を持つ)などを足したら落ちる。"""
    keys = node_json(tmp_path, "console.log(JSON.stringify(Object.keys(app.analysisPackSourcesFromState()).sort()));")
    assert keys == sorted(
        [
            "usageAllowances",
            "githubRateLimit",
            "githubActionsBilling",
            "diagnosticsSessions",
            "diagnosticsSamples",
            "diagnosticsHistoryStatus",
        ]
    )


def test_pack_generated_through_the_real_state_path_leaks_no_sentinel(tmp_path: Path) -> None:
    """buildAnalysisPackを直接呼ぶだけでは、stateからsourcesを集める経路が検査されない。

    実際の描画関数でstateへ入れ、画面のGenerateと同じgenerateAnalysisPack()で作る。
    """
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + SOURCES_JS
        + """
global.document = makeDocument(
  ["githubGraphqlDiagnosticsSessionsResult", "githubGraphqlDiagnosticsTimelineResult", "analysisPackOutput", "analysisPackStatus"],
  []
);
app.renderGithubGraphqlDiagnosticsSessions(sessions, samples);
const generated = app.generateAnalysisPack();
const sentinels = %s;
console.log(JSON.stringify({
  leaked: sentinels.filter((value) => generated.includes(value)),
  activity1: generated.includes("activity_1"),
  textarea: global.document.querySelector("#analysisPackOutput").value === generated,
  notFetched: generated.includes("未取得（0件ではありません）")
}));
"""
        % json.dumps(sorted(SENTINELS.values())),
    )
    assert result == {"leaked": [], "activity1": True, "textarea": True, "notFetched": False}


def test_pack_distinguishes_not_fetched_failed_and_empty_diagnostics_history(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        """
const now = { now: new Date("2026-09-13T00:20:00Z") };
const section = (pack) =>
  pack.slice(pack.indexOf("## GitHub GraphQL Diagnostic Sessions"), pack.indexOf("## 読み方の注意"));
const build = (extra) => section(app.buildAnalysisPack({ diagnosticsSessions: [], diagnosticsSamples: [], ...extra }, now));
const count = (text, needle) => text.split(needle).length - 1;
const notFetched = build({});
const failed = build({ diagnosticsHistoryStatus: "failed" });
const empty = build({ diagnosticsHistoryStatus: "ok" });
const readingNotes = app.buildAnalysisPack({}, now).slice(app.buildAnalysisPack({}, now).indexOf("## 読み方の注意"));
console.log(JSON.stringify({
  notFetched: [count(notFetched, "未取得（0件ではありません）"), count(notFetched, "データなし")],
  failed: [count(failed, "取得失敗（0件ではありません）"), count(failed, "データなし")],
  empty: [count(empty, "データなし（取得済み・0件）"), count(empty, "未取得"), count(empty, "取得失敗")],
  readingNote: readingNotes.includes("未取得・取得失敗と0件は別の状態です")
}));
""",
    )
    # Sessions節とSample Timeline節の2か所ずつ。末尾の読み方の注意にも区別を常置する。
    assert result == {"notFetched": [2, 0], "failed": [2, 0], "empty": [2, 0, 0], "readingNote": True}


def test_initial_history_fetch_failure_is_carried_into_the_pack(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        FAKE_DOM_JS
        + """
global.document = makeDocument(
  ["githubGraphqlDiagnosticsSessionsResult", "githubGraphqlDiagnosticsTimelineResult", "analysisPackOutput", "analysisPackStatus"],
  []
);
app.markGithubGraphqlDiagnosticsHistoryFailed(new Error("SECRET_ERROR_SENTINEL"));
const afterFailure = app.generateAnalysisPack();
const failureDom = global.document.querySelector("#githubGraphqlDiagnosticsSessionsResult").innerHTML;
app.renderGithubGraphqlDiagnosticsSessions([], []);
const afterRecovery = app.generateAnalysisPack();
console.log(JSON.stringify({
  failedInPack: afterFailure.includes("取得失敗（0件ではありません）"),
  failedShownAsEmpty: afterFailure.includes("データなし"),
  reasonLeaked: afterFailure.includes("SECRET_ERROR_SENTINEL") || failureDom.includes("SECRET_ERROR_SENTINEL"),
  domMessage: failureDom.includes("履歴の取得に失敗しました"),
  recoveredAsEmpty: afterRecovery.includes("データなし（取得済み・0件）"),
  recoveredStillFailed: afterRecovery.includes("取得失敗（0件ではありません）")
}));
""",
    )
    assert result == {
        "failedInPack": True,
        "failedShownAsEmpty": False,
        "reasonLeaked": False,
        "domMessage": True,
        "recoveredAsEmpty": True,
        "recoveredStillFailed": False,
    }
    # 起動時の取得失敗がこの関数へ配線されていること(ここはソース上の配線確認)。
    # 書き方(関数参照かarrowか)には依存させない。
    assert re.search(
        r"refreshGithubGraphqlDiagnosticsSessions\(\)\s*\.catch\([^;]*markGithubGraphqlDiagnosticsHistoryFailed",
        APP_JS_CODE,
    )


def test_pack_carries_collection_time_for_rate_limit_and_billing(tmp_path: Path) -> None:
    """seconds_until_resetは相対値なので、基準時刻が無いと生成時刻基準だと誤読される。"""
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const rateSection = pack.slice(pack.indexOf("## GitHub API Rate Limit"), pack.indexOf("## GitHub Actions"));
const billingSection = pack.slice(pack.indexOf("## GitHub Actions"), pack.indexOf("## GitHub GraphQL Diagnostic Sessions"));
const other = app.buildAnalysisPack(
  {
    githubRateLimit: {
      fetched: false,
      collected_at: null,
      last_known: {
        collected_at: "2026-09-12T23:00:00+00:00",
        overall: { status: "Normal", reason: "core and graphql are within normal limits" },
        resources: { core: { resource: "core", status: "Normal", seconds_until_reset: 10 } }
      }
    },
    githubActionsBilling: { fetched: true, stale: false, status: "plan_unknown", collected_at: "2026-09-13T00:10:00+00:00" }
  },
  { now: new Date("2026-09-13T00:20:00Z") }
);
const otherBilling = other.slice(other.indexOf("## GitHub Actions"), other.indexOf("## GitHub GraphQL Diagnostic Sessions"));
console.log(JSON.stringify({
  rateCollectedAt: rateSection.includes("collected_at: 2026-09-13T00:19:30+00:00"),
  rateRelativeNote: rateSection.includes("seconds_until_resetはcollected_at時点から数えた秒数"),
  billingCollectedAt: billingSection.includes("collected_at: 2026-09-13T00:00:00+00:00"),
  billingStaleNote: billingSection.includes("（stale）"),
  lastKnownCollectedAt: other.includes("collected_at: 2026-09-12T23:00:00+00:00"),
  lastKnownMarked: other.includes("最終取得値"),
  freshBillingCollectedAt: otherBilling.includes("collected_at: 2026-09-13T00:10:00+00:00"),
  freshBillingStaleNote: otherBilling.includes("（stale）")
}));
""",
    )
    assert result == {
        "rateCollectedAt": True,
        "rateRelativeNote": True,
        "billingCollectedAt": True,
        "billingStaleNote": True,
        "lastKnownCollectedAt": True,
        "lastKnownMarked": True,
        "freshBillingCollectedAt": True,
        "freshBillingStaleNote": False,
    }


def test_pack_session_deltas_are_not_presented_as_per_activity_consumption(tmp_path: Path) -> None:
    result = node_json(
        tmp_path,
        SOURCES_JS
        + """
const sessionsSection = pack.slice(pack.indexOf("## GitHub GraphQL Diagnostic Sessions"), pack.indexOf("## Sample Timeline"));
const note = "このActivity自身の消費量ではありません";
const outside = app.buildAnalysisPack(
  {
    diagnosticsSessions: [],
    diagnosticsSamples: [
      { collected_at: "2026-09-13T00:10:00+00:00", graphql_used: 5, fetch_status: "ok", attribution_status: "SINGLE_ACTIVITY_CORRELATION" }
    ],
    diagnosticsHistoryStatus: "ok"
  },
  { now: new Date("2026-09-13T00:20:00Z") }
);
console.log(JSON.stringify({
  accountWideNote: sessionsSection.includes(note),
  noteBeforeFirstActivity: sessionsSection.indexOf(note) < sessionsSection.indexOf("- activity_1"),
  limitedWording: outside.includes("active_activities: 取得済みsession一覧内に該当なし"),
  bareNone: /active_activities: なし/.test(outside)
}));
""",
    )
    assert result == {"accountWideNote": True, "noteBeforeFirstActivity": True, "limitedWording": True, "bareNone": False}


def test_summary_partial_payloads_are_unknown_not_disabled(tmp_path: Path) -> None:
    """部分的なオブジェクトを「無効」「待機中」と読まないこと(guardの削除を検出する)。"""
    result = node_json(
        tmp_path,
        """
console.log(JSON.stringify({
  codexPartial: app.diagnosticsSummaryCodexItem({ last_auto_refresh_error_type: "x" }).statusText,
  codexDisabled: app.diagnosticsSummaryCodexItem({ auto_refresh_enabled: false }).statusText,
  graphqlPartial: app.diagnosticsSummaryGraphqlItem({ enabled: true }).statusText,
  graphqlDisabled: app.diagnosticsSummaryGraphqlItem({ enabled: false }).statusText,
  graphqlIdle: app.diagnosticsSummaryGraphqlItem({ enabled: true, sampler_running: false }).statusText
}));
""",
    )
    assert result == {
        "codexPartial": "未取得",
        "codexDisabled": "自動更新 無効",
        "graphqlPartial": "未取得",
        "graphqlDisabled": "無効",
        "graphqlIdle": "待機中",
    }


def test_history_and_analysis_pack_handler_wiring_source_issues_no_request() -> None:
    """ソース上の配線確認(実行時テストではない)。

    実行時の「追加fetchなし」は各関数を実際に呼ぶテストとpackaged smokeで見ている。
    ここでは initApp 内のハンドラ本体が、ローカル再描画だけを呼ぶことを固定する
    (例: 期間変更でloadAll()を呼ぶ変更を検出する)。
    """
    block = strip_js_comments(
        source_region('for (const id of ["historyFilter", "historyPeriod"])', 'document.querySelector("#exportJson")')
    )
    for banned in ("api(", "fetch(", "loadAll(", "refreshGithub", "XMLHttpRequest"):
        assert banned not in block, f"履歴/Analysis Packのハンドラが取得を行っている: {banned}"
    # 別名の対応表をハンドラ側で保存する経路も塞ぐ(Pack領域だけの禁止では届かない)。
    for banned in ("dataset", "setAttribute", "localStorage", "sessionStorage", "indexedDB"):
        assert banned not in block, f"Analysis Packのハンドラが値を保存している: {banned}"
    for expected in ("renderHistory()", "generateAnalysisPack()", "copyAnalysisPackText("):
        assert expected in block


def test_generate_reads_state_only_through_the_sources_function_source() -> None:
    """ソース上の確認(実行時テストではない)。

    実行時のsentinelテストはstate.historyが空の状態で走るため、generateAnalysisPack本体で
    state.historyなどを直接連結する変更は捕まえられない。そこで、生成関数が読むstateは
    analysisPackTextへの書き込みだけであり、sources関数が読むstateはallowlistと同じ
    フィールドだけであることを固定する。
    """
    generate_body = strip_js_comments(source_region("function generateAnalysisPack() {", "function applyFiltersAndSort"))
    assert set(re.findall(r"\bstate\.(\w+)", generate_body)) == {"analysisPackText"}
    assert re.search(r"\bstate\.analysisPackText\s*=\s*text;", generate_body)
    assert "buildAnalysisPack(analysisPackSourcesFromState()," in generate_body

    sources_body = strip_js_comments(
        source_region("function analysisPackSourcesFromState() {", "const ANALYSIS_PACK_EMPTY_MESSAGE")
    )
    pairs = re.findall(r"(\w+):\s*state\.(\w+)", sources_body)
    assert pairs == [(key, key) for key, _ in pairs], "sourcesのキーと読むstateフィールドが食い違っている"
    assert sorted(key for key, _ in pairs) == sorted(
        [
            "usageAllowances",
            "githubRateLimit",
            "githubActionsBilling",
            "diagnosticsSessions",
            "diagnosticsSamples",
            "diagnosticsHistoryStatus",
        ]
    )
    assert len(re.findall(r"\bstate\.", sources_body)) == len(pairs)
