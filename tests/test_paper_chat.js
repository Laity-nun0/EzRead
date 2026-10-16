"use strict";

const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const chatModel = { provider: "deepseek", base_url: "https://api.deepseek.com", model: "deepseek-flash", max_output_tokens: 393216,
  thinking: "enabled", reasoning_effort: "high" };
const chatFields = `chat_config:${JSON.stringify(chatModel)},chat_models:[${JSON.stringify(chatModel)}],`;
const source = name => fs.readFileSync(path.join(__dirname, "../static", name), "utf8");

async function streamTests() {
  const encoder = new TextEncoder();
  function fixture(raw, contentType = "application/x-ndjson") {
    const bytes = encoder.encode(raw);
    const stream = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } });
    const context = vm.createContext({ TextDecoder, asText: String, fetch: async () => ({ ok: true, body: stream, headers: { get: () => contentType } }) });
    vm.runInContext(source("core/api.js"), context);
    return context;
  }
  const events = [];
  await fixture('{"type":"reasoning","text":"中文思考"}\n{"type":"answer","text":"回答"}\n{"type":"completed","answer":"回答"}').apiStream("/test", {}, e => events.push(e));
  assert.equal(events[0].text, "中文思考", "UTF-8 and NDJSON boundaries may split at any byte");
  assert.deepEqual(events.map(e => e.type), ["reasoning", "answer", "completed"]);
  await assert.rejects(fixture('{"type":"answer","text":"partial"}\n').apiStream("/test", {}, () => {}), /中断/);
  await assert.rejects(fixture('{"type":"error","error":"引用无效"}\n').apiStream("/test", {}, () => {}), /引用无效/);
  await assert.rejects(fixture("{}", "application/json").apiStream("/test", {}, () => {}), /版本不匹配/);
}

async function pendingTests() {
  const input = { value: "实验结果？" }, calls = [], toasts = [], timers = new Map();
  let resolve, reject, onEvent;
  const context = vm.createContext({
    state: { reader: { id: "paper-a" }, papers: [] },
    $: selector => selector === "#paper-chat-input" ? input : null,
    setInterval: fn => { timers.set(1, fn); return 1; }, clearInterval: id => timers.delete(id),
    Date, toast: text => toasts.push(text), renderLibrary() {},
    apiStream: (url, data, callback) => { calls.push({ url, data }); onEvent = callback; return new Promise((ok, bad) => { resolve = ok; reject = bad; }); },
  });
  vm.runInContext(source("paper-chat.js"), context);
  vm.runInContext(`paperChatRuntime.paperId='paper-a'; paperChatRuntime.data={${chatFields}generation:1,current:{generation:1}};
    renderPaperChat=()=>{}; updatePaperChatProgress=()=>{};
    loadPaperChat=async()=>{throw Error('history refresh failed')};`, context);
  const run = context.sendPaperChat();
  assert.equal(calls.length, 1);
  const snapshot = () => vm.runInContext("paperChatRuntime.pending.get('paper-a')", context);
  assert.equal(snapshot().question, "实验结果？", "Question becomes visible before a network response");
  await context.sendPaperChat(); assert.equal(calls.length, 1, "Duplicate sends are suppressed");
  onEvent({ type: "reasoning", text: "核对原文" });
  onEvent({ type: "answer", text: "partial" });
  context.state.reader = { id: "paper-b" };
  reject(Error("network disconnected")); await run;
  assert.equal(snapshot().failed, true); assert.equal(snapshot().answer, "");
  assert.equal(vm.runInContext("paperChatRuntime.drafts.get('paper-a')", context), "实验结果？");
  assert.equal(timers.size, 0);
  context.state.reader = { id: "paper-a" };
  const retry = context.sendPaperChat();
  onEvent({ type: "completed", answer: "saved answer" }); resolve(); await retry;
  assert.equal(snapshot().completed, true); assert.equal(snapshot().failed, false, "Saved success survives a history refresh error");
  assert.equal(vm.runInContext("paperChatRuntime.drafts.has('paper-a')", context), false, "Do not invite a duplicate retry of an already saved answer");
  assert.equal(timers.size, 0);
  assert.equal(toasts.length, 2);

  for (const failed of [false, true]) {
    vm.runInContext("paperChatRuntime.pending.clear(); paperChatRuntime.drafts.clear()", context);
    const late = context.sendPaperChat();
    vm.runInContext("paperChatRuntime.pending.clear(); paperChatRuntime.drafts.clear()", context);
    onEvent({ type: "completed", generation: 3, answer: "late deleted answer" });
    if (failed) reject(Error("deleted")); else resolve();
    await late;
    assert.equal(vm.runInContext("paperChatRuntime.drafts.has('paper-a')", context), false, "Late failure cannot restore a deleted conversation draft.");
    assert.equal(vm.runInContext("paperChatRuntime.data.generation", context), 1, "Late events cannot change the cleared conversation.");
    assert.equal(timers.size, 0);
  }
}

