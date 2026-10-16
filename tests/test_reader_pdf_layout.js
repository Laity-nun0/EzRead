"use strict";

const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
function element(tag, attrs = {}, ...children) {
  return { tag, attrs, dataset: attrs.dataset || {}, childNodes: children.flat().filter(x => x != null),
    checked: Boolean(attrs.checked), listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
    style: { values: {}, setProperty(k, v) { this.values[k] = v; } },
    setAttribute(k, v) { this.attrs[k] = v; }, replaceChildren(...nodes) { this.childNodes = nodes; },
    append(...nodes) { this.childNodes.push(...nodes); }, clientWidth: 500 };
}
const originalNode = element("p", {}, "editable text"), root = element("div", {}, originalNode);
const storage = new Map(), state = { settings: {}, papers: [], reader: { id: "paper", blocks: [{ id: "block", text: "original", translation: "retained" }], pages: [{ number: 1 }] }, readerPage: 1 };
const artifact = { id: "v1", quality: "needs_review", warnings: ["检查公式位置"], mono_url: "/mono.pdf", dual_url: "/dual.pdf", pages: [{ number: 1, width: 595, height: 842, image_url: "/page.png" }] };
const hint = element("span"), nodes = new Map([["#translation-scroll", root], ["#reader-dialog", { open: true }], ["#reader-selection-mode-hint", hint]]);
let saved = 0, fail = false, protectedText = false, request = null;
const context = { state, console, el: element, button: (label, handler, kind, icon, attrs) => element("button", { ...attrs, handler }, label),
  $: selector => nodes.get(selector), $$: () => [], safeUrl: value => typeof value === "string" && !value.startsWith("javascript:") ? value : "",
  asText: value => String(value), document: { activeElement: null }, window: { getSelection: () => ({ removeAllRanges() {} }) },
  ACTIVE_STATUSES: new Set(["running", "queued"]), paperSignature: p => p.pdf_layout_translation?.current?.id || "idle",
  localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
  readerTextProtected: () => protectedText, performance: { now: () => 1 },
  api: async (url, options) => { request = { url, options }; return { ...state.reader, blocks: [{ id: "block", text: "stale server text" }], pdf_layout_translation: { status: "paused", current: artifact } }; },
  normalizeDetail: data => data.paper || data, updatePaper: patch => Object.assign(state.reader, patch), renderQueue() {}, renderLibrary() {}, configLabel: () => "mock", statusName: x => x, titleOf: () => "paper", act: fn => fn() };
