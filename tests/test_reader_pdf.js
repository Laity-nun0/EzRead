"use strict";

const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
function node(tag, attrs = {}, ...children) {
  const classes = new Set((attrs.class || "").split(" "));
  const value = { tag, attrs, dataset: { ...attrs.dataset }, children: [], width: attrs.width || 1, height: attrs.height || 1, isConnected: true,
    classList: { add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)), contains: n => classes.has(n) },
    style: { setProperty(k, v) { this[k] = v; } }, box: { top: 0, bottom: 600, width: 500, height: 600 },
    addEventListener() {}, removeEventListener() {}, getBoundingClientRect() { return this.box; },
    getContext() { return { canvas: this, drawImage(source) { this.canvas.bitmap = source.bitmap; } }; }, contains(other) { return walk(this).includes(other); },
    append(...items) { for (const item of items.flat().filter(x => x != null)) { this.children.push(item); if (typeof item === "object") item.parent = this; } },
    replaceChildren(...items) { this.children = []; this.append(...items); }, remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); },
    get childElementCount() { return this.children.filter(x => typeof x === "object").length; } };
  value.append(...children); return value;
}
function walk(root) { return [root, ...root.children.flatMap(child => typeof child === "object" ? walk(child) : [])]; }
function find(selector, root) { return walk(root).filter(n => selector.startsWith(".") ? n.classList.contains(selector.slice(1)) : n.tag === selector); }
const frames = new Map(), pendingPages = [], pendingRenders = [], renderCanvases = [];
let frame = 0, opened = 0, destroyed = 0, cancelled = 0, fail = false, slow = false, slowPage = false, selection = null, parameters;
const library = { getDocument(options) {
  opened++; parameters = options;
  return { destroy: async () => { destroyed++; }, promise: fail ? Promise.reject(Error("synthetic missing PDF")) : Promise.resolve({ getPage: async () => {
    if (slowPage) await new Promise(resolve => pendingPages.push(resolve));
    return {
    userUnit: 1, getViewport: ({ scale }) => ({ width: 595 * scale, height: 842 * scale, scale }), streamTextContent: () => ({}),
    render({ canvasContext }) {
      renderCanvases.push(canvasContext.canvas);
      canvasContext.canvas.bitmap = "partial";
      let resolve;
      const promise = (slow ? new Promise(done => { resolve = done; pendingRenders.push(done); }) : Promise.resolve())
        .then(() => { canvasContext.canvas.bitmap = "complete"; });
      return { promise, cancel() { cancelled++; resolve?.(); } };
    }
  }; } }) };
}, TextLayer: class { constructor({ container }) { this.container = container; } async render() { this.container.append(node("span", {}, "copyable PDF text")); } cancel() {} } };
const context = { console, el: node, safeUrl: x => x, $: (selector, root) => find(selector, root)[0], $$: find,
  button: (label, handler) => node("button", { handler }, label),
  document: { addEventListener() {} }, window: { devicePixelRatio: 2, addEventListener() {}, getSelection: () => selection || { isCollapsed: true, rangeCount: 0 } },
  requestAnimationFrame: fn => { frames.set(++frame, fn); return frame; }, cancelAnimationFrame: id => frames.delete(id) };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/reader-pdf.js"), "utf8") + "\nglobalThis.P=readerPdf;", context);
