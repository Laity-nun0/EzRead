"use strict";

const EFFORT_NAMES = { none: "不启用", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "更高", max: "最高", ultra: "极高" };
function effortLabel(value) { return value ? `${EFFORT_NAMES[value] || value} · ${value}` : "模型默认"; }
function configLabel(config) { if (config?.provider) return `${apiProviderLabel(config.provider)} · ${config.model} · 输出上限 ${config.max_output_tokens}${config.thinking === "disabled" ? " · 思考关闭" : config.thinking === "enabled" ? ` · 思考开启 · ${effortLabel(config.reasoning_effort)}` : ""}`; return config?.model ? `${config.model} · 推理 ${effortLabel(config.reasoning_effort)}` : "历史模型未知"; }
async function openPdfLayoutSetup(p) {
  const task = pdfLayoutOf(p), current = task.current, active = ACTIVE_STATUSES.has(task.status);
  const legacyTask = Boolean(task.config && !task.config.provider);
  const settings = await loadTranslationApiSettings();
  const currentConfig = deepSeekFeature(settings, "translation");
  const picker = apiTranslationPicker(settings, currentConfig, false, true);
  const glossary = el("textarea", { rows: 5, "aria-label": "本篇术语（每行原文=译文）", placeholder: "例如：grain boundary=晶界" });
  const seed = pdfGlossaryEntries(task);
  const latestGlossary = task.glossary?.entries || seed;
  const seedText = latestGlossary.map(item => `${item.source}=${item.target}`).join("\n");
  glossary.value = seedText;
  const retranslate = el("input", { type: "radio", name: "pdf-task-mode", value: "new", checked: Boolean(legacyTask || current && task.status === "completed"), disabled: legacyTask, "aria-label": "使用新配置重新生成版式译文" });
  const resume = el("input", { type: "radio", name: "pdf-task-mode", value: "resume", checked: !retranslate.checked, disabled: legacyTask || task.status === "completed", "aria-label": "继续已有任务" });
  const summary = el("p", { class: "small muted translation-config-summary" }, active ? "任务正在进行" : "确认后加入翻译队列");
  const originalConfig = el("p", { class: "translation-resume-config" });
  const modeHint = el("p", { class: "small muted" });
  const glossaryCount = el("summary", {}, `本篇术语 · ${latestGlossary.length} 条`);
  const glossaryPanel = el("details", { class: "translation-glossary" }, glossaryCount, field("本篇术语（每行 原文=译文）", glossary));
  const message = el("p", { class: "small muted", role: "status" }, "正在检查本地版式组件…");
  let available = false;
  const start = button("确认保留版式翻译", event => act(async () => {
    if (!available) throw Error(message.textContent);
    const preserve = Boolean(task.config?.provider) && !retranslate.checked;
    if (!preserve && !picker.ready()) throw Error("请先在设置 → 翻译保存 API Key。");
    const payload = preserve ? { ...task.config, glossary: seed } : { ...picker.value(), glossary: parseApiGlossary(glossary.value), ...(legacyTask || retranslate.checked ? { retranslate: true } : {}) };
    await pdfLayoutRequest(p, "pdf-translate", payload); closeDialog("#translation-dialog"); toast("已加入保留版式翻译队列");
  }, event.currentTarget), "primary", "translate", { disabled: true });
  let previousResumeMode = null, editedConfig = null, editedGlossary = null;
  function updateMode() {
    const resumeOnly = Boolean(task.config?.provider && !retranslate.checked);
    if (previousResumeMode !== resumeOnly) {
      if (previousResumeMode === false) { editedConfig = picker.value(); editedGlossary = glossary.value; }
      picker.setConfig(resumeOnly && task.config.provider ? task.config : editedConfig || currentConfig);
      glossary.value = !resumeOnly && editedGlossary !== null ? editedGlossary : (resumeOnly ? seed : latestGlossary).map(item => `${item.source}=${item.target}`).join("\n");
    }
    previousResumeMode = resumeOnly;
    picker.disable(resumeOnly); glossary.disabled = resumeOnly; picker.root.hidden = resumeOnly; picker.advanced.hidden = resumeOnly;
    originalConfig.hidden = !resumeOnly;
    originalConfig.replaceChildren(el("strong", {}, "沿用原任务配置"), el("span", {}, configLabel(task.config)), el("span", { class: "small muted" }, "原缓存保留"));
    modeHint.textContent = resumeOnly ? "继续使用原配置与术语，可复用已有缓存。" : "新版本完成前，已有译文保持可读；原任务与缓存保留。";
    resume.checked = !retranslate.checked;
    glossaryCount.textContent = `本篇术语 · ${glossary.value.split(/\r?\n/).filter(line => line.trim()).length} 条`;
    start.disabled = !available || active;
    start.textContent = legacyTask ? "使用 API 重新生成，保留旧草稿" : retranslate.checked ? "生成新版本" : task.config ? "继续翻译" : "开始翻译";
  }
  retranslate.addEventListener("change", updateMode);
  resume.addEventListener("change", () => { retranslate.checked = false; updateMode(); });
  picker.root.addEventListener("change", updateMode);
  glossary.addEventListener("input", () => { glossaryCount.textContent = `本篇术语 · ${glossary.value.split(/\r?\n/).filter(line => line.trim()).length} 条`; });
  updateMode();
  const contextPanel = el("details", { class: "translation-context" }, el("summary", {}, "任务与组件详情"), message,
    el("p", { class: "small muted" }, pdfLayoutSubmissionText(task)),
    task.error ? el("p", { class: "error-text" }, asText(task.error)) : null,
    el("p", { class: "small muted" }, "请对照原 PDF 核对译文与版面。"));
  const body = el("div", { class: "translation-confirm-body" },
    el("p", { class: "translation-paper-title" }, titleOf(p)),
    task.config || current ? el("section", { class: "translation-task-mode" }, el("h3", {}, "任务方式"), el("div", { class: "translation-mode-options" }, el("label", {}, resume, "继续已有任务"), el("label", {}, retranslate, "生成新版本")), modeHint) : null,
    legacyTask ? el("p", { class: "small muted" }, "历史 Codex 任务须使用 API 生成新版本，保留旧草稿。") : null,
    el("section", { class: "translation-model-section" }, el("h3", {}, "翻译模型"), originalConfig, picker.root),
    el("div", { class: "translation-options" }, glossaryPanel, picker.advanced),
    task.config || current || task.versions?.length || task.drafts?.length ? button("版本与草稿", () => openPdfLayoutVersions(p), "subtle") : null,
    el("p", { class: "small muted translation-privacy" }, "论文文字与本篇术语将发送至所选供应商；PDF 排版在本机完成。"),
    contextPanel,
    current?.quality === "needs_review" ? el("p", { class: "translation-review" }, "当前译文有待核对内容，请对照原 PDF。") : null);
  $("#translation-content").replaceChildren(el("div", { class: "translation-confirm" },
    el("div", { class: "dialog-heading translation-confirm-heading" }, el("h2", {}, "翻译确认"), iconButton("close", "关闭版式翻译设置", () => closeDialog("#translation-dialog"))), body,
    el("div", { class: "translation-confirm-footer" }, summary, el("div", { class: "translation-footer-actions" }, button("取消", () => closeDialog("#translation-dialog")), active ? button("暂停翻译", event => act(async () => { await pdfLayoutRequest(p, "pdf-pause"); closeDialog("#translation-dialog"); }, event.currentTarget), "secondary") : start))));
  openDialog("#translation-dialog");
  const dialog = $("#translation-dialog"); let closed = false;
  dialog.addEventListener("close", () => { closed = true; picker.dispose(); }, { once: true });
  try {
    const engine = await api("/api/pdf-layout"); if (closed) return;
    available = Boolean(engine.available); message.textContent = engine.message || (available ? `${engine.engine} ${engine.version} · 本地组件已就绪` : "本地版式组件尚未准备");
    if (!available) { contextPanel.open = true; message.classList.add("error-text"); }
    updateMode();
  } catch (error) { if (!closed) { message.textContent = `本地版式组件不可用：${error.message}`; contextPanel.open = true; message.classList.add("error-text"); updateMode(); } }
}
function openPdfLayoutVersions(p) {
  const task = pdfLayoutOf(p), current = task.current, active = ACTIVE_STATUSES.has(task.status);
  const versions = el("div", { class: "pdf-layout-versions" }, (task.versions || []).map(version => el("div", { class: "translation-version" },
    el("div", {}, el("strong", {}, `${timeLabel(version.created_at)} · ${configLabel(version.config)}`), el("p", { class: "small muted" }, `${version.page_count || 0} 页 · ${version.quality === "needs_review" ? "需要核对" : "通过版式检查"}`)),
    button(current?.id === version.id ? "当前版本" : "恢复此版式版本", event => act(async () => {
      await pdfLayoutRequest(p, "pdf-restore", { version_id: version.id }); closeDialog("#translation-dialog"); toast("已恢复版式译文版本");
    }, event.currentTarget), "secondary", "", { disabled: active || current?.id === version.id }))));
  const drafts = el("div", { class: "pdf-layout-drafts" }, (task.drafts || []).map(draft => el("div", { class: "translation-version" },
    el("div", {}, el("strong", {}, `${timeLabel(draft.created_at)} · ${configLabel(draft.config)}`), el("p", { class: "small muted" }, `${draft.stage || "未完成"} · ${pdfLayoutProgressText(draft)}`)),
    button("恢复未完成草稿", event => act(async () => {
      await pdfLayoutRequest(p, "pdf-resume-draft", { draft_id: draft.id }); closeDialog("#translation-dialog"); toast(draft.config?.provider ? "版式草稿已恢复为暂停任务，请确认后继续翻译" : "历史草稿已保留，请使用 API 重新生成");
    }, event.currentTarget), "secondary", "", { disabled: active }))));
  $("#translation-content").replaceChildren(el("div", { class: "translation-confirm" },
    el("div", { class: "dialog-heading translation-confirm-heading" }, el("h2", {}, "版式译文版本与草稿"), iconButton("close", "关闭版式译文版本", () => closeDialog("#translation-dialog"))),
    el("div", { class: "translation-confirm-body" }, el("h3", {}, "版式译文版本"), task.versions?.length ? versions : el("p", { class: "muted" }, "尚无历史版本"), el("h3", {}, "未完成草稿"), task.drafts?.length ? drafts : el("p", { class: "muted" }, "尚无未完成草稿")),
    el("div", { class: "translation-confirm-footer" }, button("返回翻译确认", () => act(() => openPdfLayoutSetup(p)), "secondary"))));
  openDialog("#translation-dialog");
}
function defaultTranslationPanel() { return apiTranslationSettingsPanel(); }

