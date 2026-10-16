"use strict";

// This script is loaded before app.js. Shared app state is only read inside functions.
const readerRuntime = {
  imageObserver: null, thumbObserver: null, resizeObserver: null,
  leftFrame: 0, rightFrame: 0, suppressLeftUntil: 0, suppressRightUntil: 0,
  opening: 0, request: 0, mountedId: null, boundDialog: null,
  originalSignature: "", resizeAnchor: null, translationAnchor: null,
  thumbsOpen: false, split: .5, position: null,
  savedSignature: "", savePromise: null, closePromise: null,
  notes: null, libraryAnchor: null, lifecycleBound: false,
  layoutFrame: 0, restoring: false, splitSaveTimer: null,
  chromeCollapsed: false
};

const READER_ZOOMS = [40, 50, 60, 75, 100, 125, 150, 200];
function readerRightAnchorSelector() { return ".reader-pdf-layout-page"; }
function readerClamp(value, low, high, fallback = low) {
  const number = Number(value); return Number.isFinite(number) ? Math.max(low, Math.min(high, number)) : fallback;
}
function readerNormalizePosition(value, paper = state.reader) {
  const v = value && typeof value === "object" ? value : {};
  const zoom = Number(v.zoom), saved = paper?.blocks.find(b => b.id === v.translation_block_id);
  const block = saved?.extraction_hidden ? paper.blocks.find(b => !b.extraction_hidden &&
    (b.id === saved.extraction_parent_id || b.page === saved.page)) : saved;
  return { version: 1, page: readerPageNumber(v.page ?? paper?.read_page, paper?.pages.length || 1),
    original_offset: readerClamp(v.original_offset, 0, 1), translation_block_id: block?.id || null,
    translation_offset: readerClamp(v.translation_offset, 0, 1),
    zoom: READER_ZOOMS.includes(zoom) ? zoom : "fit",
    split_ratio: readerClamp(v.split_ratio ?? state.settings.reader_split_ratio, .3, .7, .5) };
}
function readerStorageKey(kind, id) { return `ezread-reader-${kind}:${id}`; }
function readerReadLocal(kind, id) {
  try { return JSON.parse(localStorage.getItem(readerStorageKey(kind, id)) || "null"); } catch { return null; }
}
function readerWriteLocal(kind, id, value) {
  try { localStorage.setItem(readerStorageKey(kind, id), JSON.stringify(value)); return true; } catch { return false; }
}
function readerRemoveLocal(kind, id) {
  try { localStorage.removeItem(readerStorageKey(kind, id)); } catch {}
}
function readerSaveHint(message) { const node = $("#reader-save-state"); if (node) node.textContent = message; }
function readerCapturePosition() {
  if (!state.reader) return null;
  const previous = readerRuntime.position || readerNormalizePosition(state.reader.reader_state);
  const left = readerCaptureAnchor($("#original-scroll"), ".reader-page-section");
  const page = left?.key ? Number(left.key) : previous.page;
  const position = readerNormalizePosition({ ...previous, page,
    original_offset: left?.key ? left.ratio : page === previous.page ? previous.original_offset : 0,
    translation_block_id: previous.translation_block_id,
    translation_offset: previous.translation_offset,
    zoom: state.zoom, split_ratio: readerRuntime.split });
  readerRuntime.position = position;
  return position;
}
function readerCachePosition(position = readerCapturePosition(), pending = true) {
  if (typeof readerPdfLayoutCache === "function") readerPdfLayoutCache();
  return Boolean(position && state.reader && readerWriteLocal("position", state.reader.id, { position, pending }));
}
function readerQueuePositionSave() {
  if (readerRuntime.restoring || !readerScrollActive()) return;
  readerCachePosition(); clearTimeout(readSaveTimer); readSaveTimer = setTimeout(() => { saveReadPage().catch(error => readerSaveHint(`位置未保存：${error.message}`)); }, 650);
}
function readerRestorePosition(position) {
  const p = readerNormalizePosition(position); readerRuntime.restoring = true;
  const original = $("#original-scroll"), translated = $("#translation-scroll");
  const page = readerOriginalPage(p.page); readerLoadImage(page && $("img", page));
  readerRestoreAnchor(original, ".reader-page-section", { key: String(p.page), ratio: p.original_offset, offset: 0, scrollTop: 0, scrollLeft: 0 });
  if (readerPdfLayoutActive()) {
    const anchor = readerReadLocal("pdf-view", state.reader.id)?.anchor;
    if (anchor) readerRestoreAnchor(translated, readerRightAnchorSelector(), anchor);
    else scrollTranslationToPage(p.page);
  }
  state.readerPage = p.page; readerUpdatePageUI(); readerRuntime.position = p;
  readerRuntime.resizeAnchor = readerCaptureAnchor(original, ".reader-page-section");
  readerRuntime.translationAnchor = readerCaptureAnchor(translated, readerRightAnchorSelector());
  readerRuntime.restoring = false;
}
function readerRememberLibrary() {
  const cards = $$(".paper-card");
  const card = cards.find(node => node.getBoundingClientRect().bottom > 0);
  return { y: window.scrollY, x: window.scrollX, cardId: card?.dataset.id || card?.dataset.paperId || null, top: card?.getBoundingClientRect().top, focus: document.activeElement };
}
function readerRestoreLibrary() {
  const anchor = readerRuntime.libraryAnchor; if (!anchor) return;
  requestAnimationFrame(() => {
    const card = anchor.cardId && $$(".paper-card").find(node => (node.dataset.id || node.dataset.paperId) === anchor.cardId);
    window.scrollTo({ left: anchor.x, top: card ? window.scrollY + card.getBoundingClientRect().top - anchor.top : anchor.y, behavior: "instant" });
    if (anchor.focus?.isConnected) anchor.focus.focus({ preventScroll: true });
    else card?.focus({ preventScroll: true });
  });
}

