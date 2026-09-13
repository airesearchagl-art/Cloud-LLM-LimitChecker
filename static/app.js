const state = {
  dashboard: [],
  limits: [],
  history: [],
  collectorRuns: [],
  editingLimitId: null,
  codexUsage: null,
  codexRateLimits: null,
  claudeDesktopCloudUsage: null,
  githubGraphqlDiagnostics: null,
};

const api = async (path, options = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
};

const fmtNumber = (value) => {
  if (value === null || value === undefined) return "未取得";
  return Number(value).toLocaleString("ja-JP", { maximumFractionDigits: 2 });
};
const fmtDate = (value) => (value ? new Date(value).toLocaleString("ja-JP") : "未設定");
const dateValue = (value, fallback) => (value ? new Date(value).getTime() : fallback);

function toDatetimeLocalValue(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// datetime-local入力値をISO文字列へ変換する。new Date(value)がInvalid Dateになるケース
// (空文字・壊れた値など)をtoISOString()の例外にせず、呼び出し元でvalidation errorとして
// 扱えるようnullを返す。DOMに触れない純粋関数。
function parseDatetimeLocalToIsoOrNull(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

// ブラウザ標準confirmを直接呼ばず関数越しにすることで、テスト/プレビューから差し替え可能にする。
function confirmClaudeDesktopCloudUsageSave() {
  if (typeof window === "undefined" || typeof window.confirm !== "function") return true;
  return window.confirm("Claude Desktop Cloud 使用率を保存します。よろしいですか？");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  if (status === "正常") return "status-ok";
  if (status === "注意") return "status-warn";
  if (status === "危険") return "status-danger";
  if (status === "上限到達") return "status-limit";
  return "status-pending";
}

function collectorStatusClass(status) {
  if (status === "success") return "collector-success";
  if (status === "failed") return "collector-failed";
  if (status === "blocked") return "collector-blocked";
  return "collector-started";
}

function meterClass(status) {
  if (status === "注意") return "meter-warn";
  if (status === "危険") return "meter-danger";
  if (status === "上限到達") return "meter-limit";
  return "meter-ok";
}

function isAdjustmentRecord(record) {
  return record.source_type === "manual_adjustment";
}

function sourceTypeLabel(sourceType) {
  const labels = {
    manual: "手入力",
    manual_adjustment: "補正",
    api_openai_management: "OpenAI API",
    api_gemini_management: "Gemini API",
    api_claude_management: "Claude API",
  };
  return labels[sourceType] || sourceType || "未取得";
}

function sourceTypeClass(sourceType) {
  const classes = {
    manual: "source-manual",
    manual_adjustment: "source-adjustment",
    api_openai_management: "source-api source-openai",
    api_gemini_management: "source-api source-gemini",
    api_claude_management: "source-api source-claude",
  };
  return classes[sourceType] || "source-unknown";
}

function isApiSource(sourceType) {
  return ["api_openai_management", "api_gemini_management", "api_claude_management"].includes(sourceType);
}

function githubResourceLabel(resourceName) {
  const labels = {
    core: "GitHub REST API",
    graphql: "GitHub GraphQL API",
    search: "GitHub Search API",
  };
  return labels[resourceName] || resourceName;
}

function githubStatusClass(status) {
  if (status === "Normal") return "github-status-normal";
  if (status === "Warning") return "github-status-warning";
  if (status === "Exhausted") return "github-status-exhausted";
  if (status === "Reset overdue") return "github-status-overdue";
  if (status === "Error") return "github-status-error";
  return "github-status-unknown";
}

function githubOverallClass(status) {
  if (status === "Normal") return "github-status-normal";
  if (status === "Warning") return "github-status-warning";
  if (status === "Limited") return "github-status-exhausted";
  if (status === "Error") return "github-status-error";
  return "github-status-unknown";
}

function fmtGithubDate(value) {
  if (!value) return "不明";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "不明";
  return d.toLocaleString("ja-JP");
}

function fmtGithubDateUtc(value) {
  if (!value) return "不明";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "不明";
  return d.toLocaleString("ja-JP", { timeZone: "UTC" }) + " UTC";
}

// DOMに触れない純粋関数: 0以上の経過秒数を日本語の期間表記へ変換する（「あと」は含まない）。
// 常時監視用の簡易画面(別ファイル)にある同名関数と同一ロジック(両画面で表示結果を揃えるため)。
// 「h」「m」等の略記は使わず、1分未満/分単位/時間+分/日+時間の4段階で表す。
// 0になる単位（例: ちょうど1時間）は省略する（「1時間 0分」ではなく「1時間」）。
// 入力防御: 有限かつ非負の値のみを期間として扱い、それ以外(NaN/Infinity/負値)は0扱いにする
// (呼び出し側は常に符号判定済みの値を渡す想定だが、誤用時に不正な文字列を出さないための保険)。
function fmtDurationJa(totalSeconds) {
  const seconds = Number.isFinite(totalSeconds) && totalSeconds > 0 ? totalSeconds : 0;
  if (seconds < 60) return "1分未満";
  const totalMinutes = Math.floor(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}分`;
  const totalHours = Math.floor(totalMinutes / 60);
  const remMinutes = totalMinutes % 60;
  if (totalHours < 24) return remMinutes > 0 ? `${totalHours}時間 ${remMinutes}分` : `${totalHours}時間`;
  const days = Math.floor(totalHours / 24);
  const remHours = totalHours % 24;
  return remHours > 0 ? `${days}日 ${remHours}時間` : `${days}日`;
}

// DOMに触れない純粋関数: 絶対時刻の表記と相対時間の表記を1行で併記する。
// 相対情報が無い/不明/staleで抑制された場合は絶対時刻のみを返す。
function fmtAbsoluteWithRelative(absoluteText, relativeText) {
  if (!relativeText || relativeText === "不明") return absoluteText;
  return `${absoluteText}（${relativeText}）`;
}

// DOMに触れない純粋関数: stale(最終確認値が古い可能性がある)なデータでは、
// 現在も有効なreset予定であるかのように誤認させる「あと...」という将来カウントダウンを出さない。
function suppressCountdownIfStale(relativeText, stale) {
  if (!stale) return relativeText;
  return relativeText.startsWith("あと") ? "" : relativeText;
}

function fmtSecondsUntilReset(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "不明";
  if (seconds < 0) return "リセット時刻超過";
  return `あと${fmtDurationJa(seconds)}`;
}

// DOMに触れない純粋関数: アプリ自身の次回スケジュール(GitHubの「アプリの次回取得予定」、
// Codexの「次回自動更新予定」)専用の相対時間。GitHub/Claude/Codexのresetまでの相対時間
// (fmtSecondsUntilReset等)とは意味が異なる別概念のため、「リセット時刻超過」は使わない
// (このスケジュールはGitHub側のreset予定ではなくアプリ自身の未来予定なので、reset語を
// 混同させない)。同様に、過去の予定時刻を「まもなく」とも表現しない(スケジューラが次回tickで
// 再取得するのを待っている状態を「再取得待ち」で正確に表す)。不正な日時では相対表示なし
// (空文字)を返し、呼び出し側は絶対時刻のみにフォールバックする。
function fmtAppScheduleRelative(isoString) {
  if (!isoString) return "";
  const target = new Date(isoString);
  if (Number.isNaN(target.getTime())) return "";
  const diffSeconds = Math.floor((target.getTime() - Date.now()) / 1000);
  if (diffSeconds < 0) return "再取得待ち";
  return `あと${fmtDurationJa(diffSeconds)}`;
}

// DOMに触れない純粋関数: dataからHTML文字列を組み立てるだけ。テスト容易性のため分離している。
// stale=trueはlast_known(直近取得失敗時の最終成功値)由来を意味し、resetまでの「あと...」
// カウントダウンは抑制する(絶対時刻はそのまま表示する)。
function githubResourceCardHtml(resource, stale = false) {
  if (!resource) return "";
  const statusHtml = `<span class="github-resource-status ${githubStatusClass(resource.status)}">${escapeHtml(resource.status)}</span>`;
  if (resource.status === "Error") {
    return `
      <div class="github-resource-card">
        <div class="github-resource-title">${escapeHtml(githubResourceLabel(resource.resource))}</div>
        ${statusHtml}
        <div class="github-resource-error">${escapeHtml(resource.error_message || "")}</div>
      </div>`;
  }
  const relativeText = suppressCountdownIfStale(fmtSecondsUntilReset(resource.seconds_until_reset), stale);
  const resetText = fmtAbsoluteWithRelative(fmtGithubDate(resource.reset_at_local), relativeText);
  return `
    <div class="github-resource-card">
      <div class="github-resource-title">${escapeHtml(githubResourceLabel(resource.resource))}</div>
      ${statusHtml}
      <div class="github-resource-metric">残り ${fmtNumber(resource.remaining)} / ${fmtNumber(resource.limit)}</div>
      <div class="github-resource-metric">使用 ${fmtNumber(resource.used)}（${fmtNumber(resource.usage_percent)}%）</div>
      <div class="github-resource-metric">reset: ${resetText}</div>
      <div class="github-resource-metric muted">reset (UTC): ${fmtGithubDateUtc(resource.reset_at_utc)}</div>
    </div>`;
}

// DOMに触れない純粋関数: reset後の1回限定自動再取得に関する補助表示。
// next_auto_refresh_atが過去でも負数のカウントダウンは表示しない。
// ここでの「次回」はアプリ自身が次にgh api rate_limitを叩くタイミングであり、
// GitHub側の制限解除予定(reset時刻)ではない — 相対時間はfmtAppScheduleRelative
// (fmtSecondsUntilResetとは別関数)を使い、文言でも両者を混同しない。
function githubAutoRefreshNoticeHtml(data) {
  if (!data) return "";
  if (data.refreshing) {
    return `<p class="muted">自動確認中…</p>`;
  }
  if (data.auto_refresh_pending && data.next_auto_refresh_at) {
    const nextFetchText = fmtAbsoluteWithRelative(
      fmtGithubDate(data.next_auto_refresh_at),
      fmtAppScheduleRelative(data.next_auto_refresh_at)
    );
    return `<p class="muted">reset後に1回だけ、アプリが自動で再取得します。アプリの次回取得予定: ${nextFetchText}</p>`;
  }
  if (data.last_auto_refresh_error) {
    return `<p class="muted">自動再取得に失敗しました: ${escapeHtml(data.last_auto_refresh_error.user_message || "")}</p>`;
  }
  return "";
}

// DOMに触れない純粋関数: Overallが"Limited"のとき、原因がcore/graphqlのどちらで、
// Exhausted(枠を使い切った)なのかReset overdue(reset時刻を過ぎたのに未更新)なのかを判定する。
// Overall判定自体(app/github_rate_limit.py)は変更せず、表示上の区別だけをここで行う。
// 表示優先順位はバックエンドのdetermine_overallの重大度順(Reset overdue > Exhausted)とは
// 独立に決めている: Exhaustedが1件でもあればRATE LIMITEDを優先して表示し、Exhaustedが
// 無い場合に限りRESET OVERDUEを表示する(枠を使い切っている方が利用者への影響が大きいため)。
// 同一status同士がtieする場合はcoreを優先する
// (バックエンドのdetermine_overallの同点時tie-break "core"と表示を一致させるため)。
function githubLimitedCause(resources) {
  if (!resources) return null;
  const core = resources.core;
  const graphql = resources.graphql;
  if (core && core.status === "Exhausted") return { resource: "core", variant: "rate_limited" };
  if (graphql && graphql.status === "Exhausted") return { resource: "graphql", variant: "rate_limited" };
  if (core && core.status === "Reset overdue") return { resource: "core", variant: "reset_overdue" };
  if (graphql && graphql.status === "Reset overdue") return { resource: "graphql", variant: "reset_overdue" };
  return null;
}

// DOMに触れない純粋関数: RATE LIMITED / RESET OVERDUEバナーを組み立てる。
// staleはfalseなら現在fetch成功時点、trueならlast_known(直近取得失敗時の最終成功値)を指す —
// 「今まさに制限中」と「最終確認時点では制限中だった」を文言・スタイルの両方で区別する。
function githubLimitedBannerHtml(overall, resources, stale) {
  if (!overall || overall.status !== "Limited") return "";
  const cause = githubLimitedCause(resources);
  const variant = cause ? cause.variant : "rate_limited";
  const causeLabel = cause ? githubResourceLabel(cause.resource) : "";
  const isOverdue = variant === "reset_overdue";

  const badgeText = stale
    ? isOverdue
      ? "LAST KNOWN: RESET OVERDUE"
      : "LAST KNOWN: RATE LIMITED"
    : isOverdue
      ? "RESET OVERDUE"
      : "RATE LIMITED";

  const subtext = stale
    ? isOverdue
      ? "最終確認時点でreset時刻を経過していましたが、新しい値は未取得です（現在の状態ではありません）"
      : "最終確認時点では制限中でした（現在の状態ではありません）"
    : isOverdue
      ? "reset時刻を過ぎていますが、まだ新しい値を取得できていません。"
      : "利用枠の上限に達しています。";

  const cls = [
    "github-limited-banner",
    isOverdue ? "github-banner-overdue" : "github-banner-limited",
    stale ? "github-banner-stale" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return `
    <div class="${cls}">
      <span class="github-limited-banner-badge">${escapeHtml(badgeText)}</span>
      ${causeLabel ? `<span class="github-limited-banner-cause">${escapeHtml(causeLabel)}</span>` : ""}
      <div class="github-limited-banner-subtext">${escapeHtml(subtext)}</div>
    </div>`;
}

// DOMに触れない純粋関数: secondary rate limitはcore/graphql/searchのいずれの
// resource状態でもなく、gh api rate_limit自体の呼び出しが失敗した状態(data.error)
// なので、primary resource枯渇のRATE LIMITEDバナーとは別要素として表示する。
// primary resourceのreset時刻は流用しない(そもそも保持していない)。
function githubSecondaryRateLimitBannerHtml(data) {
  if (!data || !data.error || data.error.error_type !== "secondary_rate_limit") return "";
  return `
    <div class="github-limited-banner github-banner-secondary">
      <span class="github-limited-banner-badge">SECONDARY RATE LIMIT</span>
      <div class="github-limited-banner-subtext">${escapeHtml(data.error.user_message || "")}</div>
    </div>`;
}

// DOMに触れない純粋関数: GET/POST /api/github-rate-limit のレスポンスからHTML文字列を組み立てる。
function githubRateLimitHtml(data) {
  if (!data) {
    return `<p class="muted">状態: 未取得</p>`;
  }

  const usingLastKnown = !data.fetched && !!data.last_known;
  const displayResources = data.fetched ? data.resources : usingLastKnown ? data.last_known.resources : null;
  const displayOverall = data.fetched ? data.overall : usingLastKnown ? data.last_known.overall : null;

  // secondary rate limitは専用バナーで表示するため、汎用エラー表示とは重複させない。
  const errorHtml = data.error && data.error.error_type !== "secondary_rate_limit"
    ? `<div class="github-error">${escapeHtml(data.error.user_message || "取得に失敗しました")}</div>`
    : "";
  const secondaryRateLimitHtml = githubSecondaryRateLimitBannerHtml(data);

  const staleNoticeHtml = usingLastKnown
    ? `<p class="muted">直近の取得は失敗しました。以下は${escapeHtml(fmtGithubDate(data.last_known.collected_at))}時点の古い情報（未更新）です。</p>`
    : "";

  const autoRefreshNoticeHtml = githubAutoRefreshNoticeHtml(data);

  if (!displayResources) {
    return `
      <p class="muted">状態: 未取得</p>
      ${errorHtml}
      ${secondaryRateLimitHtml}
      ${autoRefreshNoticeHtml}`;
  }

  const overallHtml = displayOverall
    ? `<div class="github-overall ${githubOverallClass(displayOverall.status)}">Overall: ${escapeHtml(displayOverall.status)} — ${escapeHtml(displayOverall.reason)}</div>`
    : "";
  const limitedBannerHtml = githubLimitedBannerHtml(displayOverall, displayResources, usingLastKnown);

  return `
    ${errorHtml}
    ${secondaryRateLimitHtml}
    ${staleNoticeHtml}
    ${limitedBannerHtml}
    ${overallHtml}
    ${autoRefreshNoticeHtml}
    <div class="github-resource-cards">
      ${githubResourceCardHtml(displayResources.core, usingLastKnown)}
      ${githubResourceCardHtml(displayResources.graphql, usingLastKnown)}
      ${displayResources.search ? githubResourceCardHtml(displayResources.search, usingLastKnown) : ""}
    </div>`;
}

function renderGithubRateLimit(data) {
  document.querySelector("#githubRateLimitResult").innerHTML = githubRateLimitHtml(data);
}

function githubActionsBillingStatusClass(status) {
  if (status === "plan_unknown") return "github-status-error";
  if (status === "usage_breakdown_inconclusive") return "github-status-unknown";
  return "";
}

// null/undefinedは"—"(未取得の"未取得"表記とは区別し、"exact値が原理的に無い"ことを示す)。
const fmtExactOrDash = (value) => (value === null || value === undefined ? "—" : fmtNumber(value));

// ============================================================================
// 使用枠(Usage Allowances)読み取り専用ビュー — 概要(#overviewView) / 上限(#limitsAllowanceView)
//
// GET /api/usage-allowancesは既存キャッシュを横断するprovider非依存の読み取り
// 専用read model(app/usage_allowance.py)であり、非throwのfetchAllowancesSafe()
// (non-2xx/JSON parse失敗/network errorのいずれも例外を投げず{ok:false}を返す)
// 経由でのみ呼び、loadAll()の他のPromise.all要素を巻き込んで全滅させない。
// ============================================================================

async function fetchAllowancesSafe() {
  try {
    const res = await fetch("/api/usage-allowances");
    if (!res.ok) return { ok: false };
    const data = await res.json();
    return { ok: true, data };
  } catch (error) {
    return { ok: false };
  }
}

const USAGE_ALLOWANCE_ERROR_MESSAGE = "使用枠情報を取得できませんでした。";

// DOMに触れない純粋関数: source_kind(open string、既知5種)を日本語ラベルへ
// 変換する。未知の値(nullを含む)は生の値をそのまま単独露出させず、
// nullは中立な「不明」、それ以外の未知文字列は生の文字列自体を返す
// (呼び出し側でescapeHtmlする — ここでは二重エスケープを避けるため
// 何もエスケープしない)。新しいsource_kind値をここで勝手に作らない。
const USAGE_ALLOWANCE_SOURCE_KIND_LABELS = {
  OFFICIAL_API: "公式API",
  OFFICIAL_LOCAL_RUNTIME: "公式ローカル取得",
  LOCAL_OBSERVATION: "ローカル観測",
  MANUAL: "手動入力",
  TEMPORAL_CORRELATION: "時間相関推定",
};

function usageAllowanceSourceKindLabel(sourceKind) {
  if (sourceKind === null || sourceKind === undefined) return "不明";
  return USAGE_ALLOWANCE_SOURCE_KIND_LABELS[sourceKind] || String(sourceKind);
}

function usageAllowanceSourceBadgeHtml(sourceKind) {
  return `<span class="source-badge">${escapeHtml(usageAllowanceSourceKindLabel(sourceKind))}</span>`;
}

// DOMに触れない純粋関数: window_duration_minutesだけからwindowラベルを作る。
// モデル名・plan名を一切参照しない(spec: 「Never label a window with a model
// or plan name」)。300/10080以外の整数はfmtNumberで千区切りしたうえで
// `${n}分枠`にfallbackする。
function usageAllowanceWindowLabel(windowDurationMinutes) {
  if (windowDurationMinutes === 300) return "5時間枠";
  if (windowDurationMinutes === 10080) return "週次枠";
  if (windowDurationMinutes === null || windowDurationMinutes === undefined) return "期間不明";
  return `${fmtNumber(windowDurationMinutes)}分枠`;
}

// DOMに触れない純粋関数: bucketの表示名。display_name -> limit_id ->
// product_surfaceそのものの順でfallbackする。存在しない名前を作らない
// (spec: 「Never fabricate a name」)。
function usageAllowanceBucketTitle(bucket) {
  if (bucket.display_name) return bucket.display_name;
  if (bucket.limit_id) return bucket.limit_id;
  return bucket.product_surface;
}

// ダッシュボードのstatus_for_usage(app/calculations.py)が使うデフォルト閾値
// (Limit.warning_threshold=70.0 / critical_threshold=85.0)をそのまま流用する。
// 使用枠のwindowにはバックエンド由来の正常/注意/危険という文字列が無く
// used_percentしか渡されないため、既存のstatusClass/meterClassをそのまま
// 再利用する(spec指示)には、ここで同じ規約の閾値から合成するのが最も
// 新しい基準を作らない選択となる。
const USAGE_ALLOWANCE_WARNING_THRESHOLD = 70;
const USAGE_ALLOWANCE_CRITICAL_THRESHOLD = 85;

// 100%以上でも「上限到達」とは言わない: 到達したかどうかはpayload自身が
// rate_limit_reached_typeという別のフィールドで(bucket単位で)表現しており、
// used_percentの丸め(99.6 -> 100)から到達を断定すると出所のない主張になる。
// ここで作るのはあくまで使用率の読み方(注意/危険)であり、状態の宣言ではない。
function usageAllowanceWindowStatus(usedPercent) {
  const percent = typeof usedPercent === "number" && Number.isFinite(usedPercent) ? usedPercent : 0;
  if (percent >= USAGE_ALLOWANCE_CRITICAL_THRESHOLD) return "危険";
  if (percent >= USAGE_ALLOWANCE_WARNING_THRESHOLD) return "注意";
  return "正常";
}

// DOMに触れない純粋関数: resets_atがnullなら絶対表記("未設定")のみを返し、
// 未来を推測したカウントダウンは出さない(spec: 「never guess」)。staleな
// bucketではsuppressCountdownIfStaleで将来カウントダウンだけを抑制する。
function usageAllowanceWindowResetText(resetsAt, stale) {
  const absoluteText = fmtDate(resetsAt);
  if (!resetsAt) return absoluteText;
  const target = new Date(resetsAt);
  // 解釈できない値を絶対表記としてそのまま出すと"Invalid Date"が画面に出る。
  // 読めない時刻は「無い」のと同じ扱いにする(推測もしない)。
  if (Number.isNaN(target.getTime())) return fmtDate(null);
  const secondsUntilReset = (target.getTime() - Date.now()) / 1000;
  const relativeText = suppressCountdownIfStale(fmtSecondsUntilReset(secondsUntilReset), stale);
  return fmtAbsoluteWithRelative(absoluteText, relativeText);
}

function usageAllowanceWindowHtml(window, stale) {
  const label = usageAllowanceWindowLabel(window.window_duration_minutes);
  const status = usageAllowanceWindowStatus(window.used_percent);
  const width = Math.min(Math.max(window.used_percent ?? 0, 0), 100);
  const remainingText = fmtExactOrDash(window.remaining_percent);
  const resetText = usageAllowanceWindowResetText(window.resets_at, stale);
  const sourceSlotHtml = window.source_slot
    ? `<span class="muted usage-allowance-window-slot">${escapeHtml(window.source_slot)}</span>`
    : "";
  return `
    <div class="usage-allowance-window">
      <div class="usage-allowance-window-head">
        <span>${escapeHtml(label)}</span>
        ${sourceSlotHtml}
      </div>
      <div class="meter" aria-label="使用率">
        <div class="${meterClass(status)}" style="width:${width}%"></div>
      </div>
      <div class="metric-line">
        <span class="status ${statusClass(status)}">${escapeHtml(status)}</span>
        <strong>${fmtNumber(window.used_percent)}%</strong>
      </div>
      <div class="metric-line">
        <span>残り</span>
        <strong>${remainingText === "—" ? "—" : `${escapeHtml(remainingText)}%`}</strong>
      </div>
      <div class="metric-line">
        <span>reset</span>
        <strong>${escapeHtml(resetText)}</strong>
      </div>
    </div>`;
}

// DOMに触れない純粋関数: bucket1件ぶんのカードHTML。windowsが空配列の場合は
// 0%のmeterを描かず、「枠情報なし」というメタ情報だけのカードにする
// (spec: 「renders as a metadata-only card ... not as 0%」)。
function usageAllowanceBucketCardHtml(bucket) {
  const stale = bucket.status === "stale";
  const title = usageAllowanceBucketTitle(bucket);
  const badgeHtml = usageAllowanceSourceBadgeHtml(bucket.source_kind);
  const staleMarkerHtml = stale ? `<span class="status status-warn">最終取得値</span>` : "";

  const metaParts = [];
  if (bucket.plan_type) metaParts.push(`プラン: ${escapeHtml(bucket.plan_type)}`);
  if (bucket.rate_limit_reached_type) metaParts.push(`到達種別: ${escapeHtml(bucket.rate_limit_reached_type)}`);
  metaParts.push(`観測: ${escapeHtml(fmtDate(bucket.observed_at))}`);
  const metaLineHtml = `<div class="muted usage-allowance-meta">${metaParts.join(" ／ ")}</div>`;

  const windows = Array.isArray(bucket.windows) ? bucket.windows : [];
  const windowsHtml = windows.length
    ? windows.map((window) => usageAllowanceWindowHtml(window, stale)).join("")
    : `<p class="muted">枠情報なし</p>`;

  return `
    <article class="card usage-allowance-card">
      <div class="card-title">
        <h3>${escapeHtml(title)}</h3>
        <div class="card-title-actions">${badgeHtml}${staleMarkerHtml}</div>
      </div>
      ${metaLineHtml}
      ${windowsHtml}
    </article>`;
}

// DOMに触れない純粋関数: unavailableエントリ専用カード。0%/100%にも
// meterにも決してしない(spec section 5)。
function usageAllowanceUnavailableCardHtml(item) {
  const badgeHtml = usageAllowanceSourceBadgeHtml(item.source_kind);
  const staleMarkerHtml = item.status === "stale" ? `<span class="status status-warn">最終取得値</span>` : "";
  return `
    <article class="card usage-allowance-card usage-allowance-unavailable">
      <div class="card-title">
        <h3>${escapeHtml(item.provider)} / ${escapeHtml(item.product_surface)}</h3>
        <div class="card-title-actions">${badgeHtml}${staleMarkerHtml}</div>
      </div>
      <p class="status status-pending">取得不能（${escapeHtml(item.status)}）</p>
    </article>`;
}

// DOMに触れない純粋関数: provider -> product_surface -> {buckets, unavailable}
// のMapを組み立てる。payload自体が無いキーは絶対に作らない(spec: 「Do not
// invent grouping keys that are not in the payload」)。
function groupUsageAllowances(payload) {
  const providers = new Map();
  const ensureSurface = (provider, surface) => {
    if (!providers.has(provider)) providers.set(provider, new Map());
    const bySurface = providers.get(provider);
    if (!bySurface.has(surface)) bySurface.set(surface, { buckets: [], unavailable: [] });
    return bySurface.get(surface);
  };
  (payload.allowances || []).forEach((bucket) => {
    ensureSurface(bucket.provider, bucket.product_surface).buckets.push(bucket);
  });
  (payload.unavailable || []).forEach((item) => {
    ensureSurface(item.provider, item.product_surface).unavailable.push(item);
  });
  return providers;
}

function renderLimitsAllowanceView(result) {
  const target = document.querySelector("#limitsAllowanceBody");
  if (!target) return;
  if (!result || !result.ok || !result.data) {
    target.innerHTML = `<p class="muted">${escapeHtml(USAGE_ALLOWANCE_ERROR_MESSAGE)}</p>`;
    return;
  }
  const groups = groupUsageAllowances(result.data);
  if (!groups.size) {
    target.innerHTML = `<p class="muted">使用枠情報はまだありません。</p>`;
    return;
  }
  const sectionsHtml = [];
  for (const [provider, bySurface] of groups) {
    const surfacesHtml = [];
    for (const [surface, group] of bySurface) {
      const cardsHtml = [
        ...group.buckets.map(usageAllowanceBucketCardHtml),
        ...group.unavailable.map(usageAllowanceUnavailableCardHtml),
      ].join("");
      surfacesHtml.push(`
        <div class="usage-allowance-surface-group">
          <h4>${escapeHtml(surface)}</h4>
          <div class="cards">${cardsHtml}</div>
        </div>`);
    }
    sectionsHtml.push(`
      <section class="usage-allowance-provider-group">
        <h3>${escapeHtml(provider)}</h3>
        ${surfacesHtml.join("")}
      </section>`);
  }
  target.innerHTML = sectionsHtml.join("");
}

// DOMに触れない純粋関数: bucketごとに1行を作り、そのbucketの中で最も使用率の
// 高いwindowを代表として選ぶ。詳細な内訳の列挙はLimitsビューの役割であり、
// 概要は1行要約に留める(spec section 6)。
//
// (provider, product_surface)で束ねてはいけない: schemas.pyのUsageAllowanceBucket
// が明示するとおりこの組は一意キーではなく、同じ面に別sourceのbucketが並ぶ
// (例: Codexの自動取得と手動入力はどちらもopenai/work_codex)。束ねると
// 片方の観測値が画面から消える。
function usageAllowanceOverviewRows(payload) {
  return (payload.allowances || []).map((bucket) => {
    const windows = Array.isArray(bucket.windows) ? bucket.windows : [];
    let representative = null;
    windows.forEach((window) => {
      const currentPercent = representative ? representative.used_percent : -Infinity;
      if (window.used_percent > currentPercent) representative = window;
    });
    return { provider: bucket.provider, surface: bucket.product_surface, window: representative, bucket };
  });
}

// used_percentが無い場合に"未取得%"のような読めない表記を作らない。
function usageAllowanceUsedPercentText(usedPercent) {
  if (usedPercent === null || usedPercent === undefined) return "使用率不明";
  return `${fmtNumber(usedPercent)}%`;
}

function usageAllowanceOverviewRowHtml(entry) {
  const badgeHtml = usageAllowanceSourceBadgeHtml(entry.bucket.source_kind);
  const stale = entry.bucket.status === "stale";
  // staleは値がある状態なので行自体は値として描くが、生の値と見分けが
  // つかないままにはしない。
  const staleMarkerHtml = stale ? `<span class="status status-warn">最終取得値</span>` : "";
  // 同じ面に複数bucketが並び得るため、面名だけでは行を識別できない。
  // bucket名が面名と同じ(display_name/limit_idがどちらもnull)ときだけ省く。
  const bucketTitle = usageAllowanceBucketTitle(entry.bucket);
  const titleText =
    bucketTitle === entry.surface
      ? `${entry.provider} / ${entry.surface}`
      : `${entry.provider} / ${entry.surface}・${bucketTitle}`;
  const titleHtml = escapeHtml(titleText);
  if (!entry.window) {
    return `
      <div class="usage-allowance-overview-row">
        <span>${titleHtml}</span>
        <span class="muted">枠情報なし</span>
        ${staleMarkerHtml}
        ${badgeHtml}
      </div>`;
  }
  const label = usageAllowanceWindowLabel(entry.window.window_duration_minutes);
  const resetText = usageAllowanceWindowResetText(entry.window.resets_at, stale);
  return `
    <div class="usage-allowance-overview-row">
      <span>${titleHtml}</span>
      <span>${escapeHtml(label)} ${usageAllowanceUsedPercentText(entry.window.used_percent)}</span>
      <span class="muted">reset: ${escapeHtml(resetText)}</span>
      ${staleMarkerHtml}
      ${badgeHtml}
    </div>`;
}

function usageAllowanceOverviewStatusLineHtml(item) {
  const badgeHtml = usageAllowanceSourceBadgeHtml(item.source_kind);
  return `
    <div class="usage-allowance-overview-row usage-allowance-unavailable">
      <span>${escapeHtml(item.provider)} / ${escapeHtml(item.product_surface)}</span>
      <span class="status status-pending">取得不能（${escapeHtml(item.status)}）</span>
      ${badgeHtml}
    </div>`;
}

function renderOverviewView(result) {
  const generatedAtEl = document.querySelector("#overviewGeneratedAt");
  const rowsEl = document.querySelector("#overviewRows");
  const unavailableEl = document.querySelector("#overviewUnavailable");
  if (!rowsEl || !unavailableEl) return;

  if (!result || !result.ok || !result.data) {
    if (generatedAtEl) generatedAtEl.textContent = "";
    rowsEl.innerHTML = `<p class="muted">${escapeHtml(USAGE_ALLOWANCE_ERROR_MESSAGE)}</p>`;
    unavailableEl.innerHTML = "";
    return;
  }

  const payload = result.data;
  if (generatedAtEl) generatedAtEl.textContent = `最終更新: ${fmtDate(payload.generated_at)}`;

  const rows = usageAllowanceOverviewRows(payload);
  rowsEl.innerHTML = rows.length
    ? rows.map(usageAllowanceOverviewRowHtml).join("")
    : `<p class="muted">使用枠情報はまだありません。</p>`;

  // ここは「取得できていない面」だけの区画。staleなbucketは値がある行として
  // 上のリストに出しており(行内に最終取得値バッジが付く)、ここへ再掲すると
  // 同じ面が2回現れ、しかも取得不能の並びに紛れて読み違えられる。
  unavailableEl.innerHTML = (payload.unavailable || []).map(usageAllowanceOverviewStatusLineHtml).join("");
}

// ============================================================================
// アプリケーションレベルナビゲーション(概要 / 上限 / 診断 / 履歴)
//
// [data-view]を持つ全要素のhiddenプロパティを切り替えるだけで、既存section
// の並び順・id・構造は一切変更しない。location.hashを唯一の真実の情報源とし、
// 戻る/進む/リロードでも同じビューを保つ。
// ============================================================================

const APP_VIEWS = ["overview", "limits", "diagnostics", "history"];
const APP_VIEW_NAV_BUTTON_IDS = {
  overview: "navOverview",
  limits: "navLimits",
  diagnostics: "navDiagnostics",
  history: "navHistory",
};

// DOMに触れない純粋関数: location.hashの生文字列から有効なview名を決める。
// 未知/空のhashは常にoverviewへfallbackする。
function normalizeAppView(hash) {
  const view = String(hash || "").replace(/^#/, "");
  return APP_VIEWS.includes(view) ? view : "overview";
}

// DOMに触れない純粋関数: 現在のhashが既にそのviewを指しているなら書き込まない。
// 書き込み続けると、hashchange経由の描画(戻る/進む)が新しい履歴を積み直し、
// 「戻る」で前のページへ抜けられなくなる。
function shouldWriteAppViewHash(currentHash, view) {
  return String(currentHash || "") !== `#${view}`;
}

function setActiveView(view, options) {
  const { updateHash = true } = options || {};
  const normalized = normalizeAppView(view);
  document.querySelectorAll("[data-view]").forEach((el) => {
    el.hidden = el.dataset.view !== normalized;
  });
  Object.entries(APP_VIEW_NAV_BUTTON_IDS).forEach(([viewName, buttonId]) => {
    const button = document.querySelector(`#${buttonId}`);
    if (!button) return;
    if (viewName === normalized) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  if (updateHash && shouldWriteAppViewHash(location.hash, normalized)) {
    location.hash = normalized;
  }
}

function githubActionsBillingPlanLabel(planName) {
  if (planName === "free") return "Free";
  if (planName === "pro") return "Pro";
  return planName || "不明";
}

// The only text ever shown for a non-429 refresh failure — deliberately never
// derived from the response body (no `.text()`, never passed into an Error),
// so a backend error page/traceback/internal detail can never reach the DOM
// through this path. Mirrors codexRateLimitsErrorDisplay's design.
const GITHUB_ACTIONS_BILLING_GENERIC_ERROR_MESSAGE = "GitHub Actions billingの更新に失敗しました。しばらく待ってから再度お試しください。";

// DOMに触れない純粋関数: POST /api/github-actions-billing/refresh のレスポンスから
// 画面へ表示してよい内容だけを決定する。status以外の入力(response本文)は429の
// 場合の`detail.user_message`/`detail.retry_after_seconds`という固定schemaの
// 2フィールドしか読まない — それ以外は本文の中身に関わらず一切参照しない。
function githubActionsBillingErrorDisplay(status, body) {
  if (status === 429) {
    const detail = (body && body.detail) || {};
    const retryAfterSeconds = typeof detail.retry_after_seconds === "number" ? detail.retry_after_seconds : 0;
    return {
      error_type: "cooldown_active",
      user_message:
        typeof detail.user_message === "string" ? detail.user_message : GITHUB_ACTIONS_BILLING_GENERIC_ERROR_MESSAGE,
      retry_after_seconds: retryAfterSeconds,
    };
  }
  return {
    error_type: "unknown_error",
    user_message: GITHUB_ACTIONS_BILLING_GENERIC_ERROR_MESSAGE,
    retry_after_seconds: 0,
  };
}

// GitHub API Rate Limit(APIリクエスト枠)とは別概念であることを明示するため、
// 別関数・別カードとして完全に独立させる。
//
// 重要: 公式Billing usage summary API(Public Preview)のdiscountQuantityは、
// 「account included usageによるdiscount」だけでなく「publicリポジトリの
// standard runner利用」「self-hosted runner利用」のdiscountも混在すると
// GitHub公式Docsに明記されている(discountの内訳を区別するrepository/
// visibility fieldはこのendpointに存在しない)。そのため exact used /
// exact remaining / usage_percentageは常にnull("—"表示)とし、0や実数へ
// 偽装しない。表示できるのはPlanから確定できるMonthly allowanceと、
// 意味を限定した参考値(discounted/billable standard usage、
// non-included paid minutes)だけ。
function githubActionsBillingHtml(data) {
  if (!data) {
    return `<p class="muted">状態: 未取得</p>`;
  }

  if (data.error) {
    const message = escapeHtml(data.error.user_message || "取得に失敗しました");
    const lastKnownHtml = data.last_known
      ? `<p class="muted">直近の取得は失敗しました。以下は${escapeHtml(fmtGithubDate(data.last_known.collected_at))}時点の古い情報（未更新）です。</p>${githubActionsBillingCardHtml(data.last_known, true)}`
      : "";
    return `<div class="github-error">${message}</div>${lastKnownHtml}`;
  }

  if (!data.fetched) {
    return `<p class="muted">状態: 未取得</p>`;
  }

  return githubActionsBillingCardHtml(data, false);
}

function githubActionsBillingCardHtml(data, isStale) {
  const statusClass = githubActionsBillingStatusClass(data.status);
  const staleNoticeHtml = isStale ? `<p class="muted">古い情報（未更新）</p>` : "";

  if (data.status === "plan_unknown" || data.included_minutes === null || data.included_minutes === undefined) {
    return `
      ${staleNoticeHtml}
      <div class="github-overall ${statusClass}">GitHub Actions — Plan不明</div>
      <p class="form-note">Planを認識できないため月間枠を判定できません。&quot;Plan: read&quot;権限（&quot;user&quot; scope）を確認してください。</p>`;
  }

  const planLabel = escapeHtml(githubActionsBillingPlanLabel(data.plan_name));
  const allowanceText = fmtNumber(data.included_minutes);
  const discountedText = fmtExactOrDash(data.discounted_standard_minutes);
  const billableText = fmtExactOrDash(data.billable_standard_minutes);
  const nonIncludedText = fmtExactOrDash(data.paid_non_included_minutes);
  const year = escapeHtml(String(data.billing_year));
  const monthPadded = escapeHtml(String(data.billing_month).padStart(2, "0"));

  return `
    ${staleNoticeHtml}
    <div class="github-overall ${statusClass}">GitHub Actions</div>
    <div class="github-billing-summary">
      <div class="github-billing-row"><span class="github-billing-label">プラン</span><span class="github-billing-value">${planLabel}</span></div>
      <div class="github-billing-row"><span class="github-billing-label">月間枠</span><span class="github-billing-value">${allowanceText}分</span></div>
      <div class="github-billing-row"><span class="github-billing-label">参考利用</span><span class="github-billing-value">${discountedText}分</span></div>
      <div class="github-billing-row"><span class="github-billing-label">課金対象</span><span class="github-billing-value">${billableText}分</span></div>
      <div class="github-billing-row"><span class="github-billing-label">追加課金</span><span class="github-billing-value">${nonIncludedText}分</span></div>
    </div>
    <p class="github-billing-note">正確な残り分数はGitHub公式Billing summaryだけでは判定できません。</p>
    <details class="github-billing-details">
      <summary>詳細を表示</summary>
      <p class="form-note">discountにはincluded allowance消費分だけでなく、publicリポジトリのstandard runner利用やself-hosted runner利用の割引も混在するため、GitHub公式Billing summary（Public Preview）だけではexact remainingを判定できません。「参考利用」「課金対象」「追加課金」はいずれも意味を限定した参考値で、正確なquota消費量ではありません。</p>
    </details>
    <p class="muted github-billing-meta">${year}-${monthPadded} ／ 最終取得: ${escapeHtml(fmtGithubDate(data.collected_at))}</p>`;
}

function renderGithubActionsBilling(data) {
  document.querySelector("#githubActionsBillingResult").innerHTML = githubActionsBillingHtml(data);
}

// ============================================================================
// GitHub GraphQL Consumption Diagnostics(v0.1)
//
// 重要な非交渉事項: これは「時間的相関(temporal correlation)」の表示であり、
// 「正確な消費元の特定(exact attribution)」ではない。GitHub APIはGraphQL
// used差分がどのclient/process/tokenによるものかを一切公開しないため、
// 「Xが消費しました」「confirmed」「exact」という表現、および2件以上の
// Activityが重複している場合のper-session内訳(按分)は、このファイルの
// どの関数からも出力しない(そのようなデータはサーバー側にも存在しない)。
// ============================================================================

const GITHUB_GRAPHQL_DIAGNOSTICS_ATTRIBUTION_LABELS = {
  UNATTRIBUTED: "相関候補なし",
  SINGLE_ACTIVITY_CORRELATION: "単一Activityと時間的相関",
  OVERLAPPING_ACTIVITIES: "複数Activityが重複（個別内訳不可）",
  RESET_BOUNDARY: "reset境界のため差分判定不可",
  COUNTER_REGRESSION: "カウンタ減少を検出（差分判定不可）",
  FETCH_FAILED: "取得失敗",
};

// DOMに触れない純粋関数: AttributionStatus(enum値)を、非attribution的な意味を
// 保ったまま短い日本語ラベルへ変換する。未知の値は生のenum文字列をそのまま
// 表示せず「不明」にfallbackする(未翻訳のenumがuser-facingラベルとして
// 単独露出しないようにするため)。
function githubGraphqlDiagnosticsAttributionLabel(status) {
  return GITHUB_GRAPHQL_DIAGNOSTICS_ATTRIBUTION_LABELS[status] || "不明";
}

const GITHUB_GRAPHQL_DIAGNOSTICS_GENERIC_ERROR_MESSAGE =
  "GraphQL消費診断の操作に失敗しました。しばらく待ってから再度お試しください。";

const GITHUB_GRAPHQL_DIAGNOSTICS_KNOWN_ERROR_STATUSES = new Set([400, 404, 409, 502]);

// DOMに触れない純粋関数: start/stopのレスポンスから画面へ表示してよい内容だけを
// 決定する。githubActionsBillingErrorDisplayと同じ設計思想 -- statusが既知の
// 4xx/5xxで、かつbody.detailが{error_type, user_message}という固定shapeの
// 場合のみuser_messageを使う。それ以外(想定外のshape、bodyがnull、statusが
// 未知)は常に固定genericメッセージへfallbackし、bodyの中身を一切echoしない。
function githubGraphqlDiagnosticsErrorDisplay(status, body) {
  const detail = body && typeof body === "object" ? body.detail : null;
  const hasValidDetail =
    detail &&
    typeof detail === "object" &&
    typeof detail.user_message === "string" &&
    typeof detail.error_type === "string";

  if (GITHUB_GRAPHQL_DIAGNOSTICS_KNOWN_ERROR_STATUSES.has(status) && hasValidDetail) {
    return { error_type: detail.error_type, user_message: detail.user_message };
  }
  return { error_type: "unknown_error", user_message: GITHUB_GRAPHQL_DIAGNOSTICS_GENERIC_ERROR_MESSAGE };
}

function githubGraphqlDiagnosticsDisabledHtml() {
  return `
    <p class="muted">GraphQL消費診断は現在無効です。有効にするには環境変数 <code>GITHUB_GRAPHQL_DIAGNOSTICS_ENABLED=true</code> を設定してください。</p>`;
}

// DOMに触れない純粋関数: 計測開始フォームのHTML。実データに依存しない固定markup
// なので、index.html側に手書きせずここで組み立てる(このファイルの他フォーム
// (Claude Desktop Cloud / Codex手動入力)がJSではなくindex.htmlへ静的に書かれて
// いるのは、フィールド自体が固定・単一のformだから。こちらはactive_sessions
// (件数不定)を同じ結果領域内に併せて描画する必要があるため、フォームも
// 動的生成側に揃える)。
function githubGraphqlDiagnosticsStartFormHtml() {
  return `
    <form id="githubGraphqlDiagnosticsForm" class="codex-usage-form">
      <label>
        <span>Actor種別</span>
        <select id="githubGraphqlDiagnosticsActorType" name="actor_type" required>
          <option value="claude_code">Claude Code</option>
          <option value="codex">Codex</option>
          <option value="other">その他</option>
        </select>
      </label>
      <label>
        <span>ラベル</span>
        <input id="githubGraphqlDiagnosticsLabel" name="label" type="text" placeholder="例: PR #25 review" required />
      </label>
      <label>
        <span>Repository（任意）</span>
        <input id="githubGraphqlDiagnosticsRepository" name="repository" type="text" placeholder="例: owner/repo" />
      </label>
      <label>
        <span>PR番号（任意）</span>
        <input id="githubGraphqlDiagnosticsPrNumber" name="pr_number" type="number" min="1" step="1" />
      </label>
      <button id="githubGraphqlDiagnosticsSubmit" type="submit">計測開始</button>
      <div id="githubGraphqlDiagnosticsFormResult" class="codex-usage-result-slot" aria-live="polite"></div>
    </form>`;
}

// DOMに触れない純粋関数: active_sessionsの1件ぶんのカードHTML。
// lastSampleがある場合のみ「現在のGraphQL used」を出す。reset境界を跨いだ
// (lastSample.graphql_reset_at !== session.reset_at_start)場合は、古い/無意味な
// 数値を出さず、専用の判定不可メッセージにする(spec section 8)。
//
// Finding 8: 「相関状態」はsession.attribution_status(作成時のbaselineサンプル
// から一度だけ設定され、以後は更新されない)ではなく、lastSample.attribution_status
// (statusエンドポイントの最新観測)から算出する -- ただしlastSampleがこの
// sessionの計測窓を実際にカバーしている場合に限る(lastSample !== null かつ
// lastSample.graphql_reset_at === session.reset_at_start かつ
// lastSample.collected_at >= session.started_at。reset境界安全性チェックは
// 「現在のGraphQL used」で使っているものと同じ比較を流用しつつ、開始前の
// サンプルを拾わないようcollected_atの下限も追加している)。該当する
// lastSampleが無ければ、stale/不正確な値の代わりに中立な「まだ観測なし」を出す。
function githubGraphqlDiagnosticsSessionCardHtml(session, lastSample) {
  const actorType = escapeHtml(session.actor_type);
  const label = escapeHtml(session.label);
  const startedAt = escapeHtml(fmtDate(session.started_at));
  const baselineUsed = fmtNumber(session.graphql_used_start);
  const sessionId = escapeHtml(String(session.id));

  let currentUsedText;
  if (!lastSample) {
    currentUsedText = "—";
  } else if (lastSample.graphql_reset_at !== session.reset_at_start) {
    currentUsedText = "reset境界を跨いだため判定不可";
  } else {
    currentUsedText = fmtNumber(lastSample.graphql_used);
  }

  const lastSampleCoversSession =
    !!lastSample &&
    lastSample.graphql_reset_at === session.reset_at_start &&
    lastSample.collected_at >= session.started_at;
  const attributionLabel = lastSampleCoversSession
    ? escapeHtml(githubGraphqlDiagnosticsAttributionLabel(lastSample.attribution_status))
    : "まだ観測なし";

  const repositoryLine = session.repository
    ? `<div class="github-graphql-diagnostics-meta">Repository: ${escapeHtml(session.repository)}${
        session.pr_number ? ` #${escapeHtml(String(session.pr_number))}` : ""
      }</div>`
    : "";

  return `
    <div class="github-graphql-diagnostics-session" data-session-id="${sessionId}" data-attribution-status="${escapeHtml(
    session.attribution_status || ""
  )}">
      <div class="github-graphql-diagnostics-session-head">
        <span class="github-graphql-diagnostics-actor">${actorType}</span>
        <span class="github-graphql-diagnostics-label">${label}</span>
      </div>
      ${repositoryLine}
      <div class="github-graphql-diagnostics-meta">開始: ${startedAt}</div>
      <div class="github-graphql-diagnostics-meta">開始時点のGraphQL used: ${escapeHtml(baselineUsed)}</div>
      <div class="github-graphql-diagnostics-meta">現在のGraphQL used: ${escapeHtml(currentUsedText)}</div>
      <div class="github-graphql-diagnostics-meta">相関状態: ${attributionLabel}</div>
      <button type="button" class="github-graphql-diagnostics-stop" data-session-id="${sessionId}">終了</button>
    </div>`;
}

// DOMに触れない純粋関数: last_sampleの1行サマリ。deltaがnull(初回サンプルや
// reset直後など「差分が原理的に無い」ケース)は0へ偽装せず必ず"—"にする。
function githubGraphqlDiagnosticsLastSampleHtml(lastSample) {
  if (!lastSample) {
    return `<p class="muted">最終観測: 未取得</p>`;
  }
  const collectedAt = escapeHtml(fmtDate(lastSample.collected_at));
  const graphqlUsed = escapeHtml(fmtNumber(lastSample.graphql_used));
  const delta =
    lastSample.graphql_delta === null || lastSample.graphql_delta === undefined
      ? "—"
      : escapeHtml(fmtNumber(lastSample.graphql_delta));
  const attributionLabel = escapeHtml(githubGraphqlDiagnosticsAttributionLabel(lastSample.attribution_status));
  return `<p class="muted">最終観測: ${collectedAt} ／ GraphQL used ${graphqlUsed} ／ 観測された差分 ${delta} ／ ${attributionLabel}</p>`;
}

// ----------------------------------------------------------------------------
// Recent Activity Sessions(完了/計測中セッションの比較表示)
// ----------------------------------------------------------------------------

const GITHUB_GRAPHQL_DIAGNOSTICS_SESSION_STATUS_LABELS = {
  ACTIVE: "計測中",
  STOPPED: "終了（手動）",
  AUTO_STOPPED: "自動終了",
  EXHAUSTED: "枠を使い切り終了",
  ABORTED: "異常終了",
};

// DOMに触れない純粋関数: session.status(enum値)を短い日本語ラベルへ変換する。
// 未知の値は生のenum文字列を単独露出させず「不明」にfallbackする。
function githubGraphqlDiagnosticsSessionStatusLabel(status) {
  return GITHUB_GRAPHQL_DIAGNOSTICS_SESSION_STATUS_LABELS[status] || "不明";
}

const GITHUB_GRAPHQL_DIAGNOSTICS_STOP_REASON_LABELS = {
  USER_STOP: "ユーザー操作による終了",
  MAX_DURATION: "最大計測時間に到達",
  GRAPHQL_EXHAUSTED: "GraphQL枠を使い切り",
  PROCESS_RESTART: "プロセス再起動により終了",
};

// DOMに触れない純粋関数: session.stop_reason(enum値、ACTIVEなsessionではnull)を
// 短い日本語ラベルへ変換する。null/undefinedは「まだ終了していない」ことを示す
// "—"(未取得の"不明"とは区別する)。
function githubGraphqlDiagnosticsStopReasonLabel(stopReason) {
  if (stopReason === null || stopReason === undefined) return "—";
  return GITHUB_GRAPHQL_DIAGNOSTICS_STOP_REASON_LABELS[stopReason] || "不明";
}

// DOMに触れない純粋関数: started_at/ended_atから人間可読な計測時間を作る。
// ended_atがまだ無い(計測中の)sessionはdurationを計算せず「計測中」を返す。
function githubGraphqlDiagnosticsSessionDurationText(startedAt, endedAt) {
  if (!endedAt) return "計測中";
  const startMs = new Date(startedAt).getTime();
  const endMs = new Date(endedAt).getTime();
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return "—";
  return fmtDurationJa((endMs - startMs) / 1000);
}

// DOMに触れない純粋関数: 完了/計測中セッション1件ぶんの比較行HTML。
// session.attribution_status(baseline取得時に一度だけ設定され、以後更新
// されない値)はここでは絶対に表示しない -- 相関状態の履歴はSample Timeline
// (githubGraphqlDiagnosticsTimelineTableHtml)側の役割とする(Finding 8)。
//
// observed session delta(graphql_delta_total)がnullのとき、開始/終了の
// どちらのGraphQL usedも取得できている(=「未計測」ではない)場合は、
// reset境界またはcounter regressionでdelta判定ができなかったことを示す
// 専用の注記を出す(app/github_graphql_diagnostics.pyのcompute_session_total_delta
// 参照)。どちらの原因だったかは判別できないため、断定はしない。
function githubGraphqlDiagnosticsSessionComparisonRowHtml(session) {
  const actorType = escapeHtml(session.actor_type);
  const label = escapeHtml(session.label);
  const sessionId = escapeHtml(String(session.id));

  const repositoryLine =
    session.repository || session.pr_number
      ? `<div class="github-graphql-diagnostics-meta">Repository: ${escapeHtml(session.repository || "")}${
          session.pr_number ? ` #${escapeHtml(String(session.pr_number))}` : ""
        }</div>`
      : "";

  const startedAt = escapeHtml(fmtDate(session.started_at));
  const endedAt = session.ended_at ? escapeHtml(fmtDate(session.ended_at)) : "計測中";
  const duration = escapeHtml(githubGraphqlDiagnosticsSessionDurationText(session.started_at, session.ended_at));

  const startUsed = escapeHtml(fmtExactOrDash(session.graphql_used_start));
  const endUsed = escapeHtml(fmtExactOrDash(session.graphql_used_end));

  const hasBothEndpointsMeasured =
    session.graphql_used_start !== null &&
    session.graphql_used_start !== undefined &&
    session.graphql_used_end !== null &&
    session.graphql_used_end !== undefined;

  let deltaText;
  if (session.graphql_delta_total !== null && session.graphql_delta_total !== undefined) {
    deltaText = fmtNumber(session.graphql_delta_total);
  } else if (hasBothEndpointsMeasured) {
    deltaText = "reset境界または異常検出のため差分判定不可";
  } else {
    deltaText = "—";
  }
  deltaText = escapeHtml(deltaText);

  const maxIntervalDelta = escapeHtml(fmtExactOrDash(session.max_valid_interval_delta));

  const statusLabel = githubGraphqlDiagnosticsSessionStatusLabel(session.status);
  const stopReasonLabel = githubGraphqlDiagnosticsStopReasonLabel(session.stop_reason);
  const statusText = escapeHtml(stopReasonLabel === "—" ? statusLabel : `${statusLabel} / ${stopReasonLabel}`);

  return `
    <div class="github-graphql-diagnostics-history-row" data-session-id="${sessionId}">
      <div class="github-graphql-diagnostics-session-head">
        <span class="github-graphql-diagnostics-actor">${actorType}</span>
        <span class="github-graphql-diagnostics-label">${label}</span>
      </div>
      ${repositoryLine}
      <div class="github-graphql-diagnostics-meta">開始: ${startedAt} ／ 終了: ${endedAt} ／ 計測時間: ${duration}</div>
      <div class="github-graphql-diagnostics-meta">開始時点のGraphQL used: ${startUsed} ／ 終了時点のGraphQL used: ${endUsed}</div>
      <div class="github-graphql-diagnostics-meta">観測されたSession全体の差分: ${deltaText}</div>
      <div class="github-graphql-diagnostics-meta">最大区間差分: ${maxIntervalDelta}</div>
      <div class="github-graphql-diagnostics-meta">状態: ${statusText}</div>
    </div>`;
}

// DOMに触れない純粋関数: GET /api/github-graphql-diagnostics/sessions のitems配列
// (完了済みも計測中も両方含む)から、「Recent Activity Sessions」比較表全体の
// HTMLを組み立てる。空配列のときは表の代わりに固定メッセージを出す。
function githubGraphqlDiagnosticsSessionComparisonTableHtml(sessions) {
  const rows = Array.isArray(sessions) ? sessions : [];
  if (!rows.length) {
    return `<p class="muted">履歴はありません。</p>`;
  }
  const body = rows.map((session) => githubGraphqlDiagnosticsSessionComparisonRowHtml(session)).join("");
  return `
    <h3>Recent Activity Sessions</h3>
    <p class="muted">相関状態(の推移)はSample Timelineで確認できます。ここではstatus/stop_reasonのみを表示します。</p>
    <div class="github-graphql-diagnostics-history">${body}</div>`;
}

// ----------------------------------------------------------------------------
// Sample Timeline
// ----------------------------------------------------------------------------

// DOMに触れない純粋関数: sessions配列のうち、sample.collected_atの瞬間に
// activeだったものだけを返す。app/github_graphql_diagnostics.pyの
// count_active_sessions_at/ActivityWindowと同じ境界含む(boundary-inclusive)
// 判定 -- started_at <= instant かつ (ended_at is null または ended_at >= instant)
// -- をミラーする。fmtDateと同じくnew Date(...)経由の数値比較にすることで、
// このファイル内での日時パース方法を統一する。
function githubGraphqlDiagnosticsActiveSessionsAtSample(sample, sessions) {
  const rows = Array.isArray(sessions) ? sessions : [];
  const instant = new Date(sample.collected_at).getTime();
  return rows.filter((session) => {
    const startedAt = new Date(session.started_at).getTime();
    const endedAt =
      session.ended_at === null || session.ended_at === undefined ? null : new Date(session.ended_at).getTime();
    // A session's own start-baseline sample is a special case: the backend
    // (app/github_graphql_diagnostics_controller.py's `_start_session_sync`)
    // deliberately classifies that specific sample's attribution using only
    // PRE-EXISTING active sessions, excluding the session that is about to
    // be created -- its baseline interval is the time BEFORE this instant,
    // when it did not exist yet. Without this exclusion, this function's
    // ordinary boundary-inclusive check (startedAt <= instant) would still
    // list that session as "active", contradicting the sample's own
    // attribution_status (e.g. showing 2 Active Activity labels next to a
    // stored "SINGLE_ACTIVITY_CORRELATION"/single-activity classification).
    const isOwnStartBaselineSample = sample.trigger_session_id === session.id && startedAt === instant;
    if (isOwnStartBaselineSample) {
      return false;
    }
    return startedAt <= instant && (endedAt === null || endedAt >= instant);
  });
}

// DOMに触れない純粋関数: サンプル1件ぶんのタイムライン行HTML。activeLabelsは
// githubGraphqlDiagnosticsActiveSessionsAtSampleの結果から呼び出し側が
// 取り出したlabel文字列の配列 -- ここでは複数件あってもラベルを列挙する
// だけで、per-session数値内訳(按分)は一切計算・表示しない。
function githubGraphqlDiagnosticsTimelineRowHtml(sample, activeLabels) {
  const time = escapeHtml(fmtDate(sample.collected_at));
  const used = escapeHtml(fmtExactOrDash(sample.graphql_used));
  const delta = escapeHtml(fmtExactOrDash(sample.graphql_delta));
  const labels = Array.isArray(activeLabels) ? activeLabels : [];
  const activityText = labels.length ? escapeHtml(labels.join(", ")) : "—";
  const attributionLabel = escapeHtml(githubGraphqlDiagnosticsAttributionLabel(sample.attribution_status));

  return `
    <div class="github-graphql-diagnostics-timeline-row">
      <div class="github-graphql-diagnostics-meta">時刻: ${time}</div>
      <div class="github-graphql-diagnostics-meta">GraphQL used: ${used}</div>
      <div class="github-graphql-diagnostics-meta">観測された差分: ${delta}</div>
      <div class="github-graphql-diagnostics-meta">Active Activity: ${activityText}</div>
      <div class="github-graphql-diagnostics-meta">相関状態: ${attributionLabel}</div>
    </div>`;
}

// DOMに触れない純粋関数: GET /api/github-graphql-diagnostics/samples のitems配列
// (最新50件を想定)と、GET /api/github-graphql-diagnostics/sessions のitems配列
// から、Sample Timeline全体のHTMLを組み立てる。sample.fetch_statusが
// "reset_boundary"/"counter_regression"/"fetch_failed"/"no_previous"のいずれで
// あってもgraphql_deltaはnullであり、"—"に落ちるだけでcrash/undefined/NaNには
// ならない(fmtExactOrDash経由)。空配列のときは表の代わりに固定メッセージを出す。
function githubGraphqlDiagnosticsTimelineTableHtml(samples, sessions) {
  const rows = Array.isArray(samples) ? samples : [];
  if (!rows.length) {
    return `<p class="muted">サンプルはありません。</p>`;
  }
  const sessionRows = Array.isArray(sessions) ? sessions : [];
  const body = rows
    .map((sample) => {
      const activeSessions = githubGraphqlDiagnosticsActiveSessionsAtSample(sample, sessionRows);
      const activeLabels = activeSessions.map((session) => session.label);
      return githubGraphqlDiagnosticsTimelineRowHtml(sample, activeLabels);
    })
    .join("");
  return `
    <h3>Sample Timeline</h3>
    <div class="github-graphql-diagnostics-timeline">${body}</div>`;
}

// DOMに触れない純粋関数: GET /api/github-graphql-diagnostics のレスポンスから
// パネル全体のHTMLを組み立てる。data.enabled === falseのときはstart formすら
// 出さない(無効時に開始操作を誘発しないため)。
function githubGraphqlDiagnosticsRenderPanel(data) {
  if (!data) {
    return `<p class="muted">状態: 未取得</p>`;
  }
  if (data.enabled === false) {
    return githubGraphqlDiagnosticsDisabledHtml();
  }

  const lastSample = data.last_sample || null;
  const sessions = Array.isArray(data.active_sessions) ? data.active_sessions : [];
  const sessionsHtml = sessions.length
    ? sessions.map((session) => githubGraphqlDiagnosticsSessionCardHtml(session, lastSample)).join("")
    : `<p class="muted">計測中のActivityはありません。</p>`;

  return `
    ${githubGraphqlDiagnosticsStartFormHtml()}
    <div id="githubGraphqlDiagnosticsSessions">
      ${sessionsHtml}
    </div>
    ${githubGraphqlDiagnosticsLastSampleHtml(lastSample)}`;
}

function renderGithubGraphqlDiagnostics(data) {
  document.querySelector("#githubGraphqlDiagnosticsResult").innerHTML = githubGraphqlDiagnosticsRenderPanel(data);
}

async function refreshGithubGraphqlDiagnostics() {
  const data = await api("/api/github-graphql-diagnostics");
  state.githubGraphqlDiagnostics = data;
  renderGithubGraphqlDiagnostics(data);
  return data;
}

// Recent Activity Sessions + Sample Timelineをまとめて描画する。sessionsは
// タイムライン側の「そのサンプル時点でどのActivityがactiveだったか」の判定にも
// 使うため、1回のfetchで両方の描画に使い回す(2回目のsessions fetchはしない)。
function renderGithubGraphqlDiagnosticsSessions(sessions, samples) {
  const sessionsTarget = document.querySelector("#githubGraphqlDiagnosticsSessionsResult");
  if (sessionsTarget) {
    sessionsTarget.innerHTML = githubGraphqlDiagnosticsSessionComparisonTableHtml(sessions);
  }
  const timelineTarget = document.querySelector("#githubGraphqlDiagnosticsTimelineResult");
  if (timelineTarget) {
    timelineTarget.innerHTML = githubGraphqlDiagnosticsTimelineTableHtml(samples, sessions);
  }
}

// 状態パネル本体(GET /api/github-graphql-diagnostics)とは別の、独立した
// fetch/再描画サイクル。sessions/samplesの履歴は状態パネルほど頻繁に
// 更新される必要が無いため、loadAll()の主Promise.allには含めない
// (ここが失敗してもメインダッシュボード側は壊さない -- 呼び出し側で
// catchする設計)。GET /sessions?limit=20、GET /samples?limit=50 は
// どちらも読み取り専用でstart/stopのような副作用は無い。
async function refreshGithubGraphqlDiagnosticsSessions() {
  const [sessionsResponse, samplesResponse] = await Promise.all([
    api("/api/github-graphql-diagnostics/sessions?limit=20"),
    api("/api/github-graphql-diagnostics/samples?limit=50"),
  ]);
  const sessions = Array.isArray(sessionsResponse.items) ? sessionsResponse.items : [];
  const samples = Array.isArray(samplesResponse.items) ? samplesResponse.items : [];
  renderGithubGraphqlDiagnosticsSessions(sessions, samples);
  return { sessions, samples };
}

function applyFiltersAndSort(rows) {
  const serviceText = document.querySelector("#filterService").value.trim().toLowerCase();
  const accountType = document.querySelector("#filterAccountType").value;
  const status = document.querySelector("#filterStatus").value;
  const sortBy = document.querySelector("#sortBy").value;

  const filtered = rows.filter((row) => {
    const matchesService = !serviceText || row.service_name.toLowerCase().includes(serviceText);
    const matchesAccount = !accountType || row.account_type === accountType;
    const matchesStatus = !status || row.status === status;
    return matchesService && matchesAccount && matchesStatus;
  });

  filtered.sort((a, b) => {
    if (sortBy === "reset_asc") return dateValue(a.next_reset_at, Infinity) - dateValue(b.next_reset_at, Infinity);
    if (sortBy === "service_asc") return `${a.service_name} ${a.model_name}`.localeCompare(`${b.service_name} ${b.model_name}`, "ja");
    if (sortBy === "updated_desc") return dateValue(b.last_updated_at, -Infinity) - dateValue(a.last_updated_at, -Infinity);
    return (b.usage_percent ?? -1) - (a.usage_percent ?? -1);
  });

  return filtered;
}

async function loadAll() {
  const [
    services,
    limits,
    dashboard,
    alerts,
    history,
    collectorRuns,
    githubRateLimit,
    githubActionsBilling,
    githubGraphqlDiagnostics,
    claudeDesktopCloudUsage,
    codexUsage,
    codexRateLimits,
    usageAllowances,
  ] = await Promise.all([
    api("/api/services"),
    api("/api/limits"),
    api("/api/dashboard"),
    api("/api/alerts"),
    api("/api/usage-records"),
    api("/api/collector-runs"),
    api("/api/github-rate-limit"),
    api("/api/github-actions-billing"),
    // GET /api/github-graphql-diagnosticsは保存済みのcontroller/sampler状態を
    // 返すだけの読み取り専用endpoint(GET /api/github-rate-limitと同じ性質)であり、
    // GitHub Rate Limit/Actions Billingの「ボタンを押すまで取得しない」action系
    // 更新とは違う -- ページ表示のたびに呼んでよい。start/stopは絶対にここから
    // 呼ばない(明示的なボタン操作でのみ呼ぶ)。
    api("/api/github-graphql-diagnostics"),
    api("/api/claude-code-usage/manual"),
    api("/api/codex-usage"),
    api("/api/codex-rate-limits"),
    // 他のPromise.all要素とは異なりapi()(throwする)ではなくfetchAllowancesSafe()
    // (throwしない)を使う: この1エンドポイントの失敗でPromise.all全体を落とし、
    // 既存ダッシュボード全体を壊すことを避けるため(spec section 5)。
    fetchAllowancesSafe(),
  ]);
  state.dashboard = dashboard;
  state.limits = limits;
  state.history = history;
  state.collectorRuns = collectorRuns;
  state.githubGraphqlDiagnostics = githubGraphqlDiagnostics;
  renderSelects(services, limits);
  renderDashboard();
  renderAlerts(alerts);
  renderHistory();
  renderCollectorRuns();
  renderGithubRateLimit(githubRateLimit);
  renderGithubActionsBilling(githubActionsBilling);
  renderGithubGraphqlDiagnostics(githubGraphqlDiagnostics);
  renderClaudeDesktopCloudUsage(claudeDesktopCloudUsage);
  renderCodexUsage(codexUsage);
  renderCodexRateLimits(codexRateLimits);
  renderOverviewView(usageAllowances);
  renderLimitsAllowanceView(usageAllowances);
}

async function refreshCollectorRuns() {
  state.collectorRuns = await api("/api/collector-runs");
  renderCollectorRuns();
}

let githubCooldownIntervalId = null;

function stopGithubCooldownCountdown() {
  if (githubCooldownIntervalId) {
    clearInterval(githubCooldownIntervalId);
    githubCooldownIntervalId = null;
  }
}

function startGithubCooldownCountdown(retryAfterSeconds) {
  stopGithubCooldownCountdown();
  const button = document.querySelector("#githubRateLimitRefresh");
  let remaining = Math.max(0, Math.ceil(retryAfterSeconds));
  const tick = () => {
    if (remaining <= 0) {
      stopGithubCooldownCountdown();
      button.disabled = false;
      button.textContent = "更新";
      return;
    }
    button.disabled = true;
    button.textContent = `更新（あと${remaining}秒）`;
    remaining -= 1;
  };
  tick();
  githubCooldownIntervalId = setInterval(tick, 1000);
}

let githubActionsBillingCooldownIntervalId = null;

function stopGithubActionsBillingCooldownCountdown() {
  if (githubActionsBillingCooldownIntervalId) {
    clearInterval(githubActionsBillingCooldownIntervalId);
    githubActionsBillingCooldownIntervalId = null;
  }
}

function startGithubActionsBillingCooldownCountdown(retryAfterSeconds) {
  stopGithubActionsBillingCooldownCountdown();
  const button = document.querySelector("#githubActionsBillingRefresh");
  let remaining = Math.max(0, Math.ceil(retryAfterSeconds));
  const tick = () => {
    if (remaining <= 0) {
      stopGithubActionsBillingCooldownCountdown();
      button.disabled = false;
      button.textContent = "更新";
      return;
    }
    button.disabled = true;
    button.textContent = `更新（あと${remaining}秒）`;
    remaining -= 1;
  };
  tick();
  githubActionsBillingCooldownIntervalId = setInterval(tick, 1000);
}

// The only text ever shown for a non-429 refresh failure — deliberately never
// derived from the response body, so a backend error page/traceback/JSON-RPC
// error text can never reach the DOM through this path.
const CODEX_RATE_LIMITS_GENERIC_ERROR_MESSAGE = "Codex使用枠の取得に失敗しました。しばらく待ってから再度お試しください。";

// DOMに触れない純粋関数: POST /api/codex-rate-limits/refresh のレスポンスから
// 画面へ表示してよい内容だけを決定する。status以外の入力(response本文)は429の
// 場合の`detail.user_message`/`detail.retry_after_seconds`という固定schemaの
// 2フィールドしか読まない — それ以外は本文の中身に関わらず一切参照しない。
// これにより、500本文にtoken風文字列・Traceback・JSON-RPC error風の文字列が
// 含まれていても、あるいはbodyがnull(JSON parse失敗・network error)でも、
// 返るuser_messageは常にこの2種類の固定文言のいずれかになる。
function codexRateLimitsErrorDisplay(status, body) {
  if (status === 429) {
    const detail = (body && body.detail) || {};
    const retryAfterSeconds = typeof detail.retry_after_seconds === "number" ? detail.retry_after_seconds : 0;
    return {
      error_type: "cooldown_active",
      user_message: typeof detail.user_message === "string" ? detail.user_message : CODEX_RATE_LIMITS_GENERIC_ERROR_MESSAGE,
      retry_after_seconds: retryAfterSeconds,
    };
  }
  return {
    error_type: "unknown_error",
    user_message: CODEX_RATE_LIMITS_GENERIC_ERROR_MESSAGE,
    retry_after_seconds: 0,
  };
}

let codexRateLimitsCooldownIntervalId = null;

function stopCodexRateLimitsCooldownCountdown() {
  if (codexRateLimitsCooldownIntervalId) {
    clearInterval(codexRateLimitsCooldownIntervalId);
    codexRateLimitsCooldownIntervalId = null;
  }
}

function startCodexRateLimitsCooldownCountdown(retryAfterSeconds) {
  stopCodexRateLimitsCooldownCountdown();
  const button = document.querySelector("#codexRateLimitsRefresh");
  let remaining = Math.max(0, Math.ceil(retryAfterSeconds));
  const tick = () => {
    if (remaining <= 0) {
      stopCodexRateLimitsCooldownCountdown();
      button.disabled = false;
      button.textContent = "今すぐ更新";
      return;
    }
    button.disabled = true;
    button.textContent = `今すぐ更新（あと${remaining}秒）`;
    remaining -= 1;
  };
  tick();
  codexRateLimitsCooldownIntervalId = setInterval(tick, 1000);
}

function renderSelects(services, limits) {
  document.querySelector("#serviceSelect").innerHTML = services
    .map((s) => `<option value="${s.id}">${escapeHtml(s.name)} / ${escapeHtml(s.plan_name)}</option>`)
    .join("");
  document.querySelector("#limitSelect").innerHTML = limits
    .map((l) => `<option value="${l.id}">#${l.id} ${escapeHtml(l.model_name)} / ${escapeHtml(l.limit_type)}</option>`)
    .join("");
}

// Claude Desktop Cloud usage is manual-only: this only reflects the last value
// the user typed in, never anything scraped from Claude Desktop or read from
// Claude's own session/transcript files. See docs/claude-code-usage-bridge.md
// for why a Cloud-environment Code session can't update the CLI statusLine
// cache directly, and why this manual fallback exists as a separate cache
// from `claude-code-usage.json`.
function renderClaudeDesktopCloudUsage(data) {
  state.claudeDesktopCloudUsage = data;
  const lastConfirmedEl = document.querySelector("#claudeDesktopCloudUsageLastConfirmed");
  if (!data || !data.available) {
    lastConfirmedEl.textContent = "最終手動確認: 未入力";
    return;
  }
  const staleSuffix = data.stale ? "（古い可能性があります）" : "";
  lastConfirmedEl.textContent = `最終手動確認: ${fmtDate(data.observed_at)}${staleSuffix}`;

  const fiveHour = data.five_hour;
  const sevenDay = data.seven_day;
  if (fiveHour) {
    document.querySelector("#claudeDesktopCloudFiveHourRemaining").value = fiveHour.remaining_percentage;
    document.querySelector("#claudeDesktopCloudFiveHourResetsAt").value = toDatetimeLocalValue(fiveHour.resets_at);
  }
  if (sevenDay) {
    document.querySelector("#claudeDesktopCloudSevenDayRemaining").value = sevenDay.remaining_percentage;
    document.querySelector("#claudeDesktopCloudSevenDayResetsAt").value = toDatetimeLocalValue(sevenDay.resets_at);
  }
}

// Codex usage is manual-only: this only reflects the last value the user typed
// in, never anything fetched from Codex itself.
function renderCodexUsage(data) {
  state.codexUsage = data;
  const lastConfirmedEl = document.querySelector("#codexUsageLastConfirmed");
  if (!data || !data.available) {
    lastConfirmedEl.textContent = "最終手動確認: 未入力";
    return;
  }
  const staleSuffix = data.stale ? "（古い可能性があります）" : "";
  lastConfirmedEl.textContent = `最終手動確認: ${fmtDate(data.observed_at)}${staleSuffix}`;

  const fiveHour = data.five_hour;
  const weekly = data.weekly;
  if (fiveHour) {
    document.querySelector("#codexFiveHourRemaining").value = fiveHour.remaining_percentage;
    document.querySelector("#codexFiveHourResetsAt").value = toDatetimeLocalValue(fiveHour.resets_at);
  }
  if (weekly) {
    document.querySelector("#codexWeeklyRemaining").value = weekly.remaining_percentage;
    document.querySelector("#codexWeeklyResetsAt").value = toDatetimeLocalValue(weekly.resets_at);
  }
}

// 自動更新間隔は「あと」を伴わない期間の長さそのものなので、fmtDurationJaの結果をそのまま使う。
function fmtMinutesFromSeconds(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds)) return "不明";
  return fmtDurationJa(Math.max(seconds, 0));
}

