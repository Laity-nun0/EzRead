async function loadLibrary(showLoading = false) {
  if (showLoading) { state.loading = true; renderLibrary(); }
  const data = await api("/api/papers");
  state.papers = data.papers || []; state.collections = list(data.collections).map(c => typeof c === "object" ? c.name : c).filter(Boolean); state.loading = false;
  renderLibrary(); if ($("#queue-dialog").open) renderQueue();
  if (typeof readerTabsRestore === "function") await readerTabsRestore();
}
function allCollections() { return [...new Set([...state.collections, ...list(state.settings.collections), ...state.papers.map(p => p.collection).filter(Boolean)])].sort((a, b) => String(a).localeCompare(String(b), "zh-CN")); }
async function movePapers(ids, collection) {
  if (!allCollections().includes(collection)) throw new Error("合集不存在，请刷新后重试");
  let moved = 0;
  for (const id of ids) {
    try { await api(`/api/papers/${encodeURIComponent(id)}`, { method: "PATCH", body: { collection } }); moved++; }
    catch (error) { toast(`${id}：${error.message}`, "error"); }
  }
  await loadLibrary();
  if (moved) toast(`已将 ${moved} 篇论文移入「${collection}」`);
}
async function permanentlyDeletePaper(id) {
  if (typeof flushDetailNotes === "function") await flushDetailNotes(id);
  if (typeof readerRuntime !== "undefined" && readerRuntime.notes?.id === id) {
    clearTimeout(readerRuntime.notes.timer);
    if (readerRuntime.notes.saving) await readerRuntime.notes.saving.catch(() => {});
  }
  await api(`/api/papers/${encodeURIComponent(id)}`, { method: "DELETE" });
  clearDeletedPaperState(id);
}
function clearDeletedPaperState(id) {
  if (typeof readerTabsDeleted === "function") readerTabsDeleted(id);
  if (typeof detailNoteSaves !== "undefined") {
    const entry = detailNoteSaves.get(id);
    if (entry) { entry.released = true; clearTimeout(entry.timer); detailNoteSaves.delete(id); }
  }
  state.selectedPapers.delete(id);
  if (state.reader?.id === id) {
    if (typeof readerRuntime !== "undefined") {
      if (readerRuntime.notes?.id === id) {
        readerRuntime.notes.released = true;
        clearTimeout(readerRuntime.notes.timer);
        readerRuntime.notes = null;
      }
      if (typeof readerDispose === "function") readerDispose();
    }
    state.reader = null;
    closeDialog("#reader-dialog");
  }
  if (state.detail?.id === id) { state.detail = null; closeDialog("#detail-dialog"); closeDialog("#edit-dialog"); }
  if (typeof paperChatRuntime !== "undefined") {
    paperChatRuntime.drafts.delete(id); paperChatRuntime.pending.delete(id);
    if (paperChatRuntime.paperId === id) { paperChatRuntime.paperId = null; paperChatRuntime.data = null; }
  }
  try { removePaperBrowserData(id); }
  catch { toast("旧文献已清除，但本机阅读草稿清理失败，请检查浏览器存储权限。", "error"); }
}
async function deletePapers(ids) {
  let removed = 0;
  for (const id of ids) {
    try { await permanentlyDeletePaper(id); removed++; }
    catch (error) { toast(`${id}：${error.message}`, "error"); }
  }
  await loadLibrary();
  if (removed) toast(`已永久删除 ${removed} 篇论文，无法在应用内恢复`);
}
function closeCardMenu(restoreFocus = false) {
  const id = activeCardMenuId; activeCardMenuId = null;
  $("#card-context-menu").classList.add("hidden");
  if (restoreFocus && id) $(`.paper-card[data-paper-id="${CSS.escape(id)}"]`)?.focus();
}
function beginPaperSelection(id) {
  state.selectionMode = true; state.selectedPapers.add(id); renderLibrary();
}
function cancelPaperSelection() {
  state.selectionMode = false; state.selectedPapers.clear(); renderLibrary();
}
async function paperFileAction(id, action) {
  const result = await api(`/api/papers/${encodeURIComponent(id)}/${action}`, { method: "POST", body: {} });
  toast(action === "copy-pdf" ? `已复制 PDF 文件「${result.filename}」` : "已在文件夹中选中原 PDF", "success", 6500);
}
function openCardMenu(p, x, y) {
  closeFilterMenu(); closeCardMenu(); activeCardMenuId = p.id;
  const menu = $("#card-context-menu");
  const item = (label, iconName, action) => el("button", { type: "button", role: "menuitem", onclick: () => { closeCardMenu(); action(); } },
    icon(iconName), el("span", {}, el("span", { class: "context-menu-label" }, label)));
  const config = cardModelConfig(p), configText = config.model ? `${config.legacy ? "历史 Codex" : apiProviderLabel(config.provider)} · ${config.model}` : "API 模型待配置";
  menu.replaceChildren(
    item("论文简介", "file", () => void act(() => openDetail(p.id))),
    item(cardTranslationLabel(p), "translate", () => void act(() => openPdfLayoutSetup(p))),
    item(configText, "settings", () => void act(() => openPaperModel(p))),
    item("论文对话", "chat", () => void act(async () => { await openReader(p.id); await openPaperChat(); })),
    item("多选", "select", () => beginPaperSelection(p.id)),
    item("复制 PDF", "copy", () => void act(() => paperFileAction(p.id, "copy-pdf"))),
    item("在文件夹显示", "folder", () => void act(() => paperFileAction(p.id, "reveal-pdf"))),
    item("永久删除", "trash", () => { if (confirm(`永久删除《${titleOf(p)}》及其文件、笔记、译文和全部会话？无法在应用内恢复。`)) void act(() => deletePapers([p.id])); })
  );
  menu.classList.remove("hidden");
  const box = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - box.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - box.height - 8))}px`;
  menu.querySelector("button").focus({ preventScroll: true });
}
function openMovePapers(ids) {
  const names = allCollections(), select = el("select", { "aria-label": "目标合集" }, names.map(name => el("option", { value: name }, name)));
  $("#move-collection-content").replaceChildren(
    el("div", { class: "dialog-heading" }, el("h2", {}, "移入合集"), iconButton("close", "关闭移入合集", () => closeDialog("#move-collection-dialog"))),
    el("p", { class: "small muted" }, `已选择 ${ids.length} 篇论文`),
    names.length ? field("目标合集", select) : el("p", {}, "暂无可用合集"),
    el("div", { class: "form-actions" }, button("取消", () => closeDialog("#move-collection-dialog"), "secondary"),
      button("确认移入", event => void act(async () => { await movePapers(ids, select.value); closeDialog("#move-collection-dialog"); }, event.currentTarget), "primary", "folder", { disabled: !names.length }))
  );
  openDialog("#move-collection-dialog");
}
function positionPaperDragPreview(event) {
  if (!paperDragPreview || !(event.clientX || event.clientY)) return;
  paperDragPreview.style.left = `${Math.max(8, Math.min(event.clientX + 14, innerWidth - paperDragPreview.offsetWidth - 8))}px`;
  paperDragPreview.style.top = `${Math.max(8, Math.min(event.clientY + 14, innerHeight - paperDragPreview.offsetHeight - 8))}px`;
}
function startPaperDragPreview(event, p) {
  closeCardMenu(); paperDragPreview?.remove();
  const sourceImage = event.currentTarget.querySelector(".card-cover img");
  const image = sourceImage ? sourceImage.cloneNode(false) : icon("file");
  image.removeAttribute("loading"); image.removeAttribute("id");
  paperDragPreview = el("div", { class: `paper-drag-preview tier-${tierOf(p)}`, "aria-hidden": "true" }, image,
    el("div", {}, el("span", { class: "drag-preview-title" }, titleOf(p)), el("strong", {}, `共 ${draggingPaperIds.length} 篇`)));
  document.body.append(paperDragPreview); positionPaperDragPreview(event);
  // Hide the browser's large translucent ghost; the opaque mini card follows dragover.
  const blank = document.createElement("canvas"); blank.width = blank.height = 1;
  event.dataTransfer.setDragImage(blank, 0, 0);
}
function endPaperDrag() {
  draggingPaperIds = []; paperDragPreview?.remove(); paperDragPreview = null;
  $$(".is-dragging,.drop-active").forEach(node => node.classList.remove("is-dragging", "drop-active"));
  if (paperDragDeferredRender) { paperDragDeferredRender = false; renderLibrary(); }
}
function makeDropTarget(node, kind, collection = "") {
  node.addEventListener("dragover", event => {
    if (!draggingPaperIds.length) return;
    event.preventDefault(); event.dataTransfer.dropEffect = "move"; node.classList.add("drop-active");
  });
  node.addEventListener("dragleave", event => { if (!node.contains(event.relatedTarget)) node.classList.remove("drop-active"); });
  node.addEventListener("drop", event => {
    node.classList.remove("drop-active");
    if (!draggingPaperIds.length) return;
    event.preventDefault(); event.stopPropagation();
    const ids = [...draggingPaperIds]; draggingPaperIds = [];
    if (kind === "trash" && !confirm(`永久删除选中的 ${ids.length} 篇论文及其文件、笔记、译文和全部会话？无法在应用内恢复。`)) return;
    void act(() => kind === "trash" ? deletePapers(ids) : movePapers(ids, collection));
  });
  return node;
}
