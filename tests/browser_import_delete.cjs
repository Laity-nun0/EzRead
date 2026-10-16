// 由 verify-import-delete.py 启动隔离 HTTP 服务；只使用合成 PDF 和浏览器新 profile。
const {chromium} = require(process.env.EZREAD_PLAYWRIGHT || 'playwright');
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const assert = require('node:assert/strict');
const origin = process.env.EZREAD_TEST_URL, output = process.env.EZREAD_TEST_OUTPUT;
const oldId = process.env.EZREAD_TEST_OLD_ID, fixture = process.env.EZREAD_TEST_PDF;
const orphan = process.env.EZREAD_TEST_ORPHAN === '1';
const edge = process.env.EZREAD_BROWSER || path.join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:edge});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage(), errors = [], forbidden = [], httpErrors = [], checks = [];
  let newId = null;
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response=>{if(response.status()>=400) httpErrors.push({path:new URL(response.url()).pathname,status:response.status()});});
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || /^\/api\/(models|usage|status|selection-session)$/.test(url.pathname) || /\/(chat-stream|summarize|publication-lookup)$/.test(url.pathname)) {
      forbidden.push(url.pathname); return route.abort();
    }
    return route.continue();
  });
  const snapshot = async () => {
    const response = await page.request.get(origin + '/__test/import-delete-state');
    assert.equal(response.status(),200); return response.json();
  };
  const assertOldCleared = async () => {
    const deleted = await snapshot();
    assert.deepEqual(deleted.oldCounts,{papers:0,paper_ai:0,paper_ai_generations:0,paper_ai_messages:0});
    assert(!deleted.oldResourcesExist && !deleted.oldClipboardExists);
    assert.deepEqual(deleted.snapshotKeys,['ezread-sort']);
    const browserDeleted = await page.evaluate(id=>({keys:Object.keys(localStorage).filter(key=>key.startsWith('ezread-reader-') && key.endsWith(`:${id}`)),
      unrelated:localStorage.getItem('ezread-reader-notes:unrelated-fixture'),chatDraft:paperChatRuntime.drafts.has(id),pending:paperChatRuntime.pending.has(id)}),oldId);
    assert.deepEqual(browserDeleted,{keys:[],unrelated:'keep-unrelated',chatDraft:false,pending:false});
    return {deleted,browserDeleted};
  };
  try {
    await page.goto(origin);
    const oldCard = page.locator(`[data-paper-id="${oldId}"]`);
    await oldCard.waitFor();
    assert.match(await oldCard.innerText(),/历史 Codex.*fixture-old-codex/);
    const before = await snapshot();
    assert.deepEqual(before.oldCounts,{papers:1,paper_ai:1,paper_ai_generations:1,paper_ai_messages:1});
    assert.equal(before.oldResourcesExist,!orphan);assert(before.oldClipboardExists);
    if (!orphan) {
      await oldCard.getByRole('button',{name:'对话',exact:true}).click();
      await page.getByText('Synthetic previous Codex answer',{exact:true}).waitFor();
      await page.getByLabel('向论文提问',{exact:true}).fill('Synthetic previous browser chat draft');
      await page.getByRole('button',{name:'文献库',exact:true}).click();
      await page.waitForFunction(()=>!document.querySelector('#reader-dialog').open);
    }
    await page.evaluate(id=>{
      localStorage.setItem(`ezread-reader-notes:${id}`,JSON.stringify({text:'Synthetic local note draft'}));
      localStorage.setItem(`ezread-reader-position:${id}`,JSON.stringify({position:{page:1},pending:true}));
      localStorage.setItem(`ezread-reader-translation-draft:${id}`,JSON.stringify({text:'Synthetic translation draft'}));
      localStorage.setItem('ezread-reader-notes:unrelated-fixture','keep-unrelated');
      paperChatRuntime.drafts.set(id,'Synthetic previous browser chat draft');
    },oldId);
    await page.screenshot({path:path.join(output,orphan?'before-orphan-reimport.png':'before-permanent-delete.png')});
    let cleanup;
    if (!orphan) {
      let confirmation = '';
      page.once('dialog', async dialog=>{confirmation=dialog.message();await dialog.accept();});
      const deletion = page.waitForResponse(response=>response.request().method()==='DELETE' && response.url()===`${origin}/api/papers/${oldId}`);
      await oldCard.getByRole('button',{name:/^永久删除论文：/}).click();
      assert.equal((await deletion).status(),200);
      assert.match(confirmation,/永久删除.*全部会话.*无法在应用内恢复/s);
      await oldCard.waitFor({state:'detached'});
      cleanup = await assertOldCleared();
      await page.screenshot({path:path.join(output,'after-permanent-delete.png')});
      checks.push('UI confirmation permanently deletes all four database tables, library/clipboard resources, desktop snapshot and browser drafts; unrelated key preserved');
    }
    const upload = page.waitForEvent('filechooser');
    await page.getByRole('button',{name:'导入 PDF',exact:true}).click();
    const imported = page.waitForResponse(response=>response.request().method()==='POST' && response.url()===origin+'/api/import');
    await (await upload).setFiles(fixture);
    const importResponse = await imported;
    assert.equal(importResponse.status(),200);
    const result = await importResponse.json();
    assert.deepEqual(result.errors||[],[]);
    assert.equal(result.papers.length,1);
    newId=result.papers[0].id;
    assert.match(newId,/^[a-f0-9]{16}$/);assert.notEqual(newId,oldId);
    assert.equal(Boolean(result.papers[0].duplicate),false);
    if (orphan) assert.equal(result.papers[0].replaced_paper_id,oldId);
    const newCard = page.locator(`[data-paper-id="${newId}"]`);
    await newCard.waitFor();
    const publicationClose = page.getByRole('button',{name:'关闭出版信息补全',exact:true});
    if (await publicationClose.isVisible()) await publicationClose.click();
    await page.locator('#publication-dialog').waitFor({state:'hidden'});
    cleanup = await assertOldCleared();
    if (orphan) {
      assert.equal(await oldCard.count(),0);
      await page.screenshot({path:path.join(output,'after-orphan-reimport.png')});
      checks.push('Orphan reimport reports replaced_paper_id and clears old four-table records, clipboard, desktop snapshot and browser/memory drafts while preserving unrelated key');
    }
    assert.doesNotMatch(await newCard.innerText(),/历史 Codex|fixture-old-codex/);
    const pdf = await page.request.get(`${origin}/media/${newId}/original.pdf`);
    assert.equal(pdf.status(),200);
    const bytes=await pdf.body();
    assert(bytes.subarray(0,8).toString().startsWith('%PDF-'));
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),process.env.EZREAD_TEST_PDF_HASH);
    const aiResponse = await page.request.get(`${origin}/api/papers/${newId}/ai`);
    assert.equal(aiResponse.status(),200);
    const ai=await aiResponse.json();
    assert.deepEqual(ai.messages,[]);assert.equal(ai.thread_id,null);assert.equal(ai.generations.length,1);
    assert.equal(ai.current.generation,1);assert.equal(ai.current.model,null);assert.equal(ai.current.provider,null);
    assert.equal(ai.current.reasoning_effort,null);assert.equal(ai.current.has_chat,false);
    checks.push('Uploading the same PDF creates a different identity; original PDF HTTP 200 and SHA256 identical; empty initial AI generation has no legacy model or threads');
    await page.waitForFunction(()=>document.querySelector('#toasts').childElementCount===0);
    await newCard.getByRole('button',{name:/^本篇 API 模型：/}).click();
    assert.equal(await page.getByLabel('API 模型',{exact:true}).inputValue(),'fixture-api-default');
    assert.equal(await page.locator('select[aria-label="API 供应商"]').inputValue(),'deepseek');
    assert.doesNotMatch(await page.locator('#translation-dialog').innerText(),/历史 Codex|fixture-old-codex/);
    await page.screenshot({path:path.join(output,'reimport-api-defaults.png')});
    await page.locator('#translation-dialog').getByRole('button',{name:'取消',exact:true}).click();
    await newCard.getByRole('button',{name:'对话',exact:true}).click();
    await page.getByText('向论文提问，回答将附论文出处',{exact:true}).waitFor();
    assert.equal(await page.getByLabel('向论文提问',{exact:true}).inputValue(),'');
    await page.waitForSelector('#original-scroll .reader-pdf-page[data-pdf-state="loaded"]');
    const rendered = await page.locator('#original-scroll .reader-pdf-page').evaluate(node=>({state:node.dataset.pdfState,width:node.querySelector('canvas').width,height:node.querySelector('canvas').height,text:node.querySelector('.textLayer').textContent}));
    assert(rendered.width>1 && rendered.height>1);assert.match(rendered.text,/Synthetic import lifecycle paper/);
    await page.screenshot({path:path.join(output,'reimport-readable-pdf-empty-chat.png')});
    checks.push('New model picker uses current API defaults; rendered PDF canvas and selectable synthetic text are readable; new chat is empty without old draft');
    const after=await snapshot();
    assert.equal(after.queued,0);assert.equal(after.structureQueued,0);
    assert.deepEqual(errors,[]);assert.deepEqual(forbidden,[]);
    assert(httpErrors.every(item=>orphan && item.path.startsWith(`/media/${oldId}/`)),'Only deliberately missing old orphan assets may fail HTTP.');
    fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:true,orphan,oldId,newId,replacedPaperId:result.papers[0].replaced_paper_id||null,before,checks,...cleanup,rendered,errors,forbidden,httpErrors,realModelCalls:0,realLibraryWrites:0},null,2));
  } catch(error) {
    await page.screenshot({path:path.join(output,'failure.png')});
    fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:false,orphan,oldId,newId,error:String(error.stack||error),checks,errors,forbidden,httpErrors},null,2));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