// Codex App Server(account/rateLimits/read)の自動取得状態のみを表示する。
// 実際のカード表示・fallback判定は監視用ダッシュボード側(resolveCodexDisplay)が担い、
// ここでは「今どの状態か」を確認できれば十分な最小表示にとどめる。
// 画面上のタイマー表示は/api/codex-rate-limitsのGET結果を表示するだけで、ここから
// 更新系リクエストを送ることはない(定期更新はサーバー側schedulerが行う)。
function renderCodexRateLimits(data) {
  state.codexRateLimits = data;
  const resultEl = document.querySelector("#codexRateLimitsResult");
  if (!resultEl) return;
  if (!data) {
    resultEl.innerHTML = "";
    return;
  }

  const statusLabel = data.available ? (data.stale ? "最終自動取得値（古い可能性あり）" : "自動取得成功") : "未取得";
  const currentSource = data.available ? "codex_app_server" : data.fallback_available ? data.fallback_source : "未取得";
  const lastAttemptText = data.observed_at ? fmtDate(data.observed_at) : "未実行";
  const errorHtml = data.error_type
    ? `<div class="codex-usage-error">${escapeHtml(data.user_message || "")}</div>`
    : "";

  const autoRefreshEnabledText = data.auto_refresh_enabled ? "有効" : "無効";
  const autoRefreshIntervalText = fmtMinutesFromSeconds(data.auto_refresh_interval_seconds);
  const nextAutoRefreshText = data.next_auto_refresh_at
    ? fmtAbsoluteWithRelative(fmtDate(data.next_auto_refresh_at), fmtAppScheduleRelative(data.next_auto_refresh_at))
    : "未定";
  const lastAutoAttemptText = data.last_auto_refresh_attempt_at ? fmtDate(data.last_auto_refresh_attempt_at) : "未実行";
  const lastAutoSuccessText = data.last_auto_refresh_success_at ? fmtDate(data.last_auto_refresh_success_at) : "未成功";

  resultEl.innerHTML = `
    <div class="codex-rate-limits-status">
      <div>自動取得状態: ${escapeHtml(statusLabel)}</div>
      <div>最終自動取得時刻: ${escapeHtml(lastAttemptText)}</div>
      <div>現在表示中のsource: ${escapeHtml(currentSource)}</div>
    </div>
    <div class="codex-rate-limits-status codex-rate-limits-periodic">
      <div>自動更新: ${escapeHtml(autoRefreshEnabledText)}</div>
      <div>更新間隔: ${escapeHtml(autoRefreshIntervalText)}</div>
      <div>次回自動更新予定: ${escapeHtml(nextAutoRefreshText)}</div>
      <div>最終自動更新試行: ${escapeHtml(lastAutoAttemptText)}</div>
      <div>最終成功: ${escapeHtml(lastAutoSuccessText)}</div>
    </div>
    ${errorHtml}
  `;
}

