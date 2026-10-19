"use strict";

// 使用隔离存储与模拟接口验证标签会话和真实打开/关闭函数，不访问文献库或模型。
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const assert = require("node:assert/strict");
const reader = fs.readFileSync(path.join(__dirname, "../static/reader.js"), "utf8");
const tabs = fs.readFileSync(path.join(__dirname, "../static/reader-tabs.js"), "utf8");
function deferred() { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return { promise, resolve, reject }; }
async function waitRequest(fixture, id) {
  for (let turn = 0; turn < 20 && !fixture.requests.includes(id); turn++) await Promise.resolve();
  assert(fixture.requests.includes(id), `文献 ${id} 的模拟请求应已启动`);
}
function fixture(cached) {
  const storage = new Map(), saves = [], requests = [], dialog = { open: false, closes: 0, close() { this.closes++; this.open = false; } };
  if (cached) storage.set("ezread-reader-tabs:session", JSON.stringify(cached));
  const papers = ["a", "b", "c"].map(id => ({ id, title: id, pages: [{ number: 1 }, { number: 2 }, { number: 3 }], blocks: [], notes: "", reader_state: {} }));
  const state = { papers, settings: {}, reader: null };
  const content = { inert: false }, pending = new Map();
  let failSave = false, failStorage = false, flush = async () => {};
  const context = { state, console, readSaveTimer: null,
    $: selector => selector === "#reader-dialog" ? dialog : selector === "#reader-content" ? content : null,
    $$: () => [], requestAnimationFrame() {}, clearTimeout() {},
    localStorage: { get length() { return storage.size; }, key: index => [...storage.keys()][index], getItem: key => storage.get(key), setItem: (key, value) => { if (failStorage) throw Error("quota"); storage.set(key, value); } },
    normalizeDetail: value => value, api: async url => {
      const id = url.split("/").at(-1);
      requests.push(id);
      return pending.has(id) ? pending.get(id).promise : { ...papers.find(p => p.id === id) };
    },
    flushDetailNotes: (...args) => flush(...args), openDialog: () => { dialog.open = true; }, toast() {},
  };
  vm.createContext(context);
  vm.runInContext(reader + "\n" + tabs + "\nglobalThis.R = readerRuntime; globalThis.T = readerTabs;", context);
  context.renderReader = () => {};
  context.readerRememberLibrary = () => ({});
  context.readerDispose = () => {};
  context.readerCachePosition = () => true;
  context.readerPersistDrafts = () => !failStorage;
  context.readerSaveNotes = async () => {
    saves.push({ id: context.R.notes.id, notes: context.R.notes.value });
    if (failSave) throw Error("offline");
  };
  context.saveReadPage = async () => {
    const value = { page: state.readerPage, zoom: state.zoom };
    saves.push({ id: state.reader.id, ...value });
    context.readerWriteLocal("position", state.reader.id, { pending: true, position: value });
    if (context.R.notes.value !== context.R.notes.savedValue) context.readerWriteLocal("notes", state.reader.id, { value: context.R.notes.value });
  };
  return { context, state, storage, saves, requests, dialog, pending,
    setFailSave: value => { failSave = value; }, setFailStorage: value => { failStorage = value; },
    setFlush: value => { flush = value; } };
}
async function main() {
  const f = fixture(), { context: c, state, storage, saves, dialog } = f;
  await c.readerTabsRestore();
  await c.openReader("a"); state.readerPage = 2; state.zoom = 125; c.R.notes.value = "A 的笔记";
  await c.openReader("b"); state.readerPage = 3; state.zoom = 75; c.R.notes.value = "B 的笔记";
  await c.openReader("a");
  assert.equal(dialog.closes, 0, "切换文献不能关闭再打开阅读器外层");
  assert.deepEqual([...c.T.ids], ["a", "b"]);
  assert.equal(state.readerPage, 2); assert.equal(state.zoom, 125); assert.equal(c.R.notes.value, "A 的笔记");
  assert(saves.some(value => value.id === "b" && value.page === 3 && value.zoom === 75));
  const same = state.reader; await c.openReader("a"); assert.equal(state.reader, same);
  await c.closeReader(); assert.equal(dialog.open, false); assert.equal(c.T.active, null);
  assert.deepEqual([...c.T.ids], ["a", "b"]);
  await c.openReader("b"); assert.equal(state.readerPage, 3); assert.equal(c.R.notes.value, "B 的笔记");
  await c.readerTabsClose("a"); assert.equal(c.T.active, "b");
  await c.openReader("c"); await c.readerTabsClose("c"); assert.equal(c.T.active, "b");
  assert.equal(dialog.closes, 1, "关闭活跃标签后切到相邻文献也应保持阅读器打开");
  assert.deepEqual(JSON.parse(storage.get("ezread-reader-tabs:session")), { ids: ["b"], active: "b" });
  const restart = fixture({ ids: ["b", "missing", "a", "b", 42], active: "b" });
  await restart.context.readerTabsRestore();
  assert.deepEqual([...restart.context.T.ids], ["b", "a"]); assert.equal(restart.state.reader.id, "b");
  restart.context.readerTabsDeleted("b"); assert.deepEqual([...restart.context.T.ids], ["a"]); assert.equal(restart.context.T.active, null);
  const libraryRestart = fixture({ ids: ["a", "b"], active: null });
  await libraryRestart.context.readerTabsRestore(); assert.equal(libraryRestart.dialog.open, false);

  // 旧恢复请求迟到失败，不得清掉用户已经打开的新标签。
  const restoreRace = fixture({ ids: ["a"], active: "a" }), restoreFailure = deferred();
  restoreRace.pending.set("a", restoreFailure);
  const restoring = restoreRace.context.readerTabsRestore();
  await waitRequest(restoreRace, "a");
  await restoreRace.context.openReader("b");
  restoreFailure.reject(Error("旧恢复请求失败")); await restoring;
  assert.equal(restoreRace.context.T.active, "b"); assert.equal(restoreRace.state.reader.id, "b");
  assert.equal(JSON.parse(restoreRace.storage.get("ezread-reader-tabs:session")).active, "b");

  // 删除无关后台文献不取消新打开；删除正在加载的文献仍须取消。
  const deleteRace = fixture(), openingAfterDelete = deferred(), dr = deleteRace.context;
  await dr.openReader("b"); await dr.closeReader();
  deleteRace.pending.set("a", openingAfterDelete);
  const keptOpening = dr.openReader("a"); await waitRequest(deleteRace, "a");
  dr.readerTabsDeleted("b"); openingAfterDelete.resolve(deleteRace.state.papers[0]); await keptOpening;
  assert.equal(deleteRace.state.reader.id, "a"); assert.equal(dr.T.active, "a"); assert(!dr.T.ids.includes("b"));
  await dr.closeReader();
  deleteRace.requests.length = 0;
  const deletedOpening = deferred(); deleteRace.pending.set("a", deletedOpening);
  const removedOpening = dr.openReader("a"); await waitRequest(deleteRace, "a");
  dr.readerTabsDeleted("a"); deletedOpening.resolve(deleteRace.state.papers[0]); await removedOpening;
  assert.equal(deleteRace.dialog.open, false); assert(!dr.T.ids.includes("a")); assert.equal(dr.R.openingId, null);

  // 后发打开、返回文献库与关闭保存都能使过期请求失效。
  const race = fixture(), r = race.context;
  const slow = deferred(); race.pending.set("a", slow);
  const old = r.openReader("a"); await Promise.resolve(); await Promise.resolve();
  await r.openReader("b"); slow.resolve(race.state.papers[0]); await old;
  assert.equal(race.state.reader.id, "b"); assert.deepEqual([...r.T.ids], ["b"]);
  await r.closeReader();
  const slowAgain = deferred(); race.pending.set("a", slowAgain);
  const cancelled = r.openReader("a"); await Promise.resolve(); await r.closeReader();
  slowAgain.resolve(race.state.papers[0]); await cancelled; assert.equal(race.dialog.open, false);
  const flush = deferred(); race.setFlush(() => flush.promise);
  const firstFlush = r.openReader("a"); const secondFlush = r.openReader("c");
  flush.resolve(); await Promise.all([firstFlush, secondFlush]); assert.equal(race.state.reader.id, "c");

  const retained = fixture(), keep = retained.context;
  await keep.openReader("a");
  const missing = deferred(); retained.pending.set("b", missing);
  const failure = assert.rejects(keep.openReader("b"), /missing paper/);
  await waitRequest(retained, "b");
  assert(retained.dialog.open && retained.state.reader.id === "a", "等待目标加载时保留当前文献");
  assert.equal(keep.$("#reader-content").inert, true, "等待目标加载期间不能新增尚未保存的旧篇编辑");
  missing.reject(Error("missing paper")); await failure;
  assert(retained.dialog.open && retained.state.reader.id === "a" && keep.T.active === "a");
  assert.equal(retained.dialog.closes, 0, "目标加载失败不能闪回文献库");
  assert.equal(keep.$("#reader-content").inert, false, "加载失败后恢复当前篇编辑");

  const cancelledLoad = deferred(); retained.pending.set("b", cancelledLoad);
  const staleLoad = keep.openReader("b"); await waitRequest(retained, "b");
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  await keep.openReader("a");
  assert.equal(keep.$("#reader-content").inert, false, "点击当前标签取消切换后恢复编辑");
  cancelledLoad.resolve(retained.state.papers[1]); await staleLoad;
  assert.equal(retained.state.reader.id, "a");

  keep.T.ids.push("b");
  const closedTarget = deferred(); retained.pending.set("b", closedTarget);
  const abandoned = keep.openReader("b");
  for (let turn = 0; turn < 20 && !keep.$("#reader-content").inert; turn++) await Promise.resolve();
  assert.equal(keep.$("#reader-content").inert, true);
  await keep.readerTabsClose("b");
  assert.equal(keep.$("#reader-content").inert, false, "关闭正在加载的后台标签后恢复当前篇编辑");
  closedTarget.resolve(retained.state.papers[1]); await abandoned;
  assert.equal(retained.state.reader.id, "a");

  const returnDuringSave = fixture(), back = returnDuringSave.context;
  await back.openReader("a");
  const delayedSave = deferred(); back.readerSaveNotes = () => delayedSave.promise;
  const switching = back.openReader("b"), returning = back.closeReader();
  delayedSave.resolve(); await Promise.all([switching, returning]);
  assert.equal(returnDuringSave.dialog.open, false, "切换保存期间点击文献库仍应真正关闭阅读器");
  assert.equal(back.T.active, null);

  const closingRace = fixture(), cr = closingRace.context;
  await cr.openReader("b"); await cr.openReader("a");
  const save = deferred(); cr.readerSaveNotes = () => save.promise;
  const closeTab = cr.readerTabsClose("a"), newerOpen = cr.openReader("c");
  save.resolve(); await Promise.all([closeTab, newerOpen]);
  assert.equal(closingRace.state.reader.id, "c", "关闭标签后的相邻标签不能覆盖后发打开");
  await cr.closeReader();
  const reopen = deferred(); closingRace.pending.set("b", reopen);
  const closedPending = cr.openReader("b"); await Promise.resolve();
  await cr.readerTabsClose("b"); reopen.resolve(closingRace.state.papers[1]); await closedPending;
  assert.equal(closingRace.dialog.open, false); assert(!cr.T.ids.includes("b"));

  const blocked = fixture(); await blocked.context.openReader("a");
  blocked.setFailSave(true); blocked.setFailStorage(true);
  assert.equal(await blocked.context.openReader("b"), false);
  assert.equal(blocked.state.reader.id, "a"); assert.equal(blocked.dialog.open, true); assert.equal(blocked.context.T.active, "a");
  blocked.context.act = action => action();
  blocked.context.activeFilterTrigger = null;
  blocked.state.view = "all";
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/library/filters.js"), "utf8"), blocked.context);
  await blocked.context.setView("favorites");
  assert.equal(blocked.state.view, "all", "笔记和草稿都保存失败时，侧栏导航不能离开当前阅读器");
  assert.equal(blocked.dialog.open, true);
  blocked.setFailStorage(false); await blocked.context.openReader("b"); assert.equal(blocked.state.reader.id, "b");
  await blocked.context.readerTabsClose("b"); await blocked.context.readerTabsClose("a");
  assert.equal(blocked.dialog.open, false); assert.deepEqual([...blocked.context.T.ids], []);
  const desktop = fs.readFileSync(path.join(__dirname, "../static/desktop.js"), "utf8");
  vm.runInContext(desktop, c);
  const snapshot = c.ezreadDesktopDraftSnapshot();
  assert.equal(snapshot["ezread-reader-tabs:session"], JSON.stringify({ ids: ["b"], active: "b" }));
  await c.closeReader(); f.setFailStorage(true);
  assert.equal(c.ezreadDesktopDraftSnapshot(), null, "文献库当前激活时也必须阻止未保存会话的原生关闭");
  f.setFailStorage(false);
  const dialogs = new Map(["#reader-dialog", "#settings-dialog", "#queue-dialog"].map(id => [id, {
    open: false, method: null, show() { this.open = true; this.method = "show"; }, showModal() { this.open = true; this.method = "showModal"; }
  }]));
  const apiContext = vm.createContext({ $: selector => dialogs.get(selector) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/core/api.js"), "utf8"), apiContext);
  for (const id of dialogs.keys()) apiContext.openDialog(id);
  assert.equal(dialogs.get("#reader-dialog").method, "show", "阅读器应保留侧栏交互");
  for (const id of ["#settings-dialog", "#queue-dialog"]) assert.equal(dialogs.get(id).method, "showModal");
  console.log("Reader tabs passed: unique ordered tabs, independent reading state, safe switch/close, restore, deletion filtering and stale-open cancellation.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