function readerPageNumber(value, count) {
  return Math.max(1, Math.min(Math.max(1, count), Math.round(Number(value) || 1)));
}
function readerSyncEnabled() { return state.settings.reader_sync !== false; }
function readerIsEditing() { return Boolean(document.activeElement?.closest(".reader-notes") || typeof readerPdfHasSelection === "function" && readerPdfHasSelection()); }
function readerScrollActive() { return Boolean(state.reader && $("#reader-dialog")?.open); }
function readerAtPage(container, nodes) {
  if (!nodes.length) return null;
  const line = container.getBoundingClientRect().top + Math.min(100, container.clientHeight * 0.2);
  // Use a fixed reading line; a tall page must not lose to a short adjacent page.
  let chosen = nodes[0], rowTop = -Infinity;
  for (const node of [...nodes].sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top || a.getBoundingClientRect().left - b.getBoundingClientRect().left)) {
    if (node.getBoundingClientRect().top <= line) {
      const top = node.getBoundingClientRect().top;
      if (top > rowTop + 1) { chosen = node; rowTop = top; }
    }
    else break;
  }
  return chosen;
}
function readerCaptureAnchor(container, selector) {
  if (!container) return null;
  const nodes = $$(selector, container), top = container.getBoundingClientRect().top;
  const node = nodes.find(n => n.getBoundingClientRect().bottom > top + 1) || nodes.at(-1);
  if (!node) return { scrollTop: container.scrollTop, scrollLeft: container.scrollLeft };
  const rect = node.getBoundingClientRect();
  return { key: node.dataset.blockId || node.dataset.page, ratio: Math.max(0, Math.min(1, (top - rect.top) / Math.max(1, rect.height))), offset: Math.max(0, rect.top - top), scrollTop: container.scrollTop, scrollLeft: container.scrollLeft };
}
function readerRestoreAnchor(container, selector, anchor) {
  if (!container || !anchor) return;
  const node = $$(selector, container).find(n => (n.dataset.blockId || n.dataset.page) === anchor.key || n.dataset.sourceIds?.split(" ").includes(anchor.key));
  let top = anchor.scrollTop;
  if (node) {
    const rect = node.getBoundingClientRect();
    top = container.scrollTop + rect.top - container.getBoundingClientRect().top + rect.height * anchor.ratio - anchor.offset;
  }
  readerMoveScroll(container, top, anchor.scrollLeft);
}
function readerMoveScroll(container, top, left) {
  if (!container) return;
  if (left === undefined) left = container.scrollLeft;
  const side = container.id === "original-scroll" ? "suppressLeftUntil" : "suppressRightUntil";
  readerRuntime[side] = performance.now() + 220;
  container.scrollTo({ top: Math.max(0, top || 0), left: Math.max(0, left || 0), behavior: "instant" });
  if (container.id === "original-scroll") readerRuntime.resizeAnchor = readerCaptureAnchor(container, ".reader-page-section");
  else if (container.id === "translation-scroll") readerRuntime.translationAnchor = readerCaptureAnchor(container, readerRightAnchorSelector());
}
function readerScrollToNode(container, node, align = "start") {
  if (!container || !node) return;
  const r = node.getBoundingClientRect(), pane = container.getBoundingClientRect();
  const offset = align === "center" ? Math.max(16, (container.clientHeight - Math.min(r.height, container.clientHeight * 0.7)) / 2) : 18;
  let left = container.scrollLeft;
  if (r.left < pane.left + 12) left += r.left - pane.left - 12;
  else if (r.right > pane.right - 12 && r.width < container.clientWidth) left += r.right - pane.right + 12;
  readerMoveScroll(container, container.scrollTop + r.top - pane.top - offset, left);
}
function readerOriginalPage(page) { return $$(".reader-page-section", $("#original-scroll")).find(n => Number(n.dataset.page) === Number(page)); }
function readerTranslationPage(page) {
  const root = $("#translation-scroll");
  return $$(".reader-pdf-layout-page", root).find(n => Number(n.dataset.page) === Number(page));
}

async function openReader(id) {
  if (readerScrollActive() && !(await closeReader())) return;
  if (typeof flushDetailNotes === "function") await flushDetailNotes(id);
  const opening = ++readerRuntime.opening;
  const p = normalizeDetail(await api(`/api/papers/${encodeURIComponent(id)}`));
  if (opening !== readerRuntime.opening) return;
  readerRuntime.libraryAnchor = readerRememberLibrary();
  const detail = $("#detail-dialog"); if (detail?.open) detail.close();
  state.reader = p;
  const cached = readerReadLocal("position", id);
  const position = readerNormalizePosition(cached?.pending ? cached.position : p.reader_state || cached?.position, p);
  readerRuntime.position = position; readerRuntime.split = position.split_ratio;
  state.readerPage = position.page; state.selectedBlock = null; state.zoom = position.zoom;
  readerRuntime.thumbsOpen = false; readerRuntime.savedSignature = "";
  const display = readerReadLocal("display", id);
  readerRuntime.chromeCollapsed = display?.collapsed === true;
  if (typeof readerPdfLayoutReset === "function") readerPdfLayoutReset(p);
  const notesDraft = readerReadLocal("notes", id);
  readerRuntime.notes = { id, value: typeof notesDraft?.value === "string" ? notesDraft.value : p.notes || "", savedValue: p.notes || "", timer: null, saving: null, storageOk: true };
  renderReader(); openDialog("#reader-dialog");
  requestAnimationFrame(() => {
    if (state.reader?.id !== id || !readerScrollActive()) return;
    readerRestorePosition(position);
    if (notesDraft?.value !== undefined && notesDraft.value !== p.notes) toggleReaderNotes(true);
    readerQueuePositionSave();
  });
}