function renderDashboard() {
  const rows = applyFiltersAndSort(state.dashboard);
  document.querySelector("#resultCount").textContent = `${rows.length} / ${state.dashboard.length}`;
  renderCards(rows);
}

function renderCards(rows) {
  const cards = document.querySelector("#cards");
  if (!rows.length) {
    cards.innerHTML = `<div class="card empty">条件に一致する項目はありません。</div>`;
    return;
  }
  cards.innerHTML = rows.map(renderCard).join("");
}

function renderCard(row) {
  const hasMax = row.max_value !== null && row.max_value !== undefined;
  const percent = row.usage_percent ?? 0;
  const width = Math.min(Math.max(percent, 0), 100);
  const usageText = `${fmtNumber(row.used_value)} / ${hasMax ? fmtNumber(row.max_value) : "未登録"} ${escapeHtml(row.unit)}`;

  const usageBlock = hasMax
    ? `
      <div class="meter" aria-label="使用率">
        <div class="${meterClass(row.status)}" style="width:${width}%"></div>
      </div>
      <div class="metric-line">
        <span>使用率</span>
        <strong>${fmtNumber(row.usage_percent)}%</strong>
      </div>
    `
    : `<div class="manual-required">使用率計算には上限値の登録が必要です。</div>`;

  const isEditing = state.editingLimitId === row.limit_id;
  const editingLimit = isEditing ? state.limits.find((l) => l.id === row.limit_id) : null;

  return `
    <article class="card">
      <div class="card-title">
        <div>
          <h2>${escapeHtml(row.service_name)}</h2>
          <div class="muted">${escapeHtml(row.provider)} / ${escapeHtml(row.account_type)}</div>
        </div>
        <div class="card-title-actions">
          <span class="status ${statusClass(row.status)}">${escapeHtml(row.status)}</span>
          ${!isEditing ? `<button type="button" class="edit-limit-button" data-limit-id="${row.limit_id}">編集</button>` : ""}
        </div>
      </div>

      ${
        isEditing && editingLimit
          ? limitEditFormHtml(editingLimit)
          : `
      <dl class="details">
        <div><dt>プラン</dt><dd>${escapeHtml(row.plan_name)}</dd></div>
        <div><dt>モデル</dt><dd>${escapeHtml(row.model_name)}</dd></div>
        <div><dt>制限種別</dt><dd>${escapeHtml(row.limit_type)}</dd></div>
        <div><dt>取得元</dt><dd><span class="source-badge ${sourceTypeClass(row.source_type)}">${escapeHtml(sourceTypeLabel(row.source_type))}</span></dd></div>
        <div><dt>使用量 / 上限</dt><dd>${usageText}</dd></div>
        <div><dt>残量</dt><dd>${hasMax ? `${fmtNumber(row.remaining_value)} ${escapeHtml(row.unit)}` : "未取得"}</dd></div>
      </dl>

      ${usageBlock}

      <div class="timestamps">
        <div><span>次回リセット</span><strong>${fmtDate(row.next_reset_at)}</strong></div>
        <div><span>最終更新</span><strong>${fmtDate(row.last_updated_at)}</strong></div>
      </div>
      `
      }
    </article>
  `;
}

