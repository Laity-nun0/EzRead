"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
let source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
source = source.slice(0, source.lastIndexOf("init().catch("));
function fixture() {
  const calls = [], rendered = [], nodes = new Map();
  const context = vm.createContext({
    state: { settings: {}, loading: true }, DEFAULT_PREFERENCES: {},
    readBrowserSetting: () => null, applyPreferences() {}, loadUsage() { throw Error("Unexpected automatic usage lookup"); }, loadModels: async () => { throw Error("Unexpected automatic model lookup"); },
    renderLibrary() { rendered.push(context.state.loading); }, list: value => value || [], toast() {},
    renderUsagePanels() {}, refreshStatus() { calls.push({ url: "/api/ai/status" }); return Promise.resolve({}); },
    $: selector => { if (!nodes.has(selector)) nodes.set(selector, { open: false, replaceChildren() {} }); return nodes.get(selector); },
    setInterval: () => 1,
    api: (url, options) => {
      calls.push({ url, options });
      if (["/api/selection-session", "/api/status", "/api/models", "/api/usage"].includes(url)) throw Error("Unexpected automatic CLI request");
      return Promise.resolve(url === "/api/papers" ? { papers: [], collections: [] } : {});
    }
  });
  vm.runInContext(source, context);
  vm.runInContext("installEvents = () => {}", context);
  return { context, calls, rendered };
}
(async () => {
  const test = fixture();
  const completed = await Promise.race([
    vm.runInContext("init()", test.context).then(() => true),
    new Promise(resolve => setTimeout(() => resolve(false), 100))
  ]);
  assert.equal(completed, true, "Local library startup must complete without a model connection");
  assert.equal(test.context.state.loading, false);
  assert.ok(test.rendered.includes(false));
  assert.deepEqual(test.calls.map(call => call.url).sort(), ["/api/ai/status", "/api/papers", "/api/settings"]);
  console.log("Startup: local API status/settings/library load without selection warmup or automatic CLI queries.");
})().catch(error => { console.error(error); process.exitCode = 1; });
