const state = {
  dashboard: [],
  limits: [],
  history: [],
  collectorRuns: [],
  editingLimitId: null,
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
  const [services, limits, dashboard, alerts, history, collectorRuns] = await Promise.all([
    api("/api/services"),
    api("/api/limits"),
    api("/api/dashboard"),
    api("/api/alerts"),
    api("/api/usage-records"),
    api("/api/collector-runs"),
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
}

async function refreshCollectorRuns() {
  state.collectorRuns = await api("/api/collector-runs");
  renderCollectorRuns();
}

function renderSelects(services, limits) {
  document.querySelector("#serviceSelect").innerHTML = services
    .map((s) => `<option value="${s.id}">${escapeHtml(s.name)} / ${escapeHtml(s.plan_name)}</option>`)
    .join("");
  document.querySelector("#limitSelect").innerHTML = limits
    .map((l) => `<option value="${l.id}">#${l.id} ${escapeHtml(l.model_name)} / ${escapeHtml(l.limit_type)}</option>`)
    .join("");
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
  module.exports = { sourceTypeLabel, sourceTypeClass, collectorStatusClass };
}
