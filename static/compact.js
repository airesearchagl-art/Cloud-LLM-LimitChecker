const REFRESH_INTERVAL_MS = 30000;

const state = {
  dashboard: [],
  github: null,
};

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtNumber(value) {
  if (value === null || value === undefined) return "未取得";
  return Number(value).toLocaleString("ja-JP", { maximumFractionDigits: 2 });
}

function fmtDateOrUnknown(value) {
  if (!value) return "未更新";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "不明";
  return d.toLocaleString("ja-JP");
}

function dashboardStatusClass(status) {
  if (status === "正常") return "compact-status-normal";
  if (status === "注意") return "compact-status-warning";
  if (status === "危険" || status === "上限到達") return "compact-status-exhausted";
  return "compact-status-unknown";
}

function sourceTypeLabel(sourceType) {
  const labels = {
    manual: "手入力",
    manual_required: "手入力",
    manual_adjustment: "補正",
    api_openai_management: "OpenAI API",
    api_gemini_management: "Gemini API",
    api_claude_management: "Claude API",
  };
  return labels[sourceType] || sourceType || "未取得";
}

// next_reset_atはISO文字列またはnull。負数の秒数を表示せず「reset時刻超過」に丸める。
function resetRelativeText(nextResetAt) {
  if (!nextResetAt) return "未設定";
  const target = new Date(nextResetAt);
  if (Number.isNaN(target.getTime())) return "不明";
  const diffSeconds = Math.floor((target.getTime() - Date.now()) / 1000);
  if (diffSeconds < 0) return "reset時刻超過";
  if (diffSeconds < 60) return `あと${diffSeconds}秒`;
  if (diffSeconds < 3600) return `あと${Math.floor(diffSeconds / 60)}分`;
  return `あと${Math.floor(diffSeconds / 3600)}時間`;
}

// サーバー側のseconds_until_resetは負値になり得るため、同様に丸める。
function githubSecondsUntilResetText(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "不明";
  if (seconds < 0) return "reset時刻超過";
  if (seconds < 60) return `あと${seconds}秒`;
  if (seconds < 3600) return `あと${Math.floor(seconds / 60)}分`;
  return `あと${Math.floor(seconds / 3600)}時間`;
}

function githubStatusClass(status) {
  if (status === "Normal") return "compact-status-normal";
  if (status === "Warning") return "compact-status-warning";
  if (status === "Exhausted" || status === "Reset overdue" || status === "Limited") return "compact-status-exhausted";
  return "compact-status-unknown";
}

function githubResourceLabel(resourceName) {
  const labels = { core: "REST", graphql: "GraphQL", search: "Search" };
  return labels[resourceName] || resourceName;
}

// DOMに触れない純粋関数: dashboard 1件分のカードHTMLを組み立てる。
function limitCardHtml(row) {
  const hasMax = row.max_value !== null && row.max_value !== undefined;
  const hasPercent = row.usage_percent !== null && row.usage_percent !== undefined;
  const usedPercent = hasPercent ? row.usage_percent : null;
  const remainingPercent = hasPercent ? Math.max(0, Math.round((100 - usedPercent) * 100) / 100) : null;
  const width = hasPercent ? Math.min(Math.max(usedPercent, 0), 100) : 0;
  const statusClass = dashboardStatusClass(row.status);

  const percentBlock = hasPercent
    ? `
      <div class="compact-percent-row">
        <div class="compact-percent-main">
          <span class="compact-percent-label">残り</span>
          <span class="compact-percent-value">${fmtNumber(remainingPercent)}%</span>
        </div>
        <div class="compact-percent-sub">使用済み ${fmtNumber(usedPercent)}%</div>
      </div>
      <div class="compact-meter"><div class="compact-meter-fill ${statusClass}" style="width:${width}%"></div></div>
    `
    : `<div class="compact-no-limit">上限未登録</div>`;

  const remainingLine = hasMax
    ? `${fmtNumber(row.remaining_value)} / ${fmtNumber(row.max_value)} ${escapeHtml(row.unit)}`
    : "上限未登録";

  return `
    <article class="compact-card">
      <div class="compact-card-head">
        <div>
          <div class="compact-service-name">${escapeHtml(row.service_name)}</div>
          <div class="compact-model-name">${escapeHtml(row.model_name)}</div>
        </div>
        <span class="compact-status ${statusClass}">${escapeHtml(row.status)}</span>
      </div>
      ${percentBlock}
      <div class="compact-detail-row">${remainingLine}</div>
      <div class="compact-detail-row">reset: ${resetRelativeText(row.next_reset_at)}</div>
      <div class="compact-footer-row">
        <span class="compact-detail-row" style="margin-top:0">最終更新: ${fmtDateOrUnknown(row.last_updated_at)}</span>
        <span class="compact-source-badge">${escapeHtml(sourceTypeLabel(row.source_type))}</span>
      </div>
    </article>
  `;
}

