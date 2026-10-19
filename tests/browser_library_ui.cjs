const { chromium } = require(process.env.EZREAD_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict'), path = require('node:path'), fs = require('node:fs');
const origin = process.env.EZREAD_TEST_URL, output = process.env.EZREAD_TEST_OUTPUT;
const edge = process.env.EZREAD_BROWSER || path.join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: edge });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 }, reducedMotion: 'reduce' });
  const report = { status: 'running', layouts: [], errors: [], externalRequests: [] };
  page.on('pageerror', error => report.errors.push(error.message));
  await page.route('**/*', route => {
    if (!route.request().url().startsWith(origin) && !route.request().url().startsWith('data:')) {
      report.externalRequests.push(route.request().url()); return route.abort();
    }
    return route.continue();
  });
  const ready = async () => {
    await page.waitForSelector('#paper-grid.is-laid-out');
    // 强制加载离屏测试封面，避免将浏览器的正常懒加载等待误判为失败。
    await page.locator('.paper-card img').evaluateAll(images => images.forEach(img => { img.loading = 'eager'; }));
    await page.waitForFunction(() => [...document.querySelectorAll('.paper-card img')].every(img => img.complete && img.naturalWidth > 0));
    await page.waitForTimeout(150);
  };
  const resetScroll = () => page.evaluate(() => window.scrollTo(0, 0));
  const sort = async value => {
    await page.locator('#library-sort-trigger').click();
    await page.locator(`#library-sort-menu [data-value="${value}"]`).click();
  };
  try {
    await page.goto(origin); await ready();
    const health = await (await page.request.get(origin + '/api/health')).json();
    assert.equal(path.resolve(health.app_root), path.resolve(process.env.EZREAD_TEST_ROOT));
    assert.ok(path.resolve(health.data_dir).startsWith(path.resolve(output) + path.sep));
    report.health = health;
    assert.equal(await page.locator('.paper-card').count(), 45);
    assert.deepEqual(await page.locator('.paper-card').evaluateAll(nodes => [...new Set(nodes.map(node => [...node.classList].find(name => name.startsWith('tier-'))))].sort()), ['tier-conference', 'tier-important', 'tier-other', 'tier-preprint', 'tier-top']);
    const latest = await (await page.request.get(origin + '/__test/library-state')).json();
    const latestTitle = await page.locator(`.paper-card[data-paper-id="${latest.latestId}"] .card-title`).innerText();
    assert.equal(await page.locator('.recent-title').innerText(), latestTitle);
    assert.match(await page.locator('.recent-position').innerText(), /第 4 页.*8 页/);
    assert.equal(await page.locator('#theme-toggle').count(), 1);
    for (const viewport of [{ width: 1440, height: 1024 }, { width: 960, height: 800 }, { width: 1440, height: 520 }, { width: 1440, height: 1024, font: 22 }]) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.evaluate(font => { changePreference({ ui_font_size: font }, true); }, viewport.font || 16);
      await resetScroll(); await ready();
      const layout = await page.evaluate(() => {
        const rect = node => { const r = node.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }; };
        const cards = [...document.querySelectorAll('.paper-card')].map(card => ({ card: rect(card), cover: rect(card.querySelector('.card-cover')), body: rect(card.querySelector('.card-body')), title: rect(card.querySelector('.card-title')), titleOverflow: card.querySelector('.card-title').scrollHeight > card.querySelector('.card-title').clientHeight + 1 }));
        const view = document.querySelector('#library-view');
        return { cards, contentLeft: rect(view).left + parseFloat(getComputedStyle(view).paddingLeft), columns: new Set(cards.map(item => Math.round(item.card.left))).size, horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1, outerScroll: document.scrollingElement.scrollHeight > innerHeight + 1, scrollHeight: view.scrollHeight, clientHeight: view.clientHeight };
      });
      report.layouts.push({ viewport, ...layout });
      assert.ok(layout.columns >= 2 && layout.columns <= 5);
      assert.ok(Math.abs(layout.cards[0].card.left - layout.contentLeft) <= 1, '卡片应与文献库内容区左侧对齐');
      if (viewport.width === 1440 && !viewport.font) assert.equal(layout.columns, 4, '1440px 文献库应容纳四列卡片，包括滚动条占位');
      if (viewport.width === 960) assert.equal(layout.columns, 2, '窄窗口保留两列完整卡片');
      assert.equal(layout.horizontalOverflow, false, '文献库不能产生页面横向滚动');
      assert.equal(layout.outerScroll, true, '浏览器长列表保留正常纵向滚动；原生外层滚动由宿主另验收');
      const arrows = await page.locator('.library-filter-actions .filter-trigger[data-select]').evaluateAll(buttons => buttons.map(button => {
        const arrow = button.querySelector('.filter-chevron'), box = arrow.getBoundingClientRect(), style = getComputedStyle(button);
        return { display: style.display, width: box.width, height: box.height, transform: getComputedStyle(arrow).transform };
      }));
      assert.equal(arrows.length, 2);
      for (const arrow of arrows) {
        assert.ok(['inline-flex', 'flex'].includes(arrow.display), '筛选箭头必须处于 flex 布局，不能退化成竖线');
        assert.ok(arrow.width > 5 && arrow.height > 5 && arrow.transform !== 'none', '下拉箭头具备实际尺寸和旋转');
      }
      for (const item of layout.cards) {
        assert.ok(item.cover.bottom <= item.body.top + 1, '卡片封面位于正文上方');
        assert.equal(item.titleOverflow, false, '英文标题完整显示');
        assert.ok(item.title.bottom <= item.card.bottom + 1, '标题位于卡片内');
      }
      await page.screenshot({ path: path.join(output, `layout-${viewport.width}-${viewport.height}-${viewport.font || 16}.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 1024 });
    await page.evaluate(async () => { changePreference({ ui_font_size: 16 }, true); await flushPreferences(); });
    await ready(); await resetScroll();
    await page.screenshot({ path: path.join(output, 'library-paper.png') });
    await page.locator('#theme-toggle').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'graphite');
    await page.evaluate(() => flushPreferences());
    await page.waitForFunction(() => !preferenceSaveBusy && Object.keys(pendingPreferences).length === 0);
    await page.reload(); await ready();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'graphite');
    await page.screenshot({ path: path.join(output, 'library-graphite.png') });
    await page.locator('#theme-toggle').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'paper');
    await page.locator('#search').fill('Study 01');
    await page.waitForFunction(() => document.querySelectorAll('.paper-card').length === 1);
    await page.locator('#search').fill(''); await ready();
    await page.locator('[data-filter-type="conference"]').click();
    assert.equal(await page.locator('.paper-card').count(), 9);
    assert.equal(await page.locator('.recent-reading').isVisible(), false);
    await page.locator('[data-filter-type=""]').click(); await ready();
    await page.locator('.collection-nav').filter({ hasText: '界面与损伤' }).click();
    assert.equal(await page.locator('.paper-card').count(), 15);
    await page.locator('[data-view="all"]').click(); await ready();
    for (const value of ['recent:desc', 'recent:asc', 'imported:desc', 'imported:asc', 'year:desc', 'year:asc']) {
      await sort(value);
      const papers = await page.evaluate(() => state.papers);
      const ids = await page.locator('.paper-card').evaluateAll(nodes => nodes.map(node => node.dataset.paperId));
      const [field, direction] = value.split(':'), byId = new Map(papers.map(paper => [paper.id, paper]));
      const values = ids.map(id => { const p = byId.get(id); return field === 'year' ? Number(p.year) || 0 : Date.parse(field === 'recent' ? p.last_read : p.created_at || p.imported_at) || 0; });
      let missing = false;
      for (let index = 0; index < values.length; index++) {
        if (!values[index]) { missing = true; continue; }
        assert.equal(missing, false, '无日期记录在排序末尾');
        if (index && values[index - 1]) assert.ok(direction === 'asc' ? values[index - 1] <= values[index] : values[index - 1] >= values[index]);
      }
      assert.equal(await page.locator('#sort').inputValue(), value.split(':')[0]);
      assert.equal(await page.locator('#sort-direction').inputValue(), value.split(':')[1]);
    }
    await sort('recent:desc'); await ready();
    await page.mouse.move(1100, 700); await page.mouse.wheel(0, 550);
    await page.waitForTimeout(250);
    const scrollBeforeDialog = await page.evaluate(() => window.scrollY);
    assert.ok(scrollBeforeDialog > 0, '卡片区域滚轮移动文献列表');
    await resetScroll();
    await page.locator('.card-more').first().click();
    const menu = page.locator('#card-context-menu');
    for (const label of ['论文简介', '论文对话', '多选', '复制 PDF', '在文件夹显示', '永久删除']) assert.equal(await menu.getByRole('menuitem', { name: label, exact: true }).count(), 1);
    assert.match(await menu.innerText(), /翻译/);
    assert.equal(await menu.getByRole('menuitem').count(), 8, '包含独立模型配置入口');
    assert.equal(await menu.locator('use[href="#i-settings"]').count(), 1);
    await menu.getByRole('menuitem', { name: '多选', exact: true }).click();
    assert.equal(await page.locator('#bulkbar').isVisible(), true);
    assert.equal(await page.locator('.card-select[aria-pressed="true"]').count(), 1);
    await page.locator('#bulkbar').getByRole('button', { name: '取消', exact: true }).click(); await ready();
    const detailCard = page.locator('.card-more').nth(24);
    await detailCard.scrollIntoViewIfNeeded();
    const detailScroll = await page.evaluate(() => window.scrollY);
    assert.ok(detailScroll > 0, '在非零滚动位置检查弹窗恢复');
    await detailCard.click();
    await menu.getByRole('menuitem', { name: '论文简介', exact: true }).click();
    await page.waitForSelector('#detail-dialog[open]');
    await page.getByRole('button', { name: '关闭论文简介', exact: true }).click();
    assert.equal(await page.evaluate(() => window.scrollY), detailScroll, '关闭弹窗恢复文献列表位置');
    await resetScroll();
    await page.locator('.recent-resume').click();
    await page.waitForSelector('#reader-dialog[open]');
    await page.waitForFunction(() => state.readerPage === 4);
    assert.equal(await page.locator('#reader-page-input').inputValue(), '4');
    await page.waitForFunction(() => [...document.querySelectorAll('#original-scroll canvas')].some(canvas => canvas.width > 0 && canvas.height > 0));
    await page.locator('.workspace > .reader-tabs .reader-tab-library').click();
    await page.waitForSelector('#reader-dialog[open]', { state: 'hidden' });
    await page.locator('.recent-resume').click();
    await page.waitForSelector('#reader-dialog[open]');
    await page.waitForFunction(() => state.readerPage === 4);
    await page.locator('.workspace > .reader-tabs .reader-tab-library').click();
    await page.waitForSelector('#reader-dialog[open]', { state: 'hidden' });
    // 验证创建、取消与删除，以及刷新后论文和本机文件仍可用。
    await resetScroll();
    const createCollection = async name => {
      await page.locator('#new-collection').click();
      await page.locator('#collection-name').fill(name);
      await page.locator('#collection-form button[type="submit"]').click();
      await page.waitForSelector('#collection-dialog[open]', { state: 'hidden' });
    };
    const emptyName = '空合集 / 删除? #验证';
    await createCollection(emptyName);
    const emptyDelete = page.getByRole('button', { name: `删除合集「${emptyName}」`, exact: true });
    page.once('dialog', dialog => dialog.dismiss());
    await emptyDelete.click();
    assert.equal(await emptyDelete.count(), 1, '取消时保留合集');
    page.once('dialog', dialog => dialog.accept());
    await emptyDelete.click();
    await page.waitForFunction(() => state.view === 'all' && !allCollections().includes('空合集 / 删除? #验证'));
    await ready();
    const collectionName = '界面与损伤';
    const members = await page.evaluate(name => state.papers.filter(p => p.collection === name).map(p => p.id), collectionName);
    const beforePaper = await (await page.request.get(`${origin}/api/papers/${members[0]}`)).json();
    await page.setViewportSize({ width: 960, height: 800 });
    await page.locator('.collection-nav').filter({ hasText: collectionName }).click();
    const deleteButton = page.getByRole('button', { name: `删除合集「${collectionName}」`, exact: true });
    await deleteButton.focus();
    await page.screenshot({ path: path.join(output, 'collection-delete-narrow.png') });
    page.once('dialog', async dialog => {
      assert.match(dialog.message(), /论文、文件、译文和笔记都会保留/);
      await dialog.accept();
    });
    await deleteButton.press('Enter');
    await page.waitForFunction(() => state.view === 'all' && !allCollections().includes('界面与损伤'));
    await page.reload(); await ready();
    assert.equal(await page.locator('.paper-card').count(), 45);
    assert.equal(await page.getByRole('button', { name: `删除合集「${collectionName}」`, exact: true }).count(), 0);
    assert.equal(await page.locator('.collection-nav').filter({ hasText: '机器人感知' }).count(), 1);
    const papersAfter = await (await page.request.get(origin + '/api/papers')).json();
    assert.ok(papersAfter.papers.filter(p => members.includes(p.id)).every(p => p.collection === ''));
    const afterPaper = await (await page.request.get(`${origin}/api/papers/${members[0]}`)).json();
    for (const key of ['notes', 'blocks', 'reader_state', 'pdf_layout_translation']) assert.deepEqual(afterPaper[key], beforePaper[key]);
    assert.equal((await page.request.get(`${origin}/media/${members[0]}/original.pdf`)).status(), 200);
    report.collectionDeletion = { empty: true, cancelled: true, populated: true, keyboard: true, persisted: true, papersPreserved: 45 };
    const fixture = await (await page.request.get(origin + '/__test/library-state')).json();
    assert.deepEqual(fixture.forbiddenCalls, []);
    assert.deepEqual(report.errors, []); assert.deepEqual(report.externalRequests, []);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed'; report.error = error.stack;
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    fs.writeFileSync(path.join(output, 'failure.html'), await page.content());
    process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(output, 'browser-result.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ status: report.status, error: report.error, layouts: report.layouts.length }));
    await browser.close();
  }
})();