context.readerPdfLibrary = async () => library;
function pane(count) {
  const pages = Array.from({ length: count }, (_, i) => ({ number: i + 1, width: 595, height: 842, image_url: "/preview.jpg" }));
  const entries = pages.map(page => context.readerPdfPageNode(page));
  entries.forEach((entry, i) => { entry.box = { top: i * 730, bottom: i * 730 + 707, width: 500, height: 707 }; });
  return { root: node("div", {}, entries), pages, entries };
}
async function flush(times = 8) { for (let i = 0; i < times; i++) { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn()); await new Promise(setImmediate); } }
async function main() {
  const left = pane(80), right = pane(2);
  context.readerPdfMount("original", left.root, "/source.pdf", "source", left.pages);
  context.readerPdfMount("layout", right.root, "/source.pdf", "layout", right.pages);
  await flush();
  assert.equal(opened, 1, "Both panes share a document when they use the same immutable PDF URL.");
  assert.equal(parameters.disableAutoFetch, true); assert.equal(parameters.disableStream, true);
  assert.equal(parameters.isEvalSupported, false);
  assert.equal(left.entries.filter(entry => find("canvas", entry)[0].width > 1).length, 2, "An 80-page paper allocates only the visible page and its neighbour.");
  assert.equal(find("canvas", left.entries[0])[0].width, 1000, "Canvas resolution follows the actual CSS width times devicePixelRatio.");
  assert.equal(left.entries[0].dataset.pdfState, "loaded"); assert.ok(left.entries[0].classList.contains("page-loaded"));
  assert.equal(find("span", left.entries[0])[0].children[0], "copyable PDF text");
  selection = { rangeCount: 1, isCollapsed: false, anchorNode: find("span", left.entries[0])[0],
    getRangeAt: () => ({ intersectsNode: entry => entry === left.entries[0] || entry === left.entries[1] }) };
  left.entries.forEach((entry, i) => { entry.box.top = i * 730 - 4000; entry.box.bottom = entry.box.top + 707; });
  context.readerPdfScan(); await flush();
  assert.equal(find("span", left.entries[0]).length, 1);
  assert.equal(find("span", left.entries[1]).length, 1, "Cross-page selection protects the non-anchor page as well.");
  selection = null; context.readerPdfScan(); await flush();
  assert.equal(find("span", left.entries[0]).length, 0); assert.equal(find("span", left.entries[1]).length, 0);
  assert.equal(find("canvas", left.entries[1])[0].width, 1, "Selected pages are released after clearing the native selection.");
  left.entries.forEach((entry, i) => { entry.box.top = i * 730; entry.box.bottom = entry.box.top + 707; });
  context.readerPdfScan(); await flush();
  left.entries[0].box.width = 200000; context.readerPdfScan(); await flush();
  assert.ok(find("canvas", left.entries[0])[0].width * find("canvas", left.entries[0])[0].height <= 8000000);
  assert.ok([...left.entries, ...right.entries].reduce((sum, entry) => { const canvas = find("canvas", entry)[0]; return sum + canvas.width * canvas.height; }, 0) <= 32000000);
  slow = true; left.entries[0].box.width = 900; context.readerPdfScan(); await flush(2);
  const stale = left.entries[0];
  left.entries.forEach((entry, i) => { entry.box.top = i * 730 - 4000; entry.box.bottom = entry.box.top + 707; });
  context.readerPdfScan(); await flush(2);
  assert.ok(cancelled > 0); assert.equal(find("canvas", stale)[0].width, 1);
  assert.equal(stale.dataset.pdfState, "deferred", "A cancelled old render cannot mark an evicted page as loaded.");
  slow = false;
  context.readerPdfDispose(); await flush();
  assert.equal(context.P.documents.size, 0); assert.equal(context.P.panes.size, 0); assert.equal(destroyed, 1);
  fail = true; const missing = pane(1);
  context.readerPdfMount("original", missing.root, "/missing.pdf", "missing", missing.pages); await flush();
  assert.equal(missing.entries[0].dataset.pdfState, "error"); assert.ok(missing.entries[0].classList.contains("page-failed"));
  assert.equal(find("img", missing.entries[0]).length, 0, "A failed PDF never silently reports a JPEG preview as successful PDF rendering.");
  context.readerPdfDispose(); await flush();
  assert.equal(context.P.documents.size, 0);
  fail = false;
  slow = true;
  const opening = pane(1); context.readerPdfMount("opening", opening.root, "/opening.pdf", "opening", opening.pages); await flush(2);
  assert.ok(pendingRenders.length);
  assert.equal(find("canvas", opening.entries[0])[0].width, 1, "首次加载不能显示尚未画完的 PDF 画布。");
  assert.notEqual(find("canvas", opening.entries[0])[0].bitmap, "partial");
  slow = false; pendingRenders.splice(0).forEach(resolve => resolve()); await flush();
  assert.equal(find("canvas", opening.entries[0])[0].bitmap, "complete");
  context.readerPdfDispose(); await flush();
  const racing = pane(1); context.readerPdfMount("race", racing.root, "/race.pdf", "race", racing.pages); await flush();
  const originalSpan = find("span", racing.entries[0])[0], originalWidth = find("canvas", racing.entries[0])[0].width;
  slowPage = true; racing.entries[0].box.width = 600; context.readerPdfScan(); await flush(2);
  assert.ok(pendingPages.length, "The page fetch must actually be pending before creating the test selection.");
  selection = { rangeCount: 1, isCollapsed: false, anchorNode: originalSpan };
  slowPage = false; pendingPages.splice(0).forEach(resolve => resolve()); await flush();
  assert.equal(find("span", racing.entries[0])[0], originalSpan, "A selection made during getPage preserves the original text node.");
  assert.equal(find("canvas", racing.entries[0])[0].width, originalWidth);
  selection = null; context.readerPdfScan(); await flush();
  assert.notEqual(find("span", racing.entries[0])[0], originalSpan, "Clearing the selection permits the delayed page redraw.");
  const renderSpan = find("span", racing.entries[0])[0], displayedCanvas = find("canvas", racing.entries[0])[0], displayedWidth = displayedCanvas.width;
  slow = true; racing.entries[0].box.width = 700; context.readerPdfScan(); await flush(2);
  assert.ok(pendingRenders.length);
  assert.equal(displayedCanvas.width, displayedWidth, "重绘期间保留原画布，不能先清空已经显示的论文。");
  assert.equal(displayedCanvas.bitmap, "complete", "不能向用户显示逐步绘制中的文字和图形。");
  selection = { rangeCount: 1, isCollapsed: false, anchorNode: renderSpan };
  slow = false; pendingRenders.splice(0).forEach(resolve => resolve()); await flush();
  assert.equal(find("span", racing.entries[0])[0], renderSpan, "A selection made during canvas rendering survives text-layer cleanup.");
  assert.equal(displayedCanvas.width, displayedWidth, "中途产生文字选区时也不能提交新的画布。");
  selection = null; context.readerPdfScan(); await flush();
  assert.notEqual(find("span", racing.entries[0])[0], renderSpan);
  assert.equal(find("canvas", racing.entries[0])[0].width, 1400);
  assert.equal(racing.entries[0].dataset.pdfState, "loaded");
  context.readerPdfDispose(); await flush();
  assert.ok(renderCanvases.every(canvas => canvas.width === 1 && canvas.height === 1), "完成、取消或选区保护后都应释放离屏缓冲区。");
  console.log("PDF rendering scheduler passed: shared documents, lazy raster allocation, DPR scaling, pixel budget, atomic raster display, cancellation, explicit failure and close cleanup.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
