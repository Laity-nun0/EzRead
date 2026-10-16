"use strict";

// PDF documents are shared by URL; only visible pages and their neighbours own
// raster buffers. PDF text layers are copyable, never HTML revision anchors.
const readerPdf = { library: null, documents: new Map(), panes: new Map(), queue: [], busy: 0, frame: 0 };
const READER_PDF_PAGE_PIXELS = 8000000, READER_PDF_TOTAL_PIXELS = 32000000;
document.addEventListener("selectionchange", readerPdfSchedule);
window.addEventListener("resize", readerPdfSchedule);
function readerPdfWatchDensity() {
  if (typeof matchMedia !== "function") return;
  matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`).addEventListener("change", () => { readerPdfWatchDensity(); readerPdfSchedule(); }, { once: true });
}
readerPdfWatchDensity();
function readerPdfLibrary() {
  if (!readerPdf.library) readerPdf.library = import("/static/vendor/pdfjs/pdf.mjs").then(library => {
    library.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.mjs"; return library;
  }).catch(error => { readerPdf.library = null; throw error; });
  return readerPdf.library;
}
function readerPdfPageNode(page) {
  return el("div", { class: "paper-page reader-pdf-page", dataset: { pdfPage: page.number }, style: { aspectRatio: `${Number(page.width) || 595}/${Number(page.height) || 842}` } },
    el("canvas", { width: 1, height: 1, "aria-hidden": "true" }),
    el("div", { class: "pdf-text-layer textLayer", "aria-label": `PDF 第 ${page.number} 页文字，可选择复制` }));
}
function readerPdfSelected(entry) {
  const selection = window.getSelection?.();
  if (!selection?.rangeCount || selection.isCollapsed) return false;
  try {
    if (entry.node.contains(selection.anchorNode) || entry.node.contains(selection.focusNode)) return true;
    for (let i = 0; i < selection.rangeCount; i++) if (selection.getRangeAt(i).intersectsNode(entry.node)) return true;
  } catch { /* A detached selection must not stop page cleanup. */ }
  return false;
}
function readerPdfHasSelection() { return [...readerPdf.panes.values()].some(pane => pane.entries.some(readerPdfSelected)); }
function readerPdfDocument(url) {
  let record = readerPdf.documents.get(url);
  if (!record) {
    record = { references: 0, loading: null, document: null };
    record.promise = readerPdfLibrary().then(library => {
      if (!record.references) throw Error("PDF view was closed");
      record.loading = library.getDocument({ url, cMapUrl: "/static/vendor/pdfjs/cmaps/", cMapPacked: true,
        standardFontDataUrl: "/static/vendor/pdfjs/standard_fonts/", wasmUrl: "/static/vendor/pdfjs/wasm/", disableAutoFetch: true, disableStream: true, isEvalSupported: false });
      return record.loading.promise;
    }).then(document => { record.document = document; return document; });
    // The per-pane handler reports failures; prevent an unhandled rejection
    // if the pane closes before its document download finishes.
    record.promise.catch(() => {}); readerPdf.documents.set(url, record);
  }
  record.references++; return record;
}
function readerPdfRelease(pane) {
  if (!pane) return;
  pane.alive = false; pane.observer?.disconnect(); pane.resize?.disconnect();
  if (pane.scroll) pane.root.removeEventListener("scroll", pane.scroll);
  for (const entry of pane.entries) {
    entry.generation++; entry.task?.cancel(); entry.textTask?.cancel();
    entry.page?.cleanup?.(); entry.page = null;
    entry.canvas.width = entry.canvas.height = 1;
  }
  const record = pane.record;
  if (record && --record.references === 0) {
    readerPdf.documents.delete(pane.url);
    record.loading?.destroy().catch(() => {});
  }
}
function readerPdfUnmount(side) {
  readerPdfRelease(readerPdf.panes.get(side)); readerPdf.panes.delete(side);
}
function readerPdfDispose() {
  cancelAnimationFrame(readerPdf.frame); readerPdf.frame = 0;
  for (const side of [...readerPdf.panes.keys()]) readerPdfUnmount(side);
  readerPdf.queue = [];
}
function readerPdfFailure(entry, message) {
  if (!entry.pane.alive || !entry.node.isConnected) return;
  entry.failed = true; entry.node.classList.add("page-failed"); entry.node.classList.remove("page-loaded");
  entry.node.dataset.pdfState = "error";
  $(".reader-pdf-error", entry.node)?.remove();
  entry.node.append(el("div", { class: "reader-pdf-error", role: "status" },
    el("p", {}, `PDF 页面读取失败：${message}`),
    button("重试 PDF", () => { const pane = entry.pane; readerPdfMount(pane.side, pane.root, pane.url, pane.key, pane.pages, true); }, "secondary"),
    entry.preview ? button("显示预览图", () => {
      if ($("img", entry.node)) return;
      const image = imageNode(entry.preview, { alt: "PDF 预览图（非清晰 PDF 页面）", onload: () => entry.node.classList.add("page-loaded") });
      entry.canvas.style.display = entry.layer.style.display = "none";
      entry.node.append(image); entry.node.dataset.pdfPreview = "true";
      $(".reader-pdf-error p", entry.node).textContent = "当前显示预览图；清晰 PDF 尚未载入。";
    }, "secondary") : null));
}
function readerPdfMount(side, root, url, key, pages, retry = false) {
  if (!root) return;
  const previous = readerPdf.panes.get(side);
  if (!retry && previous?.root === root && previous.key === key && previous.entries.every(e => e.node.isConnected)) { readerPdfSchedule(); return; }
  readerPdfUnmount(side);
  const pane = { side, root, url: safeUrl(url), key, pages, alive: true, entries: [] };
  pane.entries = $$(".reader-pdf-page", root).map(node => ({ pane, node, canvas: $("canvas", node), layer: $(".textLayer", node), number: Number(node.dataset.pdfPage), generation: 0,
    active: false, visible: false, queued: false, rendering: false, failed: false, drawn: "", target: "", preview: pages.find(p => Number(p.number) === Number(node.dataset.pdfPage))?.image_url }));
  readerPdf.panes.set(side, pane);
  if (retry) for (const entry of pane.entries) {
    $(".reader-pdf-error", entry.node)?.remove(); $("img", entry.node)?.remove();
    entry.canvas.style.display = entry.layer.style.display = "";
    delete entry.node.dataset.pdfPreview; entry.node.classList.remove("page-failed", "page-loaded");
  }
  if (!pane.entries.length) return;
  if (!pane.url) { pane.entries.forEach(entry => readerPdfFailure(entry, "文档地址不可用，请通过文献重新打开")); return; }
  pane.record = readerPdfDocument(pane.url);
  pane.record.promise.then(() => readerPdfSchedule()).catch(error => pane.entries.forEach(entry => readerPdfFailure(entry, error.message)));
  pane.scroll = () => readerPdfSchedule(); root.addEventListener("scroll", pane.scroll, { passive: true });
  if (typeof ResizeObserver === "function") { pane.resize = new ResizeObserver(readerPdfSchedule); pane.resize.observe(root); pane.entries.forEach(e => pane.resize.observe(e.node)); }
  if (typeof IntersectionObserver === "function") { pane.observer = new IntersectionObserver(readerPdfSchedule, { root, rootMargin: "100% 0px" }); pane.entries.forEach(e => pane.observer.observe(e.node)); }
  readerPdfSchedule();
}
function readerPdfSchedule() {
  if (readerPdf.frame) return;
  readerPdf.frame = requestAnimationFrame(() => { readerPdf.frame = 0; readerPdfScan(); });
}
function readerPdfScan() {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  for (const pane of readerPdf.panes.values()) {
    if (!pane.alive) continue;
    const viewport = pane.root.getBoundingClientRect(), visible = [];
    pane.entries.forEach((entry, i) => { const box = entry.node.getBoundingClientRect(); entry.visible = box.bottom > viewport.top && box.top < viewport.bottom; if (entry.visible) visible.push(i); });
    const wanted = new Set(visible.flatMap(i => [i - 1, i, i + 1]));
    pane.entries.forEach((entry, i) => { entry.active = wanted.has(i); });
  }
  const budget = readerPdfBudget();
  for (const pane of readerPdf.panes.values()) {
    if (!pane.alive) continue;
    for (let i = 0; i < pane.entries.length; i++) {
      const entry = pane.entries[i];
      if (!entry.active) {
        if (!readerPdfSelected(entry) && (entry.canvas.width > 1 || entry.rendering || entry.layer.childElementCount)) {
          entry.generation++; entry.task?.cancel(); entry.textTask?.cancel();
          entry.canvas.width = entry.canvas.height = 1; entry.drawn = "";
          entry.node.dataset.pdfState = "deferred";
          entry.node.classList.remove("page-loaded"); entry.layer.replaceChildren();
          entry.page?.cleanup?.(); entry.page = null;
        }
        continue;
      }
      const width = entry.node.getBoundingClientRect().width;
      const key = `${Math.round(width * 100) / 100}:${ratio}:${budget}`;
      if (width < 1 || budget < 1 || entry.failed || entry.drawn === key || readerPdfSelected(entry)) continue;
      if (entry.target !== key) { entry.target = key; entry.generation++; entry.task?.cancel(); entry.textTask?.cancel(); }
      if (!entry.rendering && !entry.queued) { entry.queued = true; readerPdf.queue.push(entry); }
    }
  }
  readerPdfDrain();
}
function readerPdfBudget() {
  const entries = [...readerPdf.panes.values()].flatMap(pane => pane.entries);
  const activeCount = entries.filter(entry => entry.active && !readerPdfSelected(entry)).length;
  const heldPixels = entries.filter(readerPdfSelected).reduce((n, entry) => n + entry.canvas.width * entry.canvas.height, 0);
  return Math.max(0, Math.min(READER_PDF_PAGE_PIXELS, (READER_PDF_TOTAL_PIXELS - heldPixels) / Math.max(1, activeCount)));
}
function readerPdfDrain() {
  while (readerPdf.busy < 2 && readerPdf.queue.length) {
    const entry = readerPdf.queue.shift(); entry.queued = false;
    if (!entry.pane.alive || !entry.active || entry.rendering || entry.failed || readerPdfSelected(entry)) continue;
    readerPdf.busy++; entry.rendering = true;
    const generation = entry.generation, key = entry.target;
    readerPdfRender(entry, generation, key).catch(error => {
      if (entry.generation === generation && error.name !== "RenderingCancelledException") readerPdfFailure(entry, error.message);
    }).finally(() => { readerPdf.busy--; entry.rendering = false; entry.task = entry.textTask = null; readerPdfSchedule(); readerPdfDrain(); });
  }
}
async function readerPdfRender(entry, generation, key) {
  const pane = entry.pane, document = await pane.record.promise;
  const page = await document.getPage(entry.number);
  // A native selection can appear while the page or canvas promise is pending.
  // Recheck at each boundary before touching the existing text layer.
  if (!pane.alive || entry.generation !== generation || !entry.active || !entry.node.isConnected || readerPdfSelected(entry)) return;
  const library = await readerPdfLibrary();
  if (!pane.alive || entry.generation !== generation || !entry.active || !entry.node.isConnected || readerPdfSelected(entry)) return;
  const width = entry.node.getBoundingClientRect().width, base = page.getViewport({ scale: 1 });
  const viewport = page.getViewport({ scale: width / base.width });
  entry.page = page;
  const budget = readerPdfBudget();
  if (budget < 1) return;
  const ratio = Math.min(Math.max(1, window.devicePixelRatio || 1), Math.sqrt(budget / (viewport.width * viewport.height)));
  entry.canvas.width = Math.max(1, Math.floor(viewport.width * ratio)); entry.canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
  entry.node.style.aspectRatio = `${base.width}/${base.height}`;
  entry.node.dataset.pdfPixelRatio = String(ratio); entry.node.dataset.pdfState = "rendering";
  entry.task = page.render({ canvasContext: entry.canvas.getContext("2d", { alpha: false }), viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
  await entry.task.promise;
  if (!pane.alive || entry.generation !== generation || !entry.node.isConnected || readerPdfSelected(entry)) return;
  entry.layer.replaceChildren();
  // These variables match the official PDF.js 6.4 TextLayer stylesheet/API.
  entry.layer.style.setProperty("--scale-factor", String(viewport.scale));
  entry.layer.style.setProperty("--user-unit", String(page.userUnit || 1));
  entry.textTask = new library.TextLayer({ textContentSource: page.streamTextContent(), container: entry.layer, viewport });
  await entry.textTask.render();
  if (!pane.alive || entry.generation !== generation || !entry.node.isConnected) return;
  entry.drawn = key; entry.node.dataset.pdfState = "loaded"; entry.node.classList.remove("page-failed"); entry.node.classList.add("page-loaded");
  $(".reader-pdf-error", entry.node)?.remove();
}