function readerDispose() {
  if (typeof readerPdfDispose === "function") readerPdfDispose();
  readerRuntime.imageObserver?.disconnect(); readerRuntime.thumbObserver?.disconnect(); readerRuntime.resizeObserver?.disconnect();
  readerRuntime.imageObserver = null; readerRuntime.thumbObserver = null; readerRuntime.resizeObserver = null;
  cancelAnimationFrame(readerRuntime.leftFrame); cancelAnimationFrame(readerRuntime.rightFrame);
  readerRuntime.leftFrame = 0; readerRuntime.rightFrame = 0;
  cancelAnimationFrame(readerRuntime.layoutFrame); clearTimeout(readSaveTimer);
}
function renderReader() {
  const p = state.reader; if (!p) return;
  readerDispose(); readerRuntime.mountedId = p.id; readerRuntime.originalSignature = "";
  const pageInput = el("input", { type: "number", min: "1", max: p.pages.length || 1, value: state.readerPage, id: "reader-page-input", "aria-label": "当前页码", onchange: event => setReaderPage(Number(event.target.value), true) });
  const zoom = el("select", { class: "zoom-select", id: "reader-zoom", "aria-label": "原文页面缩放", onchange: event => readerSetZoom(event.target.value) }, el("option", { value: "fit" }, "适合宽度"), READER_ZOOMS.map(value => el("option", { value: String(value) }, `${value}%`))); zoom.value = state.zoom;
  const original = el("section", { class: "original-pane", "aria-label": "原版 PDF 连续阅读" },
    el("div", { class: "pane-bar" }, el("span", { class: "pane-label" }, icon("file"), "原版 PDF"), zoom,
      el("div", { class: "page-control" }, iconButton("chevron", "上一页", () => setReaderPage(state.readerPage - 1, true)), pageInput, ` / ${p.pages.length}`, iconButton("chevron", "下一页", () => setReaderPage(state.readerPage + 1, true))),
      el("button", { class: "reader-thumbs-toggle", id: "reader-thumbs-toggle", "aria-expanded": "false", "aria-controls": "reader-thumbs", onclick: toggleReaderThumbnails }, "缩略图")),
    el("div", { class: "original-scroll", id: "original-scroll", tabindex: "0", "aria-label": "连续 PDF 页面，可上下滚动" }),
    el("div", { class: "reader-thumbs", id: "reader-thumbs", hidden: true }));
  original.append(button("展开工具栏", () => readerSetDisplay({ collapsed: false }), "secondary", "", { id: "reader-expand-toolbar", hidden: true }));
  $(".page-control .icon-button", original).style.transform = "rotate(180deg)";
  const sync = el("input", { type: "checkbox", id: "reader-sync", checked: readerSyncEnabled(), onchange: event => act(async () => {
    const previous = readerSyncEnabled(), next = event.target.checked;
    state.settings.reader_sync = next;
    try { const data = await api("/api/settings", { method: "PATCH", body: { reader_sync: next } }); state.settings = { ...state.settings, ...(data.settings || data) }; }
    catch (error) { state.settings.reader_sync = previous; event.target.checked = previous; throw error; }
    if (typeof syncPreferenceControls === "function") syncPreferenceControls();
    if (next && !readerIsEditing()) readerSyncScroll($("#original-scroll"));
  }, event.target) });
  const trans = el("section", { class: "translation-pane", "aria-label": "版式译文 PDF" },
    el("div", { class: "pane-bar" }, el("span", { class: "pane-label", id: "reader-text-pane-label" }, icon("translate"), "版式译文"),
      el("select", { id: "reader-text-zoom", "aria-label": "译文缩放", onchange: event => readerSetTextZoom(Number(event.target.value)) }, READER_ZOOMS.map(value => el("option", { value: String(value) }, `${value}%`))),
      el("label", { class: "reader-sync-control", title: "按原 PDF 页及页内进度连续联动" }, sync, "同步滚动"),
      el("span", { class: "selection-status", id: "reader-translation-status" }, `第 ${state.readerPage} 页`)),
    el("div", { class: "translation-scroll", id: "translation-scroll", tabindex: "0", "aria-label": "译文 PDF，可自由选择与复制" }));
  const tools = el("div", { class: "reader-tools" },
    button("论文简介", () => act(async () => { const id = state.reader.id; if (await closeReader()) await openDetail(id); }), "secondary", "file", { title: "论文信息、作者与来源" }),
    button("论文对话", () => togglePaperChat(), "secondary", "", { id: "reader-chat-toggle", "aria-expanded": "false", "aria-controls": "reader-paper-chat" }),
    button("笔记", () => toggleReaderNotes(), "secondary", "edit", { id: "reader-notes-toggle", "aria-expanded": "false", "aria-controls": "reader-notes" }),
    button("保留版式翻译", event => act(() => openPdfLayoutSetup(state.reader), event.currentTarget), "secondary", "translate", { id: "reader-pdf-translate" }));
  const originalLink = safeUrl(p.original_url || `/media/${p.id}/original.pdf`);
  const divider = el("div", { class: "reader-divider", id: "reader-divider", role: "separator", tabindex: "0", "aria-label": "调整原文与译文比例", "aria-orientation": "vertical", "aria-valuemin": "30", "aria-valuemax": "70", "aria-controls": "original-scroll translation-scroll", title: "拖动调整比例；方向键微调，Enter 恢复均分" }, el("span", { "aria-hidden": "true" }));
  $("#reader-content").replaceChildren(
    el("div", { class: "reader-topbar" }, el("button", { class: "reader-back", onclick: () => void closeReader(), title: "返回文献库" }, icon("chevron"), "文献库"), el("div", { class: "reader-title", title: titleOf(p) }, titleOf(p)), tools),
    el("div", { class: "reader-viewbar" },
      button("折叠工具栏", () => readerSetDisplay({ collapsed: true }), "secondary")),
    el("div", { class: "reader-workspace" }, el("div", { class: "reader-layout", id: "reader-layout" }, original, divider, trans), el("aside", { class: "reader-notes", id: "reader-notes", hidden: true, "aria-label": "论文笔记" }), el("aside", { class: "reader-paper-chat", id: "reader-paper-chat", hidden: true, "aria-label": "本篇论文对话" })),
    el("div", { class: "reader-bottom" }, el("span", { id: "reader-save-state", role: "status", "aria-live": "polite" }, "阅读位置自动保存"), el("a", { href: originalLink, target: "_blank", rel: "noopener noreferrer" }, "打开原 PDF ↗")));
  // Layout must be visible before computing page widths and scroll anchors.
  openDialog("#reader-dialog");
  readerApplyLayout(); drawOriginalPage(); renderTranslations(); updateReaderProgress(); readerBindScrolling(); readerBindDivider(divider); readerBindLifecycle();
  readerSyncTextZoom();
  const dialog = $("#reader-dialog");
  if (readerRuntime.boundDialog !== dialog) {
    dialog.addEventListener("close", () => { readerPersistDrafts(); readerDispose(); readerRestoreLibrary(); }); readerRuntime.boundDialog = dialog;
  }
}
async function closeReader() {
  if (readerRuntime.closePromise) return readerRuntime.closePromise;
  if (!readerScrollActive()) return true;
  readerCachePosition(); readerPersistDrafts();
  const content = $("#reader-content"); content.inert = true;
  readerRuntime.closePromise = (async () => {
    const results = await Promise.allSettled([readerSaveNotes()]);
    results.push(...await Promise.allSettled([saveReadPage()]));
    const failed = results.some(result => result.status === "rejected");
    const preserved = readerPersistDrafts();
    if (failed && !preserved) { toast("保存失败，草稿也未能写入本机；请保留此窗口并重试。", "error", 8000); return false; }
    if (failed) toast("未保存内容已保留为本机草稿，请重新打开后核对。", "warn", 6500);
    $("#reader-dialog").close(); return true;
  })().finally(() => { content.inert = false; readerRuntime.closePromise = null; });
  return readerRuntime.closePromise;
}

