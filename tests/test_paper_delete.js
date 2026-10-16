"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  const entries = new Map([
    ["ezread-reader-notes:paper-a", "notes"], ["ezread-reader-position:paper-a", "position"],
    ["ezread-reader-pdf-view:paper-a", "view"], ["ezread-reader-notes:paper-b", "sibling"],
    ["ezread-preferences", "preferences"],
  ]);
  const calls = [];
  const context = {
    localStorage: { get length() { return entries.size; }, key: index => [...entries.keys()][index], removeItem: key => entries.delete(key) },
    state: { selectedPapers: new Set(["paper-a", "paper-b"]), detail: { id: "paper-a" } },
    paperChatRuntime: { drafts: new Map([["paper-a", "question"], ["paper-b", "keep"]]), pending: new Map(), paperId: "paper-a", data: {} },
    flushDetailNotes: async id => { calls.push(`flush:${id}`); },
    api: async () => { calls.push("delete"); }, closeDialog: () => {}, toast: () => {}, clearTimeout, setTimeout,
  };
  vm.createContext(context);
  for (const filename of ["static/core/storage.js", "static/library/actions.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", filename), "utf8"), context);
  }
  await context.permanentlyDeletePaper("paper-a");
  assert.deepEqual(calls, ["flush:paper-a", "delete"]);
  assert.equal([...entries.keys()].some(key => key.endsWith(":paper-a")), false);
  assert.equal(entries.get("ezread-reader-notes:paper-b"), "sibling");
  assert.equal(entries.get("ezread-preferences"), "preferences");
  assert.equal(context.state.detail, null);
  assert.equal(context.paperChatRuntime.drafts.has("paper-a"), false);
  assert.equal(context.paperChatRuntime.drafts.get("paper-b"), "keep");
  context.api = async () => { throw Error("文件占用"); };
  await assert.rejects(context.permanentlyDeletePaper("paper-b"), /文件占用/);
  assert.equal(entries.get("ezread-reader-notes:paper-b"), "sibling");
  assert.equal(context.state.selectedPapers.has("paper-b"), true);
  const old = "paper-c", closed = [];
  entries.set(`ezread-reader-notes:${old}`, "orphan notes");
  context.state.selectedPapers.add(old); context.state.reader = { id: old }; context.state.detail = { id: old };
  const readerNoteEntry = { id: old, timer: null };
  context.readerRuntime = { notes: readerNoteEntry };
  const noteEntry = { id: old, timer: null };
  context.detailNoteSaves = new Map([[old, noteEntry]]);
  context.readerDispose = () => {};
  context.paperChatRuntime.drafts.set(old, "orphan question");
  context.closeDialog = selector => {
    if (selector === "#reader-dialog") { assert.equal(context.state.reader, null); assert.equal(context.readerRuntime.notes, null); }
    closed.push(selector);
  };
  context.FormData = class { append() {} };
  context.$ = () => ({ classList: { add() {}, remove() {} }, value: "" });
  context.loadLibrary = async () => {};
  context.setView = () => {};
  context.api = async () => ({ papers: [{ id: "fresh-paper", replaced_paper_id: old }] });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../static/library/import.js"), "utf8"), context);
  await context.importFiles([{ name: "fixture.pdf" }]);
  assert.equal(entries.has(`ezread-reader-notes:${old}`), false);
  assert.equal(entries.get("ezread-reader-notes:paper-b"), "sibling");
  assert.equal(context.state.selectedPapers.has(old), false);
  assert.equal(context.detailNoteSaves.has(old), false);
  assert.equal(noteEntry.released, true);
  assert.equal(readerNoteEntry.released, true);
  assert.equal(context.paperChatRuntime.drafts.has(old), false);
  assert.ok(closed.includes("#reader-dialog"));
  console.log("永久删除前端草稿清理与失败保留检查通过");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
