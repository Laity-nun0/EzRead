"use strict";

const readerPdfLayout = { signature: "" };
function pdfLayoutOf(p) { return { status: "idle", done: 0, versions: [], ...p?.pdf_layout_translation }; }
function readerPdfLayoutActive() { return Boolean(pdfLayoutOf(state.reader).current); }
function readerPdfLayoutReset() { readerPdfLayout.signature = ""; }
function readerPdfLayoutCache() {
  if (!readerPdfLayoutActive()) return;
  readerWriteLocal("pdf-view", state.reader.id, { value: "pdf", anchor: readerCaptureAnchor($("#translation-scroll"), ".reader-pdf-layout-page") });
}
function readerPdfLayoutUpdateControls() {
  const start = $("#reader-pdf-translate");
  if (start) start.textContent = ACTIVE_STATUSES.has(pdfLayoutOf(state.reader).status) ? "版式翻译任务" : "保留版式翻译";
}
function readerPdfLayoutResize() {
  const root = $("#translation-scroll");
  if (root && readerPdfLayoutActive()) root.style.setProperty("--reader-layout-page-width", `${Math.max(40, root.clientWidth - 40)}px`);
  if (typeof readerPdfSchedule === "function") readerPdfSchedule();
}
function readerPdfWarningText(warning) {
  const text = asText(warning), page = text.match(/(?:page|第|p)\s*[:=]?\s*(\d+)/i)?.[1];
  const prefix = page ? `第 ${page} 页：` : "";
  if (/isolate_formula|formula_caption|math|formula/i.test(text)) return `${prefix}公式或邻近文字需要对照原 PDF 核对`;
  if (/table/i.test(text)) return `${prefix}表格或表题需要核对`;
  if (/font|glyph|character/i.test(text)) return `${prefix}部分字符或字体需要核对`;
  if (/translation|translate|coverage|paragraph/i.test(text)) return `${prefix}部分正文译文需要核对`;
  if (/overflow|overlap|layout/i.test(text)) return `${prefix}文字位置或排版需要核对`;
  if (/[a-z]+_[a-z]+/i.test(text)) return `${prefix}部分内容或排版需要核对`;
  if (/[a-z]{3}/i.test(text) && !/[\u4e00-\u9fff]/.test(text)) return `${prefix}PDF 处理过程中产生内容核对提示`;
  return text;
}
function renderReaderPdfLayout() {
  readerPdfLayoutUpdateControls();
  if (!readerPdfLayoutActive()) {
    if (readerPdfLayout.signature && typeof readerPdfUnmount === "function") readerPdfUnmount("layout");
    $("#translation-scroll")?.replaceChildren(); readerPdfLayout.signature = ""; return false;
  }
  const current = pdfLayoutOf(state.reader).current, root = $("#translation-scroll");
  const signature = JSON.stringify([current.id, current.quality, current.warnings, current.pages, current.mono_url, current.dual_url]);
  if (signature !== readerPdfLayout.signature) {
    const anchor = readerPdfLayout.signature ? readerCaptureAnchor(root, ".reader-pdf-layout-page") :
      readerReadLocal("pdf-view", state.reader.id)?.anchor || readerCaptureAnchor($("#original-scroll"), ".reader-page-section");
    const links = [ [current.mono_url, "打开单语译文 PDF ↗"], [current.dual_url, "打开双语 PDF ↗"] ];
    const warnings = [...new Set((current.warnings || []).map(readerPdfWarningText))];
    root.replaceChildren(el("div", { class: "reader-pdf-layout-info" },
      current.quality === "needs_review" || warnings.length ? el("details", { class: "reader-pdf-diagnostics" },
        el("summary", {}, "部分内容需要核对 · 查看详情"),
        el("div", { class: "small muted" }, warnings.length ? warnings.map(message => el("p", {}, message)) : el("p", {}, "请对照原 PDF 检查内容与版面。"))) : null,
      el("div", { class: "reader-pdf-layout-links" }, links.map(([url, label]) => safeUrl(url) ? el("a", { href: safeUrl(url), target: "_blank", rel: "noopener noreferrer" }, label) : null))),
      ...(current.pages || []).map(page => el("section", { class: "translation-page reader-source-page reader-pdf-layout-page", dataset: { page: page.number } },
        typeof readerPdfPageNode === "function" ? readerPdfPageNode(page) : el("div", { class: "paper-page" }, el("img", { src: safeUrl(page.image_url), loading: "lazy", decoding: "async", width: page.width, height: page.height, alt: `版式译文 · 第 ${page.number} 页`, onload: event => event.target.closest(".paper-page")?.classList.add("page-loaded"), onerror: event => event.target.closest(".paper-page")?.classList.add("page-failed") })),
        el("footer", { class: "reader-source-page-footer" }, `版式译文 · 第 ${page.number} 页`))));
    readerPdfLayout.signature = signature;
    readerPdfLayoutResize(); readerRestoreAnchor(root, ".reader-pdf-layout-page", anchor);
  }
  readerPdfLayoutResize();
  if (typeof readerPdfMount === "function") readerPdfMount("layout", root, current.mono_url, `${state.reader.id}:${current.id}`, current.pages || []);
  state.readerRenderedSignature = paperSignature(state.reader); return true;
}
async function pdfLayoutRequest(p, action, body = {}) {
  const result = normalizeDetail(await api(`/api/papers/${encodeURIComponent(p.id)}/${action}`, { method: "POST", body }));
  // Only the PDF task metadata changes; do not overwrite in-progress text edits.
  updatePaper({ id: p.id, pdf_layout_translation: result.pdf_layout_translation, updated_at: result.updated_at });
  renderLibrary(); renderQueue();
  if (state.reader?.id === p.id) renderTranslations();
  if (state.detail?.id === p.id && $("#detail-dialog")?.open) renderDetail();
  return result;
}
function pdfLayoutProgressText(task) {
  return task.received == null ? `已处理 ${task.done || 0} 个文字单元；本次响应计数不可用` :
    `已收到 ${task.received} 个文字单元 · 已交付排版 ${task.done || 0} 个 · 待核对 ${task.review_pending || 0} 个 · 未通过 ${task.rejected || 0} 个`;
}
function pdfLayoutSubmissionText(task) {
  return task.status === "completed" && task.current ? "本次译文 PDF 已提交。" :
    task.current ? "当前译文 PDF 保留可读，新版本尚未提交。" : "译文 PDF 尚未提交。";
}
function pdfLayoutTaskState(task) {
  if (task.status === "completed") return task.current?.quality === "needs_review" ? "完成 · 待核对" : "已完成";
  if (task.status === "queued") return "排队中";
  if (task.status === "paused") return "已暂停";
  if (task.status === "error" || task.status === "failed") return "翻译中断";
  if (task.status === "running") {
    const stage = asText(task.stage);
    return /翻译与排版/.test(stage) ? "处理中" : /排版|typeset|reflow|render|核对译文字形/i.test(stage) ? "排版中" : /翻译|translate/i.test(stage) && !/分析|等待/.test(stage) ? "翻译中" : "处理中";
  }
  return statusName(task.status);
}
function pdfLayoutErrorDetails(details) {
  const labels = { request_id: "请求标识", provider: "供应商", model: "模型", http_status: "HTTP 状态", response_bytes: "响应字节数", body_complete: "响应正文完整", expected_response_bytes: "声明正文长度", captured_bytes: "保留字节数", truncated: "是否截断", stage: "解析阶段", finish_reason: "结束原因", json_line: "JSON 行", json_column: "JSON 列", json_position: "JSON 位置", usage: "供应商报告用量", diagnostic_id: "诊断记录", diagnostic_saved: "诊断已保存", diagnostic_error: "诊断保存错误", response_received: "收到响应" };
  return el("dl", { class: "pdf-task-error-details" }, Object.entries(labels).map(([key, label]) => el("div", {}, el("dt", {}, label), el("dd", {}, details[key] == null ? "未知" : typeof details[key] === "boolean" ? details[key] ? "是" : "否" : key === "usage" && typeof details[key] === "object" ? JSON.stringify(details[key], null, 2) : asText(details[key])))));
}
function pdfLayoutQueueItem(p) {
  const task = pdfLayoutOf(p), active = ACTIVE_STATUSES.has(task.status), failed = ["error", "failed"].includes(task.status);
  const review = task.current?.quality === "needs_review" || task.review_pending > 0;
  const stage = task.stage || (task.status === "queued" ? "等待开始" : "正在处理 PDF");
  const usage = [task.reused != null ? `复用 ${task.reused} 个文字单元` : "", task.model_calls != null ? `本次模型请求 ${task.model_calls} 次` : ""].filter(Boolean).join(" · ");
  const details = el("details", { class: "pdf-task-details", dataset: { queueDetail: p.id } }, el("summary", { dataset: { queueFocus: "details" } }, "查看详情"),
    el("div", { class: "pdf-task-detail-content" }, el("p", {}, configLabel(task.config)), el("p", {}, pdfLayoutProgressText(task)), usage ? el("p", {}, usage) : null,
      task.stage ? el("p", {}, `最近记录阶段：${task.stage}`) : null,
      task.error ? el("p", { class: "pdf-task-detail-error" }, asText(task.error)) : null,
      failed ? el("p", {}, "已有译文与缓存保留；继续时沿用原任务配置。") : null,
      task.error_code ? el("p", {}, `错误代码：${task.error_code}`) : null,
      task.error_details && Object.keys(task.error_details).length ? pdfLayoutErrorDetails(task.error_details) : null));
  const stageText = asText(task.stage);
  // Only explicit engine stage events select a phase; unit counts are never a percentage.
  const phase = task.status === "completed" ? 3 : /翻译与排版/.test(stageText) ? -1 : /排版|typeset|reflow|render|核对译文字形/i.test(stageText) && !/翻译与排版/.test(stageText) ? 2 : /翻译|translate/i.test(stageText) && !/分析|等待/.test(stageText) ? 1 : /分析|准备|等待|parse/i.test(stageText) ? 0 : -1;
  const steps = el("ol", { class: "pdf-task-stages", "aria-label": "处理阶段" }, ["准备", "翻译", "排版", "完成"].map((label, index) => el("li", { class: index === phase ? "current" : "", "aria-current": index === phase ? "step" : null }, label)));
  const read = button(task.current ? "阅读译文" : "阅读原文", () => act(() => openReader(p.id)), "secondary", "book", { dataset: { queueFocus: "read" } });
  const operation = task.status === "completed" ? button("版本与草稿", () => openPdfLayoutVersions(p), "secondary", "", { dataset: { queueFocus: "versions" } }) :
    button(active ? "暂停翻译" : "继续翻译", event => act(() => active ? pdfLayoutRequest(p, "pdf-pause") : openPdfLayoutSetup(p), event.currentTarget), "primary", "", { dataset: { queueFocus: "action" } });
  return el("article", { class: `queue-item pdf-layout-queue-item${failed ? " pdf-task-failed" : ""}`, dataset: { queuePaper: p.id } },
    el("header", { class: "pdf-task-heading" }, el("h3", {}, titleOf(p)), el("span", { class: `pdf-task-status${failed ? " error-text" : review ? " translation-review" : ""}` }, pdfLayoutTaskState(task))),
    el("div", { class: "pdf-task-main" }, active ? el("p", { class: "pdf-task-stage" }, stage) : null, el("p", { class: "small muted" }, pdfLayoutSubmissionText(task)),
      active ? steps : null,
      failed || task.status === "paused" && task.error ? el("div", { class: "pdf-task-error" }, el("strong", { class: "pdf-task-error-preview" }, asText(task.error) || "翻译未完成，具体原因待诊断。"), button("查看错误详情", () => { details.open = true; details.querySelector("summary")?.focus(); }, "subtle")) : null,
      review && task.status !== "completed" ? el("p", { class: "translation-review" }, "部分内容待核对，详见任务详情。") : null),
    el("footer", { class: "pdf-task-footer" }, el("div", { class: "queue-actions" }, read, operation)), details);
}