function limitEditFormHtml(limit) {
  const maxValueValue = limit.max_value === null || limit.max_value === undefined ? "" : limit.max_value;
  const resetTypes = ["hours", "days", "weeks", "months", "manual"];
  const isManual = limit.reset_interval_type === "manual";
  return `
    <form class="edit-limit-form" data-limit-id="${limit.id}">
      <label>
        <span>表示名</span>
        <input name="model_name" value="${escapeHtml(limit.model_name)}" required />
      </label>
      <label>
        <span>上限値</span>
        <input name="max_value" type="number" step="0.01" value="${escapeHtml(String(maxValueValue))}" placeholder="不明なら空欄" />
      </label>
      <label>
        <span>単位</span>
        <input name="unit" value="${escapeHtml(limit.unit)}" required />
      </label>
      <label>
        <span>リセット種別</span>
        <select name="reset_interval_type" class="reset-interval-type-input">
          ${resetTypes
            .map(
              (type) =>
                `<option value="${type}" ${limit.reset_interval_type === type ? "selected" : ""}>${type}</option>`,
            )
            .join("")}
        </select>
      </label>
      <label>
        <span>リセット間隔</span>
        <input
          name="reset_interval_value"
          class="reset-interval-value-input"
          type="number"
          min="1"
          value="${limit.reset_interval_value}"
          ${isManual ? "disabled" : ""}
        />
      </label>
      <label>
        <span>次回リセット日時</span>
        <input
          name="next_reset_at"
          class="next-reset-at-input"
          type="datetime-local"
          value="${isManual ? "" : toDatetimeLocalValue(limit.next_reset_at)}"
          ${isManual ? "disabled" : ""}
        />
      </label>
      <div id="editLimitError-${limit.id}" class="edit-limit-error"></div>
      <div class="edit-limit-actions">
        <button type="submit">保存</button>
        <button type="button" class="cancel-edit-limit" data-limit-id="${limit.id}">キャンセル</button>
      </div>
    </form>
  `;
}