async function generationTests() {
  for (const emitGeneration of [true, false]) {
    const loaded = [], input = { value: "API question" };
    const context = vm.createContext({
      state: { reader: { id: 'paper-a' }, papers: [] },
      $: selector => selector === '#paper-chat-input' ? input : null,
      setInterval: () => 1, clearInterval() {}, Date, toast() {}, renderLibrary() {},
      apiStream: async (url, body, event) => {
        if (emitGeneration) {
          event({ type: 'generation', generation: 2 });
          assert.equal(vm.runInContext("paperChatRuntime.pending.get('paper-a').generation", context), 2);
          assert.equal(vm.runInContext("paperChatRuntime.data.messages.length", context), 1, 'Generation migration preserves the continuous conversation history.');
          assert.equal(vm.runInContext("paperChatRuntime.data.thread_id", context), null);
        }
        event({ type: 'status', label: '正在生成回答' });
        event({ type: 'completed', generation: 2, answer: 'API answer', thread_id: null });
      }
    });
    vm.runInContext(source('paper-chat.js'), context);
    context.recordLoad = generation => loaded.push(generation);
    vm.runInContext(`paperChatRuntime.paperId='paper-a'; paperChatRuntime.data={${chatFields}generation:1,current:{generation:1,model:'legacy'},thread_id:'old-thread',messages:[{text:'legacy answer'}],generations:[{generation:1,model:'legacy'}]};
      renderPaperChat=()=>{}; updatePaperChatProgress=()=>{}; loadPaperChat=async generation=>recordLoad(generation);`, context);
    await context.sendPaperChat();
    assert.deepEqual(loaded, [2], 'Completion reloads the answer generation even if only completed carries migration.');
    assert.equal(vm.runInContext("paperChatRuntime.pending.get('paper-a').generation", context), 2);
    assert.equal(vm.runInContext("paperChatRuntime.pending.get('paper-a').completed", context), true);
  }
}

