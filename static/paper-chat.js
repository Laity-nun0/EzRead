"use strict";

const paperChatRuntime = { paperId: null, data: null, pending: new Map(), drafts: new Map(), request: 0, saving: new Map(),
  width: null, widthLoaded: false, resizeObserver: null };

function paperChatApplyWidth(width = null, persist = false) {
  const pane = $("#reader-paper-chat"), workspace = pane?.parentElement;
  if (!pane || !workspace) return;
  if (!paperChatRuntime.widthLoaded) {
    const saved = readBrowserSetting("chatWidth");
    paperChatRuntime.width = saved === null ? null : Number(saved);
    paperChatRuntime.widthLoaded = true;
  }
  if (getComputedStyle(workspace).flexDirection !== "row") { pane.style.removeProperty("--paper-chat-width"); return; }
  const total = workspace.getBoundingClientRect().width, rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const min = Math.min(19 * rem, total * .5), max = Math.max(min, total - Math.min(26 * rem, total * .45));
  const requested = width ?? paperChatRuntime.width;
  const actual = Math.max(min, Math.min(max, requested ?? pane.getBoundingClientRect().width));
  if (requested !== null) pane.style.setProperty("--paper-chat-width", `${actual}px`);
  const handle = $("#paper-chat-resize");
  if (handle) for (const [name, value] of [["min", min], ["max", max], ["now", actual]]) handle.setAttribute(`aria-value${name}`, String(Math.round(value)));
  if (width !== null) paperChatRuntime.width = actual;
  if (persist && !writeBrowserSetting("chatWidth", String(actual))) toast("对话宽度暂未保存，当前仍可调整", "error");
}

function paperChatResizeHandle() {
  const handle = el("div", { id: "paper-chat-resize", class: "paper-chat-resize", role: "separator", tabindex: "0",
    "aria-orientation": "vertical", "aria-label": "拖动调整论文对话宽度", "aria-controls": "reader-paper-chat" }, el("span", { "aria-hidden": "true" }));
  let dragging = false, startX = 0, startWidth = 0;
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault(); dragging = true; startX = event.clientX;
    startWidth = $("#reader-paper-chat").getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId); handle.classList.add("dragging"); handle.focus({ preventScroll: true });
  });
  handle.addEventListener("pointermove", event => { if (dragging) paperChatApplyWidth(startWidth + startX - event.clientX); });
  const finish = event => {
    if (!dragging) return;
    dragging = false; handle.classList.remove("dragging");
    if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    paperChatApplyWidth(paperChatRuntime.width, true);
  };
  for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) handle.addEventListener(event, finish);
  handle.addEventListener("keydown", event => {
    const width = $("#reader-paper-chat").getBoundingClientRect().width, step = event.shiftKey ? 80 : 24;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    paperChatApplyWidth(event.key === "Home" ? Number(handle.getAttribute("aria-valuemin")) : event.key === "End" ? Number(handle.getAttribute("aria-valuemax")) : width + (event.key === "ArrowLeft" ? step : -step), true);
  });
  return handle;
}

function paperChatReasoning(text, active = false) {
  return el("details", { class: "paper-chat-reasoning", open: active },
    el("summary", {}, active ? "思考过程" : "查看思考过程"), paperChatMarkdown(text));
}

function paperChatMessage(message) {
  if (message.kind === "reasoning") return paperChatReasoning(message.text);
  const node = el("div", { class: `paper-chat-message role-${message.role}` },
    message.kind === "summary" ? el("div", { class: "paper-chat-role" }, "研究速览") : null,
    el("div", { class: "paper-chat-text" }, message.role === "user" ? message.text : paperChatMarkdown(message.text)));
  if (message.citations?.length) node.append(el("div", { class: "paper-chat-citations" },
    message.citations.map(cite => button(`第 ${cite.page} 页 · ${cite.block_id}`, () => {
      if (state.reader?.id !== paperChatRuntime.paperId) return;
      setReaderPage(cite.page, true); selectBlock(cite.block_id, true);
    }, "secondary"))));
  return node;
}