function readerApplyLayout() {
  const layout = $("#reader-layout"); if (!layout) return;
  const content = $("#reader-content");
  if (content?.classList) content.classList.toggle("reader-chrome-collapsed", readerRuntime.chromeCollapsed);
  const expand = $("#reader-expand-toolbar"); if (expand) expand.hidden = !readerRuntime.chromeCollapsed;
  layout.style.setProperty("--reader-left", `${readerRuntime.split}fr`);
  layout.style.setProperty("--reader-right", `${1 - readerRuntime.split}fr`);
  const dual = readerPdfLayoutActive();
  layout.classList.toggle("reader-original-only", !dual);
  const pane = $(".translation-pane", layout); if (pane) pane.hidden = !dual;
  const divider = $("#reader-divider"); divider.hidden = !dual;
  divider.setAttribute("aria-valuenow", String(Math.round(readerRuntime.split * 100)));
  divider.setAttribute("aria-valuetext", `原文 ${Math.round(readerRuntime.split * 100)}%，译文 ${Math.round((1 - readerRuntime.split) * 100)}%`);
  divider.setAttribute("aria-orientation", typeof matchMedia === "function" && matchMedia("(max-width: 700px)").matches ? "horizontal" : "vertical");
  if (typeof readerPdfLayoutResize === "function") readerPdfLayoutResize();
}
function readerPreserveLayout(change) {
  const position = readerCapturePosition();
  const originalAnchor = readerCaptureAnchor($("#original-scroll"), ".reader-page-section");
  const translationAnchor = readerCaptureAnchor($("#translation-scroll"), readerRightAnchorSelector());
  change(); readerApplyLayout(); cancelAnimationFrame(readerRuntime.layoutFrame);
  readerRuntime.layoutFrame = requestAnimationFrame(() => {
    if (!readerScrollActive()) return;
    drawOriginalPage();
    readerRestoreAnchor($("#original-scroll"), ".reader-page-section", originalAnchor);
    if (typeof readerPdfLayoutResize === "function") readerPdfLayoutResize();
    readerRestoreAnchor($("#translation-scroll"), readerRightAnchorSelector(), translationAnchor);
    if (position) readerRuntime.position = { ...position, split_ratio: readerRuntime.split, zoom: state.zoom };
    readerQueuePositionSave();
  });
}
function readerSetDisplay(options) {
  readerPreserveLayout(() => {
    if (options.collapsed !== undefined) readerRuntime.chromeCollapsed = Boolean(options.collapsed);
    readerWriteLocal("display", state.reader.id, { collapsed: readerRuntime.chromeCollapsed });
  });
}
function readerSetSplit(ratio, persist = true) {
  readerPreserveLayout(() => { readerRuntime.split = Math.round(readerClamp(ratio, .3, .7, .5) * 1000) / 1000; });
  if (persist) {
    state.settings.reader_split_ratio = readerRuntime.split;
    clearTimeout(readerRuntime.splitSaveTimer);
    const split = readerRuntime.split;
    readerRuntime.splitSaveTimer = setTimeout(() => {
      api("/api/settings", { method: "PATCH", body: { reader_split_ratio: split } }).catch(() => readerSaveHint("默认分栏暂未同步，当前比例仍可继续使用"));
    }, 500);
  }
}
function readerBindDivider(divider) {
  let dragging = false;
  const move = event => {
    if (!dragging) return;
    const box = $("#reader-layout").getBoundingClientRect();
    const vertical = divider.getAttribute("aria-orientation") === "vertical";
    readerSetSplit(vertical ? (event.clientX - box.left) / box.width : (event.clientY - box.top) / box.height, false);
  };
  const finish = event => {
    if (!dragging) return;
    dragging = false; divider.classList.remove("dragging");
    if (divider.hasPointerCapture?.(event.pointerId)) divider.releasePointerCapture(event.pointerId);
    readerSetSplit(readerRuntime.split);
  };
  divider.addEventListener("pointerdown", event => { if (event.button !== 0) return; event.preventDefault(); dragging = true; divider.setPointerCapture(event.pointerId); divider.classList.add("dragging"); divider.focus({ preventScroll: true }); });
  divider.addEventListener("pointermove", move); divider.addEventListener("pointerup", finish); divider.addEventListener("pointercancel", finish); divider.addEventListener("lostpointercapture", finish);
  divider.addEventListener("keydown", event => {
    const step = event.shiftKey ? .05 : .02;
    const next = { ArrowLeft: readerRuntime.split - step, ArrowUp: readerRuntime.split - step, ArrowRight: readerRuntime.split + step, ArrowDown: readerRuntime.split + step, Home: .3, End: .7, Enter: .5 }[event.key];
    if (next === undefined) return; event.preventDefault(); readerSetSplit(next);
  });
  divider.addEventListener("dblclick", () => readerSetSplit(.5));
}
function readerPersistDrafts() {
  let safe = true;
  const notes = readerRuntime.notes;
  if (notes && notes.value !== notes.savedValue) safe = readerWriteLocal("notes", notes.id, { value: notes.value }) && safe;
  if (state.reader && readerScrollActive()) {
    const position = readerCapturePosition();
    safe = readerCachePosition(position, JSON.stringify(position) !== readerRuntime.savedSignature) && safe;
  }
  return safe;
}
function readerBindLifecycle() {
  if (readerRuntime.lifecycleBound) return; readerRuntime.lifecycleBound = true;
  window.addEventListener("beforeunload", event => {
    if (readerScrollActive() && !readerPersistDrafts()) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => { if (readerScrollActive()) readerPersistDrafts(); });
}

function readerLoadImage(image) {
  if (!image || image.getAttribute("src") || !image.dataset.src) return;
  const url = safeUrl(image.dataset.src); if (!url) return;
  image.src = url;
}
function readerMakePageImage(page, thumbnail = false) {
  const image = el("img", { alt: `第 ${page.number} 页${thumbnail ? "缩略图" : "原版 PDF"}`, decoding: "async", dataset: { src: page.image_url }, width: Math.round(Number(page.width) || 595), height: Math.round(Number(page.height) || 842), onload: event => event.target.closest(".paper-page")?.classList.add("page-loaded"), onerror: event => {
    const paper = event.target.closest(".paper-page"); if (!paper) return;
    paper.classList.add("page-failed");
    if (!$(".page-retry", paper)) paper.append(button("页面载入失败，点击重试", () => { paper.classList.remove("page-failed"); event.target.removeAttribute("src"); readerLoadImage(event.target); $(".page-retry", paper)?.remove(); }, "secondary", "", { class: "button secondary page-retry" }));
  } });
  return image;
}
function readerObserveImages() {
  readerRuntime.imageObserver?.disconnect();
  const root = $("#original-scroll"); if (!root) return;
  if (typeof IntersectionObserver !== "function") {
    $$(".reader-page-section", root).filter(n => Math.abs(Number(n.dataset.page) - state.readerPage) <= 1).forEach(n => readerLoadImage($("img", n)));
    return;
  }
  readerRuntime.imageObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) { readerLoadImage($("img", entry.target)); readerRuntime.imageObserver?.unobserve(entry.target); }
  }, { root, rootMargin: "80% 0px", threshold: 0 });
  $$(".reader-page-section", root).forEach(n => readerRuntime.imageObserver.observe(n));
}
function drawOriginalPage() {
  const p = state.reader, scroll = $("#original-scroll"); if (!p || !scroll) return;
  const anchor = readerCaptureAnchor(scroll, ".reader-page-section");
  const signature = JSON.stringify([p.id, p.pages.map(x => [x.number, x.width, x.height, x.image_url])]);
  if (readerRuntime.originalSignature !== signature) {
    scroll.replaceChildren(...p.pages.map(page => {
      const paper = typeof readerPdfPageNode === "function" ? readerPdfPageNode(page) : el("div", { class: "paper-page", style: { aspectRatio: `${Number(page.width) || 595}/${Number(page.height) || 842}` } }, readerMakePageImage(page));
      return el("section", { class: "reader-page-section", dataset: { page: page.number }, "aria-label": `原文第 ${page.number} 页` },
        paper,
        el("div", { class: "page-caption" }, `第 ${page.number} 页`));
    }));
    if (!p.pages.length) scroll.append(el("p", { class: "reader-error" }, "此 PDF 没有可显示的页面"));
    readerRuntime.originalSignature = signature;
    if (typeof readerPdfMount !== "function") readerObserveImages();
  }
  const scale = state.zoom === "fit" ? 1 : Math.max(0.4, Math.min(3, Number(state.zoom) / 100 || 1));
  scroll.style.setProperty("--pdf-page-width", `${Math.max(40, scroll.clientWidth - 40) * scale}px`);
  if (typeof readerPdfMount === "function") readerPdfMount("original", scroll, p.original_url || `/media/${encodeURIComponent(p.id)}/original.pdf`, `${p.id}:original:${p.original_url || ""}`, p.pages);
  readerRestoreAnchor(scroll, ".reader-page-section", anchor);
  readerRuntime.resizeAnchor = readerCaptureAnchor(scroll, ".reader-page-section");
}
function toggleReaderThumbnails() {
  readerRuntime.thumbsOpen = !readerRuntime.thumbsOpen;
  $("#reader-thumbs").hidden = !readerRuntime.thumbsOpen;
  $("#reader-thumbs-toggle").setAttribute("aria-expanded", String(readerRuntime.thumbsOpen));
  if (readerRuntime.thumbsOpen) renderThumbnails(); else readerRuntime.thumbObserver?.disconnect();
}
function renderThumbnails() {
  const p = state.reader, root = $("#reader-thumbs"); if (!p || !root || root.hidden) return;
  readerRuntime.thumbObserver?.disconnect();
  root.replaceChildren(...p.pages.map(page => el("button", { class: `page-thumb ${Number(page.number) === state.readerPage ? "active" : ""}`, title: `第 ${page.number} 页`, "aria-label": `跳转第 ${page.number} 页`, dataset: { page: page.number }, onclick: () => setReaderPage(Number(page.number), true) }, readerMakePageImage(page, true), el("span", {}, page.number))));
  if (typeof IntersectionObserver === "function") {
    readerRuntime.thumbObserver = new IntersectionObserver(entries => { for (const entry of entries) if (entry.isIntersecting) { readerLoadImage($("img", entry.target)); readerRuntime.thumbObserver?.unobserve(entry.target); } }, { root, rootMargin: "0px 40px", threshold: 0 });
    $$(".page-thumb", root).forEach(n => readerRuntime.thumbObserver.observe(n));
  } else $$(".page-thumb", root).slice(0, 10).forEach(n => readerLoadImage($("img", n)));
  readerUpdatePageUI();
}
function readerUpdatePageUI() {
  const input = $("#reader-page-input"); if (input && input !== document.activeElement) input.value = state.readerPage;
  $$(".page-thumb", $("#reader-thumbs")).forEach(n => { n.classList.toggle("active", Number(n.dataset.page) === state.readerPage); n.setAttribute("aria-current", Number(n.dataset.page) === state.readerPage ? "page" : "false"); });
  const thumb = $(".page-thumb.active", $("#reader-thumbs")), row = $("#reader-thumbs");
  if (thumb && row && !row.hidden) {
    const left = thumb.offsetLeft - row.offsetLeft;
    if (left < row.scrollLeft) row.scrollLeft = left;
    else if (left + thumb.offsetWidth > row.scrollLeft + row.clientWidth) row.scrollLeft = left + thumb.offsetWidth - row.clientWidth;
  }
  if ($("#reader-translation-status")) $("#reader-translation-status").textContent = `第 ${state.readerPage} 页`;
}
function readerSetActivePage(page) {
  const next = readerPageNumber(page, state.reader?.pages.length || 1), changed = next !== state.readerPage;
  state.readerPage = next; readerUpdatePageUI();
  if (changed) readerQueuePositionSave();
  return changed;
}
function setReaderPage(page, scrollChinese = false) {
  if (!state.reader) return;
  // 显式跳页优先于刚收起笔记等操作留下的布局锚点恢复。
  cancelAnimationFrame(readerRuntime.layoutFrame); readerRuntime.layoutFrame = 0;
  drawOriginalPage();
  readerSetActivePage(page);
  const node = readerOriginalPage(state.readerPage); readerLoadImage(node && $("img", node));
  readerScrollToNode($("#original-scroll"), node);
  if (scrollChinese) scrollTranslationToPage(state.readerPage);
  readerQueuePositionSave();
}
async function saveReadPage() {
  clearTimeout(readSaveTimer); if (!state.reader) return;
  if (readerRuntime.savePromise) { await readerRuntime.savePromise; return saveReadPage(); }
  const id = state.reader.id, position = readerCapturePosition(), signature = JSON.stringify(position);
  if (readerRuntime.savedSignature === signature) return;
  readerCachePosition(position);
  readerRuntime.savePromise = (async () => {
    const data = await api(`/api/papers/${encodeURIComponent(id)}`, { method: "PATCH", body: { reader_state: position } });
    if (state.reader?.id !== id) return;
    updatePaper({ id, reader_state: position, read_page: position.page, ...(data.paper || (data.id ? data : {})) });
    readerRuntime.savedSignature = signature;
    if (JSON.stringify(readerCapturePosition()) === signature) readerCachePosition(position, false);
    readerSaveHint(`已保存阅读位置 · 第 ${position.page} 页`);
  })();
  try {
    await readerRuntime.savePromise;
  } catch (error) {
    readerSaveHint("位置暂存本机；继续阅读时重试同步");
    if (!readerCachePosition()) throw error;
  } finally { readerRuntime.savePromise = null; }
}
function scrollTranslationToPage(page) { readerScrollToNode($("#translation-scroll"), readerTranslationPage(page)); }
function readerSetZoom(value) {
  const zoom = value === "fit" ? "fit" : Number(value);
  if (!readerScrollActive() || zoom !== "fit" && !READER_ZOOMS.includes(zoom) || zoom === state.zoom) return;
  state.zoom = zoom;
  const select = $("#reader-zoom"); if (select) select.value = String(zoom);
  if (typeof syncSelectMenus === "function") syncSelectMenus();
  drawOriginalPage(); readerQueuePositionSave();
}
function readerHandleZoomWheel(event, container) {
  if (!event.ctrlKey || !event.cancelable || !Number.isFinite(event.deltaY) || !event.deltaY || !readerScrollActive()) return false;
  // Cancel browser zoom, including at either limit, before changing reader content.
  event.preventDefault();
  const enlarge = event.deltaY < 0;
  if (container.id === "original-scroll") {
    const current = state.zoom === "fit" ? 100 : Number(state.zoom);
    const next = enlarge ? READER_ZOOMS.find(value => value > current) : [...READER_ZOOMS].reverse().find(value => value < current);
    if (next !== undefined) readerSetZoom(next);
  } else {
    const current = readerClamp(state.settings.reader_zoom, 40, 200, 100);
    readerSetTextZoom(readerClamp(current + (enlarge ? 10 : -10), 40, 200, 100));
  }
  return true;
}
function readerSyncTextZoom() {
  const select = $("#reader-text-zoom"); if (!select) return;
  const value = String(readerClamp(state.settings.reader_zoom, 40, 200, 100));
  for (const option of [...select.options]) if (option.dataset.custom) option.remove();
  if (![...select.options].some(o => o.value === value)) select.append(el("option", { value, dataset: { custom: "true" } }, `${value}%`));
  select.value = value;
  if (typeof syncSelectMenus === "function") syncSelectMenus();
}
function readerSetTextZoom(value) {
  const next = Math.round(readerClamp(value, 40, 200, 100));
  if (next === (state.settings.reader_zoom ?? 100)) return;
  readerRuntime.resizeAnchor = readerCaptureAnchor($("#original-scroll"), ".reader-page-section");
  readerRuntime.translationAnchor = readerCaptureAnchor($("#translation-scroll"), readerRightAnchorSelector());
  changePreference({ reader_zoom: next }); readerSyncTextZoom();
}
function readerSyncScroll(source) {
  if (!readerPdfLayoutActive()) return;
  const original = $("#original-scroll"), translated = $("#translation-scroll");
  const target = source === original ? translated : original;
  const fromSelector = source === original ? ".reader-page-section" : ".reader-source-page";
  const toSelector = source === original ? ".reader-source-page" : ".reader-page-section";
  const fromMax = Math.max(0, source.scrollHeight - source.clientHeight);
  const toMax = Math.max(0, target.scrollHeight - target.clientHeight);
  if (!fromMax) return;
  const targets = new Map($$(toSelector, target).map(n => [n.dataset.page, n]));
  const top = (node, pane) => node.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop;
  const points = [[0, 0]];
  for (const node of $$(fromSelector, source)) {
    const peer = targets.get(node.dataset.page); if (!peer) continue;
    const x = top(node, source), y = top(peer, target), last = points.at(-1);
    if (x > last[0] && y > last[1] && x < fromMax && y < toMax) points.push([x, y]);
  }
  points.push([fromMax, toMax]);
  const position = readerClamp(source.scrollTop, 0, fromMax);
  for (let i = 1; i < points.length; i++) {
    const [x, y] = points[i], [previousX, previousY] = points[i - 1];
    if (position > x) continue;
    readerMoveScroll(target, previousY + (y - previousY) * (position - previousX) / (x - previousX));
    break;
  }
}
function readerBindScrolling() {
  const original = $("#original-scroll"), translated = $("#translation-scroll");
  for (const [container, side, frame] of [[original, "suppressLeftUntil", "leftFrame"], [translated, "suppressRightUntil", "rightFrame"]]) {
    const release = () => { readerRuntime[side] = 0; };
    container.addEventListener("wheel", event => {
      if (readerHandleZoomWheel(event, container)) return;
      release();
    }, { passive: false });
    container.addEventListener("touchstart", release, { passive: true }); container.addEventListener("pointerdown", release);
    container.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End", " "].includes(event.key)) release();
    });
    container.addEventListener("scroll", () => {
      if (readerRuntime[frame]) return;
      readerRuntime[frame] = requestAnimationFrame(() => {
        readerRuntime[frame] = 0; if (!readerScrollActive() || performance.now() < readerRuntime[side]) return;
        const left = container === original;
        const node = readerAtPage(container, $$(left ? ".reader-page-section" : ".reader-source-page", container)); if (!node) return;
        const page = Number(node.dataset.page); readerSetActivePage(page);
        if (left) readerRuntime.resizeAnchor = readerCaptureAnchor(original, ".reader-page-section");
        else readerRuntime.translationAnchor = readerCaptureAnchor(translated, readerRightAnchorSelector());
        if (readerSyncEnabled() && !readerIsEditing()) readerSyncScroll(container);
        if (typeof IntersectionObserver !== "function" && left) readerObserveImages();
        readerQueuePositionSave();
      });
    }, { passive: true });
  }
  if (typeof ResizeObserver === "function") {
    let width = original.clientWidth;
    readerRuntime.resizeObserver = new ResizeObserver(() => {
      if (Math.abs(original.clientWidth - width) < 1 || !readerScrollActive()) return;
      if (!original.clientWidth) return;
      const anchor = readerRuntime.resizeAnchor, translationAnchor = readerRuntime.translationAnchor; width = original.clientWidth;
      drawOriginalPage(); readerRestoreAnchor(original, ".reader-page-section", anchor);
      if (typeof readerPdfLayoutResize === "function") readerPdfLayoutResize();
      readerRestoreAnchor(translated, readerRightAnchorSelector(), translationAnchor);
      readerApplyLayout();
    });
    readerRuntime.resizeObserver.observe(original);
  }
}

