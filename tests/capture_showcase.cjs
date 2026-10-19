'use strict';
const { chromium } = require(process.env.EZREAD_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict'), path = require('node:path'), fs = require('node:fs');
const origin = process.env.EZREAD_TEST_URL, output = process.env.EZREAD_TEST_OUTPUT;
const root = process.env.EZREAD_TEST_ROOT;
const report = { status: 'running', syntheticOnly: true, errors: [], consoleErrors: [], httpErrors: [], externalRequests: [], forbiddenRequests: [], captures: [] };
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') report.consoleErrors.push(message.text()); });
  page.on('response', response => { if (response.status() >= 400) report.httpErrors.push({ url: response.url(), status: response.status() }); });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (!['data:', 'blob:'].includes(url.protocol) && url.origin !== origin) {
      report.externalRequests.push(url.href); return route.abort();
    }
    if (route.request().method() === 'POST' && /\/(?:ai\/chat(?:-stream)?|pdf-translate|summarize|metadata-enrich|structure|translate|cover)$/.test(url.pathname)) {
      report.forbiddenRequests.push(url.pathname); return route.abort();
    }
    return route.continue();
  });
  const ready = async () => {
    await page.waitForSelector('#paper-grid.is-laid-out');
    await page.locator('.paper-card img').evaluateAll(images => images.forEach(image => { image.loading = 'eager'; }));
    await page.waitForFunction(() => [...document.querySelectorAll('.paper-card img')].every(image => image.complete && image.naturalWidth > 0));
    await page.waitForTimeout(250);
  };
  const capture = async name => {
    const visibleText = await page.locator('body').innerText();
    assert.doesNotMatch(visibleText, /[A-Z]:\\|16560|api[_-]?key\s*[:=]\s*sk-/i);
    const local = path.join(output, name);
    await page.screenshot({ path: local, animations: 'disabled' });
    fs.copyFileSync(local, path.join(root, 'docs/images', name));
    report.captures.push(name);
  };
  try {
    await page.goto(origin); await ready();
    const health = await (await page.request.get(origin + '/api/health')).json();
    assert.equal(path.resolve(health.app_root), path.resolve(root));
    assert.equal(path.resolve(health.data_dir), path.resolve(process.env.EZREAD_TEST_DATA));
    report.health = health;
    assert.equal(await page.locator('.paper-card').count(), 16);
    await capture('library.png');
    await page.locator('#theme-toggle').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'graphite');
    await page.waitForTimeout(250); await capture('library-dark.png');
    await page.locator('#theme-toggle').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'paper');
    await page.locator('.recent-resume').click();
    await page.waitForSelector('#reader-dialog[open]');
    await page.waitForFunction(() => ['original-scroll', 'translation-scroll'].every(id => {
      const root = document.getElementById(id);
      return [...root.querySelectorAll('canvas')].some(canvas => canvas.width > 0 && canvas.height > 0)
        && root.querySelectorAll('.textLayer span').length > 20;
    }));
    await page.waitForTimeout(400);
    report.pdf = await page.evaluate(() => Object.fromEntries(['original-scroll', 'translation-scroll'].map(id => [id, {
      canvases: [...document.getElementById(id).querySelectorAll('canvas')].filter(canvas => canvas.width > 0).length,
      textSpans: document.getElementById(id).querySelectorAll('.textLayer span').length,
      text: document.getElementById(id).innerText.slice(0, 160)
    }])));
    assert.match(report.pdf['translation-scroll'].text, /合成演示/);
    await page.evaluate(() => { document.getElementById('original-scroll').scrollTop = 0; document.getElementById('translation-scroll').scrollTop = 0; });
    await page.waitForTimeout(200);
    await capture('reader.png');
    await page.locator('.workspace > .reader-tabs .reader-tab-library').click();
    await page.locator('#settings-button').click();
    await page.getByRole('tab', { name: '期刊等级', exact: true }).click();
    await page.waitForSelector('.journal-catalogue-row');
    await page.waitForTimeout(200); await capture('journal-tiers.png');
    await page.getByRole('tab', { name: '翻译', exact: true }).click();
    await page.waitForSelector('#settings-panel-translation input[type=password]');
    assert.equal(await page.locator('#settings-panel-translation input[type=password]').inputValue(), '');
    await page.waitForTimeout(250); await capture('translation-settings.png');
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.consoleErrors, []);
    assert.deepEqual(report.httpErrors, []);
    assert.deepEqual(report.externalRequests, []);
    assert.deepEqual(report.forbiddenRequests, []);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed'; report.error = error.stack;
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(output, 'browser-result.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({ status: report.status, error: report.error, captures: report.captures }));
    await browser.close();
  }
})();