function paperChatPending(pending) {
  const reasoning = paperChatReasoning(pending.reasoning || "", true);
  reasoning.id = "paper-chat-reasoning-live"; reasoning.hidden = !pending.reasoning;
  return el("div", { class: "paper-chat-live" },
    el("div", { class: "paper-chat-status", role: "status", "aria-live": "polite" },
      el("span", { class: "paper-chat-spinner", "aria-hidden": "true", hidden: pending.failed || pending.completed }),
      el("span", { id: "paper-chat-status-label" }, pending.label),
      el("span", { id: "paper-chat-elapsed", class: "paper-chat-elapsed", "aria-hidden": "true" })),
    reasoning, el("div", { id: "paper-chat-answer-live", class: "paper-chat-text", hidden: !pending.answer },
      paperChatMarkdown(pending.answer || "")));
}

function paperChatModel(data) {
  const config = data?.chat_config;
  return (data?.chat_models || []).find(model => model.provider === "deepseek" && ["deepseek-flash", "deepseek-v4-pro"].includes(model.model)
    && config && ["provider", "base_url", "model"].every(key => model[key] === config[key]));
}

function paperChatEffort(config) {
  return config?.thinking === "disabled" ? "disabled" : config?.reasoning_effort || "high";
}

function paperChatConfig(effort, model = "deepseek-flash") {
  return effort === "disabled" ? { model, thinking: "disabled" } : { model, thinking: "enabled", reasoning_effort: effort };
}

function paperChatConfigValid(data) {
  return data?.chat_ready !== false && !!paperChatModel(data) && ["disabled", "low", "high", "max"].includes(paperChatEffort(data.chat_config));
}

async function savePaperChatConfig(effort, model = paperChatRuntime.data?.chat_config?.model) {
  const id = state.reader?.id, data = paperChatRuntime.data;
  const pending = paperChatRuntime.pending.get(id);
  if (!id || paperChatRuntime.paperId !== id || paperChatRuntime.saving.has(id) || pending && !pending.failed && !pending.completed) return;
  if (!paperChatConfigValid(data) || !["deepseek-flash", "deepseek-v4-pro"].includes(model) || !["disabled", "low", "high", "max"].includes(effort)) return;
  const request = ++paperChatRuntime.request;
  paperChatRuntime.saving.set(id, request);
  const input = $("#paper-chat-input");
  if (input) paperChatRuntime.drafts.set(id, input.value);
  renderPaperChat();
  try {
    const result = await api(`/api/papers/${encodeURIComponent(id)}/ai/chat-model`, { method: "POST", body: paperChatConfig(effort, model) });
    if (state.reader?.id !== id || paperChatRuntime.paperId !== id || paperChatRuntime.request !== request) return;
    paperChatRuntime.data = result;
  } catch (error) {
    if (state.reader?.id === id && paperChatRuntime.paperId === id && paperChatRuntime.request === request) toast(`问答配置未保存：${error.message}`, "error");
  } finally {
    if (paperChatRuntime.saving.get(id) === request) paperChatRuntime.saving.delete(id);
    if (state.reader?.id === id && paperChatRuntime.paperId === id) renderPaperChat();
  }
}

function paperChatControls(data, busy) {
  const available = paperChatConfigValid(data);
  const model = el("select", { id: "paper-chat-model", "aria-label": "问答模型", disabled: busy || !available,
    onchange: event => void savePaperChatConfig(paperChatEffort(data.chat_config), event.target.value) },
  [["deepseek-flash", "DeepSeek Flash"], ["deepseek-v4-pro", "DeepSeek Pro"]].map(([value, label]) => el("option", { value }, label)));
  model.value = data.chat_config?.model || "deepseek-flash";
  const reasoning = el("select", { id: "paper-chat-effort", "aria-label": "问答思考强度", disabled: busy || !available,
    onchange: event => void savePaperChatConfig(event.target.value) },
  [["disabled", "关闭思考"], ["low", "低"], ["high", "高"], ["max", "最高"]].map(([value, label]) => el("option", { value }, label)));
  reasoning.value = paperChatEffort(data.chat_config);
  return el("div", { class: "paper-chat-controls" },
    el("label", { class: "paper-chat-model-field" }, el("span", {}, "问答模型"), model),
    el("label", { class: "paper-chat-effort-field" }, el("span", {}, "思考强度"), reasoning),
    !available ? el("small", { class: "paper-chat-empty" }, "请在设置 → 问答中保存 API Key") : null);
}