function renderAlerts(rows) {
  document.querySelector("#alerts").innerHTML =
    rows
      .map((a) => `<div class="row"><strong>${escapeHtml(a.alert_level)}</strong> ${escapeHtml(a.message)}<div class="muted">${fmtDate(a.next_reset_at)}</div></div>`)
      .join("") || `<div class="muted">現在のアラートはありません。</div>`;
}

function filteredHistoryRows() {
  const mode = document.querySelector("#historyFilter").value;
  if (mode === "manual") return state.history.filter((row) => row.source_type === "manual");
  if (mode === "adjust") return state.history.filter(isAdjustmentRecord);
  if (mode === "openai") return state.history.filter((row) => row.source_type === "api_openai_management");
  if (mode === "gemini") return state.history.filter((row) => row.source_type === "api_gemini_management");
  if (mode === "claude") return state.history.filter((row) => row.source_type === "api_claude_management");
  if (mode === "api") return state.history.filter((row) => isApiSource(row.source_type));
  return state.history;
}

function renderHistory() {
  const rows = filteredHistoryRows();
  document.querySelector("#history").innerHTML =
    rows
      .slice(0, 30)
      .map((r) => {
        const adjustment = isAdjustmentRecord(r);
        const value = Number(r.used_value);
        const sign = value > 0 ? "+" : "";
        const valueClass = adjustment && value < 0 ? "history-value-negative" : adjustment ? "history-value-adjust" : "";
        return `
          <div class="row history-row ${adjustment ? "history-adjustment" : ""} ${isApiSource(r.source_type) ? "history-api" : ""}">
            <div class="history-title">
              <strong>${escapeHtml(r.service_name)} / ${escapeHtml(r.model_name)} / ${escapeHtml(r.limit_type)}</strong>
              ${adjustment ? `<span class="adjustment-label">補正</span>` : ""}
            </div>
            <div class="history-amount ${valueClass}">${sign}${fmtNumber(value)} ${escapeHtml(r.unit)}</div>
            <div class="history-source">取得元: <span class="source-badge ${sourceTypeClass(r.source_type)}">${escapeHtml(sourceTypeLabel(r.source_type))}</span></div>
            <div class="muted">recorded_at: ${fmtDate(r.recorded_at)}</div>
            <div>${escapeHtml(r.note ?? "")}</div>
          </div>
        `;
      })
      .join("") || `<div class="muted">使用履歴はありません。</div>`;
}

