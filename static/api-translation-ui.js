"use strict";

function apiProviderLabel(provider) { return { deepseek: "DeepSeek", openai: "OpenAI", openai_compatible: "OpenAI 兼容", anthropic: "Anthropic", gemini: "Gemini" }[provider] || provider; }
async function loadTranslationApiSettings() { return api("/api/translation-api"); }
function deepSeekFeature(settings, purpose = "translation") {
  return settings.features?.[purpose] || { provider: "deepseek", base_url: "https://api.deepseek.com", model: "deepseek-flash", max_output_tokens: 393216, thinking: purpose === "chat" ? "enabled" : "disabled", ...(purpose === "chat" ? { reasoning_effort: "high" } : {}), has_key: false };
}
let apiPickerSequence = 0;
function apiTranslationPicker(settings, initial = {}, editableKey = false, pdfTask = false, purpose = "translation") {
  const feature = deepSeekFeature(settings, purpose);
  let config;
  const chatModel = editableKey && purpose === "chat" ? el("select", { "aria-label": "问答默认模型" },
    (settings.chat_model_options || [{ model: "deepseek-flash", label: "DeepSeek Flash" }, { model: "deepseek-v4-pro", label: "DeepSeek Pro" }]).map(item => el("option", { value: item.model }, item.label))) : null;
  const key = el("input", { type: "password", autocomplete: "new-password", "aria-label": purpose === "chat" ? "问答 API Key" : "翻译 API Key", placeholder: "留空保留本栏已保存密钥" });
  const status = el("p", { class: "small muted", role: "status" }, `${feature.has_key ? "本栏已保存密钥" : "本栏尚未保存密钥"} · 未在线验证`);
  const thinking = el("input", { type: "checkbox", role: "switch", "aria-label": "思考模式" });
  const strengths = [["low", "低 · low"], ["high", "高 · high"], ["max", "最高 · max"]];
  const group = `api-thinking-effort-${++apiPickerSequence}`;
  const strengthInputs = strengths.map(([value, label]) => el("input", { type: "radio", name: group, value, "aria-label": label }));
  const effortField = el("fieldset", { class: "translation-effort" }, el("legend", {}, "推理强度"), el("div", { class: "translation-effort-options" }, strengths.map(([, label], index) => el("label", {}, strengthInputs[index], label))));
  const thinkingPanel = el("section", { class: "translation-thinking" }, el("label", { class: "translation-thinking-toggle" }, el("span", {}, el("strong", {}, "思考模式"), el("span", { class: "small muted translation-thinking-note" }, "开启后可能增加耗时与 token 消耗")), thinking), effortField);
  const modelInfo = el("p", { class: "deepseek-model-info" });
  const advanced = el("details", { class: "translation-advanced" }, el("summary", {}, "模型详情"), el("p", {}, "DeepSeek 官方 API · https://api.deepseek.com · 输出上限 384K（393216 token）"));
  function updateThinking() { effortField.hidden = !thinking.checked; }
  thinking.addEventListener("change", updateThinking);
  function setConfig(value) {
    config = value.provider ? { ...value } : { ...feature };
    thinking.checked = config.thinking === "enabled";
    strengthInputs.forEach(input => { input.checked = input.value === (["low", "high", "max"].includes(config.reasoning_effort) ? config.reasoning_effort : "high"); });
    if (chatModel) chatModel.value = config.model;
    key.value = ""; modelInfo.textContent = `${apiProviderLabel(config.provider)} · ${config.model}`; updateThinking();
  }
  const root = el("div", { class: "model-picker" }, editableKey ? null : modelInfo, chatModel ? field("问答默认模型", chatModel) : null, status, pdfTask || editableKey ? thinkingPanel : null, editableKey ? field("API Key（留空保留）", key) : null);
  setConfig(initial);
  return { root, key, advanced, setConfig, value: () => { const { thinking: oldThinking, reasoning_effort: oldEffort, has_key: oldKey, ...snapshot } = config; return { ...snapshot, ...(chatModel ? { model: chatModel.value } : {}), thinking: thinking.checked ? "enabled" : "disabled", ...(thinking.checked ? { reasoning_effort: strengthInputs.find(input => input.checked)?.value || "high" } : {}) }; }, ready: () => Boolean(feature.has_key && config.provider === feature.provider && config.base_url === feature.base_url && config.model === feature.model), disable(value) { for (const input of [thinking, ...strengthInputs, ...(chatModel ? [chatModel] : [])]) input.disabled = value; }, dispose() { key.value = ""; } };
}
function apiTranslationSettingsPanel(purpose = "translation") {
  const label = purpose === "chat" ? "问答" : "翻译";
  const content = el("div", {}, el("p", { role: "status" }, "正在读取本机设置…"));
  const root = el("section", { class: "settings-section deepseek-settings" }, el("h3", {}, `${label}设置`),
    el("p", { class: "small muted" }, purpose === "chat" ? "论文问答独立保存密钥与思考设置，默认开启思考，强度为 high。" : "全文 PDF 翻译独立保存密钥与思考设置，默认关闭思考；已有任务继续使用原配置。"),
    el("p", { class: "small muted deepseek-fixed-config" }, purpose === "chat" ? "DeepSeek Flash / Pro · 输出上限 384K（393216 token）" : "DeepSeek · deepseek-flash · 输出上限 384K（393216 token）"),
    el("p", { class: "small muted" }, "https://api.deepseek.com"), content,
    el("details", { class: "api-settings-help" }, el("summary", {}, "使用范围与隐私"),
      el("p", {}, "翻译与问答使用各自的 API Key，按供应商 API 计费。密钥仅保存在本机私有设置，不进入文献备份或浏览器存储。保存不测试连接，不联网读取模型目录。"),
      el("p", {}, "密钥留空会保留已保存密钥。旧共享密钥可继续使用；填入本栏新密钥后独立保存，清除只影响本栏。")));
  let picker = null, disposed = false;
  $("#settings-dialog").addEventListener("close", () => { disposed = true; picker?.dispose(); }, { once: true });
  function render(settings) {
    if (disposed) return;
    picker?.dispose(); picker = apiTranslationPicker(settings, deepSeekFeature(settings, purpose), true, false, purpose);
    const message = el("p", { class: "small muted", role: "status" });
    async function save(clear = false) {
      const value = picker.value();
      try {
        const result = await api("/api/translation-api", { method: "PUT", body: { purpose, ...(purpose === "chat" ? { model: value.model } : {}), thinking: value.thinking, ...(value.reasoning_effort ? { reasoning_effort: value.reasoning_effort } : {}), ...(clear ? { clear_key: true } : { api_key: picker.key.value }) } });
        picker.dispose(); render(result); void refreshStatus().catch(() => {}); toast(clear ? `已清除${label}密钥` : `${label}设置已保存（未在线验证）`);
      } catch (error) { if (!disposed) { message.textContent = `保存失败：${error.message}`; message.classList.add("error-text"); } }
    }
    content.replaceChildren(picker.root, message, el("div", { class: "form-actions" }, button(`保存${label}设置`, event => act(() => save(), event.currentTarget), "primary"), button(`清除${label}密钥`, event => act(() => save(true), event.currentTarget))));
  }
  void loadTranslationApiSettings().then(render).catch(error => { if (!disposed) content.replaceChildren(el("p", { class: "error-text", role: "status" }, `设置读取失败：${error.message}`)); });
  return root;
}
function pdfGlossaryEntries(task) { return Array.isArray(task.glossary_seed) ? task.glossary_seed : task.glossary_seed?.entries || []; }
function parseApiGlossary(text) {
  const entries = [], seen = new Map();
  for (const line of text.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
    const split = line.indexOf("=");
    if (split < 1 || !line.slice(split + 1).trim()) throw Error("术语请按每行 原文=译文 填写。");
    const source = line.slice(0, split).trim(), target = line.slice(split + 1).trim();
    if (source.length > 300 || target.length > 500) throw Error("术语过长，请缩短后重试。");
    if (seen.has(source) && seen.get(source) !== target) throw Error(`术语“${source}”存在不同译法，请统一。`);
    if (!seen.has(source)) entries.push({ source, target }); seen.set(source, target);
  }
  if (entries.length > 500) throw Error("本篇术语最多 500 项。");
  return entries;
}
