// 隔离本机后端与合成文献；不启动模型 worker，不访问远端页面。
const {chromium} = require(process.env.EZREAD_PLAYWRIGHT || 'playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const origin = process.env.EZREAD_TEST_URL, output = process.env.EZREAD_TEST_OUTPUT;
const edge = process.env.EZREAD_BROWSER || path.join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:edge});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage(), errors = [], forbidden = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  let queuePapers = null;
  let chatCurrent = { generation: 1, model: 'legacy-codex' };
  const chatVersions = [{ generation: 1, model: 'legacy-codex' }], chatMessages = new Map([[1, [{ role:'assistant', kind:'chat', text:'历史 Codex 回答', citations:[] }]]]);
  let chatConfig = null, chatModels = [];
  const chatData = () => ({current:chatCurrent,generation:1,generations:chatVersions,messages:[...chatMessages.values()].flat(),chat_config:chatConfig,chat_models:chatModels,chat_ready:true,thread_id:null});
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || /^\/api\/(models|usage|status|selection-session)$/.test(url.pathname)) {
      forbidden.push(url.pathname); return route.abort();
    }
    if (url.pathname === '/api/papers' && queuePapers) return route.fulfill({json:{papers:queuePapers}});
    if (url.pathname === '/api/ai/status') {
      const response=await route.fetch(), local=await response.json();
      const usage={scope:'application_run',requests:{attempted:3,succeeded:1,failed:1,cancelled:1},tokens:{input:12,output:5,total:17},usage_reported:1,models:[{provider:'deepseek',model:'offline-model',requests:{attempted:3,succeeded:1,failed:1,cancelled:1},tokens:{input:12,output:null,total:12},usage_reported:1,usage:{prompt_cache_hit_tokens:2,'completion_tokens_details.reasoning_tokens':4}}],last:{provider:'deepseek',model:'offline-model',channel:'chat',status:'success',at:'2026-10-15T01:00:00Z'},account_balance:null};
      return route.fulfill({json:{...local,usage}});
    }
    if (/^\/api\/papers\/[^/]+\/ai$/.test(url.pathname)) {
      return route.fulfill({json:chatData()});
    }
    if (/^\/api\/papers\/[^/]+\/ai\/chat-model$/.test(url.pathname)) {
      chatConfig = {...chatConfig,...route.request().postDataJSON()};
      if (chatConfig.thinking === 'disabled') delete chatConfig.reasoning_effort;
      return route.fulfill({json:chatData()});
    }
    if (/^\/api\/papers\/[^/]+\/ai\/model$/.test(url.pathname)) {
      chatCurrent = {...route.request().postDataJSON(),generation:chatCurrent.generation+1};
      chatVersions.unshift({...chatCurrent}); chatMessages.set(chatCurrent.generation,[]);
      return route.fulfill({json:{paper_ai:chatCurrent}});
    }
    if (/^\/api\/papers\/[^/]+\/ai\/chat-stream$/.test(url.pathname)) {
      const question = route.request().postDataJSON().question;
      assert(chatConfig?.profile_id, 'Chat must use an explicitly configured model.');
      const answer='模拟 API 回答：验证单对话保存与显示。';
      chatMessages.set(2,[{role:'user',kind:'chat',text:question,citations:[]},{role:'assistant',kind:'chat',text:answer,citations:[]}]);
      return route.fulfill({contentType:'application/x-ndjson',body:[{type:'generation',generation:chatCurrent.generation},{type:'status',label:'正在生成回答'},{type:'completed',generation:chatCurrent.generation,answer,thread_id:null}].map(value=>JSON.stringify(value)).join('\n')+'\n'});
    }
    return route.continue();
  });
  try {
    await page.goto(origin); await page.waitForSelector('.paper-card');
    await page.getByRole('button', {name:'设置',exact:true}).click();
    await page.getByRole('tab', {name:'翻译',exact:true}).click();
    const translationSettings=page.locator('#settings-panel-translation');
    assert.equal(await translationSettings.locator('.api-settings-help').evaluate(node=>node.open),false);
    assert.match(await translationSettings.innerText(),/deepseek-flash/); assert.match(await translationSettings.innerText(),/393216/);
    assert.equal(await page.getByRole('combobox',{name:'API 供应商'}).count(),0);
    assert.equal(await translationSettings.getByRole('switch',{name:'思考模式',exact:true}).isChecked(),false);
    await page.screenshot({path:path.join(output,'settings-first-screen.png')});
    await translationSettings.locator('.api-settings-help > summary').click();
    assert.match(await translationSettings.locator('.api-settings-help').innerText(),/API Key|私有设置/);
    await translationSettings.locator('.api-settings-help > summary').click();
    await page.getByLabel('翻译 API Key',{exact:true}).fill('fixture-browser-key');
    await page.getByRole('button',{name:'保存翻译设置',exact:true}).click();
    await translationSettings.getByText('本栏已保存密钥',{exact:false}).waitFor();
    assert.equal(await page.getByLabel('翻译 API Key',{exact:true}).inputValue(),'');
    await page.getByRole('tab',{name:'问答',exact:true}).click();
    const chatSettings=page.locator('#settings-panel-chat');
    assert.equal(await chatSettings.getByRole('switch',{name:'思考模式',exact:true}).isChecked(),true);
    assert.equal(await chatSettings.getByRole('radio',{name:'高 · high',exact:true}).isChecked(),true);
    await page.getByLabel('问答 API Key',{exact:true}).fill('fixture-chat-key');
    await page.getByRole('button',{name:'保存问答设置',exact:true}).click();
    await chatSettings.getByText('本栏已保存密钥',{exact:false}).waitFor();
    assert.equal(await page.getByLabel('问答 API Key',{exact:true}).inputValue(),'');
    await page.getByRole('button',{name:'清除问答密钥',exact:true}).click();
    await chatSettings.getByText('本栏尚未保存密钥',{exact:false}).waitFor();
    const clearedSettings=await (await page.request.get(origin+'/api/translation-api')).json();
    assert.equal(clearedSettings.features.translation.has_key,true);assert.equal(clearedSettings.features.chat.has_key,false);
    for (const feature of Object.values(clearedSettings.features)) {
      assert.equal(feature.provider,'deepseek');assert.equal(feature.base_url,'https://api.deepseek.com');assert.equal(feature.model,'deepseek-flash');assert.equal(feature.max_output_tokens,393216);
    }
    await page.getByLabel('问答 API Key',{exact:true}).fill('fixture-chat-key');
    await page.getByRole('button',{name:'保存问答设置',exact:true}).click();
    await chatSettings.getByText('本栏已保存密钥',{exact:false}).waitFor();
    assert.equal(await chatSettings.locator('select[aria-label="问答默认模型"]').inputValue(),'deepseek-flash');
    await page.getByRole('combobox',{name:'问答默认模型',exact:true}).click();
    await page.getByRole('option',{name:'DeepSeek Pro',exact:true}).click();
    await page.getByRole('button',{name:'保存问答设置',exact:true}).click();
    const proSettings=await (await page.request.get(origin+'/api/translation-api')).json();
    assert.equal(proSettings.features.chat.model,'deepseek-v4-pro');assert.equal(proSettings.features.translation.model,'deepseek-flash');
    assert.equal(proSettings.features.chat.has_key,true,'Changing only the model preserves the question-answer key.');
    await page.screenshot({path:path.join(output,'chat-settings-pro.png')});
    await page.getByRole('tab',{name:'翻译',exact:true}).click();
    checks.push('fixed DeepSeek model/token limit, independent translation/chat keys and thinking defaults; password cleared');
    await page.locator('#settings-api-status').getByText('配置已就绪（未在线验证）',{exact:true}).waitFor();
    await page.locator('#settings-usage').getByText('输入 token 12 · 输出 token 5 · 合计 17',{exact:true}).waitFor();
    const usageText=await page.locator('#settings-usage').innerText();
    assert.match(usageText,/覆盖 1\/3/);assert.match(usageText,/思考 token：4/);assert.match(usageText,/输出 token 未知/);
    assert.doesNotMatch(await page.locator('#settings-panel-translation').innerText(),/Codex|登录|订阅|重置|剩余.*%/);
    await page.locator('#settings-usage').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'api-status-usage.png')});
    checks.push('local API configuration readiness, mocked per-run usage and partial token coverage; no login or subscription UI');
    for (const [width,height,fontSize] of [[1440,1000,''],[520,650,''],[900,480,''],[520,480,'22px']]) {
      await page.setViewportSize({width,height});
      await page.evaluate(size=>document.documentElement.style.setProperty('--ui-font-size',size),fontSize);
      const button = page.getByRole('button',{name:'保存翻译设置',exact:true});
      await button.scrollIntoViewIfNeeded();
      const box = await button.boundingBox();
      assert(box && box.x >= 0 && box.y >= 0 && box.x+box.width <= width+1 && box.y+box.height <= height+1);
      const overflow = await page.locator('#settings-dialog').evaluate(node=>node.scrollWidth-node.clientWidth);
      assert(overflow<=1,`settings horizontal overflow ${overflow}`);
      await page.screenshot({path:path.join(output,`settings-${width}x${height}${fontSize?'-large-text':''}.png`)});
    }
    await page.evaluate(()=>document.documentElement.style.removeProperty('--ui-font-size'));
    checks.push('wide, narrow and short settings with reachable save button');
    await page.getByRole('button',{name:'关闭设置',exact:true}).click();
    await page.reload(); await page.waitForSelector('.paper-card');
    await page.getByRole('button',{name:'设置',exact:true}).click();
    await page.getByRole('tab',{name:'翻译',exact:true}).click();
    await page.locator('#settings-panel-translation').getByText('本栏已保存密钥',{exact:false}).waitFor();
    assert.equal(await page.getByLabel('翻译 API Key',{exact:true}).inputValue(),'');
    await page.getByRole('button',{name:'关闭设置',exact:true}).click();
    checks.push('saved config reload without plaintext key in browser storage');
    assert(!await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('fixture-browser-key')));
    assert(!await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('fixture-chat-key')));
    // 从实际加载的页面 API 打开同一个产品确认窗口；按 DOM 操作后续控件。
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>openPdfLayoutSetup(state.papers[0]));
    for (const selector of ['.translation-glossary','.translation-advanced','.translation-context']) {
      assert.equal(await page.locator(selector).evaluate(node=>node.open),false,`${selector} defaults collapsed`);
    }
    assert.match(await page.locator('.translation-confirm-footer').innerText(),/确认后加入翻译队列/);
    await page.screenshot({path:path.join(output,'translation-confirm-default.png')});
    await page.locator('.translation-glossary > summary').click();
    await page.getByLabel('本篇术语（每行原文=译文）').fill('grain boundary=晶界');
    for (const [width,height] of [[1440,1000],[520,650],[900,480]]) {
      await page.setViewportSize({width,height});
      const before = await page.locator('.translation-confirm-heading').boundingBox();
      await page.locator('.translation-confirm-body').evaluate(node => { node.scrollTop = node.scrollHeight; });
      const after = await page.locator('.translation-confirm-heading').boundingBox();
      assert.equal(before.y,after.y,'Confirmation heading remains fixed.');
      const bounds=await page.getByRole('button',{name:'开始翻译',exact:true}).boundingBox();
      assert(bounds && bounds.y>=0 && bounds.y+bounds.height<=height+1,'Confirmation action remains reachable.');
      assert(await page.locator('#translation-dialog').evaluate(node=>node.scrollWidth-node.clientWidth)<=1);
      await page.screenshot({path:path.join(output,`translation-confirm-${width}x${height}.png`)});
    }
    await page.setViewportSize({width:520,height:480});
    await page.evaluate(()=>document.documentElement.style.setProperty('--ui-font-size','22px'));
    let largeButton=await page.getByRole('button',{name:'开始翻译',exact:true}).boundingBox();
    assert(largeButton && largeButton.y>=0 && largeButton.y+largeButton.height<=481,'Large text keeps fixed action visible.');
    assert(await page.locator('#translation-dialog').evaluate(node=>node.scrollWidth-node.clientWidth)<=1);
    await page.screenshot({path:path.join(output,'translation-confirm-large-text.png')});
    await page.evaluate(()=>document.documentElement.style.removeProperty('--ui-font-size'));
    const originalTheme=await page.evaluate(()=>document.documentElement.dataset.theme);
    for (const theme of ['paper','sage','graphite']) {
      await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
      const colors=await page.evaluate(()=>{const css=getComputedStyle(document.documentElement);return {accent:css.getPropertyValue('--green').trim(),paper:css.getPropertyValue('--paper').trim()};});
      assert.equal(colors.accent,theme==='paper'?'#3b63d9':theme==='sage'?'#376144':'#9bc7a8');
      if(theme==='graphite') assert.equal(colors.paper,'#262d28');
    }
    await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},originalTheme);
    checks.push('large font at 520x480 keeps fixed action reachable; gray-white-blue paper theme and existing sage/dark semantics preserved');
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('switch',{name:'思考模式',exact:true}).check();
    assert.equal(await page.getByRole('radio',{name:'高 · high',exact:true}).isChecked(),true);
    await page.getByRole('radio',{name:'最高 · max',exact:true}).check();
    await page.getByRole('switch',{name:'思考模式',exact:true}).uncheck();
    assert.equal(await page.getByRole('radio',{name:'低 · low',exact:true}).isVisible(),false);
    checks.push('translation fixed heading/footer at wide, narrow, short sizes; thinking defaults high and strength hides when off');
    const confirm=page.getByRole('button',{name:'开始翻译',exact:true});
    await confirm.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'translation-confirm.png')});
    await confirm.click();
    await page.waitForFunction(()=>!document.querySelector('#translation-dialog').open);
    const papers=await (await page.request.get(origin+'/api/papers')).json(), task=papers.papers[0].pdf_layout_translation;
    assert.equal(task.config.thinking,'disabled'); assert.equal(task.config.provider,'deepseek'); assert.equal(task.config.model,'deepseek-flash');
    assert.deepEqual(task.glossary_seed,[{source:'grain boundary',target:'晶界'}]);
    checks.push('API task confirmation without CLI login; frozen glossary seed');
    await page.getByRole('button',{name:/^翻译任务/}).click();
    await page.locator('#queue-dialog').waitFor({state:'visible'});
    await page.locator('#refresh-status').click();
    await page.waitForFunction(()=>!document.querySelector('#refresh-status').disabled && !state.pollBusy);
    queuePapers = await page.evaluate(()=>{
      window.savedQueuePapers=state.papers;
      state.papers=[{id:'ui-running',title:'Synthetic long translation title '.repeat(8),pdf_layout_translation:{status:'running',stage:'正在翻译正文',done:3,received:4,error_details:{},config:{provider:'deepseek',model:'mock',max_output_tokens:8192}}},
        {id:'ui-error',title:'Synthetic error',pdf_layout_translation:{status:'error',stage:'正在翻译',error:'模型响应未通过解析。'.repeat(20),error_code:'invalid_content_json',error_details:{request_id:'fixture-request',stage:'content_json',diagnostic_saved:false,usage:{prompt_tokens:12}},done:3}},
        {id:'ui-paused',title:'Synthetic paused',pdf_layout_translation:{status:'paused',stage:'翻译已暂停',done:2}},
        {id:'ui-completed',title:'Synthetic review',pdf_layout_translation:{status:'completed',current:{id:'fixture-result',quality:'needs_review'}}}];
      renderQueue(); return state.papers;
    });
    const row=page.locator('[data-queue-paper="ui-running"]');
    assert.equal(await page.locator('.queue-api-details').evaluate(node=>node.open),false,'Queue API diagnostics default collapsed.');
    const firstCard=await row.boundingBox();
    assert(firstCard && firstCard.y>=0 && firstCard.y<1000,'First task appears in the first screen.');
    assert.equal(await page.locator('#queue-list details[open]').count(),0);
    await page.screenshot({path:path.join(output,'translation-tasks-collapsed.png')});
    await row.locator('summary').click();
    await row.getByRole('button',{name:'暂停翻译',exact:true}).focus();
    const queueScroll=page.locator('.queue-dialog-scroll');
    await queueScroll.evaluate(node=>{node.scrollTop=60;});
    const previousScroll=await queueScroll.evaluate(node=>node.scrollTop);
    assert(previousScroll>0,'Queue fixture must actually scroll before testing preservation.');
    queuePapers[0].pdf_layout_translation.done=4;
    await page.evaluate(()=>refreshStatus());
    assert.equal(await row.locator('details').evaluate(node=>node.open),true);
    assert.equal(await row.locator('.pdf-task-error-details').count(),0,'Empty cleared diagnostics stay hidden.');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.queueFocus),'action');
    assert.equal(await queueScroll.evaluate(node=>node.scrollTop),previousScroll);
    assert.equal(await row.locator('.pdf-task-stages').innerText(),'准备\n翻译\n排版\n完成');
    const failed=page.locator('[data-queue-paper="ui-error"]');
    assert.equal(await failed.locator('.pdf-task-stages').count(),0);
    assert.match(await failed.innerText(),/翻译中断/);
    assert.equal(await failed.locator('.pdf-task-main h4').count(),0,'Card has one status rather than a repeated stage heading.');
    const completeError='模型响应未通过解析。'.repeat(20);
    assert.equal(await failed.locator('.pdf-task-error-preview').textContent(),completeError);
    const preview=await failed.locator('.pdf-task-error-preview').evaluate(node=>({height:node.getBoundingClientRect().height,lineHeight:parseFloat(getComputedStyle(node).lineHeight),scrollHeight:node.scrollHeight}));
    assert(preview.height<=preview.lineHeight*2+2,'Error preview is limited to two lines.');
    assert(preview.scrollHeight>preview.height,'Long error actually exceeds the preview.');
    assert.equal(await failed.locator('.pdf-task-footer .pdf-task-details').count(),0,'Details do not share the action footer.');
    await failed.getByRole('button',{name:'查看错误详情',exact:true}).click();
    assert.equal(await failed.locator('.pdf-task-detail-error').innerText(),completeError);
    assert.match(await failed.locator('.pdf-task-error-details').innerText(),/HTTP 状态\n未知/);
    assert.match(await failed.locator('.pdf-task-error-details').innerText(),/prompt_tokens/);
    assert.match(await failed.locator('.pdf-task-error-details').innerText(),/响应正文完整\n未知/);
    assert.match(await failed.locator('.pdf-task-error-details').innerText(),/声明正文长度\n未知/);
    assert.equal(await page.locator('[data-queue-paper="ui-completed"] .translation-review').count()>0,true);
    for (const [width,height,fontSize] of [[1440,1000,''],[520,650,''],[900,480,''],[520,480,'22px']]) {
      await page.setViewportSize({width,height});
      await page.evaluate(size=>document.documentElement.style.setProperty('--ui-font-size',size),fontSize);
      assert(await page.locator('#queue-dialog').evaluate(node=>node.scrollWidth-node.clientWidth)<=1);
      for (const name of ['阅读原文','继续翻译']) {
        const action=failed.getByRole('button',{name,exact:true});
        await action.scrollIntoViewIfNeeded();
        const bounds=await action.boundingBox();
        assert(bounds && bounds.x>=0 && bounds.y>=0 && bounds.x+bounds.width<=width+1 && bounds.y+bounds.height<=height+1,`${name} reachable at ${width}x${height} ${fontSize}`);
      }
      await page.screenshot({path:path.join(output,`translation-tasks-${width}x${height}${fontSize?'-large-text':''}.png`)});
    }
    await page.evaluate(()=>document.documentElement.style.removeProperty('--ui-font-size'));
    queuePapers = null;
    await page.evaluate(()=>{state.papers=window.savedQueuePapers;delete window.savedQueuePapers;renderQueue();});
    checks.push('mock running/error/paused/completed-review task cards; unknown diagnostics; poll preserves details, scroll and keyboard focus; narrow/short overflow checks');
    await page.getByRole('button',{name:'关闭翻译任务',exact:true}).click();
    assert.deepEqual(forbidden,[], 'Opening the PDF task queue must not query Codex status, usage or model catalog.');
    checks.push('opening task queue makes no automatic CLI request');
    await page.evaluate(()=>openPdfLayoutSetup({id:'mode-fixture',title:'Synthetic resume mode',pdf_layout_translation:{status:'paused',config:{provider:'deepseek',base_url:'http://127.0.0.1:9/v1',model:'frozen-model',max_output_tokens:4096},glossary_seed:[{source:'seed',target:'原始'}]}}));
    assert.match(await page.locator('.translation-resume-config').innerText(),/frozen-model|原任务配置/);
    assert.equal(await page.locator('#translation-dialog').getByLabel('API 模型',{exact:true}).count(),0);
    assert.equal(await page.getByRole('switch',{name:'思考模式',exact:true}).isVisible(),false);
    await page.getByRole('radio',{name:'使用新配置重新生成版式译文',exact:true}).check();
    await page.getByRole('switch',{name:'思考模式',exact:true}).check();
    await page.getByRole('radio',{name:'低 · low',exact:true}).check();
    await page.setViewportSize({width:1200,height:1100});
    await page.locator('.translation-confirm-body').evaluate(node=>{node.scrollTop=0;});
    await page.screenshot({path:path.join(output,'translation-thinking-enabled.png')});
    await page.getByRole('radio',{name:'继续已有任务',exact:true}).check();
    assert.equal(await page.getByRole('button',{name:'继续翻译',exact:true}).isVisible(),true);
    await page.getByRole('radio',{name:'使用新配置重新生成版式译文',exact:true}).check();
    assert.equal(await page.getByRole('radio',{name:'低 · low',exact:true}).isChecked(),true);
    await page.getByRole('button',{name:'关闭版式翻译设置',exact:true}).click();
    checks.push('resume exposes read-only frozen config and absent thinking default; switching modes preserves editable new-version configuration');
    await page.setViewportSize({width:1440,height:1000});
    const savedSettings=await (await page.request.get(origin+'/api/translation-api')).json();
    const firstModel=savedSettings.features.chat;
    const reasoning_options=[{value:'disabled',label:'不思考'},{value:'low',label:'低'},{value:'high',label:'高'},{value:'max',label:'最高'}];
    chatModels=['deepseek-flash','deepseek-v4-pro'].map(model=>({...firstModel,model,label:model==='deepseek-flash'?'DeepSeek Flash':'DeepSeek Pro',reasoning_options}));
    chatConfig={...firstModel};
    await page.evaluate(async()=>{await openReader(state.papers[0].id);await openPaperChat();});
    assert.equal(await page.getByRole('button',{name:'在 Codex 中打开 ↗',exact:true}).count(),0);
    await page.getByText('历史 Codex 回答',{exact:true}).waitFor();
    await page.getByLabel('向论文提问',{exact:true}).fill('模拟迁移问题');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.getByText('模拟 API 回答：验证单对话保存与显示。',{exact:true}).waitFor();
    assert.equal(await page.locator('select[aria-label="论文对话版本"]').count(),0);
    await page.getByText('历史 Codex 回答',{exact:true}).waitFor();
    checks.push('mocked API chat aggregates legacy and new messages in one conversation without version selector');
    assert.equal(await page.locator('select#paper-chat-model').inputValue(),'deepseek-v4-pro');
    await page.getByLabel('向论文提问',{exact:true}).fill('保留的草稿');
    await page.getByRole('combobox',{name:'问答模型',exact:true}).click();
    await page.getByRole('option',{name:'DeepSeek Flash',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#paper-chat-model')?.value==='deepseek-flash'&&!document.querySelector('#paper-chat-model').disabled);
    assert.equal(await page.getByLabel('向论文提问',{exact:true}).inputValue(),'保留的草稿');
    await page.getByRole('combobox',{name:'问答模型',exact:true}).click();
    await page.getByRole('option',{name:'DeepSeek Pro',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#paper-chat-model')?.value==='deepseek-v4-pro'&&!document.querySelector('#paper-chat-model').disabled);
    const chatEffort=page.getByRole('combobox',{name:'问答思考强度',exact:true});
    assert.equal(await page.locator('select#paper-chat-effort').inputValue(),'high');
    await chatEffort.click();
    await page.getByRole('option',{name:'低',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#paper-chat-effort')?.value==='low'&&!document.querySelector('#paper-chat-effort').disabled);
    assert.equal(chatConfig.reasoning_effort,'low');
    assert.equal(chatConfig.model,'deepseek-v4-pro');

    assert.equal(chatCurrent.generation,1,'A new answer must keep the existing conversation.');
    await page.getByText('历史 Codex 回答',{exact:true}).waitFor();
    await page.getByText('模拟 API 回答：验证单对话保存与显示。',{exact:true}).waitFor();
    const afterSwitch=await (await page.request.get(origin+'/api/papers')).json();
    assert.equal(afterSwitch.papers[0].pdf_layout_translation.config.model,'deepseek-flash');
    await page.screenshot({path:path.join(output,'chat-api-model-switch.png')});
    checks.push('DeepSeek Flash/Pro and reasoning switches preserve messages, draft and the independent frozen PDF configuration');
    await page.evaluate(()=>closeDialog('#reader-dialog'));
    let legacySubmission=null;
    await page.route('**/api/papers/legacy-fixture/pdf-translate',async route=>{legacySubmission=route.request().postDataJSON();return route.fulfill({json:{paper:{id:'legacy-fixture'}}});});
    await page.evaluate(()=>openPdfLayoutSetup({id:'legacy-fixture',title:'Synthetic legacy PDF',pdf_layout_translation:{status:'paused',config:{model:'old-codex',reasoning_effort:'high'},drafts:[{id:'old-fixture-draft',config:{model:'old-codex'},done:1}]}}));
    const legacyChoice=page.getByLabel('使用新配置重新生成版式译文',{exact:true});
    assert.equal(await legacyChoice.isChecked(),true);assert.equal(await legacyChoice.isDisabled(),true);
    assert.match(await page.locator('#translation-dialog .deepseek-model-info').innerText(),/deepseek-flash/);
    await page.getByRole('button',{name:'使用 API 重新生成，保留旧草稿',exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'legacy-pdf-api-conversion.png')});
    await page.getByRole('button',{name:'使用 API 重新生成，保留旧草稿',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#translation-dialog').open);
    assert.equal(legacySubmission.provider,'deepseek');assert.equal(legacySubmission.model,'deepseek-flash');assert.equal(legacySubmission.retranslate,true);
    assert.equal(legacySubmission.reasoning_effort,undefined);
    checks.push('mocked legacy PDF conversion forces API retranslation using shared defaults while keeping old draft listed');
    for (const unavailable of [false,true]) {
      await page.route('**/api/pdf-layout', route => unavailable ? route.abort() : route.fulfill({json:{available:false,message:'模拟版式组件未安装'}}));
      await page.evaluate(()=>openPdfLayoutSetup({id:'unavailable-component-fixture',title:'Synthetic missing component'}));
      assert.equal(await page.locator('.translation-context').evaluate(node=>node.open),true);
      assert.equal(await page.getByRole('button',{name:'开始翻译',exact:true}).isDisabled(),true);
      assert.match(await page.locator('.translation-context').innerText(),unavailable?/本地版式组件不可用/:/模拟版式组件未安装/);
      await page.getByRole('button',{name:'关闭版式翻译设置',exact:true}).click();
      await page.unroute('**/api/pdf-layout');
    }
    checks.push('missing component and failed local check expand the reason and keep submission disabled');
    assert.deepEqual(errors,[]); assert.deepEqual(forbidden,[]);
    fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:true,checks,errors,forbidden,realModelCalls:0,realLibraryWrites:0},null,2));
  } catch(error) {
    await page.screenshot({path:path.join(output,'failure.png')});
    fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:false,error:String(error.stack||error),checks,errors,forbidden},null,2));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