function renderCollectorRuns() {
  const target = document.querySelector("#collectorRuns");
  if (!target) return;
  const rows = state.collectorRuns.slice(0, 10);
  target.innerHTML =
    rows
      .map(
        (run) => `
          <div class="row collector-run">
            <div class="collector-run-title">
              <strong>${escapeHtml(run.vendor)}</strong>
              <span class="collector-status ${collectorStatusClass(run.status)}">${escapeHtml(run.status)}</span>
            </div>
            <div class="collector-run-grid">
              <span>dry_run: ${run.dry_run}</span>
              <span>取得件数: ${fmtNumber(run.records_found)}</span>
              <span>保存件数: ${fmtNumber(run.records_saved)}</span>
              <span>開始: ${fmtDate(run.started_at)}</span>
              <span>終了: ${fmtDate(run.finished_at)}</span>
            </div>
            ${run.error_message ? `<div class="collector-error">${escapeHtml(run.error_message)}</div>` : ""}
          </div>
        `,
      )
      .join("") || `<div class="muted">Collector実行履歴はありません。</div>`;
}

function renderCollectorResult(run) {
  document.querySelector("#collectorResult").innerHTML = `
    <div class="collector-result">
      <div><strong>${escapeHtml(run.vendor)}</strong> <span class="collector-status ${collectorStatusClass(run.status)}">${escapeHtml(run.status)}</span></div>
      <div>取得件数: ${fmtNumber(run.records_found)}</div>
      <div>保存件数: ${fmtNumber(run.records_saved)}</div>
      <div>開始日時: ${fmtDate(run.started_at)}</div>
      <div>終了日時: ${fmtDate(run.finished_at)}</div>
      ${run.error_message ? `<div class="collector-error">${escapeHtml(run.error_message)}</div>` : ""}
    </div>
  `;
}

