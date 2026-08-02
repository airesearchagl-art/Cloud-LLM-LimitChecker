const REFRESH_INTERVAL_MS = 30000;

const state = {
  dashboard: [],
  github: null,
  claudeCodeUsage: null,
  codexRateLimits: null,
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

// DOMに触れない純粋関数: 0以上の経過秒数を日本語の期間表記へ変換する（「あと」は含まない）。
// 「h」「m」等の略記は使わず、1分未満/分単位/時間+分/日+時間の4段階で表す。
// 0になる単位（例: ちょうど1時間）は省略する（「1時間 0分」ではなく「1時間」）。
// 呼び出し側（resetRelativeText等）が符号判定と「あと」プレフィックスを担当する。
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
// 相対情報が無い/不明/未設定/staleで抑制された場合は絶対時刻のみを返す。
function fmtAbsoluteWithRelative(absoluteText, relativeText) {
  if (!relativeText || relativeText === "不明" || relativeText === "未設定") return absoluteText;
  return `${absoluteText}（${relativeText}）`;
}

// DOMに触れない純粋関数: stale(最終確認値が古い可能性がある)なデータでは、
// 現在も有効なreset予定であるかのように誤認させる「あと...」という将来カウントダウンを出さない。
// 「reset時刻超過」「不明」「未設定」はカウントダウンの主張ではない事実表記のため、staleでも維持する。
function suppressCountdownIfStale(relativeText, stale) {
  if (!stale) return relativeText;
  return relativeText.startsWith("あと") ? "" : relativeText;
}

// next_reset_atはISO文字列またはnull。負数の秒数を表示せず「reset時刻超過」に丸める。
function resetRelativeText(nextResetAt) {
  if (!nextResetAt) return "未設定";
  const target = new Date(nextResetAt);
  if (Number.isNaN(target.getTime())) return "不明";
  const diffSeconds = Math.floor((target.getTime() - Date.now()) / 1000);
  if (diffSeconds < 0) return "reset時刻超過";
  return `あと${fmtDurationJa(diffSeconds)}`;
}

// DOMに触れない純粋関数: アプリ自身の次回スケジュール(GitHubの「アプリの次回取得予定」、
// Codexの「次回自動更新予定」)専用の相対時間。GitHub/Claude/Codexのresetまでの相対時間
// (resetRelativeText/githubSecondsUntilResetText)とは意味が異なる別概念のため、
// 「reset時刻超過」は使わない(このスケジュールはGitHub側のreset予定ではなくアプリ自身の
// 未来予定なので、reset語を混同させない)。同様に、過去の予定時刻を「まもなく」とも表現しない
// (スケジューラが次回tickで再取得するのを待っている状態を「再取得待ち」で正確に表す)。
// 不正な日時では相対表示なし(空文字)を返し、呼び出し側は絶対時刻のみにフォールバックする。
function fmtAppScheduleRelative(isoString) {
  if (!isoString) return "";
  const target = new Date(isoString);
  if (Number.isNaN(target.getTime())) return "";
  const diffSeconds = Math.floor((target.getTime() - Date.now()) / 1000);
  if (diffSeconds < 0) return "再取得待ち";
  return `あと${fmtDurationJa(diffSeconds)}`;
}

// サーバー側のseconds_until_resetは負値になり得るため、同様に丸める。
function githubSecondsUntilResetText(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "不明";
  if (seconds < 0) return "reset時刻超過";
  return `あと${fmtDurationJa(seconds)}`;
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
// stale=trueはlast_known(直近取得失敗時の最終成功値)由来を意味し、resetまでの「あと...」
// カウントダウンは抑制する(絶対時刻はそのまま表示する)。
// providerの識別は色(box-shadow)だけに依存させず、"GitHub "を明示ラベルとして付与する。
function githubResourceCardHtml(resource, stale = false) {
  if (!resource) return "";
  const statusClass = githubStatusClass(resource.status);
  const titleHtml = `<span class="compact-service-name">${escapeHtml(`GitHub ${githubResourceLabel(resource.resource)}`)}</span>`;
  if (resource.status === "Error") {
    return `
      <article class="compact-card compact-github-card compact-provider-github">
        <div class="compact-card-head">
          ${titleHtml}
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

  const relativeText = suppressCountdownIfStale(githubSecondsUntilResetText(resource.seconds_until_reset), stale);
  const resetText = fmtAbsoluteWithRelative(fmtDateOrUnknown(resource.reset_at_local), relativeText);

  return `
    <article class="compact-card compact-github-card compact-provider-github">
      <div class="compact-card-head">
        ${titleHtml}
        <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
      </div>
      ${bodyBlock}
      <div class="compact-meta-row"><span>reset: ${resetText}</span></div>
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

// DOMに触れない純粋関数: app.jsのgithubLimitedCauseと同一ロジック。
// Overall判定(app/github_rate_limit.py)には触れず、core/graphqlのresource statusから
// Exhausted(枠を使い切った)かReset overdue(reset時刻経過後も未更新)かを表示用に区別する。
// 表示優先順位はバックエンドの重大度順(Reset overdue > Exhausted)とは独立に決めている:
// Exhaustedが1件でもあればRATE LIMITEDを優先表示し、Exhaustedが無い場合のみRESET OVERDUEを
// 表示する。同一status同士がtieする場合はcoreを優先し、determine_overallのtie-breakと一致させる。
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

// DOMに触れない純粋関数: app.jsのgithubLimitedBannerHtmlと同一の区別ルールをcompact向けに描画する。
// stale=trueは/api/github-rate-limitのlast_known(直近取得失敗時の最終成功値)由来を意味し、
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

  const cls = [
    "compact-github-limited-banner",
    isOverdue ? "compact-banner-overdue" : "compact-banner-limited",
    stale ? "compact-banner-stale" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return `<div class="${cls}"><span class="compact-github-limited-badge">${escapeHtml(badgeText)}</span>${causeLabel ? ` ${escapeHtml(causeLabel)}` : ""}</div>`;
}

// DOMに触れない純粋関数: app.jsのgithubSecondaryRateLimitBannerHtmlと同一ルール。
// secondary rate limitはprimary resourceの枯渇とは別要因(gh api rate_limit自体の呼び出し失敗)
// なので、primary resourceのreset時刻は流用せず、専用バナーとして分けて表示する。
function githubSecondaryRateLimitBannerHtml(data) {
  if (!data || !data.error || data.error.error_type !== "secondary_rate_limit") return "";
  return `<div class="compact-github-limited-banner compact-banner-secondary"><span class="compact-github-limited-badge">SECONDARY RATE LIMIT</span></div>`;
}

// DOMに触れない純粋関数: app.jsのgithubAutoRefreshNoticeHtmlと同一ルール。
// ここでの「次回」はアプリ自身が次にgh api rate_limitを叩くタイミングであり、GitHub側の
// 制限解除予定(reset時刻)ではない — /compactでも文言でこの区別を保つ。相対時間は
// fmtAppScheduleRelative(resetRelativeTextとは別関数)を使い、「reset時刻超過」や
// 過去日時への「まもなく」表示を出さない。
function githubAutoRefreshNoticeHtml(data) {
  if (!data) return "";
  if (data.refreshing) {
    return `<div class="compact-auto-refresh-notice">自動確認中…</div>`;
  }
  if (data.auto_refresh_pending && data.next_auto_refresh_at) {
    const nextFetchText = fmtAbsoluteWithRelative(
      fmtDateOrUnknown(data.next_auto_refresh_at),
      fmtAppScheduleRelative(data.next_auto_refresh_at)
    );
    return `<div class="compact-auto-refresh-notice">アプリの次回取得予定: ${nextFetchText}</div>`;
  }
  if (data.last_auto_refresh_error) {
    return `<div class="compact-auto-refresh-notice">自動再取得に失敗しました: ${escapeHtml(data.last_auto_refresh_error.user_message || "")}</div>`;
  }
  return "";
}

// DOMに触れない純粋関数: GET /api/github-rate-limit のレスポンスからGitHubセクションのHTMLを組み立てる。
// REST/GraphQL/Searchは固定順(状態による並び替えをしない)で常に横並び表示する。
function githubSectionHtml(data) {
  const autoRefreshNoticeHtml = githubAutoRefreshNoticeHtml(data);

  if (!data || !data.fetched) {
    const usingLastKnown = data && !data.fetched && data.last_known;
    const secondaryHtml = githubSecondaryRateLimitBannerHtml(data);
    if (!usingLastKnown) {
      return `
        ${secondaryHtml}
        <div class="compact-card compact-empty compact-provider-github">GitHub Rate Limit: 未取得</div>
        ${autoRefreshNoticeHtml}`;
    }
    const resources = data.last_known.resources;
    const overall = data.last_known.overall;
    return `
      ${secondaryHtml}
      <div class="compact-stale-notice">直近の取得は失敗しました。以下は${fmtDateOrUnknown(data.last_known.collected_at)}時点の情報です。</div>
      ${githubLimitedBannerHtml(overall, resources, true)}
      ${githubOverallHtml(overall)}
      ${autoRefreshNoticeHtml}
      <div class="compact-github-grid-inner">
        ${githubResourceCardHtml(resources.core, true)}
        ${githubResourceCardHtml(resources.graphql, true)}
        ${resources.search ? githubResourceCardHtml(resources.search, true) : ""}
      </div>`;
  }

  return `
    ${githubLimitedBannerHtml(data.overall, data.resources, false)}
    ${githubOverallHtml(data.overall)}
    ${autoRefreshNoticeHtml}
    <div class="compact-github-grid-inner">
      ${githubResourceCardHtml(data.resources.core)}
      ${githubResourceCardHtml(data.resources.graphql)}
      ${data.resources.search ? githubResourceCardHtml(data.resources.search) : ""}
    </div>`;
}

// DOMに触れない純粋関数: Claude Code statusLineブリッジのキャッシュ1枠分(5時間 or 7日)のカードHTMLを組み立てる。
// remaining/usedはブリッジ側で既に0-100%へ検証済みの値のみを渡される想定。
// stale=trueは最終観測値が古い可能性があることを意味し、resetまでの「あと...」カウントダウンは抑制する。
function claudeUsageWindowHtml(label, window, stale = false) {
  if (!window) {
    return `
      <article class="compact-card compact-provider-claude">
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
        <div class="compact-no-limit">未観測</div>
      </article>`;
  }
  const width = Math.min(Math.max(window.used_percentage, 0), 100);
  const relativeText = suppressCountdownIfStale(resetRelativeText(window.resets_at), stale);
  const resetText = fmtAbsoluteWithRelative(fmtDateOrUnknown(window.resets_at), relativeText);
  return `
    <article class="compact-card compact-provider-claude">
      <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
      <div class="compact-percent-row">
        <span class="compact-percent-label">残り</span>
        <span class="compact-percent-value compact-percent-value-sm">${fmtNumber(window.remaining_percentage)}%</span>
      </div>
      <div class="compact-usage-line">使用済み ${fmtNumber(window.used_percentage)}%</div>
      <div class="compact-meter"><div class="compact-meter-fill compact-claude-usage" style="width:${width}%"></div></div>
      <div class="compact-meta-row"><span>reset: ${resetText}</span></div>
    </article>`;
}

// DOMに触れない純粋関数: GET /api/claude-code-usage のレスポンスからセクションHTMLを組み立てる。
// statusLineはpush型のため、staleは「取得不可」ではなく「最終観測値が古い」という意味で表示する。
// providerの識別は色だけに依存させず、カード内ラベルへ"Claude "を明示する。
function claudeCodeSectionHtml(data) {
  if (!data || !data.available) {
    const message = data && data.status === "invalid_cache" ? "取得不可" : "Claude Code実行後に取得";
    return `<div class="compact-card compact-empty compact-provider-claude">Claude Code使用率: ${message}</div>`;
  }

  const staleNoticeHtml = data.stale
    ? `<div class="compact-stale-notice">最終観測値(古い可能性があります)</div>`
    : "";

  return `
    ${staleNoticeHtml}
    <div class="compact-github-grid-inner">
      ${claudeUsageWindowHtml("Claude 5時間枠", data.five_hour, data.stale)}
      ${claudeUsageWindowHtml("Claude 7日枠", data.seven_day, data.stale)}
    </div>
    <div class="compact-stale-notice">最終観測: ${fmtDateOrUnknown(data.observed_at)}</div>
  `;
}

// DOMに触れない純粋関数: Codex Usageキャッシュ(自動取得 or 手動入力)1枠分(5時間 or 週次)のカードHTMLを組み立てる。
// remaining/usedは呼び出し元(自動adapterまたは管理画面保存時)で既に0-100%へ検証済みの値のみを渡される想定。
// resets_atを過ぎている場合は、古いpercentageを現在値のように強調表示せず「reset時刻超過」だけを示す。
// badgeLabelは表示中のsourceを示すバッジ文言(「自動取得」「最終自動取得値」「手動確認値」)。
// stale=trueは最終観測値が古い可能性があることを意味し、resetまでの「あと...」カウントダウンは抑制する
// (reset時刻超過自体は事実表記のため、staleでも維持しresetExceeded判定にも使う)。
function codexUsageWindowHtml(label, window, badgeLabel = "手動確認値", stale = false) {
  if (!window) {
    return `
      <article class="compact-card compact-provider-codex">
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
        <div class="compact-no-limit">未入力</div>
      </article>`;
  }
  const rawRelative = resetRelativeText(window.resets_at);
  const resetExceeded = rawRelative === "reset時刻超過";
  const relativeText = suppressCountdownIfStale(rawRelative, stale);
  const resetText = fmtAbsoluteWithRelative(fmtDateOrUnknown(window.resets_at), relativeText);
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
    <article class="compact-card compact-provider-codex">
      <div class="compact-card-head">
        <span class="compact-service-name">${escapeHtml(label)}</span>
        <span class="compact-source-badge">${escapeHtml(badgeLabel)}</span>
      </div>
      ${bodyBlock}
      <div class="compact-meta-row"><span>reset: ${resetText}</span></div>
    </article>`;
}

// DOMに触れない純粋関数: 自動取得cache(GET /api/codex-rate-limits)と手動snapshot(GET /api/codex-usage)から
// 表示すべきsource・データ・バッジ文言を決定する。優先順位(自動cacheと手動cacheを1ファイルへ統合はしない):
//   1. 自動cacheが利用可能(staleでも) -> 自動データを表示(バッジ「自動取得」/ stale時「最終自動取得値」)
//   2. 自動cacheが利用不可・手動snapshotが利用可能 -> 手動データへfallback(バッジ「手動確認値」)
//   3. どちらも利用不可 -> 未取得 or 取得不可
function resolveCodexDisplay(auto, manual) {
  if (auto && auto.available) {
    return {
      source: "codex_app_server",
      badgeLabel: auto.stale ? "最終自動取得値" : "自動取得",
      stale: auto.stale,
      observed_at: auto.observed_at,
      five_hour: auto.five_hour,
      weekly: auto.weekly,
      status: null,
    };
  }
  if (manual && manual.available) {
    return {
      source: "codex_manual",
      badgeLabel: "手動確認値",
      stale: manual.stale,
      observed_at: manual.observed_at,
      five_hour: manual.five_hour,
      weekly: manual.weekly,
      status: null,
    };
  }
  const invalid = (auto && auto.status === "invalid_cache") || (manual && manual.status === "invalid_cache");
  return {
    source: null,
    badgeLabel: null,
    stale: false,
    observed_at: null,
    five_hour: null,
    weekly: null,
    status: invalid ? "invalid_cache" : "not_observed",
  };
}

// DOMに触れない純粋関数: GET /api/codex-rate-limits(自動取得) と GET /api/codex-usage(手動入力) の
// レスポンスからCodex Usageセクション全体のHTMLを組み立てる。
// 自動更新間隔は「あと」を伴わない期間の長さそのものなので、fmtDurationJaの結果をそのまま使う。
function fmtMinutesFromSeconds(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds)) return "不明";
  return fmtDurationJa(Math.max(seconds, 0));
}

// DOMに触れない純粋関数: サーバー側10分間隔schedulerの状態(GET /api/codex-rate-limitsに
// 含まれるauto_refresh_interval_seconds / next_auto_refresh_at)を1行だけ表示する。
// ここから更新系リクエストを送ることはない(表示専用)。「自動更新間隔」(intervalText)と
// 「次回自動更新予定」(nextText)を混同しない。nextTextはアプリ自身のスケジュールなので
// fmtAppScheduleRelativeを使い、GitHub側のresetまでを表す「reset時刻超過」は出さない。
function codexPeriodicRefreshNoticeHtml(auto) {
  if (!auto || typeof auto.auto_refresh_interval_seconds !== "number") return "";
  const intervalText = fmtMinutesFromSeconds(auto.auto_refresh_interval_seconds);
  const nextText = auto.next_auto_refresh_at
    ? fmtAbsoluteWithRelative(fmtDateOrUnknown(auto.next_auto_refresh_at), fmtAppScheduleRelative(auto.next_auto_refresh_at))
    : "未定";
  return `<div class="compact-stale-notice">自動更新: ${escapeHtml(intervalText)} / 次回予定: ${escapeHtml(nextText)}</div>`;
}

// providerの識別は色だけに依存させず、カード内ラベルへ"Codex "を明示する。
function codexUsageSectionHtml(auto, manual) {
  const resolved = resolveCodexDisplay(auto, manual);
  if (!resolved.source) {
    const message = resolved.status === "invalid_cache" ? "取得不可" : "自動取得または手動入力してください";
    return `
      <div class="compact-card compact-empty compact-provider-codex">Codex Usage: ${message}</div>
      ${codexPeriodicRefreshNoticeHtml(auto)}
    `;
  }

  const staleNoticeHtml = resolved.stale
    ? `<div class="compact-stale-notice">${escapeHtml(resolved.badgeLabel)}・古い可能性があります</div>`
    : "";
  const lastLabel = resolved.source === "codex_manual" ? "最終手動確認" : "最終自動取得";

  return `
    ${staleNoticeHtml}
    <div class="compact-github-grid-inner">
      ${codexUsageWindowHtml("Codex 5時間枠", resolved.five_hour, resolved.badgeLabel, resolved.stale)}
      ${codexUsageWindowHtml("Codex 週次枠", resolved.weekly, resolved.badgeLabel, resolved.stale)}
    </div>
    <div class="compact-stale-notice">${lastLabel}: ${fmtDateOrUnknown(resolved.observed_at)}</div>
    ${codexPeriodicRefreshNoticeHtml(auto)}
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

function renderCodexUsage(auto, manual) {
  document.querySelector("#codexUsageCards").innerHTML = codexUsageSectionHtml(auto, manual);
}

// GETのみ: /api/dashboard・/api/github-rate-limit・/api/claude-code-usage・/api/codex-rate-limits・
// /api/codex-usage はいずれも保存済みの値を返すだけで、gh api rate_limitやClaude Code/Codex App Server
// の起動などの外部コマンド/APIをここから直接実行することはない(更新系リクエストはここから一切送信しない)。
async function loadCompact() {
  try {
    const [dashboard, github, claudeCodeUsage, codexRateLimits, codexUsage] = await Promise.all([
      fetchJson("/api/dashboard"),
      fetchJson("/api/github-rate-limit"),
      fetchJson("/api/claude-code-usage"),
      fetchJson("/api/codex-rate-limits"),
      fetchJson("/api/codex-usage"),
    ]);
    state.dashboard = dashboard;
    state.github = github;
    state.claudeCodeUsage = claudeCodeUsage;
    state.codexRateLimits = codexRateLimits;
    state.codexUsage = codexUsage;
    renderLimitCards(dashboard);
    renderGithubSection(github);
    renderClaudeCodeUsage(claudeCodeUsage);
    renderCodexUsage(codexRateLimits, codexUsage);
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
    fmtDurationJa,
    fmtAbsoluteWithRelative,
    suppressCountdownIfStale,
    fmtAppScheduleRelative,
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
    githubLimitedCause,
    githubLimitedBannerHtml,
    githubSecondaryRateLimitBannerHtml,
    githubAutoRefreshNoticeHtml,
    githubSectionHtml,
    claudeUsageWindowHtml,
    claudeCodeSectionHtml,
    codexUsageWindowHtml,
    resolveCodexDisplay,
    codexPeriodicRefreshNoticeHtml,
    codexUsageSectionHtml,
  };
}
