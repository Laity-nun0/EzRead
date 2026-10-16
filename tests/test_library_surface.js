"use strict";

const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
class Node {
  constructor(tag = "div", text = "") { this.tag = tag; this.text = text; this.children = []; this.dataset = {}; this.attrs = {}; this.style = { setProperty() {} }; this.listeners = {}; this.value = ""; }
  get classList() { return { remove: value => { this.className = (this.className || "").split(" ").filter(name => name !== value).join(" "); }, add: value => { this.className = `${this.className || ""} ${value}`; }, toggle: (value, active) => { const names = new Set((this.className || "").split(" ")); if (active) names.add(value); else names.delete(value); this.className = [...names].join(" "); }, contains: value => (this.className || "").split(" ").includes(value) }; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key]; }
  addEventListener(key, action) { this.listeners[key] = action; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]).filter(child => selector.startsWith(".") ? child.classList.contains(selector.slice(1)) : child.tag === selector); }
  focus() {}
  getBoundingClientRect() { return { left: 0, right: 30, bottom: 30, width: 100, height: 100 }; }
}
const nodes = new Map(), storage = new Map(), writes = [], opened = [];
const document = { createElement: tag => new Node(tag), createTextNode: text => new Node("text", text), createElementNS: (_, tag) => new Node(tag), documentElement: new Node("html"), hidden: false,
  querySelector: selector => { if (!nodes.has(selector)) nodes.set(selector, new Node()); return nodes.get(selector); }, querySelectorAll: () => [], addEventListener() {} };
const context = vm.createContext({ Node, document, console, setTimeout, clearTimeout, window: { addEventListener() {} }, innerWidth: 1000, innerHeight: 800,
  localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
  imageNode: (url, attrs) => { const node = new Node("img"); node.attrs = { src: url, ...attrs }; return node; }, placeholderImage: () => new Node("svg"),
  rankAppearance: () => ({ type: "journal", tier: "top" }), journalOf: paper => paper.journal, titleOf: paper => paper.title,
  closeFilterMenu() {}, cardModelConfig: () => ({model: "fixture-model", provider: "openai"}), apiProviderLabel: () => "OpenAI", cardTranslationLabel: () => "保留版式翻译",
  installToastLayer() {}, installSelectMenus() {}, positionFilterMenu() {}, openReader: async id => opened.push(id), act: action => action(),
  api: async (url, options) => { writes.push({ url, options }); return options.body; }, renderLibrary() {},
  dateMs: value => value ? new Date(value).getTime() || 0 : 0,
});
function load(file) { vm.runInContext(fs.readFileSync(path.join(__dirname, "../static", file), "utf8"), context); }
for (const file of ["core/state.js", "core/dom.js", "core/storage.js", "core/preferences.js", "library/actions.js", "library/cards.js"]) load(file);
const paper = { id: "read", title: "Original English title", title_zh: "译名", journal: "Nature", year: 2025, tags: ["alpha", "beta", "gamma"], cover_url: "/cover.jpg", last_read: "2026-10-15T08:00:00Z", reader_state: { page: 4 }, page_count: 12 };
context.paper = paper;
const card = vm.runInContext("renderCard(paper)", context);
assert.equal(card.children[0].className, "card-content", "Cover precedes metadata and title");
assert.equal(card.querySelector(".card-cover").querySelector("img").attrs.src, paper.cover_url);
assert.equal(card.querySelector(".card-title").textContent, paper.title);
assert.deepEqual(Array.from(card.querySelectorAll(".card-tag"), tag => tag.textContent), ["alpha", "beta"]);
assert.equal(card.querySelectorAll(".card-tier").length, 1);
assert.ok(card.querySelector(".card-more"));
assert.equal(card.querySelector(".card-controls"), null, "Secondary actions live in the menu");
assert.equal(card.querySelector(".card-delete"), null);
assert.equal(card.attrs.draggable, "true");
vm.runInContext("openCardMenu(paper, 20, 20)", context);
assert.deepEqual(Array.from(nodes.get("#card-context-menu").querySelectorAll(".context-menu-label"), node => node.textContent),
  ["论文简介", "保留版式翻译", "OpenAI · fixture-model", "论文对话", "多选", "复制 PDF", "在文件夹显示", "永久删除"]);