function openPaperModel() { return openSettings("chat"); }

async function openTranslationVersions(p) {
  const data = await api(`/api/papers/${encodeURIComponent(p.id)}/translation-versions`);
  const versions = data.versions || [];
  const body = el("div", {}, el("div", { class: "dialog-heading" }, el("h2", {}, "译文版本"), iconButton("close", "关闭译文版本", () => closeDialog("#translation-dialog"))));
  if (!versions.length) body.append(el("p", { class: "no-results" }, "尚无历史译文"));
  for (const version of versions) body.append(el("div", { class: "translation-version" }, el("div", {}, el("strong", {}, configLabel(version.config)), el("p", { class: "small muted" }, `${version.created_at ? new Date(version.created_at).toLocaleString("zh-CN") : "历史版本"} · ${version.block_count || 0} 段${version.current ? " · 当前" : ""}`)), !version.current ? button("恢复此版本", event => act(async () => {
    await api(`/api/papers/${encodeURIComponent(p.id)}/translation-restore`, { method: "POST", body: { version_id: version.id } });
    await loadLibrary(); if (state.reader?.id === p.id) await refreshReader();
    if (state.detail?.id === p.id && $("#detail-dialog").open) { state.detail = normalizeDetail(await api(`/api/papers/${encodeURIComponent(p.id)}`)); renderDetail(); }
    closeDialog("#translation-dialog"); toast("已恢复译文版本");
  }, event.currentTarget), "secondary") : null));
  if (data.drafts?.length) body.append(el("h3", { class: "settings-section" }, "未完成的重译草稿"));
  for (const draft of data.drafts || []) body.append(el("div", { class: "translation-version" }, el("div", {}, el("strong", {}, configLabel(draft.config)), el("p", { class: "small muted" }, `${draft.block_count || 0} 段已保存 · 尚未完成`)), button("恢复历史草稿存档", event => act(async () => {
    await api(`/api/papers/${encodeURIComponent(p.id)}/translation-resume-draft`, { method: "POST", body: { draft_id: draft.id } });
    await loadLibrary();
    if (state.reader?.id === p.id) await refreshReader();
    if (state.detail?.id === p.id && $("#detail-dialog").open) { state.detail = normalizeDetail(await api(`/api/papers/${encodeURIComponent(p.id)}`)); renderDetail(); }
    closeDialog("#translation-dialog"); toast("重译草稿已恢复");
  }, event.currentTarget))));
  $("#translation-content").replaceChildren(body); openDialog("#translation-dialog");
}