async function configTests() {
  const requests = [], input = { value: "未发送草稿" }, toasts = [];
  function element(tag, attrs = {}, ...children) { return { tag, ...attrs, children: children.flat(Infinity).filter(Boolean) }; }
  const context = vm.createContext({
    state: { reader: { id: "paper-a" }, papers: [] }, $: selector => selector === "#paper-chat-input" ? input : null,
    el: element, apiProviderLabel: value => value, toast: text => toasts.push(text),
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
  });
  vm.runInContext(source("paper-chat.js"), context);
  const initial = { generation: 1, current: { generation: 1 }, messages: [{ text: "历史回答" }], chat_config: chatModel, chat_models: [chatModel] };
  context.initial = initial;
  vm.runInContext("paperChatRuntime.paperId='paper-a'; paperChatRuntime.data=initial; renderPaperChat=()=>{}", context);
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.equal(context.paperChatConfigValid(initial), true);
  const controls = context.paperChatControls(initial, false);
  const effortSelect = controls.children[1].children[1];
  const modelSelect = controls.children[0].children[1];
  assert.deepEqual(modelSelect.children.map(option => option.value), ["deepseek-flash", "deepseek-v4-pro"]);
  assert.equal(modelSelect.value, "deepseek-flash");
  assert.equal(effortSelect.value, "high");
  assert.deepEqual(effortSelect.children.map(option => option.value), ["disabled", "low", "high", "max"]);
  assert.deepEqual(plain(context.paperChatConfig("disabled")), { model: "deepseek-flash", thinking: "disabled" });
  for (const effort of ["low", "high", "max"]) {
    assert.deepEqual(plain(context.paperChatConfig(effort)), { model: "deepseek-flash", thinking: "enabled", reasoning_effort: effort });
  }
  const stale = { ...initial, chat_config: { ...chatModel, model: "legacy-model" } };
  assert.equal(context.paperChatConfigValid(stale), false);
  assert.equal(context.paperChatConfigValid({ ...initial, chat_models: [] }), false);
  const unavailableControls = context.paperChatControls({ ...initial, chat_models: [] }, false);
  assert.equal(unavailableControls.children[1].children[1].disabled, true);
  assert.match(unavailableControls.children[2].children[0], /设置 → 问答/);
  assert.equal(context.paperChatControls(initial, true).children[1].children[1].disabled, true);

  const save = context.savePaperChatConfig("low");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.body.reasoning_effort, "low");
  assert.equal(vm.runInContext("paperChatRuntime.saving.has('paper-a')", context), true);
  await context.savePaperChatConfig("disabled");
  assert.equal(requests.length, 1, "Concurrent config changes are suppressed");
  assert.equal(vm.runInContext("paperChatRuntime.drafts.get('paper-a')", context), "未发送草稿");
  requests[0].resolve({ ...initial, chat_config: { ...chatModel, ...context.paperChatConfig("low") } }); await save;
  assert.equal(vm.runInContext("paperChatRuntime.data.messages[0].text", context), "历史回答");
  assert.equal(vm.runInContext("paperChatRuntime.data.chat_config.reasoning_effort", context), "low");
  const failed = context.savePaperChatConfig("disabled");
  requests[1].reject(Error("offline")); await failed;
  assert.equal(vm.runInContext("paperChatRuntime.data.chat_config.reasoning_effort", context), "low", "Failure restores the saved selection");
  assert.equal(toasts.length, 1);

  const late = context.savePaperChatConfig("disabled");
  context.state.reader = { id: "paper-b" };
  vm.runInContext("paperChatRuntime.paperId='paper-b'; paperChatRuntime.data={messages:[{text:'B'}]}; ++paperChatRuntime.request", context);
  requests[2].resolve(initial); await late;
  assert.equal(vm.runInContext("paperChatRuntime.data.messages[0].text", context), "B", "Late save never contaminates another paper");
  context.state.reader = { id: "paper-a" };
  const loadA = context.loadPaperChat(2);
  assert.equal(requests[3].url.endsWith("/ai"), true, "Legacy generation argument no longer forks history");
  context.state.reader = { id: "paper-b" };
  const loadB = context.loadPaperChat();
  requests[4].resolve({ ...initial, messages: [{ text: "B refreshed" }] }); await loadB;
  requests[3].resolve(initial); await loadA;
  assert.equal(vm.runInContext("paperChatRuntime.data.messages[0].text", context), "B refreshed");
  vm.runInContext("paperChatRuntime.data.chat_models=[]", context);
  await context.sendPaperChat();
  assert.equal(requests.length, 5, "Missing valid config suppresses send");

  context.initial = initial;
  vm.runInContext("paperChatRuntime.data=initial", context);
  const supersededSave = context.savePaperChatConfig("disabled");
  vm.runInContext("++paperChatRuntime.request", context);
  requests[5].resolve({ ...initial, chat_config: { ...chatModel, thinking: "disabled" } }); await supersededSave;
  assert.equal(vm.runInContext("paperChatRuntime.data.chat_config.thinking", context), "enabled", "A superseded response cannot overwrite the latest same-paper state");
  await context.savePaperChatConfig("default");
  assert.equal(requests.length, 6, "Unsupported reasoning values are rejected locally");
  const pro = { ...chatModel, model: "deepseek-v4-pro", profile_id: "deepseek_chat" };
  const dual = { ...initial, chat_models: [{ ...chatModel, profile_id: "deepseek_chat" }, pro] };
  context.dual = dual;
  vm.runInContext("paperChatRuntime.data=dual", context);
  const switchPro = context.savePaperChatConfig("high", "deepseek-v4-pro");
  assert.deepEqual(plain(requests[6].options.body), { model: "deepseek-v4-pro", thinking: "enabled", reasoning_effort: "high" });
  requests[6].resolve({ ...dual, chat_config: pro }); await switchPro;
  assert.equal(context.paperChatControls({ ...dual, chat_config: pro }, false).children[0].children[1].value, "deepseek-v4-pro");
  assert.equal(vm.runInContext("paperChatRuntime.data.messages[0].text", context), "历史回答", "Switching Flash to Pro preserves the continuous history");
  assert.equal(context.paperChatConfigValid({ ...dual, chat_ready: false }), false);
  assert.equal(context.paperChatControls({ ...dual, chat_ready: false }, false).children[0].children[1].disabled, true);



}

(async () => { await streamTests(); await pendingTests(); await generationTests(); await configTests(); console.log("Paper chat: streaming, continuous history, Flash/Pro selection, thinking parameters, config recovery and stale-response isolation passed."); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
