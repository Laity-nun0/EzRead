"use strict";
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
function element(tag, attrs = {}, ...children) {
  return { tag, attrs, value: attrs.value || "", checked: Boolean(attrs.checked), disabled: Boolean(attrs.disabled), childNodes: children.flat(Infinity).filter(x => x != null), listeners: {}, classList: { add() {}, toggle() {} }, addEventListener(name, fn) { this.listeners[name] = fn; }, replaceChildren(...nodes) { this.childNodes = nodes; }, append(...nodes) { this.childNodes.push(...nodes); } };
}
function all(node) { return [node, ...node.childNodes.flatMap(child => typeof child === "object" ? all(child) : [])]; }
function text(node) { return node.textContent ?? node.childNodes.map(child => typeof child === "object" ? text(child) : String(child)).join(" "); }
const nodes = new Map([['#translation-dialog', element('dialog')], ['#translation-content', element('div')], ['#settings-dialog', element('dialog')]]);
const requests = [], submissions = [];
const config = { provider: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', max_output_tokens: 393216 };
const settings = { features:{translation:{...config,has_key:true,thinking:'disabled'},chat:{...config,has_key:true,thinking:'enabled',reasoning_effort:'high'}} };
const context = { console, el: element, button: (label, handler, kind, icon, attrs) => element('button', { ...attrs, handler }, label), field: (label, input) => element('label', {}, label, input), iconButton: () => element('button'), $: selector => nodes.get(selector), ACTIVE_STATUSES: new Set(['running', 'queued']), pdfLayoutOf: p => p.pdf_layout_translation || {}, titleOf: () => 'synthetic', pdfLayoutProgressText: () => '', pdfLayoutSubmissionText: () => '', timeLabel: () => '', asText: x => x, act: fn => fn(), toast() {}, openDialog() {}, closeDialog() {}, api: async url => { requests.push(url); if (url === '/api/translation-api') return settings; if (url === '/api/pdf-layout') return { available: true }; throw Error('Unexpected network call ' + url); }, pdfLayoutRequest: async (p, action, payload) => submissions.push({ action, payload }) };
vm.createContext(context);
for (const file of ['api-translation-ui.js', 'translation-ui.js']) vm.runInContext(fs.readFileSync(`${__dirname}/../static/${file}`, 'utf8'), context);
async function main() {
  assert.match(context.configLabel({ ...config, thinking: 'disabled' }), /思考关闭/);
  assert.doesNotMatch(context.configLabel(config), /思考关闭/);
  assert.equal(context.parseApiGlossary('grain=晶粒\ngrain=晶粒').length, 1);
  assert.throws(() => context.parseApiGlossary('grain=晶粒\ngrain=晶界'), /不同译法/);
  assert.throws(() => context.parseApiGlossary('missing delimiter'), /每行/);
  assert.deepEqual(JSON.parse(JSON.stringify(context.pdfGlossaryEntries({ glossary_seed: [{source:'a', target:'甲'}] }))), [{source:'a',target:'甲'}]);
  await context.openPdfLayoutSetup({ paper_ai: { model: 'locked-codex' }, pdf_layout_translation: { status: 'idle', glossary_seed: [{ source:'grain', target:'晶粒' }] } });
  assert.deepEqual(requests, ['/api/translation-api', '/api/pdf-layout']);
  let controls = all(nodes.get('#translation-content'));
  assert.equal(controls.filter(x=>x.attrs['aria-label']==='API 模型').length,0);
  await controls.find(x => x.tag === 'button' && x.textContent === '开始翻译').attrs.handler({currentTarget:{}});
  assert.equal(submissions[0].payload.model, config.model);
  assert.equal(submissions[0].payload.provider, 'deepseek');
  assert.equal(submissions[0].payload.thinking, 'disabled');
  assert.equal(submissions[0].payload.reasoning_effort, undefined);
  assert.equal(submissions[0].payload.glossary[0].target, '晶粒');
  const old = { ...config, model: 'prior-model', max_output_tokens: 262144 };
  await context.openPdfLayoutSetup({ pdf_layout_translation: { status:'paused', config: old, glossary_seed: [{source:'seed',target:'原始'}], glossary: {entries:[{source:'old',target:'旧'}]} } });
  controls = all(nodes.get('#translation-content'));
  assert.equal(controls.find(x => x.tag === 'textarea').disabled, true);
  assert.match(text(controls.find(x => x.attrs.class === 'translation-resume-config')), /原缓存保留/);
  await controls.find(x => x.tag === 'button' && x.textContent === '继续翻译').attrs.handler({currentTarget:{}});
  assert.equal(submissions[1].payload.model, 'prior-model');
  assert.equal(submissions[1].payload.max_output_tokens, 262144, 'Resume preserves the historical output limit.');
  assert.equal(submissions[1].payload.thinking, undefined);
  assert.equal(submissions[1].payload.reasoning_effort, undefined);
  assert.equal(submissions[1].payload.glossary[0].source, 'seed', 'Resume must preserve the original seed rather than the learned final glossary.');
  assert.equal(controls.find(x => x.tag === 'textarea').value, 'seed=原始');
  const checkbox = controls.find(x => x.tag === 'input' && x.attrs['aria-label'] === '使用新配置重新生成版式译文'); checkbox.checked = true; checkbox.listeners.change();
  assert.equal(controls.find(x => x.tag === 'textarea').disabled, false);
  assert.equal(controls.find(x => x.tag === 'textarea').value, 'old=旧', 'Retranslation may inherit the final glossary.');
  const thinking = controls.find(x => x.attrs['aria-label'] === '思考模式');
  const high = controls.find(x => x.attrs['aria-label'] === '高 · high');
  const max = controls.find(x => x.attrs['aria-label'] === '最高 · max');
  assert.equal(high.checked, true);
  thinking.checked = true; thinking.listeners.change(); high.checked = false; max.checked = true;
  await controls.find(x => x.tag === 'button' && x.textContent === '生成新版本').attrs.handler({currentTarget:{}});
  assert.equal(submissions[2].payload.thinking, 'enabled');
  assert.equal(submissions[2].payload.reasoning_effort, 'max');
  const resume = controls.find(x => x.attrs['aria-label'] === '继续已有任务'); resume.listeners.change();
  checkbox.checked = true; checkbox.listeners.change();
  assert.equal(thinking.checked, true, 'Switching mode preserves edited new-task values.');
  assert.equal(max.checked, true);
  thinking.checked = false; thinking.listeners.change();
  await controls.find(x => x.tag === 'button' && x.textContent === '生成新版本').attrs.handler({currentTarget:{}});
  assert.equal(submissions.at(-1).payload.reasoning_effort, undefined);

  assert.equal(submissions[2].payload.glossary[0].source, 'old');
  context.api = async url => { if (url === '/api/translation-api') return settings; if (url === '/api/pdf-layout') return { available: true }; throw Error('Unexpected legacy call ' + url); };
  await context.openPdfLayoutSetup({ pdf_layout_translation: { status: 'paused', config: {model:'old-codex',reasoning_effort:'high'}, drafts:[{id:'old-draft',config:{model:'old-codex'}}] } });
  controls = all(nodes.get('#translation-content'));
  const legacyChoice = controls.find(x => x.tag === 'input' && x.attrs['aria-label'] === '使用新配置重新生成版式译文');
  assert.equal(legacyChoice.checked, true); assert.equal(legacyChoice.disabled, true);
  await controls.find(x => x.tag === 'button' && x.textContent === '使用 API 重新生成，保留旧草稿').attrs.handler({currentTarget:{}});
  assert.equal(submissions.at(-1).payload.retranslate, true);
  assert.equal(submissions.at(-1).payload.provider, 'deepseek');
  assert.equal(submissions.at(-1).payload.model, 'deepseek-flash');
  for (const unavailable of [false, true]) {
    context.api = async url => {
      if (url === '/api/translation-api') return settings;
      if (unavailable) throw Error('模拟组件读取失败');
      return { available: false, message: '模拟组件未安装' };
    };
    await context.openPdfLayoutSetup({});
    controls = all(nodes.get('#translation-content'));
    assert.equal(controls.find(x => x.attrs.class === 'translation-context').open, true, '组件故障必须主动展示，不能藏在折叠区。');
    assert.equal(controls.find(x => x.tag === 'button' && x.textContent === '开始翻译').disabled, true);
    assert.match(text(nodes.get('#translation-content')), /模拟组件/);
  }
  const updates=[]; context.refreshStatus=async()=>{};
  context.api=async(url,options)=>{assert.equal(url,'/api/translation-api');if(options)updates.push(options.body);return settings;};
  const translationPanel=context.apiTranslationSettingsPanel('translation'),chatPanel=context.apiTranslationSettingsPanel('chat');
  await new Promise(resolve=>setImmediate(resolve));
  let panelControls=all(translationPanel);
  const secret=panelControls.find(node=>node.attrs['aria-label']==='翻译 API Key');secret.value='temporary-secret';
  await panelControls.find(node=>node.tag==='button'&&text(node)==='保存翻译设置').attrs.handler({currentTarget:{}});
  assert.deepEqual(JSON.parse(JSON.stringify(updates[0])),{purpose:'translation',thinking:'disabled',api_key:'temporary-secret'});assert.equal(secret.value,'');
  panelControls=all(chatPanel);
  panelControls.find(node=>node.attrs['aria-label']==='问答默认模型').value='deepseek-v4-pro';
  await panelControls.find(node=>node.tag==='button'&&text(node)==='保存问答设置').attrs.handler({currentTarget:{}});
  assert.deepEqual(JSON.parse(JSON.stringify(updates[1])),{purpose:'chat',model:'deepseek-v4-pro',thinking:'enabled',reasoning_effort:'high',api_key:''});
  await all(translationPanel).find(node=>node.tag==='button'&&text(node)==='清除翻译密钥').attrs.handler({currentTarget:{}});
  assert.deepEqual(JSON.parse(JSON.stringify(updates[2])),{purpose:'translation',thinking:'disabled',clear_key:true});
  const settingsSource=fs.readFileSync(`${__dirname}/../static/settings-ui.js`,'utf8');assert.match(settingsSource,/\["translation", "翻译"/);assert.match(settingsSource,/\["chat", "问答"/);
  context.openSettings=panel=>{assert.equal(panel,'chat');};context.openPaperModel();
  const app = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
  assert.doesNotMatch(app, /prepareSelectionSession|void loadModels\(|^  loadUsage\(false\)/m);
  assert.match(app, /void refreshStatus\(\)\.catch/);
  const index = fs.readFileSync(`${__dirname}/../static/index.html`, 'utf8'); assert.ok(index.indexOf('api-translation-ui.js') < index.indexOf('translation-ui.js"'));
  console.log('API translation UI offline checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