vm.createContext(context);
for (const file of ["reader.js", "reader-layout.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../static", file), "utf8"), context);
vm.runInContext("globalThis.layout=readerPdfLayout; readerCaptureAnchor=()=>({key:'1',ratio:.3}); readerRestoreAnchor=()=>{}; readerQueuePositionSave=()=>{}; scrollTranslationToPage=()=>{}; readerSaveNotes=async()=>{if(testFail())throw Error('offline');recordSave();}; readerPersistDrafts=()=>true; updateReaderProgress=()=>{}; renderTranslations=()=>{renderReaderPdfLayout();};", context);
context.testFail = () => fail; context.recordSave = () => saved++;
function all(node) { return [node, ...node.childNodes.flatMap(x => typeof x === "object" ? all(x) : [])]; }
function text(node) { return node.textContent ?? node.childNodes.map(x => typeof x === "object" ? text(x) : String(x)).join(" "); }
async function main() {
  assert.equal(context.pdfLayoutOf({}).status, "idle");
  const failedTask = { status: "error", received: 2, done: 0, review_pending: 1, rejected: 1, reused: 0, model_calls: 2, error: "验证未通过" };
  const failedQueue = context.pdfLayoutQueueItem({ pdf_layout_translation: failedTask }), failedText = text(failedQueue);
  assert.match(failedText, /已收到 2 个文字单元/);
  assert.match(failedText, /已交付排版 0 个/);
  assert.match(failedText, /待核对 1 个 · 未通过 1 个/);
  assert.match(failedText, /复用 0 个文字单元 · 本次模型请求 2 次/);
  assert.match(failedText, /译文 PDF 尚未提交/);
  assert.match(failedText, /mock/);
  assert.match(failedText, /验证未通过/);
  assert.doesNotMatch(failedText, /0\s*\/\s*0|%|翻译成功|已翻译/);
  const completedText = text(context.pdfLayoutQueueItem({ pdf_layout_translation: { ...failedTask, status: "completed", done: 2, current: artifact } }));
  assert.match(completedText, /本次译文 PDF 已提交/);
  assert.doesNotMatch(completedText, /尚未提交/);
  assert.match(text(context.pdfLayoutQueueItem({ pdf_layout_translation: { status: "completed", done: 2 } })), /译文 PDF 尚未提交/);
  const legacyText = text(context.pdfLayoutQueueItem({ pdf_layout_translation: { status: "paused", done: 3, total: 0, current: artifact } }));
  assert.match(legacyText, /已处理 3 个文字单元；本次响应计数不可用/);
  assert.match(legacyText, /当前译文 PDF 保留可读，新版本尚未提交/);
  assert.doesNotMatch(legacyText, /已收到|3\s*\/\s*0|%|本次译文 PDF 已提交/);
  const runningQueue = context.pdfLayoutQueueItem({ id: "paper", pdf_layout_translation: { status: "running", received: 0, done: 0 } });
  assert.match(text(runningQueue), /已收到 0 个文字单元/);
  context.readerPdfLayoutReset(state.reader); assert.equal(context.readerPdfLayoutActive(), false);
  context.renderReaderPdfLayout(); assert.equal(root.childNodes.length, 0, "No HTML or empty translation content is displayed before a PDF is submitted.");
  state.reader.pdf_layout_translation = { status: "completed", current: artifact, versions: [artifact] };
  context.readerPdfLayoutReset(state.reader);
  assert.equal(context.readerPdfLayoutActive(), true, "Submitted PDF opens automatically without a stored preference.");
  context.renderReaderPdfLayout();
  assert.equal(all(root).filter(n => n.tag === "img").length, 1);
  assert.ok(all(root).some(n => n.tag === "summary" && n.childNodes.includes("部分内容需要核对 · 查看详情")));
  assert.ok(all(root).filter(n => n.tag === "details").every(n => !n.attrs.open));
  assert.equal(context.readerPdfWarningText("page 2 isolate_formula"), "第 2 页：公式或邻近文字需要对照原 PDF 核对");
  assert.equal(context.readerPdfWarningText("page 3 unknown_diagnostic"), "第 3 页：部分内容或排版需要核对");
  assert.equal(context.readerPdfWarningText("unrecognized upstream warning"), "PDF 处理过程中产生内容核对提示");
  assert.equal(all(root).filter(n => n.tag === "a").length, 2);
  context.readerPdfLayoutCache();
  assert.equal(context.readerReadLocal("pdf-view", "paper").anchor.ratio, .3);
  assert.equal(root.style.values["--reader-layout-page-width"], "460px");
  root.clientWidth = 200; context.readerPdfLayoutResize(); assert.equal(root.style.values["--reader-layout-page-width"], "160px");
  await all(runningQueue).find(n => n.tag === "button" && n.childNodes.includes("暂停翻译")).attrs.handler({});
  assert.equal(request.url, "/api/papers/paper/pdf-pause");
  assert.equal(state.reader.blocks[0].text, "original", "Task responses must not overwrite text edits with a stale full-paper response.");
  assert.equal(state.reader.blocks[0].translation, "retained");
  context.readerWriteLocal("pdf-view", "paper", { value: "text" });
  context.readerWriteLocal("text-action", "paper", { value: "draft" });
  context.readerPdfLayoutReset(state.reader);
  assert.equal(context.readerPdfLayoutActive(), true, "Retired view preferences and preserved drafts cannot bring back the HTML view.");
  assert.equal(context.readerReadLocal("text-action", "paper").value, "draft");
  context.list = value => value || [];
  context.translationOf = value => value.translation || { status: "idle" };
  context.syncJournalCataloguePanel = async () => {};
  nodes.set("#queue-dialog", { open: false }); nodes.set("#detail-dialog", { open: false });
  protectedText = true;
  context.api = async () => ({ papers: [{ ...state.reader, pdf_layout_translation: { ...state.reader.pdf_layout_translation, stage: "翻译正文", current: { id: artifact.id } } }], collections: [] });
  const app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  vm.runInContext(app.slice(app.indexOf("function paperSignature"), app.indexOf("function installEvents")), context);
  await context.poll();
  assert.equal(state.reader.pdf_layout_translation.stage, "翻译正文");
  assert.equal(state.reader.pdf_layout_translation.current.pages.length, 1, "Metadata-only polling cannot replace full artifact pages, even during editing.");
  assert.equal(all(root).filter(n => n.tag === "img").length, 1);
  const index = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  assert.ok(!index.includes("reader-text.js") && index.indexOf("reader-layout.js") < index.indexOf("app.js"));
  const content = element("div"), dialog = element("dialog");
  nodes.set("#translation-content", content); nodes.set("#translation-dialog", dialog);
  context.openDialog = () => { dialog.open = true; }; context.closeDialog = () => { dialog.open = false; };
  context.iconButton = (name, label, handler) => element("button", { handler }, label);
  context.toast = () => {}; context.timeLabel = () => "date";
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/api-translation-ui.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/translation-ui.js"), "utf8"), context);
  let picker, submitted;
  const apiDefault = { provider: "deepseek", base_url: "https://api.deepseek.com", model: "deepseek-flash", max_output_tokens: 393216, thinking: "disabled" };
  context.field = (label, input) => element("label", {}, label, input);
  context.apiTranslationPicker = (settings, initial) => (picker = { root: element("div"), advanced: element("details"), config: { ...apiDefault, ...initial },
    value() { return this.config; }, setConfig(value) { this.config = { ...apiDefault, ...value }; }, disable(value) { this.disabled = value; }, ready: () => true, dispose() {} });
  const requests = [];
  context.api = async url => { requests.push(url); return url === "/api/translation-api" ? { features: { translation: { ...apiDefault, has_key: true } } } : { available: true, engine: "BabelDOC", version: "0.6.4" }; };
  context.pdfLayoutRequest = async (paper, action, body) => { submitted = { action, body }; };
  function start() { const button = all(content).find(n => n.tag === "button" && ["开始翻译", "继续翻译", "生成新版本", "使用 API 重新生成，保留旧草稿"].includes(n.textContent)); return button.attrs.handler({ currentTarget: button }); }
  await context.openPdfLayoutSetup({ id: "paper" }); await start();
  assert.equal(submitted.body.model, "deepseek-flash");
  assert.equal(submitted.body.provider, "deepseek");
  assert.equal(submitted.body.reasoning_effort, undefined, "New API tasks have no Codex reasoning field.");
  assert.deepEqual(requests, ["/api/translation-api", "/api/pdf-layout"], "Opening confirmation never queries the Codex model catalog.");
  const locked = { id: "paper", paper_ai: { model: "locked-model", reasoning_effort: "high" } };
  await context.openPdfLayoutSetup(locked);
  assert.equal(picker.disabled, false); await start();
  assert.equal(submitted.body.model, "deepseek-flash", "Paper chat model does not lock the independent API model.");
  const oldConfig = { model: "old-task", reasoning_effort: "low" };
  await context.openPdfLayoutSetup({ ...locked, pdf_layout_translation: { status: "paused", config: oldConfig } });
  await start(); assert.equal(submitted.body.model, "deepseek-flash"); assert.equal(submitted.body.provider, "deepseek"); assert.equal(submitted.body.retranslate, true, "Legacy task can only restart through API while preserving old drafts.");
  assert.match(text(content), /使用 API 重新生成，保留旧草稿/);
  const checkbox = all(content).find(n => n.tag === "input" && n.attrs.value === "new");
  assert.equal(checkbox.attrs.disabled, true); assert.equal(checkbox.checked, true);
  checkbox.checked = true; checkbox.listeners.change();
  assert.equal(picker.config.model, "deepseek-flash"); assert.equal(picker.disabled, false);
  await start(); assert.equal(submitted.body.model, "deepseek-flash"); assert.equal(submitted.body.provider, "deepseek"); assert.equal(submitted.body.retranslate, true);
  await context.openPdfLayoutSetup({ id: "paper", pdf_layout_translation: { status: "paused", config: oldConfig } });
  const unlockedCheckbox = all(content).find(n => n.tag === "input" && n.attrs.value === "new"); unlockedCheckbox.checked = true; unlockedCheckbox.listeners.change();
  assert.equal(picker.config.model, "deepseek-flash"); assert.equal(picker.disabled, false, "New configurations must use current defaults rather than the old task picker state.");
  const paperWithDrafts = { id: "paper", pdf_layout_translation: { ...failedTask, stage: "验证响应", drafts: [
    { id: "new-draft", received: 4, done: 2, review_pending: 1, rejected: 1 },
    { id: "old-draft", done: 7, total: 0 }
  ] } };
  await context.openPdfLayoutSetup(paperWithDrafts);
  assert.match(text(content), /译文 PDF 尚未提交/);
  assert.doesNotMatch(text(content), /已收到 4 个文字单元/, 'Drafts have an independent management surface.');
  context.openPdfLayoutVersions(paperWithDrafts);
  const setupText = text(content);
  assert.match(setupText, /未完成 · 已收到 4 个文字单元 · 已交付排版 2 个 · 待核对 1 个 · 未通过 1 个/);
  assert.match(setupText, /未完成 · 已处理 7 个文字单元；本次响应计数不可用/);
  assert.doesNotMatch(setupText, /0\s*\/\s*0|7\s*\/\s*0|%/);
  await context.openPdfLayoutSetup({ id: "paper", pdf_layout_translation: { status: "completed", current: artifact } });
  assert.match(text(content), /本次译文 PDF 已提交/);
  console.log("PDF layout reader checks passed: response/delivery counts, submission status, legacy progress, pause, automatic PDF view, preserved drafts, read-only pages, quality warnings, responsive width and task isolation.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