function updateUsageModeUi() {
  const mode = document.querySelector("#usageMode").value;
  const input = document.querySelector("#usedValueInput");
  const note = document.querySelector("#usageNote");
  const help = document.querySelector("#usageHelp");
  const button = document.querySelector("#usageSubmit");

  if (mode === "adjust") {
    input.removeAttribute("min");
    input.placeholder = "補正値 例: -10";
    note.required = true;
    note.placeholder = "補正理由";
    help.textContent = "補正は履歴を削除せず、差分レコードを追加して調整します。補正時はメモが必須です。";
    button.textContent = "補正を追加";
    return;
  }

  input.min = "0.01";
  input.placeholder = "加算する使用量";
  note.required = false;
  note.placeholder = "メモ";
  help.textContent = "通常加算は現在値の上書きではありません。使用した分だけ加算します。";
  button.textContent = "使用量を加算";
}

function initApp() {
  Object.entries(APP_VIEW_NAV_BUTTON_IDS).forEach(([viewName, buttonId]) => {
    const button = document.querySelector(`#${buttonId}`);
    if (!button) return;
    button.addEventListener("click", () => setActiveView(viewName));
  });
  // hashchange時は描画するだけ。ここで書き戻すと戻る/進むが無効化される。
  window.addEventListener("hashchange", () => setActiveView(normalizeAppView(location.hash), { updateHash: false }));
  // 初回同期はreplaceState: hash無しで開いたときに履歴を1つ増やさないため
  // (増やすと最初の「戻る」がこのページ内に吸われる)。
  const initialView = normalizeAppView(location.hash);
  setActiveView(initialView, { updateHash: false });
  if (shouldWriteAppViewHash(location.hash, initialView) && window.history && window.history.replaceState) {
    window.history.replaceState(null, "", `#${initialView}`);
  }

  document.querySelector("#serviceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    await api("/api/services", { method: "POST", body: JSON.stringify(data) });
    event.target.reset();
    await loadAll();
  });

  document.querySelector("#limitForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    data.service_id = Number(data.service_id);
    data.reset_interval_value = Number(data.reset_interval_value || 1);
    data.max_value = data.max_value === "" ? null : Number(data.max_value);
    data.next_reset_at = data.next_reset_at ? new Date(data.next_reset_at).toISOString() : null;
    await api("/api/limits", { method: "POST", body: JSON.stringify(data) });
    event.target.reset();
    await loadAll();
  });

  document.querySelector("#cards").addEventListener("click", (event) => {
    const editButton = event.target.closest(".edit-limit-button");
    if (editButton) {
      state.editingLimitId = Number(editButton.dataset.limitId);
      renderDashboard();
      return;
    }
    const cancelButton = event.target.closest(".cancel-edit-limit");
    if (cancelButton) {
      state.editingLimitId = null;
      renderDashboard();
    }
  });

  document.querySelector("#cards").addEventListener("change", (event) => {
    const typeSelect = event.target.closest(".reset-interval-type-input");
    if (!typeSelect) return;
    const form = typeSelect.closest(".edit-limit-form");
    const valueInput = form.querySelector(".reset-interval-value-input");
    const nextResetInput = form.querySelector(".next-reset-at-input");
    const isManual = typeSelect.value === "manual";
    valueInput.disabled = isManual;
    nextResetInput.disabled = isManual;
    if (isManual) {
      nextResetInput.value = "";
    }
  });

  document.querySelector("#cards").addEventListener("submit", async (event) => {
    const form = event.target.closest(".edit-limit-form");
    if (!form) return;
    event.preventDefault();

    const limitId = form.dataset.limitId;
    const errorTarget = document.querySelector(`#editLimitError-${limitId}`);
    errorTarget.innerHTML = "";

    const data = Object.fromEntries(new FormData(form));
    const payload = {
      model_name: data.model_name,
      unit: data.unit,
      reset_interval_type: data.reset_interval_type,
      reset_interval_value: data.reset_interval_value === undefined ? 1 : Number(data.reset_interval_value),
      max_value: data.max_value === "" ? null : Number(data.max_value),
      next_reset_at: data.next_reset_at ? new Date(data.next_reset_at).toISOString() : null,
    };

    const submitButton = form.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      await api(`/api/limits/${limitId}`, { method: "PUT", body: JSON.stringify(payload) });
      state.editingLimitId = null;
      await loadAll();
    } catch (error) {
      errorTarget.innerHTML = escapeHtml(error.message);
    } finally {
      submitButton.disabled = false;
    }
  });

  document.querySelector("#usageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const limitId = data.limit_id;
    await api(`/api/limits/${limitId}/usage`, {
      method: "POST",
      body: JSON.stringify({
        used_value: Number(data.used_value),
        mode: data.mode,
        note: data.note || null,
      }),
    });
    event.target.reset();
    updateUsageModeUi();
    await loadAll();
  });

  document.querySelector("#claudeDesktopCloudUsageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const resultEl = document.querySelector("#claudeDesktopCloudUsageResult");
    resultEl.innerHTML = "";

    const data = Object.fromEntries(new FormData(event.target));
    const fiveHourRemaining = data.five_hour_remaining_percentage;
    const sevenDayRemaining = data.seven_day_remaining_percentage;

    // 表示側はauto/manualをsnapshot単位でしか切り替えず、windowをまたいだ合成をしない。
    // 片方だけの保存を許すと、より新しいmanual snapshotが、完全なauto snapshotの片方の
    // 枠を(表示上)覆い隠してしまうため、両方必須にする。
    if (fiveHourRemaining === "" || sevenDayRemaining === "" || !data.five_hour_resets_at || !data.seven_day_resets_at) {
      resultEl.innerHTML = `<div class="codex-usage-error">5時間枠・7日枠の両方(残り%とreset日時)を入力してください。</div>`;
      return;
    }

    const fiveHourResetsAtIso = parseDatetimeLocalToIsoOrNull(data.five_hour_resets_at);
    const sevenDayResetsAtIso = parseDatetimeLocalToIsoOrNull(data.seven_day_resets_at);
    if (!fiveHourResetsAtIso || !sevenDayResetsAtIso) {
      resultEl.innerHTML = `<div class="codex-usage-error">reset日時の形式が正しくありません。</div>`;
      return;
    }

    const payload = {
      five_hour: { remaining_percentage: Number(fiveHourRemaining), resets_at: fiveHourResetsAtIso },
      seven_day: { remaining_percentage: Number(sevenDayRemaining), resets_at: sevenDayResetsAtIso },
    };

    if (!confirmClaudeDesktopCloudUsageSave()) return;

    const submitButton = document.querySelector("#claudeDesktopCloudUsageSubmit");
    submitButton.disabled = true;
    try {
      const snapshot = await api("/api/claude-code-usage/manual", { method: "PUT", body: JSON.stringify(payload) });
      renderClaudeDesktopCloudUsage(snapshot);
      resultEl.innerHTML = `<div class="codex-usage-success">保存しました。</div>`;
    } catch (error) {
      resultEl.innerHTML = `<div class="codex-usage-error">${escapeHtml(error.message)}</div>`;
    } finally {
      submitButton.disabled = false;
    }
  });

  document.querySelector("#codexUsageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const resultEl = document.querySelector("#codexUsageResult");
    resultEl.innerHTML = "";

    const data = Object.fromEntries(new FormData(event.target));
    const fiveHourRemaining = data.five_hour_remaining_percentage;
    const weeklyRemaining = data.weekly_remaining_percentage;

    const payload = {};
    if (fiveHourRemaining !== "") {
      if (!data.five_hour_resets_at) {
        resultEl.innerHTML = `<div class="codex-usage-error">5時間枠のreset日時を入力してください。</div>`;
        return;
      }
      payload.five_hour = {
        remaining_percentage: Number(fiveHourRemaining),
        resets_at: new Date(data.five_hour_resets_at).toISOString(),
      };
    }
    if (weeklyRemaining !== "") {
      if (!data.weekly_resets_at) {
        resultEl.innerHTML = `<div class="codex-usage-error">週次枠のreset日時を入力してください。</div>`;
        return;
      }
      payload.weekly = {
        remaining_percentage: Number(weeklyRemaining),
        resets_at: new Date(data.weekly_resets_at).toISOString(),
      };
    }
    if (!payload.five_hour && !payload.weekly) {
      resultEl.innerHTML = `<div class="codex-usage-error">5時間枠・週次枠のどちらかは入力してください。</div>`;
      return;
    }

    const submitButton = document.querySelector("#codexUsageSubmit");
    submitButton.disabled = true;
    try {
      const snapshot = await api("/api/codex-usage", { method: "PUT", body: JSON.stringify(payload) });
      renderCodexUsage(snapshot);
      resultEl.innerHTML = `<div class="codex-usage-success">保存しました。</div>`;
    } catch (error) {
      resultEl.innerHTML = `<div class="codex-usage-error">${escapeHtml(error.message)}</div>`;
    } finally {
      submitButton.disabled = false;
    }
  });

  document.querySelector("#collectorForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const vendor = data.vendor;
    const dryRun = data.dry_run === "true";
    if (!dryRun && !window.confirm("dry_run=false のため、取得結果を usage_records に保存します。実行しますか？")) {
      return;
    }
    const button = document.querySelector("#collectorSubmit");
    button.disabled = true;
    button.textContent = "実行中...";
    try {
      const run = await api(`/api/collect/${vendor}?dry_run=${dryRun}`, { method: "POST" });
      renderCollectorResult(run);
      await Promise.all([refreshCollectorRuns(), loadAll()]);
    } catch (error) {
      document.querySelector("#collectorResult").innerHTML = `<div class="collector-error">${escapeHtml(error.message)}</div>`;
      await refreshCollectorRuns();
    } finally {
      button.disabled = false;
      button.textContent = "Collectorを実行";
    }
  });

  document.querySelector("#githubRateLimitRefresh").addEventListener("click", async () => {
    stopGithubCooldownCountdown();
    const button = document.querySelector("#githubRateLimitRefresh");
    button.disabled = true;
    button.textContent = "更新中...";
    try {
      const response = await fetch("/api/github-rate-limit/refresh", { method: "POST" });
      if (response.status === 429) {
        const body = await response.json().catch(() => ({}));
        const detail = body.detail || {};
        renderGithubRateLimit({ error: { user_message: detail.user_message } });
        if (detail.retry_after_seconds > 0) {
          startGithubCooldownCountdown(detail.retry_after_seconds);
        } else {
          button.disabled = false;
          button.textContent = "更新";
        }
        return;
      }
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = await response.json();
      renderGithubRateLimit(data);
      button.disabled = false;
      button.textContent = "更新";
    } catch (error) {
      renderGithubRateLimit({ error: { user_message: error.message } });
      button.disabled = false;
      button.textContent = "更新";
    }
  });

  document.querySelector("#githubActionsBillingRefresh").addEventListener("click", async () => {
    stopGithubActionsBillingCooldownCountdown();
    const button = document.querySelector("#githubActionsBillingRefresh");
    button.disabled = true;
    button.textContent = "更新中...";
    // Response bodies are never read for display here (no `.text()`, never
    // passed into an Error) — `githubActionsBillingErrorDisplay` is the only
    // path that turns a response into displayed text, and it never echoes
    // body content back except the two fixed 429 fields.
    let cooldownStarted = false;
    try {
      const response = await fetch("/api/github-actions-billing/refresh", { method: "POST" });
      if (response.status === 429) {
        const body = await response.json().catch(() => null);
        const resolved = githubActionsBillingErrorDisplay(429, body);
        renderGithubActionsBilling({ error: { user_message: resolved.user_message } });
        if (resolved.retry_after_seconds > 0) {
          cooldownStarted = true;
          startGithubActionsBillingCooldownCountdown(resolved.retry_after_seconds);
        }
        return;
      }
      if (!response.ok) {
        const resolved = githubActionsBillingErrorDisplay(response.status, null);
        renderGithubActionsBilling({ error: { user_message: resolved.user_message } });
        return;
      }
      const data = await response.json();
      renderGithubActionsBilling(data);
    } catch (error) {
      const resolved = githubActionsBillingErrorDisplay(null, null);
      renderGithubActionsBilling({ error: { user_message: resolved.user_message } });
    } finally {
      if (!cooldownStarted) {
        button.disabled = false;
        button.textContent = "更新";
      }
    }
  });

  // #githubGraphqlDiagnosticsResultの中身は毎回丸ごとinnerHTMLで再生成される
  // (フォーム自体・active_sessionsの各stopボタンとも)ため、個別にaddEventListener
  // せずコンテナへのイベント委譲(delegation)で拾う。responseの本文は成功時の
  // JSONを再取得(GET /api/github-graphql-diagnostics)して丸ごと再描画する用途以外
  // には使わず、失敗時はgithubGraphqlDiagnosticsErrorDisplayを経由した固定文言
  // 以外を一切表示しない(.text()は呼ばない)。
  const githubGraphqlDiagnosticsContainer = document.querySelector("#githubGraphqlDiagnosticsResult");

  githubGraphqlDiagnosticsContainer.addEventListener("submit", async (event) => {
    const form = event.target.closest("#githubGraphqlDiagnosticsForm");
    if (!form) return;
    event.preventDefault();

    const formData = new FormData(form);
    const actorType = String(formData.get("actor_type") || "").trim();
    const label = String(formData.get("label") || "").trim();
    const repository = String(formData.get("repository") || "").trim();
    const prNumberRaw = String(formData.get("pr_number") || "").trim();
    const body = {
      actor_type: actorType,
      label: label,
      repository: repository ? repository : null,
      pr_number: prNumberRaw ? Number(prNumberRaw) : null,
    };

    const submitButton = form.querySelector("#githubGraphqlDiagnosticsSubmit");
    const resultSlot = form.querySelector("#githubGraphqlDiagnosticsFormResult");
    submitButton.disabled = true;
    try {
      const response = await fetch("/api/github-graphql-diagnostics/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const errorBody = await response.json().catch(() => null);
        const resolved = githubGraphqlDiagnosticsErrorDisplay(response.status, errorBody);
        resultSlot.innerHTML = `<div class="github-error">${escapeHtml(resolved.user_message)}</div>`;
        return;
      }
      await refreshGithubGraphqlDiagnostics();
      // Recent Activity Sessions / Sample Timelineの再取得はあくまで補助表示の
      // 更新であり、これが失敗してもstart自体は成功しているので、失敗を
      // start操作のエラーとしてresultSlotへ表示しない(握りつぶして良い)。
      await refreshGithubGraphqlDiagnosticsSessions().catch(() => {});
    } catch (error) {
      const resolved = githubGraphqlDiagnosticsErrorDisplay(null, null);
      resultSlot.innerHTML = `<div class="github-error">${escapeHtml(resolved.user_message)}</div>`;
    } finally {
      submitButton.disabled = false;
    }
  });

  githubGraphqlDiagnosticsContainer.addEventListener("click", async (event) => {
    const stopButton = event.target.closest(".github-graphql-diagnostics-stop");
    if (!stopButton) return;

    const sessionId = stopButton.dataset.sessionId;
    stopButton.disabled = true;
    try {
      const response = await fetch(`/api/github-graphql-diagnostics/${encodeURIComponent(sessionId)}/stop`, {
        method: "POST",
      });
      if (!response.ok) {
        const errorBody = await response.json().catch(() => null);
        const resolved = githubGraphqlDiagnosticsErrorDisplay(response.status, errorBody);
        githubGraphqlDiagnosticsContainer.insertAdjacentHTML(
          "afterbegin",
          `<div class="github-error">${escapeHtml(resolved.user_message)}</div>`
        );
        return;
      }
      await refreshGithubGraphqlDiagnostics();
      // 同上: 補助表示の再取得失敗はstop操作自体のエラーとして表示しない。
      await refreshGithubGraphqlDiagnosticsSessions().catch(() => {});
    } catch (error) {
      const resolved = githubGraphqlDiagnosticsErrorDisplay(null, null);
      githubGraphqlDiagnosticsContainer.insertAdjacentHTML(
        "afterbegin",
        `<div class="github-error">${escapeHtml(resolved.user_message)}</div>`
      );
    } finally {
      stopButton.disabled = false;
    }
  });

  document.querySelector("#codexRateLimitsRefresh").addEventListener("click", async () => {
    stopCodexRateLimitsCooldownCountdown();
    const button = document.querySelector("#codexRateLimitsRefresh");
    button.disabled = true;
    button.textContent = "取得中...";
    // Response bodies are never read for display here (no `.text()`, never
    // passed into an Error) — `codexRateLimitsErrorDisplay` is the only path
    // that turns a response into displayed text, and it never echoes body
    // content back except the two fixed 429 fields.
    let cooldownStarted = false;
    try {
      const response = await fetch("/api/codex-rate-limits/refresh", { method: "POST" });
      if (response.status === 429) {
        const body = await response.json().catch(() => null);
        const resolved = codexRateLimitsErrorDisplay(429, body);
        renderCodexRateLimits({
          ...state.codexRateLimits,
          error_type: resolved.error_type,
          user_message: resolved.user_message,
        });
        if (resolved.retry_after_seconds > 0) {
          cooldownStarted = true;
          startCodexRateLimitsCooldownCountdown(resolved.retry_after_seconds);
        }
        return;
      }
      if (!response.ok) {
        const resolved = codexRateLimitsErrorDisplay(response.status, null);
        renderCodexRateLimits({
          ...state.codexRateLimits,
          error_type: resolved.error_type,
          user_message: resolved.user_message,
        });
        return;
      }
      const data = await response.json();
      renderCodexRateLimits(data);
    } catch (error) {
      const resolved = codexRateLimitsErrorDisplay(null, null);
      renderCodexRateLimits({
        ...state.codexRateLimits,
        error_type: resolved.error_type,
        user_message: resolved.user_message,
      });
    } finally {
      if (!cooldownStarted) {
        button.disabled = false;
        button.textContent = "今すぐ更新";
      }
    }
  });

  for (const id of ["filterService", "filterAccountType", "filterStatus", "sortBy"]) {
    document.querySelector(`#${id}`).addEventListener("input", renderDashboard);
    document.querySelector(`#${id}`).addEventListener("change", renderDashboard);
  }

  document.querySelector("#usageMode").addEventListener("change", updateUsageModeUi);
  document.querySelector("#historyFilter").addEventListener("change", renderHistory);

  document.querySelector("#exportJson").addEventListener("click", () => {
    window.location.href = "/api/export/json";
  });

  document.querySelector("#exportCsv").addEventListener("click", () => {
    window.location.href = "/api/export/limits.csv";
  });

  document.querySelector("#exportUsageCsv").addEventListener("click", () => {
    window.location.href = "/api/export/usage-records.csv";
  });

  updateUsageModeUi();
  loadAll().catch((error) => {
    document.querySelector("#cards").innerHTML = `<div class="card error">${escapeHtml(error.message)}</div>`;
  });
  // メインダッシュボード(#cards)のloadAll()とは独立した読み込みサイクル。
  // ここが失敗してもメインダッシュボードは壊さず、Recent Activity Sessions /
  // Sample Timelineの領域内だけに固定メッセージを出す。
  refreshGithubGraphqlDiagnosticsSessions().catch(() => {
    const sessionsTarget = document.querySelector("#githubGraphqlDiagnosticsSessionsResult");
    if (sessionsTarget) {
      sessionsTarget.innerHTML = `<p class="muted">履歴の取得に失敗しました。</p>`;
    }
    const timelineTarget = document.querySelector("#githubGraphqlDiagnosticsTimelineResult");
    if (timelineTarget) {
      timelineTarget.innerHTML = `<p class="muted">サンプルの取得に失敗しました。</p>`;
    }
  });
}

