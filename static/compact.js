const REFRESH_INTERVAL_MS = 30000;

const state = {
  dashboard: [],
  github: null,
  claudeCodeUsage: null,
  codexUsage: null,
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

// Error(取得失敗)はUnknown/未取得(灰)とは別に、赤系との識別のため濃い橙で区別する。
function githubStatusClass(status) {
  if (status === "Normal") return "compact-status-normal";
  if (status === "Warning") return "compact-status-warning";
  if (status === "Exhausted" || status === "Reset overdue" || status === "Limited") return "compact-status-exhausted";
  if (status === "Error") return "compact-status-error";
  return "compact-status-unknown";
}

function githubResourceLabel(resourceName) {
  const labels = { core: "REST", graphql: "GraphQL", search: "Search" };
  return labels[resourceName] || resourceName;
}

// 監視優先度: Exhausted/Limited/Error(赤・橙) > Warning(黄) > Normal(緑) > 手入力待ち/未取得(灰)。
function statusPriorityRank(statusClass) {
  const order = {
    "compact-status-exhausted": 0,
    "compact-status-error": 0,
    "compact-status-warning": 1,
    "compact-status-normal": 2,
    "compact-status-unknown": 3,
  };
  return order[statusClass] ?? 4;
}

// DOMに触れない純粋関数: 監視優先度→service名→model名の順で決定的に並び替える。
// 元の配列順や取得タイミングに依存しないため、30秒ごとの再取得でも同一データなら同一順序になる。
function sortDashboardRows(rows) {
  return [...rows].sort((a, b) => {
    const rankDiff = statusPriorityRank(dashboardStatusClass(a.status)) - statusPriorityRank(dashboardStatusClass(b.status));
    if (rankDiff !== 0) return rankDiff;
    const nameA = `${a.service_name} ${a.model_name}`;
    const nameB = `${b.service_name} ${b.model_name}`;
    return nameA.localeCompare(nameB, "ja");
  });
}

// DOMに触れない純粋関数: dashboard 1件分のカードHTMLを組み立てる。
// 優先順位: 残り% > status(ヘッダーバッジ) > 使用量/上限 > resetまで > 最終更新 > 取得元。
function limitCardHtml(row) {
  const hasPercent = row.usage_percent !== null && row.usage_percent !== undefined;
  const statusClass = dashboardStatusClass(row.status);

  const bodyBlock = hasPercent
    ? (() => {
        const remainingPercent = Math.max(0, Math.round((100 - row.usage_percent) * 100) / 100);
        const width = Math.min(Math.max(row.usage_percent, 0), 100);
        return `
          <div class="compact-percent-row">
            <span class="compact-percent-label">残り</span>
            <span class="compact-percent-value">${fmtNumber(remainingPercent)}%</span>
          </div>
          <div class="compact-usage-line">使用 ${fmtNumber(row.used_value)} / ${fmtNumber(row.max_value)} ${escapeHtml(row.unit)}（${fmtNumber(row.usage_percent)}%）</div>
          <div class="compact-meter"><div class="compact-meter-fill ${statusClass}" style="width:${width}%"></div></div>
        `;
      })()
    : `<div class="compact-no-limit">上限未登録</div>`;

  return `
    <article class="compact-card">
      <div class="compact-card-head">
        <div class="compact-card-head-text">
          <div class="compact-service-name">${escapeHtml(row.service_name)}</div>
          <div class="compact-model-name">${escapeHtml(row.model_name)}</div>
        </div>
        <span class="compact-status ${statusClass}">${escapeHtml(row.status)}</span>
      </div>
      ${bodyBlock}
      <div class="compact-meta-row">
        <span>reset: ${resetRelativeText(row.next_reset_at)}</span>
        <span>更新: ${fmtDateOrUnknown(row.last_updated_at)}</span>
        <span class="compact-source-badge">${escapeHtml(sourceTypeLabel(row.source_type))}</span>
      </div>
    </article>
  `;
}

// DOMに触れない純粋関数: GitHubの1リソース分のカードHTMLを組み立てる。UTC詳細は表示しない。
function githubResourceCardHtml(resource) {
  if (!resource) return "";
  const statusClass = githubStatusClass(resource.status);
  if (resource.status === "Error") {
    return `
      <article class="compact-card compact-github-card">
        <div class="compact-card-head">
          <span class="compact-service-name">${escapeHtml(githubResourceLabel(resource.resource))}</span>
          <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
        </div>
        <div class="compact-usage-line">${escapeHtml(resource.error_message || "")}</div>
      </article>`;
  }

  const hasPercent = resource.remaining_percent !== null && resource.remaining_percent !== undefined;
  const width = Math.min(Math.max(resource.usage_percent ?? 0, 0), 100);
  const bodyBlock = hasPercent
    ? `
      <div class="compact-percent-row">
        <span class="compact-percent-label">残り</span>
        <span class="compact-percent-value compact-percent-value-sm">${fmtNumber(resource.remaining_percent)}%</span>
      </div>
      <div class="compact-usage-line">${fmtNumber(resource.used)} / ${fmtNumber(resource.limit)}</div>
      <div class="compact-meter"><div class="compact-meter-fill ${statusClass}" style="width:${width}%"></div></div>
    `
    : `<div class="compact-no-limit">上限未登録</div>`;

  return `
    <article class="compact-card compact-github-card">
      <div class="compact-card-head">
        <span class="compact-service-name">${escapeHtml(githubResourceLabel(resource.resource))}</span>
        <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
      </div>
      ${bodyBlock}
      <div class="compact-meta-row"><span>reset: ${githubSecondsUntilResetText(resource.seconds_until_reset)}</span></div>
    </article>
  `;
}

function githubOverallStatusClass(status) {
  if (status === "Normal") return "compact-status-normal";
  if (status === "Warning") return "compact-status-warning";
  if (status === "Limited") return "compact-status-exhausted";
  if (status === "Error") return "compact-status-error";
  return "compact-status-unknown";
}

function githubOverallHtml(overall) {
  if (!overall) return "";
  const cls = githubOverallStatusClass(overall.status);
  const reason = overall.reason ? ` — ${escapeHtml(overall.reason)}` : "";
  return `<div class="compact-github-overall ${cls}">GitHub Overall: ${escapeHtml(overall.status)}${reason}</div>`;
}

// DOMに触れない純粋関数: GET /api/github-rate-limit のレスポンスからGitHubセクションのHTMLを組み立てる。
// REST/GraphQL/Searchは固定順(状態による並び替えをしない)で常に横並び表示する。
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

// DOMに触れない純粋関数: Claude Code statusLineブリッジのキャッシュ1枠分(5時間 or 7日)のカードHTMLを組み立てる。
// remaining/usedはブリッジ側で既に0-100%へ検証済みの値のみを渡される想定。
function claudeUsageWindowHtml(label, window) {
  if (!window) {
    return `
      <article class="compact-card">
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
        <div class="compact-no-limit">未観測</div>
      </article>`;
  }
  const width = Math.min(Math.max(window.used_percentage, 0), 100);
  return `
    <article class="compact-card">
      <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
      <div class="compact-percent-row">
        <span class="compact-percent-label">残り</span>
        <span class="compact-percent-value compact-percent-value-sm">${fmtNumber(window.remaining_percentage)}%</span>
      </div>
      <div class="compact-usage-line">使用済み ${fmtNumber(window.used_percentage)}%</div>
      <div class="compact-meter"><div class="compact-meter-fill compact-claude-usage" style="width:${width}%"></div></div>
      <div class="compact-meta-row"><span>reset: ${resetRelativeText(window.resets_at)}</span></div>
    </article>`;
}

// DOMに触れない純粋関数: GET /api/claude-code-usage のレスポンスからセクションHTMLを組み立てる。
// statusLineはpush型のため、staleは「取得不可」ではなく「最終観測値が古い」という意味で表示する。
function claudeCodeSectionHtml(data) {
  if (!data || !data.available) {
    const message = data && data.status === "invalid_cache" ? "取得不可" : "Claude Code実行後に取得";
    return `<div class="compact-card compact-empty">Claude Code使用率: ${message}</div>`;
  }

  const staleNoticeHtml = data.stale
    ? `<div class="compact-stale-notice">最終観測値(古い可能性があります)</div>`
    : "";

  return `
    ${staleNoticeHtml}
    <div class="compact-github-grid-inner">
      ${claudeUsageWindowHtml("5時間枠", data.five_hour)}
      ${claudeUsageWindowHtml("7日枠", data.seven_day)}
    </div>
    <div class="compact-stale-notice">最終観測: ${fmtDateOrUnknown(data.observed_at)}</div>
  `;
}

// DOMに触れない純粋関数: Codex Usage手動入力キャッシュ1枠分(5時間 or 週次)のカードHTMLを組み立てる。
// remaining/usedは管理画面での保存時に既に0-100%へ検証済みの値のみを渡される想定。
// resets_atを過ぎている場合は、古いpercentageを現在値のように強調表示せず「reset時刻超過」だけを示す。
function codexUsageWindowHtml(label, window) {
  if (!window) {
    return `
      <article class="compact-card">
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
        <div class="compact-no-limit">未入力</div>
      </article>`;
  }
  const resetText = resetRelativeText(window.resets_at);
  const resetExceeded = resetText === "reset時刻超過";
  const bodyBlock = resetExceeded
    ? `<div class="compact-no-limit">reset時刻超過</div>`
    : (() => {
        const width = Math.min(Math.max(window.used_percentage, 0), 100);
        return `
          <div class="compact-percent-row">
            <span class="compact-percent-label">残り</span>
            <span class="compact-percent-value compact-percent-value-sm">${fmtNumber(window.remaining_percentage)}%</span>
          </div>
          <div class="compact-usage-line">使用済み ${fmtNumber(window.used_percentage)}%</div>
          <div class="compact-meter"><div class="compact-meter-fill compact-codex-usage" style="width:${width}%"></div></div>
        `;
      })();
  return `
    <article class="compact-card">
      <div class="compact-card-head">
        <span class="compact-service-name">${escapeHtml(label)}</span>
        <span class="compact-source-badge">手動確認値</span>
      </div>
      ${bodyBlock}
      <div class="compact-meta-row"><span>reset: ${resetText}</span></div>
    </article>`;
}

// DOMに触れない純粋関数: GET /api/codex-usage のレスポンスからセクションHTMLを組み立てる。
// 自動取得ではなく手動入力のため、staleは「最終手動確認値が古い可能性がある」という意味で表示する。
function codexUsageSectionHtml(data) {
  if (!data || !data.available) {
    const message = data && data.status === "invalid_cache" ? "取得不可" : "Codex /statusで確認後に手動入力";
    return `<div class="compact-card compact-empty">Codex Usage: ${message}</div>`;
  }

  const staleNoticeHtml = data.stale
    ? `<div class="compact-stale-notice">最終手動確認値・古い可能性があります</div>`
    : "";

  return `
    ${staleNoticeHtml}
    <div class="compact-github-grid-inner">
      ${codexUsageWindowHtml("5時間枠", data.five_hour)}
      ${codexUsageWindowHtml("週次枠", data.weekly)}
    </div>
    <div class="compact-stale-notice">最終手動確認: ${fmtDateOrUnknown(data.observed_at)}</div>
  `;
}

function renderLastRendered() {
  document.querySelector("#lastRenderedAt").textContent = `最終描画: ${new Date().toLocaleString("ja-JP")}`;
}

function renderLimitCards(rows) {
  const container = document.querySelector("#limitCards");
  const sorted = sortDashboardRows(rows);
  container.innerHTML = sorted.length
    ? sorted.map(limitCardHtml).join("")
    : `<div class="compact-card compact-empty">表示できる制限項目がありません。</div>`;
}

function renderGithubSection(data) {
  document.querySelector("#githubCards").innerHTML = githubSectionHtml(data);
}

function renderClaudeCodeUsage(data) {
  document.querySelector("#claudeCodeUsageCards").innerHTML = claudeCodeSectionHtml(data);
}

function renderCodexUsage(data) {
  document.querySelector("#codexUsageCards").innerHTML = codexUsageSectionHtml(data);
}

// GETのみ: /api/dashboard・/api/github-rate-limit・/api/claude-code-usage・/api/codex-usage はいずれも
// 保存済みの値を返すだけで、gh api rate_limitやClaude Code/Codexの起動などの外部コマンド/APIを
// ここから直接実行することはない。
async function loadCompact() {
  try {
    const [dashboard, github, claudeCodeUsage, codexUsage] = await Promise.all([
      fetchJson("/api/dashboard"),
      fetchJson("/api/github-rate-limit"),
      fetchJson("/api/claude-code-usage"),
      fetchJson("/api/codex-usage"),
    ]);
    state.dashboard = dashboard;
    state.github = github;
    state.claudeCodeUsage = claudeCodeUsage;
    state.codexUsage = codexUsage;
    renderLimitCards(dashboard);
    renderGithubSection(github);
    renderClaudeCodeUsage(claudeCodeUsage);
    renderCodexUsage(codexUsage);
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
    statusPriorityRank,
    sortDashboardRows,
    limitCardHtml,
    githubResourceCardHtml,
    githubOverallHtml,
    githubOverallStatusClass,
    githubSectionHtml,
    claudeUsageWindowHtml,
    claudeCodeSectionHtml,
    codexUsageWindowHtml,
    codexUsageSectionHtml,
  };
}
