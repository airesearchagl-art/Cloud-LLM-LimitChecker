const REFRESH_INTERVAL_MS = 30000;

const state = {
  dashboard: [],
  github: null,
  claudeCodeUsage: null,
  claudeDesktopCloudUsage: null,
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

// DOMに触れない純粋関数: GitHub/Claude/Codexカード共通の右側RESETブロックを組み立てる。
// reset絶対時刻と残り時間を、カード下端の小さな補助行ではなく、カード右側の余白を使った
// 独立ブロックとして主要情報の扱いで表示する。
// 左側のquota残量(残り97.9%等)と同じ「残り」ラベルを使うと同一カード内で意味が
// 衝突するため、右ブロックのラベルは値の種類で出し分ける:
//   - countdown(relativeTextが「あと」で始まる) -> ラベル「リセットまで」、値は「あと」を除いた期間
//   - reset時刻超過/不明/未設定(カウントダウンではない事実表記) -> ラベル「状態」、値はそのまま
//   - relativeTextが空(stale抑制等で何も出さない) -> 絶対時刻のみ表示
function resetBlockHtml(absoluteText, relativeText) {
  if (!relativeText) {
    return `
      <div class="compact-reset-block">
        <div class="compact-reset-label">RESET</div>
        <div class="compact-reset-absolute">${escapeHtml(absoluteText)}</div>
      </div>`;
  }
  const isCountdown = relativeText.startsWith("あと");
  const remainingLabel = isCountdown ? "リセットまで" : "状態";
  const remainingValue = isCountdown ? relativeText.slice("あと".length) : relativeText;
  return `
    <div class="compact-reset-block">
      <div class="compact-reset-label">RESET</div>
      <div class="compact-reset-absolute">${escapeHtml(absoluteText)}</div>
      <div class="compact-reset-remaining-label">${escapeHtml(remainingLabel)}</div>
      <div class="compact-reset-remaining-value">${escapeHtml(remainingValue)}</div>
    </div>`;
}

// DOMに触れない純粋関数: GitHubの1リソース分のカードHTMLを組み立てる。UTC詳細は表示しない。
// stale=trueはlast_known(直近取得失敗時の最終成功値)由来を意味し、resetまでの「あと...」
// カウントダウンは抑制する(絶対時刻はそのまま表示する)。
// providerの識別は色(box-shadow)だけに依存させず、"GitHub "を明示ラベルとして付与する。
// カードのstable ID(github.core/github.graphql/github.search)はresource.resourceの値と
// 1対1で対応する(githubResourceLabelが対応する3値のみを想定するのと同じ前提)。
// DOM位置からの推測ではなく、HTML生成時にdata-card-idとして直接埋め込む。
function githubResourceCardHtml(resource, stale = false) {
  if (!resource) return "";
  const cardId = `github.${resource.resource}`;
  const statusClass = githubStatusClass(resource.status);
  const titleHtml = `<span class="compact-service-name">${escapeHtml(`GitHub ${githubResourceLabel(resource.resource)}`)}</span>`;
  if (resource.status === "Error") {
    return `
      <article class="compact-card compact-github-card compact-provider-github" data-card-id="${escapeHtml(cardId)}">
        <div class="compact-card-head">
          ${titleHtml}
          <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
        </div>
        <div class="compact-usage-line">${escapeHtml(resource.error_message || "")}</div>
      </article>`;
  }

  const hasPercent = resource.remaining_percent !== null && resource.remaining_percent !== undefined;
  const width = Math.min(Math.max(resource.usage_percent ?? 0, 0), 100);
  const leftBlock = hasPercent
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
  const absoluteText = fmtDateOrUnknown(resource.reset_at_local);

  return `
    <article class="compact-card compact-github-card compact-provider-github" data-card-id="${escapeHtml(cardId)}">
      <div class="compact-card-head">
        ${titleHtml}
        <span class="compact-status ${statusClass}">${escapeHtml(resource.status)}</span>
      </div>
      <div class="compact-card-body">
        <div class="compact-card-left">${leftBlock}</div>
        ${resetBlockHtml(absoluteText, relativeText)}
      </div>
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
// cardId(例: "claude.five_hour")はレイアウトカスタマイズ用のstable ID。表示文言(label)や
// DOM位置から推測せず、呼び出し元(claudeCodeSectionHtml)が明示的に渡す。省略時(null)は
// data-card-id属性を付与しない(既存呼び出し・既存テストとの後方互換のため末尾の省略可能引数とする)。
// badgeLabel(例:「CLI自動取得」「Desktop Cloud 手動確認値」)はresolveClaudeCodeUsageDisplayが
// 決めたsource_labelをそのまま渡すだけで、ここではCLI/manualの判定は一切行わない。
function claudeUsageWindowHtml(label, window, stale = false, cardId = null, badgeLabel = null) {
  const cardIdAttr = cardId ? ` data-card-id="${escapeHtml(cardId)}"` : "";
  const badgeHtml = badgeLabel ? `<span class="compact-source-badge">${escapeHtml(badgeLabel)}</span>` : "";
  if (!window) {
    return `
      <article class="compact-card compact-provider-claude"${cardIdAttr}>
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span>${badgeHtml}</div>
        <div class="compact-no-limit">未観測</div>
      </article>`;
  }
  const width = Math.min(Math.max(window.used_percentage, 0), 100);
  const relativeText = suppressCountdownIfStale(resetRelativeText(window.resets_at), stale);
  const absoluteText = fmtDateOrUnknown(window.resets_at);
  return `
    <article class="compact-card compact-provider-claude"${cardIdAttr}>
      <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span>${badgeHtml}</div>
      <div class="compact-card-body">
        <div class="compact-card-left">
          <div class="compact-percent-row">
            <span class="compact-percent-label">残り</span>
            <span class="compact-percent-value compact-percent-value-sm">${fmtNumber(window.remaining_percentage)}%</span>
          </div>
          <div class="compact-usage-line">使用済み ${fmtNumber(window.used_percentage)}%</div>
          <div class="compact-meter"><div class="compact-meter-fill compact-claude-usage" style="width:${width}%"></div></div>
        </div>
        ${resetBlockHtml(absoluteText, relativeText)}
      </div>
    </article>`;
}

// DOMに触れない純粋関数: CLI statusLine自動cache(GET /api/claude-code-usage)とDesktop Cloud
// 手動snapshot(GET /api/claude-code-usage/manual)から、表示すべきsource・データ・バッジ文言を
// 決定する。windowを個別に混ぜない(five_hourは自動、seven_dayは手動、のような合成をしない) —
// 常にどちらか一方のsnapshot全体だけを選ぶ。
// 規則: 有効な(available=trueの)snapshotだけを候補にし、observed_atが新しい方を選ぶ。
// 同時刻ならCLI自動を優先する。無効(invalid_cache)なsnapshotは候補にしない。
// manual側はavailable=trueに加えて両window(five_hour・seven_day)が揃っていることも確認する —
// サーバー側(claude_desktop_cloud_usage_cache.validate_cache_record)は既に両window必須で
// 検証しているが、ここでも同じ不変条件を守ることで、片方だけのmanual snapshotが完全なauto
// snapshotの片方の枠を(表示上)覆い隠す事態を防ぐ。auto側はstatusLine由来で片方だけの観測が
// 正当にあり得るため、この追加チェックはmanualにのみ課す(autoの片方欠落許容は変更しない)。
// stale判定は各snapshot自身が既に計算済みの値(load_snapshotのSTALE_THRESHOLD_SECONDS)を
// そのまま使う(ここで独自の閾値判定はしない)。CLIが後で新しいobserved_atを書けば、
// このresolve関数が自動的に自動snapshotへ選び直す(手動値へ固定されたままにはならない)。
function resolveClaudeCodeUsageDisplay(auto, manual) {
  const autoValid = !!(auto && auto.available);
  const manualValid = !!(manual && manual.available && manual.five_hour && manual.seven_day);

  let winner = null;
  if (autoValid && manualValid) {
    const autoTime = new Date(auto.observed_at).getTime();
    const manualTime = new Date(manual.observed_at).getTime();
    // 同時刻(またはどちらかの日時が不正でNaN比較になった場合)はCLI自動を優先する。
    winner = manualTime > autoTime ? "manual" : "auto";
  } else if (autoValid) {
    winner = "auto";
  } else if (manualValid) {
    winner = "manual";
  }

  if (winner === "auto") {
    return {
      available: true,
      source: "claude_code_statusline",
      // source_labelは常に取得元(CLI自動取得)を表す。staleかどうかはdata.staleが別途持ち、
      // 「最終観測値(古い可能性があります)」はclaudeCodeSectionHtml側でstale専用の表示として
      // source_labelとは別に一度だけ組み立てる(取得元とstale表記を同じ語で二重に出さないため)。
      source_label: "CLI自動取得",
      stale: auto.stale,
      observed_at: auto.observed_at,
      five_hour: auto.five_hour,
      seven_day: auto.seven_day,
      status: null,
    };
  }
  if (winner === "manual") {
    return {
      available: true,
      source: "claude_desktop_cloud_manual",
      source_label: "Desktop Cloud 手動確認値",
      stale: manual.stale,
      observed_at: manual.observed_at,
      five_hour: manual.five_hour,
      seven_day: manual.seven_day,
      status: null,
    };
  }

  const invalid = (auto && auto.status === "invalid_cache") || (manual && manual.status === "invalid_cache");
  return {
    available: false,
    source: null,
    source_label: null,
    stale: false,
    observed_at: null,
    five_hour: null,
    seven_day: null,
    status: invalid ? "invalid_cache" : "not_observed",
  };
}

// DOMに触れない純粋関数: resolveClaudeCodeUsageDisplayが選んだ1つのsnapshotからセクションHTMLを
// 組み立てる。statusLineはpush型・Desktop Cloud手動値は確認型のため、staleは「取得不可」ではなく
// 「最終観測値が古い」という意味で表示する。providerの識別は色だけに依存させず、
// カード内ラベルへ"Claude "を明示する。data.source_labelが無い(=旧呼び出し・テスト互換)場合は
// バッジ自体を表示しない。source_label(取得元: CLI自動取得/Desktop Cloud 手動確認値)とstale表記
// (最終観測値(古い可能性があります))は常に別々に組み立てる — stale時でも取得元が分かるようにしつつ、
// 同じ語("最終観測値")を二重に表示しないため。
function claudeCodeSectionHtml(data) {
  if (!data || !data.available) {
    const message = data && data.status === "invalid_cache" ? "取得不可" : "Claude Code実行後に取得";
    return `<div class="compact-card compact-empty compact-provider-claude">Claude Code使用率: ${message}</div>`;
  }

  const staleNoticeHtml = data.stale
    ? `<div class="compact-stale-notice">${data.source_label ? `${escapeHtml(data.source_label)}・` : ""}最終観測値(古い可能性があります)</div>`
    : "";

  return `
    ${staleNoticeHtml}
    <div class="compact-github-grid-inner">
      ${claudeUsageWindowHtml("Claude 5時間枠", data.five_hour, data.stale, "claude.five_hour", data.source_label)}
      ${claudeUsageWindowHtml("Claude 7日枠", data.seven_day, data.stale, "claude.seven_day", data.source_label)}
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
// cardId(例: "codex.five_hour")はレイアウトカスタマイズ用のstable ID。表示文言(label)や
// DOM位置から推測せず、呼び出し元(codexUsageSectionHtml)が明示的に渡す。省略時(null)は
// data-card-id属性を付与しない(既存呼び出し・既存テストとの後方互換のため末尾の省略可能引数とする)。
function codexUsageWindowHtml(label, window, badgeLabel = "手動確認値", stale = false, cardId = null) {
  const cardIdAttr = cardId ? ` data-card-id="${escapeHtml(cardId)}"` : "";
  if (!window) {
    return `
      <article class="compact-card compact-provider-codex"${cardIdAttr}>
        <div class="compact-card-head"><span class="compact-service-name">${escapeHtml(label)}</span></div>
        <div class="compact-no-limit">未入力</div>
      </article>`;
  }
  const rawRelative = resetRelativeText(window.resets_at);
  const resetExceeded = rawRelative === "reset時刻超過";
  const relativeText = suppressCountdownIfStale(rawRelative, stale);
  const absoluteText = fmtDateOrUnknown(window.resets_at);
  // reset時刻超過はRESETブロック(状態: reset時刻超過)側にのみ表示し、ここでは重複させない。
  // percentage/meterは引き続き非表示にする(空文字を返すだけで、代替テキストは出さない)。
  const leftBlock = resetExceeded
    ? ""
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
    <article class="compact-card compact-provider-codex"${cardIdAttr}>
      <div class="compact-card-head">
        <span class="compact-service-name">${escapeHtml(label)}</span>
        <span class="compact-source-badge">${escapeHtml(badgeLabel)}</span>
      </div>
      <div class="compact-card-body">
        <div class="compact-card-left">${leftBlock}</div>
        ${resetBlockHtml(absoluteText, relativeText)}
      </div>
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
      ${codexUsageWindowHtml("Codex 5時間枠", resolved.five_hour, resolved.badgeLabel, resolved.stale, "codex.five_hour")}
      ${codexUsageWindowHtml("Codex 週次枠", resolved.weekly, resolved.badgeLabel, resolved.stale, "codex.weekly")}
    </div>
    <div class="compact-stale-notice">${lastLabel}: ${fmtDateOrUnknown(resolved.observed_at)}</div>
    ${codexPeriodicRefreshNoticeHtml(auto)}
  `;
}

// ============================================================================
// レイアウトカスタマイズ(presentation層のみ): セクション/カードの並べ替え・表示切替。
// 取得ロジック・domain判定・stale判定・app schedule等には一切触れない。
// 保存内容にはID/順序/表示状態のみを含み、usage値・reset時刻・account情報は含めない。
// ============================================================================

const LAYOUT_STORAGE_KEY = "cloudLlmCompactLayout";
const LAYOUT_VERSION = 1;

// 各Providerセクションの識別子・見出し・カード格納先grid(card-levelの並べ替え対象が
// あるセクションのみgridSelectorを持つ。dashboardは既存DB行由来でstable IDを持たないため
// section単位の並べ替えのみ対象とする)。
const SECTION_META = [
  { id: "section.dashboard", label: "ダッシュボード", containerId: "dashboardSection", gridSelector: null },
  {
    id: "section.github",
    label: "GitHub API Rate Limit",
    containerId: "githubSection",
    gridSelector: "#githubCards .compact-github-grid-inner",
  },
  {
    id: "section.claude",
    label: "Claude Code Usage",
    containerId: "claudeSection",
    gridSelector: "#claudeCodeUsageCards .compact-github-grid-inner",
  },
  {
    id: "section.codex",
    label: "Codex Usage",
    containerId: "codexSection",
    gridSelector: "#codexUsageCards .compact-github-grid-inner",
  },
];

const DEFAULT_SECTION_ORDER = SECTION_META.map((section) => section.id);

// 各セクション内のカードのstable ID。表示文言や配列indexではなく、この固定IDで
// 並べ替え・表示状態を保存する。レンダリング関数(githubSectionHtml等)は常にこの順で
// カードを出力するため、DOM上は位置によってIDへ対応付ける(レンダリング関数自体は変更しない)。
const CARD_META_BY_SECTION = {
  "section.github": [
    { id: "github.core", label: "GitHub REST API" },
    { id: "github.graphql", label: "GitHub GraphQL API" },
    { id: "github.search", label: "GitHub Search API" },
  ],
  "section.claude": [
    { id: "claude.five_hour", label: "Claude 5時間枠" },
    { id: "claude.seven_day", label: "Claude 7日枠" },
  ],
  "section.codex": [
    { id: "codex.five_hour", label: "Codex 5時間枠" },
    { id: "codex.weekly", label: "Codex 週次枠" },
  ],
};

const ALL_KNOWN_CARD_IDS = Object.values(CARD_META_BY_SECTION)
  .flat()
  .map((card) => card.id);

function cardMetaById(cardId) {
  for (const cards of Object.values(CARD_META_BY_SECTION)) {
    const found = cards.find((card) => card.id === cardId);
    if (found) return found;
  }
  return null;
}

function sectionMetaById(sectionId) {
  return SECTION_META.find((section) => section.id === sectionId) || null;
}

// DOMに触れない純粋関数: 初期(既定)のlayout stateを返す。
function defaultLayoutState() {
  const cardOrderBySection = {};
  for (const sectionId of Object.keys(CARD_META_BY_SECTION)) {
    cardOrderBySection[sectionId] = CARD_META_BY_SECTION[sectionId].map((card) => card.id);
  }
  return {
    version: LAYOUT_VERSION,
    sectionOrder: [...DEFAULT_SECTION_ORDER],
    cardOrderBySection,
    hiddenCardIds: [],
  };
}

// DOMに触れない純粋関数: 候補のID配列を「既知IDの集合」に対して検証・正規化する。
// 不明ID(defaultOrderに存在しないID)は無視し、重複IDは除去し、欠落ID(defaultOrderには
// あるが候補に無いID)はdefaultOrderでの順序を保ったまま末尾へ補完する。
// sectionOrder・cardOrderBySectionの各セクション別リストの両方で共通に使う。
function sanitizeIdOrder(candidate, defaultOrder) {
  const knownIds = new Set(defaultOrder);
  const seen = new Set();
  const result = [];
  if (Array.isArray(candidate)) {
    for (const id of candidate) {
      if (typeof id === "string" && knownIds.has(id) && !seen.has(id)) {
        seen.add(id);
        result.push(id);
      }
    }
  }
  for (const id of defaultOrder) {
    if (!seen.has(id)) {
      seen.add(id);
      result.push(id);
    }
  }
  return result;
}

// DOMに触れない純粋関数: JSON.parse済みの値を安全なlayout stateへ正規化する。
// 型不正・version不一致は丸ごと初期値へフォールバックする(部分的な信用はしない)。
// sectionOrder/cardOrderBySection内の各セクションはsanitizeIdOrderで個別に検証する
// (Providerをまたいだ不明なカードIDが紛れ込んでいても、そのセクションのdefaultOrderに
// 存在しない限り無視されるため、セクションをまたぐカード混在はデータレベルでも起こらない)。
// hiddenCardIdsは既知カードIDのみを保持し、文字列以外・重複・不明IDを除去する。
function sanitizeLayoutState(raw) {
  const fallback = defaultLayoutState();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback;
  if (raw.version !== LAYOUT_VERSION) return fallback;

  const sectionOrder = sanitizeIdOrder(raw.sectionOrder, DEFAULT_SECTION_ORDER);

  const cardOrderBySection = {};
  const rawCardOrderBySection =
    raw.cardOrderBySection && typeof raw.cardOrderBySection === "object" && !Array.isArray(raw.cardOrderBySection)
      ? raw.cardOrderBySection
      : {};
  for (const sectionId of Object.keys(CARD_META_BY_SECTION)) {
    const defaults = CARD_META_BY_SECTION[sectionId].map((card) => card.id);
    cardOrderBySection[sectionId] = sanitizeIdOrder(rawCardOrderBySection[sectionId], defaults);
  }

  const hiddenCardIds = Array.isArray(raw.hiddenCardIds)
    ? [...new Set(raw.hiddenCardIds.filter((id) => typeof id === "string" && ALL_KNOWN_CARD_IDS.includes(id)))]
    : [];

  return { version: LAYOUT_VERSION, sectionOrder, cardOrderBySection, hiddenCardIds };
}

// DOMに触れない純粋関数: localStorageから読み込んだ生文字列(nullや空文字を含む)を
// 安全なlayout stateへ変換する。JSON.parse自体が失敗する場合も初期値へフォールバックする。
function loadLayoutStateFromRaw(rawString) {
  if (!rawString) return defaultLayoutState();
  let parsed;
  try {
    parsed = JSON.parse(rawString);
  } catch (error) {
    return defaultLayoutState();
  }
  return sanitizeLayoutState(parsed);
}

// DOMに触れない純粋関数: 保存用のJSON文字列を組み立てる。usage値・reset時刻・
// account情報等は一切含めず、ID・順序・表示状態のみを保存する。
function serializeLayoutState(state) {
  return JSON.stringify({
    version: LAYOUT_VERSION,
    sectionOrder: state.sectionOrder,
    cardOrderBySection: state.cardOrderBySection,
    hiddenCardIds: state.hiddenCardIds,
  });
}

// DOMに触れない純粋関数: 配列内の指定IDを新しいindex位置へ移動する。
// 上へ/下へボタン(index±1を指定)とドラッグ&ドロップ(ドロップ先のindexを指定)の
// 両方がこの関数を経由するため、ボタン操作とドラッグ操作は同じ並べ替え結果になる。
// 存在しないIDやindex範囲外の指定は安全に無視/クランプする。
function moveIdToIndex(order, id, newIndex) {
  const currentIndex = order.indexOf(id);
  if (currentIndex === -1) return order;
  const next = [...order];
  next.splice(currentIndex, 1);
  const clampedIndex = Math.max(0, Math.min(newIndex, next.length));
  next.splice(clampedIndex, 0, id);
  return next;
}

// 現在のlayout stateと編集モードフラグ(共にDOM外で保持する純粋なデータ)。
const layout = {
  state: defaultLayoutState(),
  editMode: false,
};

function loadLayoutStateFromStorage() {
  let raw = null;
  try {
    raw = window.localStorage.getItem(LAYOUT_STORAGE_KEY);
  } catch (error) {
    // プライベートブラウジング等でlocalStorageが使えない環境でも表示は継続する。
    raw = null;
  }
  return loadLayoutStateFromRaw(raw);
}

function saveLayoutStateToStorage(currentState) {
  try {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, serializeLayoutState(currentState));
  } catch (error) {
    // 容量超過やprivateモード等で保存に失敗しても、表示自体は継続する(この回だけ復元されない)。
  }
}

function announceLayout(message) {
  const el = document.getElementById("layoutAnnounce");
  if (el) el.textContent = message;
}

function cardLabelById(cardId) {
  const meta = cardMetaById(cardId);
  return meta ? meta.label : cardId;
}

function sectionLabelById(sectionId) {
  const meta = sectionMetaById(sectionId);
  return meta ? meta.label : sectionId;
}

// ブラウザ標準confirmを直接呼ばず関数越しにすることで、テスト/プレビューから差し替え可能にする。
function confirmLayoutReset() {
  if (typeof window === "undefined" || typeof window.confirm !== "function") return true;
  return window.confirm("レイアウトを初期配置に戻しますか？ 表示・非表示や並び順の変更が失われます。");
}

function persistAndApplyLayout(message) {
  saveLayoutStateToStorage(layout.state);
  applyFullLayout();
  if (message) announceLayout(message);
}

function moveCard(sectionId, cardId, direction) {
  const order = layout.state.cardOrderBySection[sectionId] || [];
  const currentIndex = order.indexOf(cardId);
  if (currentIndex === -1) return;
  layout.state.cardOrderBySection[sectionId] = moveIdToIndex(order, cardId, currentIndex + direction);
  persistAndApplyLayout(`${cardLabelById(cardId)}を移動しました`);
}

function moveCardToIndex(sectionId, cardId, targetIndex) {
  const order = layout.state.cardOrderBySection[sectionId] || [];
  layout.state.cardOrderBySection[sectionId] = moveIdToIndex(order, cardId, targetIndex);
  persistAndApplyLayout(`${cardLabelById(cardId)}を移動しました`);
}

function moveSection(sectionId, direction) {
  const currentIndex = layout.state.sectionOrder.indexOf(sectionId);
  if (currentIndex === -1) return;
  layout.state.sectionOrder = moveIdToIndex(layout.state.sectionOrder, sectionId, currentIndex + direction);
  persistAndApplyLayout(`${sectionLabelById(sectionId)}セクションを移動しました`);
}

function moveSectionToIndex(sectionId, targetIndex) {
  layout.state.sectionOrder = moveIdToIndex(layout.state.sectionOrder, sectionId, targetIndex);
  persistAndApplyLayout(`${sectionLabelById(sectionId)}セクションを移動しました`);
}

function toggleCardVisibility(cardId, hidden) {
  const hiddenSet = new Set(layout.state.hiddenCardIds);
  if (hidden) {
    hiddenSet.add(cardId);
  } else {
    hiddenSet.delete(cardId);
  }
  layout.state.hiddenCardIds = [...hiddenSet];
  persistAndApplyLayout(`${cardLabelById(cardId)}を${hidden ? "非表示" : "表示"}にしました`);
}

function resetLayoutToDefault() {
  layout.state = defaultLayoutState();
  saveLayoutStateToStorage(layout.state);
  applyFullLayout();
  announceLayout("レイアウトを初期配置に戻しました");
}

function setLayoutEditMode(enabled) {
  layout.editMode = enabled;
  const toggleBtn = document.getElementById("layoutEditToggle");
  const editBar = document.getElementById("layoutEditBar");
  if (toggleBtn) {
    toggleBtn.setAttribute("aria-pressed", String(enabled));
    toggleBtn.textContent = enabled ? "編集を終了" : "レイアウト編集";
  }
  if (editBar) editBar.hidden = !enabled;
  document.body.classList.toggle("compact-layout-editing", enabled);
  applyFullLayout();
  announceLayout(enabled ? "レイアウト編集モードを開始しました" : "レイアウト編集モードを終了しました");
}

// 編集モード時、1カードぶんのドラッグハンドル+上下ボタン+表示切替チェックボックスを持つ
// 操作行を組み立てる。カード本体(article.compact-card、既存の純粋レンダリング関数の出力)は
// 変更せず、外側から包むだけにする。
function buildCardEditControlsHtml(cardId, label, hidden) {
  return `
    <div class="compact-card-edit-controls">
      <button type="button" class="compact-drag-handle" draggable="true" aria-label="${escapeHtml(label)}をドラッグして並べ替え">⠿⠿</button>
      <button type="button" class="compact-move-btn" data-direction="-1" aria-label="${escapeHtml(label)}を上へ移動">▲</button>
      <button type="button" class="compact-move-btn" data-direction="1" aria-label="${escapeHtml(label)}を下へ移動">▼</button>
      <label class="compact-visibility-toggle">
        <input type="checkbox" class="compact-visibility-checkbox" ${hidden ? "" : "checked"} aria-label="${escapeHtml(label)}を表示" />
        表示
      </label>
    </div>`;
}

// 編集モード時、1セクションぶんのドラッグハンドル+上下ボタンを持つ操作行を組み立てる。
function buildSectionEditBarHtml(sectionId, label) {
  return `
    <div class="compact-section-edit-bar" data-section-role="edit-bar">
      <button type="button" class="compact-drag-handle compact-section-drag-handle" draggable="true" aria-label="${escapeHtml(label)}セクションをドラッグして並べ替え">⠿⠿ ${escapeHtml(label)}</button>
      <button type="button" class="compact-move-btn compact-section-move-btn" data-direction="-1" aria-label="${escapeHtml(label)}セクションを上へ移動">▲</button>
      <button type="button" class="compact-move-btn compact-section-move-btn" data-direction="1" aria-label="${escapeHtml(label)}セクションを下へ移動">▼</button>
    </div>`;
}

// grid配下(直接の子だけでなく、既に.compact-card-editableでラップ済みの場合も含めて
// 再帰的)から、data-card-id属性を持つ.compact-cardを集めてIDへ対応付ける。
// data-card-idはDOM位置からの推測ではなく、各レンダリング関数(githubResourceCardHtml等)が
// HTML生成時に直接埋め込んだ値をそのまま読む。再帰検索により、前回の適用でラップ済みの
// カードも同じ関数で見つけられるため、この関数(および呼び出し元のapplyLayoutToSection)は
// 何度呼んでも同じ入力に対して同じ結果になる(冪等)。
function collectCardsById(gridEl) {
  const byId = {};
  gridEl.querySelectorAll(".compact-card[data-card-id]").forEach((el) => {
    byId[el.dataset.cardId] = el;
  });
  return byId;
}

// 1セクションぶんのcard並べ替え・表示切替・編集モード操作行の付与をまとめて適用する。
// loadCompact()の毎回のレンダリング後(30秒ごとの自動更新含む)や、編集モード切替・
// move/hide/show/reset操作のたびに呼び出される。
// 冪等性: collectCardsByIdは.compact-card-editableでラップ済みのカードも見つけられ、
// fragment.appendChild(cardEl)はcardElを(元がどのラッパー内にあっても)自動的にそこから
// 取り除いて新しい位置へ移動する(DOM標準の挙動)ため、二重ラップや取りこぼしは発生しない。
// 最後にgrid.innerHTMLを空にしてfragmentだけを追加するため、古いラッパーの残骸も残らない。
function applyLayoutToSection(sectionId) {
  const meta = sectionMetaById(sectionId);
  if (!meta || !meta.gridSelector) return;
  const grid = document.querySelector(meta.gridSelector);
  if (!grid) return;

  const cardMetas = CARD_META_BY_SECTION[sectionId] || [];
  const cardById = collectCardsById(grid);
  const order = layout.state.cardOrderBySection[sectionId] || cardMetas.map((card) => card.id);
  const hiddenSet = new Set(layout.state.hiddenCardIds);

  // 非表示カードも(通常モードでは)DOMから取り除かず、CSSのdisplay:noneだけで隠す。
  // 一度DOMから完全に取り除いてしまうと、次のfetchサイクル(30秒後)まで編集モードへ
  // 戻っても再表示できなくなるため(「非表示カードの再表示」「hidden全件でも編集モードから
  // 復元可能」という要件を満たすため、カードのDOM要素自体は常に保持する)。
  const fragment = document.createDocumentFragment();
  let visibleCount = 0;
  for (const cardId of order) {
    const cardEl = cardById[cardId];
    if (!cardEl) continue;
    const hidden = hiddenSet.has(cardId);
    if (!hidden) visibleCount += 1;

    if (layout.editMode) {
      cardEl.classList.remove("compact-card-hidden-mode");
      const wrapper = document.createElement("div");
      wrapper.className = "compact-card-editable" + (hidden ? " compact-card-editable-hidden" : "");
      wrapper.dataset.cardId = cardId;
      wrapper.dataset.sectionId = sectionId;
      wrapper.innerHTML = buildCardEditControlsHtml(cardId, cardLabelById(cardId), hidden);
      wrapper.appendChild(cardEl);
      fragment.appendChild(wrapper);
    } else {
      cardEl.classList.toggle("compact-card-hidden-mode", hidden);
      fragment.appendChild(cardEl);
    }
  }

  if (visibleCount === 0 && !layout.editMode) {
    const notice = document.createElement("div");
    notice.className = "compact-card compact-empty compact-all-hidden-notice";
    notice.textContent = "すべてのカードが非表示です（レイアウト編集で再表示できます）";
    fragment.insertBefore(notice, fragment.firstChild);
  }
  grid.innerHTML = "";
  grid.appendChild(fragment);
}

// セクション自体(GitHub/Claude/Codex/ダッシュボード)のDOM順を保存順へ並べ替え、
// 編集モードなら各セクション先頭へドラッグハンドル+上下ボタンの操作行を挿入する。
function applySectionLayout() {
  const main = document.getElementById("mainSections");
  if (!main) return;

  for (const sectionId of layout.state.sectionOrder) {
    const meta = sectionMetaById(sectionId);
    const el = meta && document.getElementById(meta.containerId);
    if (el) main.appendChild(el);
  }

  document.querySelectorAll('[data-section-role="edit-bar"]').forEach((el) => el.remove());
  if (layout.editMode) {
    for (const meta of SECTION_META) {
      const el = document.getElementById(meta.containerId);
      if (!el) continue;
      const wrapper = document.createElement("div");
      wrapper.innerHTML = buildSectionEditBarHtml(meta.id, meta.label);
      el.insertBefore(wrapper.firstElementChild, el.firstChild);
    }
  }
}

// セクション順・各セクション内のcard順/表示状態/編集モード操作行を、現在のlayout stateに
// 合わせて丸ごと再適用する。データ取得(loadCompact)の成否には関与しない表示専用の処理。
function applyFullLayout() {
  applySectionLayout();
  for (const sectionId of Object.keys(CARD_META_BY_SECTION)) {
    applyLayoutToSection(sectionId);
  }
}

let dragContext = null;

function clearDragVisualState() {
  document.querySelectorAll(".compact-dragging, .compact-drag-over").forEach((el) => {
    el.classList.remove("compact-dragging", "compact-drag-over");
  });
}

// ドラッグ&ドロップ(HTML5 Drag and Drop API)。編集モードでのみdraggable要素が
// 存在するため、通常モードでは誤操作によるドラッグは発生しない。
// タッチ環境ではHTML5 DnDの挙動が不安定なため、タッチ操作は上下ボタン(標準button要素、
// タップ操作可能)を主経路として案内する(ドラッグはマウス操作の補助手段と位置づける)。
function setupLayoutEventDelegation() {
  const main = document.getElementById("mainSections");
  if (!main) return;

  main.addEventListener("click", (event) => {
    const moveBtn = event.target.closest(".compact-move-btn");
    if (!moveBtn) return;
    const direction = Number(moveBtn.dataset.direction);
    const cardWrapper = moveBtn.closest(".compact-card-editable");
    if (cardWrapper) {
      moveCard(cardWrapper.dataset.sectionId, cardWrapper.dataset.cardId, direction);
      return;
    }
    const sectionEl = moveBtn.closest("section[data-section-id]");
    if (sectionEl) {
      moveSection(sectionEl.dataset.sectionId, direction);
    }
  });

  main.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".compact-visibility-checkbox");
    if (!checkbox) return;
    const wrapper = checkbox.closest(".compact-card-editable");
    if (wrapper) toggleCardVisibility(wrapper.dataset.cardId, !checkbox.checked);
  });

  main.addEventListener("dragstart", (event) => {
    const sectionHandle = event.target.closest(".compact-section-drag-handle");
    const cardHandle = !sectionHandle && event.target.closest(".compact-drag-handle");
    if (sectionHandle) {
      const sectionEl = sectionHandle.closest("section[data-section-id]");
      if (!sectionEl) return;
      dragContext = { type: "section", id: sectionEl.dataset.sectionId };
      sectionEl.classList.add("compact-dragging");
    } else if (cardHandle) {
      const wrapper = cardHandle.closest(".compact-card-editable");
      if (!wrapper) return;
      dragContext = { type: "card", sectionId: wrapper.dataset.sectionId, id: wrapper.dataset.cardId };
      wrapper.classList.add("compact-dragging");
    } else {
      return;
    }
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      try {
        event.dataTransfer.setData("text/plain", dragContext.id);
      } catch (error) {
        // 一部環境ではsetDataが例外を投げることがあるが、dragContext自体で状態は追跡できる。
      }
    }
  });

  main.addEventListener("dragover", (event) => {
    if (!dragContext) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    if (dragContext.type === "card") {
      const overWrapper = event.target.closest(".compact-card-editable");
      document.querySelectorAll(".compact-drag-over").forEach((el) => el.classList.remove("compact-drag-over"));
      if (overWrapper && overWrapper.dataset.sectionId === dragContext.sectionId) {
        overWrapper.classList.add("compact-drag-over");
      }
    } else if (dragContext.type === "section") {
      const overSection = event.target.closest("section[data-section-id]");
      document.querySelectorAll(".compact-drag-over").forEach((el) => el.classList.remove("compact-drag-over"));
      if (overSection) overSection.classList.add("compact-drag-over");
    }
  });

  main.addEventListener("drop", (event) => {
    if (!dragContext) return;
    event.preventDefault();
    if (dragContext.type === "card") {
      const overWrapper = event.target.closest(".compact-card-editable");
      if (overWrapper && overWrapper.dataset.sectionId === dragContext.sectionId) {
        const order = layout.state.cardOrderBySection[dragContext.sectionId] || [];
        const targetIndex = order.indexOf(overWrapper.dataset.cardId);
        if (targetIndex !== -1) moveCardToIndex(dragContext.sectionId, dragContext.id, targetIndex);
      }
    } else if (dragContext.type === "section") {
      const overSection = event.target.closest("section[data-section-id]");
      if (overSection) {
        const targetIndex = layout.state.sectionOrder.indexOf(overSection.dataset.sectionId);
        if (targetIndex !== -1) moveSectionToIndex(dragContext.id, targetIndex);
      }
    }
    dragContext = null;
    clearDragVisualState();
  });

  main.addEventListener("dragend", () => {
    dragContext = null;
    clearDragVisualState();
  });
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

// 自動(CLI statusLine)snapshotとDesktop Cloud手動snapshotを、resolveClaudeCodeUsageDisplayで
// 1つに絞ってから描画する。windowをまたいだ混合はresolve関数側で行われないため、ここでも行わない。
function renderClaudeCodeUsage(auto, manual) {
  const resolved = resolveClaudeCodeUsageDisplay(auto, manual);
  document.querySelector("#claudeCodeUsageCards").innerHTML = claudeCodeSectionHtml(resolved);
}

function renderCodexUsage(auto, manual) {
  document.querySelector("#codexUsageCards").innerHTML = codexUsageSectionHtml(auto, manual);
}

// GETのみ: /api/dashboard・/api/github-rate-limit・/api/claude-code-usage・
// /api/claude-code-usage/manual・/api/codex-rate-limits・/api/codex-usage はいずれも保存済みの値を
// 返すだけで、gh api rate_limitやClaude Code/Codex App Serverの起動などの外部コマンド/APIを
// ここから直接実行することはない(更新系リクエストはここから一切送信しない)。
async function loadCompact() {
  try {
    const [dashboard, github, claudeCodeUsage, claudeDesktopCloudUsage, codexRateLimits, codexUsage] =
      await Promise.all([
        fetchJson("/api/dashboard"),
        fetchJson("/api/github-rate-limit"),
        fetchJson("/api/claude-code-usage"),
        fetchJson("/api/claude-code-usage/manual"),
        fetchJson("/api/codex-rate-limits"),
        fetchJson("/api/codex-usage"),
      ]);
    state.dashboard = dashboard;
    state.github = github;
    state.claudeCodeUsage = claudeCodeUsage;
    state.claudeDesktopCloudUsage = claudeDesktopCloudUsage;
    state.codexRateLimits = codexRateLimits;
    state.codexUsage = codexUsage;
    renderLimitCards(dashboard);
    renderGithubSection(github);
    renderClaudeCodeUsage(claudeCodeUsage, claudeDesktopCloudUsage);
    renderCodexUsage(codexRateLimits, codexUsage);
  } catch (error) {
    document.querySelector("#limitCards").innerHTML = `<div class="compact-card compact-empty">取得に失敗しました: ${escapeHtml(error.message)}</div>`;
  } finally {
    renderLastRendered();
    // innerHTML差し替え(上のrender*)で失われるカード/セクションの並び順・表示状態を、
    // 保存済みのlayout stateへ合わせて毎回(30秒ごとの自動更新を含む)再適用する。
    applyFullLayout();
  }
}

function initCompact() {
  layout.state = loadLayoutStateFromStorage();
  setupLayoutEventDelegation();
  const layoutEditToggle = document.querySelector("#layoutEditToggle");
  if (layoutEditToggle) {
    layoutEditToggle.addEventListener("click", () => {
      setLayoutEditMode(!layout.editMode);
    });
  }
  const layoutResetButton = document.querySelector("#layoutResetButton");
  if (layoutResetButton) {
    layoutResetButton.addEventListener("click", () => {
      if (confirmLayoutReset()) resetLayoutToDefault();
    });
  }
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
    resetBlockHtml,
    githubResourceCardHtml,
    githubOverallHtml,
    githubOverallStatusClass,
    githubLimitedCause,
    githubLimitedBannerHtml,
    githubSecondaryRateLimitBannerHtml,
    githubAutoRefreshNoticeHtml,
    githubSectionHtml,
    claudeUsageWindowHtml,
    resolveClaudeCodeUsageDisplay,
    claudeCodeSectionHtml,
    codexUsageWindowHtml,
    resolveCodexDisplay,
    codexPeriodicRefreshNoticeHtml,
    codexUsageSectionHtml,
    LAYOUT_STORAGE_KEY,
    LAYOUT_VERSION,
    SECTION_META,
    CARD_META_BY_SECTION,
    DEFAULT_SECTION_ORDER,
    ALL_KNOWN_CARD_IDS,
    defaultLayoutState,
    sanitizeIdOrder,
    sanitizeLayoutState,
    loadLayoutStateFromRaw,
    serializeLayoutState,
    moveIdToIndex,
    buildCardEditControlsHtml,
    buildSectionEditBarHtml,
  };
}