if (typeof document !== "undefined") {
  initApp();
}

if (typeof module !== "undefined") {
  module.exports = {
    sourceTypeLabel,
    sourceTypeClass,
    collectorStatusClass,
    githubResourceLabel,
    githubStatusClass,
    githubOverallClass,
    fmtGithubDateUtc,
    fmtDurationJa,
    fmtAbsoluteWithRelative,
    suppressCountdownIfStale,
    fmtAppScheduleRelative,
    fmtSecondsUntilReset,
    githubResourceCardHtml,
    githubRateLimitHtml,
    githubAutoRefreshNoticeHtml,
    githubLimitedCause,
    githubLimitedBannerHtml,
    githubSecondaryRateLimitBannerHtml,
    githubActionsBillingStatusClass,
    githubActionsBillingPlanLabel,
    githubActionsBillingHtml,
    githubActionsBillingCardHtml,
    githubActionsBillingErrorDisplay,
    githubGraphqlDiagnosticsAttributionLabel,
    githubGraphqlDiagnosticsErrorDisplay,
    githubGraphqlDiagnosticsDisabledHtml,
    githubGraphqlDiagnosticsStartFormHtml,
    githubGraphqlDiagnosticsSessionCardHtml,
    githubGraphqlDiagnosticsLastSampleHtml,
    githubGraphqlDiagnosticsRenderPanel,
    githubGraphqlDiagnosticsSessionStatusLabel,
    githubGraphqlDiagnosticsStopReasonLabel,
    githubGraphqlDiagnosticsSessionDurationText,
    githubGraphqlDiagnosticsSessionComparisonRowHtml,
    githubGraphqlDiagnosticsSessionComparisonTableHtml,
    githubGraphqlDiagnosticsActiveSessionsAtSample,
    githubGraphqlDiagnosticsTimelineRowHtml,
    githubGraphqlDiagnosticsTimelineTableHtml,
    codexRateLimitsErrorDisplay,
    confirmClaudeDesktopCloudUsageSave,
    parseDatetimeLocalToIsoOrNull,
    fetchAllowancesSafe,
    usageAllowanceSourceKindLabel,
    usageAllowanceSourceBadgeHtml,
    usageAllowanceWindowLabel,
    usageAllowanceBucketTitle,
    usageAllowanceWindowStatus,
    usageAllowanceWindowResetText,
    usageAllowanceWindowHtml,
    usageAllowanceBucketCardHtml,
    usageAllowanceUnavailableCardHtml,
    groupUsageAllowances,
    renderLimitsAllowanceView,
    usageAllowanceOverviewRows,
    usageAllowanceOverviewRowHtml,
    usageAllowanceOverviewStatusLineHtml,
    usageAllowanceUsedPercentText,
    shouldWriteAppViewHash,
    renderOverviewView,
    normalizeAppView,
    setActiveView,
  };
}
