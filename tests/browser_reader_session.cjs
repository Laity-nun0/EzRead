const { chromium } = require(process.env.EZREAD_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const origin = process.env.EZREAD_TEST_URL;
const edge = process.env.EZREAD_BROWSER || path.join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: edge });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  try {
    for (const phase of [1, 2, 3]) {
      await page.request.post(origin + '/__test/phase', { data: { phase } });
      await page.goto(origin);
      let data;
      for (let attempt = 0; attempt < 200; attempt++) {
        data = await (await page.request.get(origin + '/__test/results')).json();
        if (data.failures.length || data.results.some(value => value.phase === phase)) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      assert.deepEqual(data.failures, []);
      assert(data.results.some(value => value.phase === phase), '尚未收到本阶段验收完成结果');
      if (phase === 1) {
        await page.waitForFunction(() => document.querySelector('.reader-page-section[data-page="3"] .reader-pdf-page')?.dataset.pdfState === 'loaded');
        for (const theme of ['paper', 'graphite']) {
          await page.evaluate(theme => changePreference({ theme }, true), theme);
          for (const selector of ['.reader-tab.active .reader-tab-title', '.reader-tab.active .reader-tab-close', '.reader-tab:not(.active) .reader-tab-title']) {
            await page.locator(selector).hover();
            const appearance = await page.locator('.reader-tab').evaluateAll(tabs => tabs.map(tab => {
              const style = getComputedStyle(tab);
              return { active: tab.classList.contains('active'), radius: style.borderRadius, clip: style.overflowX,
                background: style.backgroundColor, color: style.color,
                buttons: [...tab.querySelectorAll('button')].map(button => ({ background: getComputedStyle(button).backgroundColor, color: getComputedStyle(button).color })) };
            }));
            assert.equal(appearance[0].radius, appearance[1].radius, '选中后不得改变标签圆角');
            assert.notEqual(appearance[0].background, appearance[1].background, '应保留选中状态区分');
            for (const tab of appearance) {
              assert.equal(tab.clip, 'hidden', '标签必须裁切内部绘制，避免直角底色覆盖圆角');
              assert(parseFloat(tab.radius) > 0);
              for (const button of tab.buttons) {
                assert.equal(button.background, 'rgba(0, 0, 0, 0)', '标题和关闭按钮应使用整个标签的统一底色');
                assert.equal(button.color, tab.color, '标题和关闭图标应继承同一状态颜色');
              }
            }
          }
          await page.locator('.reader-tab.active .reader-tab-title').click();
          await page.locator('.reader-tabs').screenshot({ path: path.join(process.env.EZREAD_TEST_ROOT, `work/reader-session-browser/tabs-${theme}.png`) });
        }
        await page.screenshot({ path: path.join(process.env.EZREAD_TEST_ROOT, 'work/reader-session-browser/wide.png') });
        await page.setViewportSize({ width: 550, height: 700 });
        const geometry = await page.evaluate(() => {
          const nav = document.querySelector('.workspace > .reader-tabs');
          return { width: nav.getBoundingClientRect().width, viewport: innerWidth,
            bodyOverflow: document.documentElement.scrollWidth - innerWidth,
            tabs: nav.querySelectorAll('.reader-tab').length,
            libraryVisible: nav.querySelector('.reader-tab-library').getBoundingClientRect().right <= innerWidth };
        });
        assert(geometry.width <= geometry.viewport + 1 && geometry.bodyOverflow <= 1 && geometry.tabs === 2 && geometry.libraryVisible, JSON.stringify(geometry));
        await page.screenshot({ path: path.join(process.env.EZREAD_TEST_ROOT, 'work/reader-session-browser/narrow.png') });
        await page.setViewportSize({ width: 1440, height: 1000 });
      }
    }
    assert.deepEqual(errors, []);
    console.log('Browser session tabs and reload restoration passed; no model requests.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
