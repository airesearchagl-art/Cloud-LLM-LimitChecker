const state = {
  dashboard: [],
  limits: [],
  history: [],
  collectorRuns: [],
  editingLimitId: null,
  codexUsage: null,
  codexRateLimits: null,
  claudeDesktopCloudUsage: null,
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

const GITHUB_ACTIONS_BILLING_STATUS_LABEL = {
  usage_breakdown_inconclusive: "使用内訳: 判定不可",
  plan_unknown: "Plan不明",
};

// null/undefinedは"—"(未取得の"未取得"表記とは区別し、"exact値が原理的に無い"ことを示す)。
const fmtExactOrDash = (value) => (value === null || value === undefined ? "—" : fmtNumber(value));

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
      <div class="github-overall ${statusClass}">Plan: ${escapeHtml(data.plan_name || "不明")} — Plan不明</div>
      <p class="form-note">Planを安全に認識できないため、Monthly allowanceを判定していません。現在のGitHub credentialに"Plan: read"権限（"user" scope）があるか確認してください。</p>
      <div class="github-resource-cards">
        <div class="github-resource-card">
          <div class="github-resource-name">GitHub Actions</div>
          <div>Monthly allowance: —</div>
          <div>Exact used: —</div>
          <div>Exact remaining: —</div>
        </div>
      </div>`;
  }

  const allowanceText = fmtNumber(data.included_minutes);
  const discountedText = fmtExactOrDash(data.discounted_standard_minutes);
  const billableText = fmtExactOrDash(data.billable_standard_minutes);
  const nonIncludedText = fmtExactOrDash(data.paid_non_included_minutes);

  return `
    ${staleNoticeHtml}
    <div class="github-overall ${statusClass}">Plan: ${escapeHtml(data.plan_name || "不明")}</div>
    <div class="github-resource-cards">
      <div class="github-resource-card">
        <div class="github-resource-name">GitHub Actions</div>
        <div>Monthly allowance: ${allowanceText} min</div>
        <div>Exact used: —</div>
        <div>Exact remaining: —</div>
      </div>
    </div>
    <p class="form-note">Exact remainingは、現在の公式Billing summary（Public Preview）だけでは判定できません。discountにはincluded allowance消費分だけでなく、publicリポジトリのstandard runner利用やself-hosted runner利用の割引も混在するためです。</p>
    <div class="github-resource-cards">
      <div class="github-resource-card">
        <div class="github-resource-name">内訳（参考値。exactなquota消費量ではありません）</div>
        <div>Discounted standard usage: ${discountedText} min</div>
        <div>Billable standard usage: ${billableText} min</div>
        <div>Non-included paid minutes: ${nonIncludedText} min</div>
      </div>
    </div>
    <p class="form-note">対象月: ${escapeHtml(String(data.billing_year))}-${String(data.billing_month).padStart(2, "0")} ／ 取得元: ${escapeHtml(data.source || "-")} ／ 最終取得: ${escapeHtml(fmtGithubDate(data.collected_at))}</p>`;
}

function renderGithubActionsBilling(data) {
  document.querySelector("#githubActionsBillingResult").innerHTML = githubActionsBillingHtml(data);
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
    claudeDesktopCloudUsage,
    codexUsage,
    codexRateLimits,
  ] = await Promise.all([
    api("/api/services"),
    api("/api/limits"),
    api("/api/dashboard"),
    api("/api/alerts"),
    api("/api/usage-records"),
    api("/api/collector-runs"),
    api("/api/github-rate-limit"),
    api("/api/github-actions-billing"),
    api("/api/claude-code-usage/manual"),
    api("/api/codex-usage"),
    api("/api/codex-rate-limits"),
  ]);
  state.dashboard = dashboard;
  state.limits = limits;
  state.history = history;
  state.collectorRuns = collectorRuns;
  renderSelects(services, limits);
  renderDashboard();
  renderAlerts(alerts);
  renderHistory();
  renderCollectorRuns();
  renderGithubRateLimit(githubRateLimit);
  renderGithubActionsBilling(githubActionsBilling);
  renderClaudeDesktopCloudUsage(claudeDesktopCloudUsage);
  renderCodexUsage(codexUsage);
  renderCodexRateLimits(codexRateLimits);
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
    githubActionsBillingHtml,
    githubActionsBillingCardHtml,
    githubActionsBillingErrorDisplay,
    codexRateLimitsErrorDisplay,
    confirmClaudeDesktopCloudUsageSave,
    parseDatetimeLocalToIsoOrNull,
  };
}
