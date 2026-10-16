let apiStatusRequest = null, apiStatusError = "";
async function refreshStatus() {
  if (apiStatusRequest) return apiStatusRequest;
  apiStatusRequest = (async () => {
    try {
      const value = await api("/api/ai/status");
      state.status = value; state.usage = value.usage; apiStatusError = "";
      renderLibrary(); return value;
    } catch (error) { apiStatusError = `本机 API 状态读取失败：${error.message}`; throw error; }
    finally { renderApiStatusPanels(); if ($("#queue-dialog")?.open) renderQueue(); renderUsagePanels(); }
  })().finally(() => { apiStatusRequest = null; });
  return apiStatusRequest;
}
function timeLabel(value, seconds = false) { if (value === null || value === undefined || value === "") return "未知"; const date = new Date(seconds ? Number(value) * 1000 : value); return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }); }
function apiUsageNumber(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value.toLocaleString("zh-CN") : "未知"; }
function apiRequestLabel(status) { return ({ pending: "进行中", success: "成功", failed: "失败", cancelled: "已取消" })[status] || "未知"; }
function apiStatusPanel() {
  const config = state.status?.configuration;
  return el("div", {}, el("div", { class: "usage-heading" }, el("h3", {}, "AI API 配置"),
    el("span", { class: `connection-state ${config?.configured ? "" : "offline"}` }, config?.configured ? "配置已就绪（未在线验证）" : "配置未就绪")),
    el("p", {}, config ? `${config.label || apiProviderLabel(config.provider)} · ${config.model || "模型未填写"}` : "正在读取本机配置…"),
    config?.base_url ? el("p", { class: "small muted" }, config.base_url) : null,
    config?.message && !config.configured ? el("p", { class: "small muted" }, config.message) : null,
    apiStatusError ? el("p", { class: "error-text", role: "status" }, apiStatusError) : null);
}
function renderApiStatusPanels() { for (const selector of ["#api-status", "#settings-api-status"]) $(selector)?.replaceChildren(apiStatusPanel()); }
function usagePanel(showRefresh = true) {
  const usage = state.usage, requests = usage?.requests, tokens = usage?.tokens;
  const panel = el("div", {}, el("div", { class: "usage-heading" }, el("h3", {}, "本次运行 API 用量"),
    showRefresh ? button("刷新 API 状态", event => act(refreshStatus, event.currentTarget), "secondary", "clock") : null));
  panel.append(el("p", { class: "usage-note" }, "仅计本次运行中供应商已报告的用量，缺失项显示未知。账户余额与费用请到供应商控制台查看。"));
  if (!usage) panel.append(el("p", { class: "usage-note" }, "尚未读取本次运行用量。"));
  panel.append(el("p", { class: "usage-numbers" }, `请求 ${apiUsageNumber(requests?.attempted)} · 成功 ${apiUsageNumber(requests?.succeeded)} · 失败 ${apiUsageNumber(requests?.failed)} · 取消 ${apiUsageNumber(requests?.cancelled)}`),
    el("p", { class: "usage-numbers" }, `输入 token ${apiUsageNumber(tokens?.input)} · 输出 token ${apiUsageNumber(tokens?.output)} · 合计 ${apiUsageNumber(tokens?.total)}`),
    el("p", { class: "small muted" }, `用量报告覆盖 ${apiUsageNumber(usage?.usage_reported)}/${apiUsageNumber(requests?.attempted)} 个请求`));
  for (const model of usage?.models || []) {
    const row = el("section", { class: "usage-bucket" }, el("h4", {}, `${apiProviderLabel(model.provider)} · ${model.model}`),
      el("p", {}, `请求 ${apiUsageNumber(model.requests?.attempted)} · 成功 ${apiUsageNumber(model.requests?.succeeded)} · 失败 ${apiUsageNumber(model.requests?.failed)} · 取消 ${apiUsageNumber(model.requests?.cancelled)}`),
      el("p", {}, `输入 token ${apiUsageNumber(model.tokens?.input)} · 输出 token ${apiUsageNumber(model.tokens?.output)} · 合计 ${apiUsageNumber(model.tokens?.total)}`));
    const thinking = model.usage?.["completion_tokens_details.reasoning_tokens"] ?? model.usage?.["output_tokens_details.reasoning_tokens"] ?? model.usage?.thoughtsTokenCount;
    row.append(el("p", { class: "small muted" }, `思考 token：${apiUsageNumber(thinking)} · 报告覆盖 ${apiUsageNumber(model.usage_reported)}/${apiUsageNumber(model.requests?.attempted)} 个请求`));
    const reported = Object.entries(model.usage || {}).filter(([, value]) => typeof value === "number" && Number.isFinite(value));
    if (reported.length) row.append(el("details", { class: "usage-report-details" }, el("summary", {}, "供应商报告详情"),
      el("p", { class: "small muted" }, "思考 token 沿用供应商统计口径，不另加到合计。"),
      el("p", { class: "small muted" }, reported.map(([key, value]) => `${key} ${apiUsageNumber(value)}`).join(" · "))));
    panel.append(row);
  }
  if (usage?.last) panel.append(el("p", { class: "usage-stamp" }, `最近请求：${apiProviderLabel(usage.last.provider)} · ${usage.last.model} · ${usage.last.channel || "通道未记录"} · ${apiRequestLabel(usage.last.status)}${usage.last.code ? `（${usage.last.code}）` : ""} · ${timeLabel(usage.last.at)}`));
  return panel;
}
function renderUsagePanels() { ["#usage-status", "#settings-usage"].forEach(selector => { const node = $(selector); if (node) node.replaceChildren(usagePanel(selector !== "#settings-usage")); }); }
function renderQueue() {
  const panel = $("#api-status"); if (!panel) return; panel.replaceChildren(apiStatusPanel());
  const jobs = state.papers.filter(p => p.summarize_status && p.summarize_status !== "idle");
  jobs.sort((a, b) => Number(ACTIVE_STATUSES.has(b.summarize_status)) - Number(ACTIVE_STATUSES.has(a.summarize_status)) || dateMs(b.updated_at) - dateMs(a.updated_at));
  const rows = jobs.map(p => el("article", { class: "queue-item" }, imageNode(p.cover_url, { class: "queue-image", alt: "论文封面" }),
    el("div", { class: "queue-item-body" }, el("h3", {}, titleOf(p)), el("p", {}, `研究速览：${taskStatusName(p.summarize_status)}`),
      p.summarize_error ? el("p", { class: "error-text" }, asText(p.summarize_error)) : null),
    el("div", { class: "queue-actions" }, button("阅读", () => act(() => openReader(p.id)), "secondary", "book"))));
  rows.push(...state.papers.filter(p => pdfLayoutOf(p).status !== "idle").map(pdfLayoutQueueItem));
  const list = $("#queue-list"), scroll = list.closest(".queue-dialog-scroll");
  const opened = new Set([...list.querySelectorAll("details[open][data-queue-detail]")].map(node => node.dataset.queueDetail));
  const focused = document.activeElement?.closest("[data-queue-focus]");
  const focusedPaper = focused?.closest("[data-queue-paper]")?.dataset.queuePaper;
  const focusedControl = focused?.dataset.queueFocus;
  const scrollTop = scroll?.scrollTop;
  list.replaceChildren(...(rows.length ? rows : [el("div", { class: "no-results" }, icon("translate"),
    el("h3", { style: { marginTop: "15px" } }, "还没有任务"), button("浏览文献库", () => setView("all"), "subtle", "", { style: { marginTop: "18px" } }))]));
  for (const node of list.querySelectorAll("details[data-queue-detail]")) node.open = opened.has(node.dataset.queueDetail);
  if (focusedPaper) {
    const row = [...list.querySelectorAll("[data-queue-paper]")].find(node => node.dataset.queuePaper === focusedPaper);
    [...(row?.querySelectorAll("[data-queue-focus]") || [])].find(node => node.dataset.queueFocus === focusedControl)?.focus({ preventScroll: true });
  }
  if (scroll && scrollTop != null) scroll.scrollTop = scrollTop;
  renderUsagePanels();
}

async function showTrash() {
  const data = await api("/api/trash"), papers = data.papers || []; const container = $("#trash-list"); if (!container) return;
  container.replaceChildren(...(papers.length ? papers.map(p => el("div", { style: { borderTop: "1px solid var(--line)", padding: "10px 0", display: "flex", gap: "12px", alignItems: "center" } }, el("span", { class: "small", style: { flex: "1", minWidth: "0", overflowWrap: "anywhere" } }, titleOf(p)), button("恢复", event => act(async () => { await api(`/api/papers/${encodeURIComponent(p.id)}/restore`, { method: "POST", body: {} }); await loadLibrary(); await showTrash(); toast("论文已恢复"); }, event.currentTarget), "secondary"))) : [el("p", { class: "small muted" }, "回收站是空的。") ]));
}
async function createCollection(name) {
  if (!name.trim()) return;
  const collections = [...new Set([...allCollections(), name.trim()])];
  state.settings = await api("/api/settings", { method: "PATCH", body: { collections } }); state.collections = collections;
  $("#collection-name").value = ""; closeDialog("#collection-dialog"); setView("collection", name.trim()); toast("合集已创建");
}