// DOMに触れない純粋関数: GitHubの1リソース分のカードHTMLを組み立てる。
function githubResourceCardHtml(resource) {
  if (!resource) return "";
  const statusClass = githubStatusClass(resource.status);
  if (resource.status === "Error") {
    return `
      <article class="compact-card">
        <div class="compact-card-head">
          <div class="compact-service-name">${escapeHtml(githubResourceLabel(resource.resource))}</div>
          <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
        </div>
        <div class="compact-detail-row">${escapeHtml(resource.error_message || "")}</div>
      </article>`;
  }

  const hasPercent = resource.remaining_percent !== null && resource.remaining_percent !== undefined;
  const width = Math.min(Math.max(resource.usage_percent ?? 0, 0), 100);
  const percentBlock = hasPercent
    ? `
      <div class="compact-percent-row">
        <div class="compact-percent-main">
          <span class="compact-percent-label">残り</span>
          <span class="compact-percent-value">${fmtNumber(resource.remaining_percent)}%</span>
        </div>
        <div class="compact-percent-sub">使用済み ${fmtNumber(resource.usage_percent)}%</div>
      </div>
      <div class="compact-meter"><div class="compact-meter-fill ${statusClass}" style="width:${width}%"></div></div>
    `
    : `<div class="compact-no-limit">上限未登録</div>`;

  return `
    <article class="compact-card">
      <div class="compact-card-head">
        <div class="compact-service-name">${escapeHtml(githubResourceLabel(resource.resource))}</div>
        <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
      </div>
      ${percentBlock}
      <div class="compact-detail-row">${fmtNumber(resource.remaining)} / ${fmtNumber(resource.limit)}</div>
      <div class="compact-detail-row">reset: ${githubSecondsUntilResetText(resource.seconds_until_reset)}</div>
    </article>
  `;
}

function githubOverallStatusClass(status) {
  if (status === "Normal") return "compact-status-normal";
  if (status === "Warning") return "compact-status-warning";
  if (status === "Limited") return "compact-status-exhausted";
  return "compact-status-unknown";
}

function githubOverallHtml(overall) {
  if (!overall) return "";
  const cls = githubOverallStatusClass(overall.status);
  const reason = overall.reason ? ` — ${escapeHtml(overall.reason)}` : "";
  return `<div class="compact-github-overall ${cls}">Overall: ${escapeHtml(overall.status)}${reason}</div>`;
}

// DOMに触れない純粋関数: GET /api/github-rate-limit のレスポンスからGitHubセクションのHTMLを組み立てる。
function githubSectionHtml(data) {
  if (!data || !data.fetched) {
    const usingLastKnown = data && !data.fetched && data.last_known;
    if (!usingLastKnown) {
      return `<div class="compact-card compact-empty">GitHub Rate Limit: 未取得</div>`;
    }
    const resources = data.last_known.resources;
    const overall = data.last_known.overall;
    return `
      <div class="compact-stale-notice">直近の取得は失敗しました。以下は${fmtDateOrUnknown(data.last_known.collected_at)}時点の情報です。</div>
      ${githubOverallHtml(overall)}
      <div class="compact-github-grid-inner">
        ${githubResourceCardHtml(resources.core)}
        ${githubResourceCardHtml(resources.graphql)}
        ${resources.search ? githubResourceCardHtml(resources.search) : ""}
      </div>`;
  }

  return `
    ${githubOverallHtml(data.overall)}
    <div class="compact-github-grid-inner">
      ${githubResourceCardHtml(data.resources.core)}
      ${githubResourceCardHtml(data.resources.graphql)}
      ${data.resources.search ? githubResourceCardHtml(data.resources.search) : ""}
    </div>`;
}

function renderLastRendered() {
  document.querySelector("#lastRenderedAt").textContent = `最終描画: ${new Date().toLocaleString("ja-JP")}`;
}

function renderLimitCards(rows) {
  const container = document.querySelector("#limitCards");
  container.innerHTML = rows.length
    ? rows.map(limitCardHtml).join("")
    : `<div class="compact-card compact-empty">表示できる制限項目がありません。</div>`;
}

function renderGithubSection(data) {
  document.querySelector("#githubCards").innerHTML = githubSectionHtml(data);
}

// GETのみ: /api/dashboard と /api/github-rate-limit はどちらも保存済みの値を返すだけで、
// gh api rate_limit などの外部コマンド/APIをここから直接実行することはない。
async function loadCompact() {
  try {
    const [dashboard, github] = await Promise.all([fetchJson("/api/dashboard"), fetchJson("/api/github-rate-limit")]);
    state.dashboard = dashboard;
    state.github = github;
    renderLimitCards(dashboard);
    renderGithubSection(github);
  } catch (error) {
    document.querySelector("#limitCards").innerHTML = `<div class="compact-card compact-empty">取得に失敗しました: ${escapeHtml(error.message)}</div>`;
  } finally {
    renderLastRendered();
  }
}

function initCompact() {
  document.querySelector("#reloadButton").addEventListener("click", () => {
    loadCompact();
  });
  loadCompact();
  setInterval(loadCompact, REFRESH_INTERVAL_MS);
}

if (typeof document !== "undefined") {
  initCompact();
}

if (typeof module !== "undefined") {
  module.exports = {
    fmtNumber,
    fmtDateOrUnknown,
    dashboardStatusClass,
    sourceTypeLabel,
    resetRelativeText,
    githubSecondsUntilResetText,
    githubStatusClass,
    githubResourceLabel,
    limitCardHtml,
    githubResourceCardHtml,
    githubOverallHtml,
    githubOverallStatusClass,
    githubSectionHtml,
  };
}