function renderPaperChat() {
  const pane = $("#reader-paper-chat"), data = paperChatRuntime.data, id = paperChatRuntime.paperId;
  if (!pane || !data) return;
  const pending = paperChatRuntime.pending.get(id), visiblePending = pending;
  const busy = paperChatRuntime.saving.has(id) || pending && !pending.failed && !pending.completed;
  const previous = $("#paper-chat-history");
  const scroll = previous?.scrollTop || 0, follow = !previous || previous.scrollHeight - previous.scrollTop - previous.clientHeight < 60;
  const history = el("div", { class: "paper-chat-history", id: "paper-chat-history", "aria-label": "论文对话消息" }, data.messages.map(paperChatMessage));
  if (visiblePending) history.append(paperChatMessage({ role: "user", text: visiblePending.question }), paperChatPending(visiblePending));
  if (!data.messages.length && !visiblePending) history.append(el("p", { class: "paper-chat-empty" }, "向论文提问，回答将附论文出处"));
  const input = el("textarea", { id: "paper-chat-input", rows: "3", maxlength: "4000", placeholder: "向论文提问",
    value: paperChatRuntime.drafts.get(id) || "", disabled: busy, "aria-label": "向论文提问" });
  input.addEventListener("input", () => paperChatRuntime.drafts.set(id, input.value));
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); void sendPaperChat(); }
  });
  pane.replaceChildren(paperChatResizeHandle(), el("div", { class: "paper-chat-heading" }, el("strong", {}, "论文对话"),
    iconButton("close", "收起论文对话", () => closePaperChat())), paperChatControls(data, busy), history,
    el("div", { class: "paper-chat-compose" }, input,
      el("div", { class: "paper-chat-compose-footer" }, el("small", {}, "Enter 发送 · Shift+Enter 换行"),
        el("button", { type: "button", class: "button primary paper-chat-send", id: "paper-chat-send",
          disabled: busy || !paperChatConfigValid(data), "aria-label": busy ? "正在回答" : "发送", onclick: () => void sendPaperChat() },
          busy ? el("span", { class: "paper-chat-spinner", "aria-hidden": "true" }) : icon("arrow")))));
  history.scrollTop = follow ? history.scrollHeight : scroll;
  paperChatApplyWidth();
  paperChatRuntime.resizeObserver?.disconnect();
  paperChatRuntime.resizeObserver = new ResizeObserver(() => paperChatApplyWidth());
  paperChatRuntime.resizeObserver.observe(pane.parentElement);
  updatePaperChatProgress(id);
}

function updatePaperChatProgress(id) {
  const pending = paperChatRuntime.pending.get(id), data = paperChatRuntime.data;
  if (!pending || paperChatRuntime.paperId !== id || !data) return;
  const history = $("#paper-chat-history"), follow = history && history.scrollHeight - history.scrollTop - history.clientHeight < 60;
  const label = $("#paper-chat-status-label"), elapsed = $("#paper-chat-elapsed");
  if (label && label.textContent !== pending.label) label.textContent = pending.label;
  if (elapsed) elapsed.textContent = `${Math.floor((Date.now() - pending.started) / 1000)} 秒`;
  const reasoning = $("#paper-chat-reasoning-live"), answer = $("#paper-chat-answer-live");
  if (reasoning && reasoning.dataset.text !== pending.reasoning) {
    reasoning.dataset.text = pending.reasoning; reasoning.hidden = !pending.reasoning;
    reasoning.lastElementChild.replaceWith(paperChatMarkdown(pending.reasoning));
  }
  if (answer && answer.dataset.text !== pending.answer) {
    answer.dataset.text = pending.answer; answer.hidden = !pending.answer;
    answer.replaceChildren(paperChatMarkdown(pending.answer));
  }
  if (follow) history.scrollTop = history.scrollHeight;
}