vm.runInContext("state.loading = false; state.papers = [paper, { id: 'unread', title: 'Newly imported', created_at: '2027-01-01', last_read: 'invalid' }, { ...paper, id: 'older', last_read: '2026-10-14' }]; renderRecentReading();", context);
const recent = nodes.get("#recent-reading");
assert.equal(recent.classList.contains("hidden"), false);
assert.equal(recent.querySelector(".recent-title").textContent, paper.title);
assert.equal(recent.querySelector(".recent-source").textContent, "Nature · 2025");
assert.equal(recent.querySelector(".recent-eyebrow").querySelector("use").attrs.href, "#i-clock");
assert.equal(recent.querySelector(".recent-position").textContent, "第 4 页 / 共 12 页");
recent.querySelector("button").listeners.click({ currentTarget: new Node("button") });
assert.deepEqual(opened, ["read"]);
for (const setup of ["state.query = 'query'", "state.query = ''; state.selectionMode = true", "state.selectionMode = false; state.view = 'favorites'", "state.view = 'all'; state.papers = [{...paper, last_read: null}]"]) {
  vm.runInContext(`${setup}; renderRecentReading();`, context);
  assert.equal(recent.classList.contains("hidden"), true);
}
vm.runInContext("state.papers = [{...paper, reader_state: {}, read_page: undefined, page_count: undefined}]; renderRecentReading();", context);
assert.equal(recent.querySelector(".recent-position"), null, "Missing progress must not invent page numbers");
vm.runInContext("state.papers = [{...paper, year: 'unknown'}]; renderRecentReading();", context);
assert.equal(recent.querySelector(".recent-source").textContent, "Nature", "Invalid year is omitted");

let app = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
vm.runInContext(app.slice(0, app.lastIndexOf("init().catch(")), context);
vm.runInContext("installEvents(); state.settings.theme = 'sage'; applyPreferences();", context);
const toggle = nodes.get("#theme-toggle");
assert.equal(document.documentElement.dataset.theme, "paper");
assert.equal(toggle.querySelector("use").attrs.href, "#i-moon");
(async () => {
  toggle.listeners.click(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(document.documentElement.dataset.theme, "graphite");
  assert.equal(toggle.querySelector("use").attrs.href, "#i-sun");
  assert.equal(JSON.parse(storage.get("ezread-preferences")).theme, "graphite");
  assert.equal(writes.at(-1).options.body.theme, "graphite");
  toggle.listeners.click(); await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(document.documentElement.dataset.theme, "paper");
  assert.equal(JSON.parse(storage.get("ezread-preferences")).theme, "paper");
  assert.equal(writes.at(-1).options.body.theme, "paper");
  Object.assign(context, { defaultTranslationPanel: () => new Node(), journalCataloguePanel: () => new Node(), apiStatusPanel: () => new Node(), usagePanel: () => new Node(), apiTranslationSettingsPanel: () => new Node(),
    openDialog: () => {}, requestAnimationFrame: () => 1, ResizeObserver: class { observe() {} disconnect() {} } });
  load("settings-ui.js");
  await vm.runInContext("openSettings()", context);
  const settings = nodes.get("#settings-content");
  assert.equal(settings.querySelector(".theme-grid"), null, "The sidebar icon is the only theme control");
  assert.deepEqual(Array.from(settings.querySelectorAll("input"), input => input.dataset.preference), ["ui_font_size", "reader_font_size"]);
  assert.equal(settings.querySelector(".preference-status").querySelector("p").attrs.role, "status", "Automatic saving status stays available");
  const sort = nodes.get("#library-sort"); sort.value = "year:asc";
  // Avoid masonry work: this assertion exercises the installed selector handler.
  vm.runInContext("renderLibrary = () => {}", context);
  sort.listeners.change({ target: sort });
  assert.equal(nodes.get("#sort").value, "year");
  assert.equal(nodes.get("#sort-direction").value, "asc");
  assert.equal(storage.get("ezread-sort"), "year");
  assert.equal(storage.get("ezread-sort-direction"), "asc");
  console.log("Library surface: real card DOM, latest read, missing progress, theme click persistence and sort selector passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