function renderTranslations() {
  if (!state.reader) return;
  const anchor = readerCaptureAnchor($("#original-scroll"), ".reader-page-section");
  readerApplyLayout();
  renderReaderPdfLayout();
  drawOriginalPage();
  readerRestoreAnchor($("#original-scroll"), ".reader-page-section", anchor);
  state.readerRenderedSignature = paperSignature(state.reader);
}
// 论文对话中的旧源块引用继续按其 PDF 页定位，不创建旧正文节点。
function selectBlock(id, scrollChinese) {
  const block = state.reader?.blocks.find(item => item.id === id);
  if (block) setReaderPage(block.page, scrollChinese);
}
function readerNotesHint(message) { const node = $("#reader-notes-hint"); if (node) node.textContent = message; }
async function readerSaveNotes() {
  const entry = readerRuntime.notes; if (!entry || entry.released) return;
  clearTimeout(entry.timer);
  if (entry.saving) { await entry.saving; if (entry.value !== entry.savedValue) return readerSaveNotes(); return; }
  if (entry.value === entry.savedValue) return;
  const value = entry.value; readerNotesHint("正在保存…");
  entry.saving = (async () => {
    const data = await api(`/api/papers/${encodeURIComponent(entry.id)}`, { method: "PATCH", body: { notes: value } });
    if (entry.released) return;
    entry.savedValue = value; updatePaper({ id: entry.id, notes: value, ...(data.paper || (data.id ? data : {})) });
    if (entry.value === value) { readerRemoveLocal("notes", entry.id); readerNotesHint("笔记已保存在文献库"); }
    else readerNotesHint("最新修改保存在本机草稿中");
  })();
  try { await entry.saving; }
  catch (error) {
    if (entry.released) return;
    entry.storageOk = readerWriteLocal("notes", entry.id, { value: entry.value });
    readerNotesHint(entry.storageOk ? "同步失败，笔记草稿已保存在本机" : "保存失败，请保持窗口打开"); throw error;
  } finally { entry.saving = null; }
}
function toggleReaderNotes(force) {
  const pane = $("#reader-notes"), entry = readerRuntime.notes; if (!pane || !entry) return;
  const show = typeof force === "boolean" ? force : pane.hidden;
  if (show && typeof closePaperChat === "function") closePaperChat();
  readerPreserveLayout(() => {
    pane.hidden = !show; $("#reader-notes-toggle").setAttribute("aria-expanded", String(show));
    if (!show) { readerSaveNotes().catch(() => {}); return; }
    if (pane.childElementCount) return;
    const textarea = el("textarea", { class: "notes-area", value: entry.value, placeholder: "论文笔记", "aria-label": "论文笔记", id: "reader-notes-text" });
    textarea.addEventListener("input", () => {
      entry.value = textarea.value; entry.storageOk = readerWriteLocal("notes", entry.id, { value: entry.value });
      readerNotesHint(entry.storageOk ? "草稿已保存在本机，正在等待同步…" : "草稿暂未保存，请保持窗口打开");
      clearTimeout(entry.timer); entry.timer = setTimeout(() => { readerSaveNotes().catch(() => {}); }, 650);
    });
    textarea.addEventListener("blur", () => { readerSaveNotes().catch(() => {}); });
    pane.append(el("div", { class: "section-heading" }, el("h3", {}, "论文笔记"), iconButton("close", "收起笔记", () => toggleReaderNotes(false))), textarea,
      el("div", { class: "save-hint", id: "reader-notes-hint", role: "status" }, entry.value !== entry.savedValue ? "已恢复本机草稿" : ""),
      button("立即保存", event => act(readerSaveNotes, event.currentTarget), "secondary", "check"));
  });
  if (show) $("#reader-notes-text")?.focus({ preventScroll: true });
}
function updateReaderProgress() {
  if (!state.reader) return;
  readerPdfLayoutUpdateControls();
}
async function refreshReader() {
  if (!readerScrollActive() || typeof readerPdfHasSelection === "function" && readerPdfHasSelection()) return;
  const id = state.reader.id, request = ++readerRuntime.request;
  const p = normalizeDetail(await api(`/api/papers/${encodeURIComponent(id)}`));
  if (state.reader?.id !== id || !readerScrollActive() || request !== readerRuntime.request || typeof readerPdfHasSelection === "function" && readerPdfHasSelection()) return;
  updatePaper(p); renderTranslations(); updateReaderProgress();
}
function applyReaderPreferences() {
  const toggle = $("#reader-sync"); if (toggle) toggle.checked = readerSyncEnabled();
  if (!readerScrollActive()) return;
  readerSyncTextZoom();
  const originalAnchor = readerRuntime.resizeAnchor, translationAnchor = readerRuntime.translationAnchor;
  drawOriginalPage(); readerRestoreAnchor($("#original-scroll"), ".reader-page-section", originalAnchor);
  if (typeof readerPdfLayoutResize === "function") readerPdfLayoutResize();
  readerRestoreAnchor($("#translation-scroll"), readerRightAnchorSelector(), translationAnchor);
  readerQueuePositionSave();
}