async function loadPaperChat(_generation = null) {
  const id = state.reader?.id;
  if (!id) return;
  const request = ++paperChatRuntime.request;
  const data = await api(`/api/papers/${encodeURIComponent(id)}/ai`);
  if (state.reader?.id !== id || request !== paperChatRuntime.request) return;
  paperChatRuntime.paperId = id; paperChatRuntime.data = data;
  const pending = paperChatRuntime.pending.get(id);
  if (pending?.completed) paperChatRuntime.pending.delete(id);
  renderPaperChat();
}

async function openPaperChat() {
  const pane = $("#reader-paper-chat");
  if (!pane || !state.reader) return;
  if (paperChatRuntime.paperId !== state.reader.id) paperChatRuntime.data = null;
  const notes = $("#reader-notes");
  if (notes && !notes.hidden) { notes.hidden = true; $("#reader-notes-toggle")?.setAttribute("aria-expanded", "false"); }
  pane.hidden = false; $("#reader-chat-toggle")?.setAttribute("aria-expanded", "true");
  await loadPaperChat(); $("#paper-chat-input")?.focus({ preventScroll: true });
}

function closePaperChat() {
  const pane = $("#reader-paper-chat");
  if (pane) pane.hidden = true;
  $("#reader-chat-toggle")?.setAttribute("aria-expanded", "false");
}

function togglePaperChat() {
  if ($("#reader-paper-chat")?.hidden) void openPaperChat().catch(error => toast(error.message, "error"));
  else closePaperChat();
}

function paperChatSetGeneration(id, pending, generation) {
  if (!Number.isInteger(generation) || generation < 1) return;
  pending.generation = generation;
  const data = paperChatRuntime.data;
  if (paperChatRuntime.paperId !== id || !data) return;
  data.current = { ...data.current, generation };
  data.generation = generation;
  data.thread_id = null;
  renderPaperChat();
}

async function sendPaperChat() {
  const input = $("#paper-chat-input"), id = state.reader?.id, data = paperChatRuntime.data;
  const question = input?.value.trim(), existing = paperChatRuntime.pending.get(id);
  if (!id || !question || !data || paperChatRuntime.paperId !== id || !paperChatConfigValid(data) || paperChatRuntime.saving.has(id) || (existing && !existing.failed && !existing.completed)) return;
  const pending = { question, generation: data.generation, label: "正在生成回答", started: Date.now(), reasoning: "", answer: "", completed: false, failed: false };
  paperChatRuntime.pending.set(id, pending); paperChatRuntime.drafts.delete(id);
  renderPaperChat();
  const timer = setInterval(() => updatePaperChatProgress(id), 1000);
  try {
    await apiStream(`/api/papers/${encodeURIComponent(id)}/ai/chat-stream`, { question }, event => {
      if (paperChatRuntime.pending.get(id) !== pending) return;
      if (event.type === "generation") paperChatSetGeneration(id, pending, event.generation);
      else if (event.type === "status") pending.label = event.label;
      else if (event.type === "reasoning") { pending.reasoning = event.text; pending.label = "正在思考"; }
      else if (event.type === "answer") { pending.answer = event.text; pending.label = "正在生成回答"; }
      else if (event.type === "thread" && paperChatRuntime.paperId === id && paperChatRuntime.data?.generation === pending.generation) {
        paperChatRuntime.data.thread_id = event.thread_id; renderPaperChat();
      } else if (event.type === "completed") { paperChatSetGeneration(id, pending, event.generation); pending.completed = true; pending.answer = event.answer; pending.label = "回答已完成"; }
      updatePaperChatProgress(id);
    });
    if (paperChatRuntime.pending.get(id) !== pending) return;
    if (state.reader?.id === id) { await loadPaperChat(pending.generation); renderLibrary(); }
  } catch (error) {
    if (paperChatRuntime.pending.get(id) !== pending) return;
    if (!pending.completed) {
      pending.failed = true; pending.label = `回答未完成：${error.message}`;
      pending.answer = ""; paperChatRuntime.drafts.set(id, question);
    }
    toast(`论文对话：${error.message}`, "error", 8000);
  } finally {
    clearInterval(timer);
    if (state.reader?.id === id && paperChatRuntime.paperId === id) renderPaperChat();
  }
}
