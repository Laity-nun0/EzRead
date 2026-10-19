// 浏览器和真实 WebView2 共用同一份标签会话行为验收。
(async () => {
  const first = "0000000000000001", second = "0000000000000002";
  const checks = [];
  const assert = (value, message) => { if (!value) throw Error(message); };
  const wait = async (predicate, message) => {
    for (let index = 0; index < 400; index++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw Error(message);
  };
  const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const rect = selector => document.querySelector(selector).getBoundingClientRect();
  const reachable = selector => {
    const node = document.querySelector(selector), bounds = node.getBoundingClientRect();
    assert(bounds.width > 0 && bounds.height > 0 && bounds.left >= 0 && bounds.bottom <= innerHeight + 1,
      `${selector} 不在可见窗口内`);
    assert(node.contains(document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2)),
      `${selector} 被其他界面遮挡`);
  };
  const switchReader = async id => {
    const dialog = document.querySelector('#reader-dialog'), original = state.reader.id;
    const retainedPage = dialog.querySelector('.reader-page-section');
    const bounds = dialog.getBoundingClientRect(), changes = [];
    const observer = new MutationObserver(items => changes.push(...items));
    observer.observe(dialog, { attributes: true, attributeFilter: ['open'] });
    const savedApi = api;
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    api = async (path, ...args) => { if (path === `/api/papers/${id}` && !args.length) await pending; return savedApi(path, ...args); };
    try {
      const opening = openReader(id);
      await frames();
      assert(dialog.open && state.reader.id === original && retainedPage.isConnected, '切换加载期间未保留当前文献');
      release(); await opening; await frames();
      const next = dialog.getBoundingClientRect();
      assert(dialog.open && changes.length === 0, '标签切换关闭或重新打开了外层阅读器');
      assert(Math.abs(next.width - bounds.width) <= 1 && Math.abs(next.height - bounds.height) <= 1, '标签切换导致阅读器整体尺寸跳动');
    } finally { release(); api = savedApi; observer.disconnect(); }
  };
  const post = body => fetch('/__test/result', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let phase;
  try {
    const config = await (await fetch('/__test/phase')).json();
    phase = config.phase;
    await wait(() => !state.loading && typeof readerTabs !== 'undefined' && readerTabs.ready, '文献库或标签会话未初始化');
    if (phase === 1) {
      const nav = document.querySelector('.workspace > .reader-tabs'), theme = document.querySelector('#theme-toggle');
      const before = rect('.reader-tab-library');
      await openReader(first);
      await openReader(first);
      await frames();
      assert(document.querySelectorAll('.reader-tabs').length === 1 && nav === document.querySelector('.reader-tabs') && theme.isConnected,
        '阅读器未复用共享导航与主题按钮');
      const after = rect('.reader-tab-library'), reader = document.querySelector('#reader-dialog');
      assert(!reader.matches(':modal') && reader.parentElement.matches('.workspace'), '阅读器仍阻塞侧边栏');
      assert(Math.abs(after.left - before.left) <= 1 && Math.abs(after.top - before.top) <= 1, '打开文献后导航位置改变');
      assert(rect('#theme-toggle').right <= after.left, '主题按钮未放在文献库左侧');
      assert(rect('#reader-dialog').left >= rect('#sidebar').right - 1 && Math.abs(rect('#reader-dialog').top - rect('.reader-tabs').bottom) <= 1,
        '阅读器覆盖侧边栏或共享导航');
      const queue = rect('.sidebar-bottom [data-view="queue"]'), settings = rect('#settings-button');
      const all = rect('#main-nav [data-view="all"]');
      assert(settings.top >= queue.bottom && Math.abs(queue.left - settings.left) <= 1 && Math.abs(queue.width - settings.width) <= 1,
        '设置和翻译任务应上下排列为同一列');
      assert(Math.abs(queue.left - all.left) <= 1 && Math.abs(queue.width - all.width) <= 1 &&
        getComputedStyle(document.querySelector('#settings-button')).fontSize === getComputedStyle(document.querySelector('#main-nav [data-view="all"]')).fontSize,
        '底部入口应与全部论文采用相同列宽和字号');
      const count = document.querySelector('#queue-count');
      count.textContent = '100'; count.classList.remove('hidden'); await frames();
      assert(count.getBoundingClientRect().right <= queue.right && rect('#settings-button').right <= rect('#sidebar').right,
        '任务数量挤压或遮挡设置入口');
      renderNav();
      for (const selector of ['#theme-toggle', '.reader-tab-library', '#settings-button', '.sidebar-bottom [data-view="queue"]', '#main-nav [data-view="favorites"]']) reachable(selector);
      const oldTheme = state.settings.theme;
      theme.click(); assert(state.settings.theme !== oldTheme, '阅读期间主题按钮失效');
      theme.click(); assert(state.settings.theme === oldTheme, '主题无法切回');
      document.querySelector('#settings-button').click();
      await wait(() => document.querySelector('#settings-dialog').matches(':modal'), '阅读期间设置未打开');
      closeDialog('#settings-dialog'); await frames();
      assert(readerScrollActive() && state.reader.id === first, '关闭设置丢失阅读器');
      document.querySelector('.sidebar-bottom [data-view="queue"]').click();
      await wait(() => document.querySelector('#queue-dialog').matches(':modal'), '阅读期间翻译任务未打开');
      document.querySelector('#queue-close').click(); await frames();
      assert(readerScrollActive() && state.reader.id === first, '关闭翻译任务丢失阅读器');
      checks.push('one stable shared navigation, persistent reachable sidebar, theme toggle and modal settings/tasks preserve reader');
      if (config.native) {
        for (const size of ['short', 'wide']) {
          await fetch('/__test/resize', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ size }) });
          await wait(() => size === 'short' ? innerHeight < 600 : innerHeight > 600, '原生窗口尺寸未更新');
          await new Promise(resolve => setTimeout(resolve, 250)); await frames();
          for (const selector of ['#theme-toggle', '.reader-tab-library', '#settings-button', '.sidebar-bottom [data-view="queue"]']) reachable(selector);
          assert(document.documentElement.scrollWidth <= innerWidth + 1 && document.documentElement.scrollHeight <= innerHeight + 1,
            '阅读器引入外层滚动条');
          document.querySelector('#settings-button').click();
          await wait(() => document.querySelector('#settings-dialog').matches(':modal'), '调整窗口后设置不可用');
          assert(rect('#settings-dialog').top >= 0 && rect('#settings-dialog').bottom <= innerHeight + 1, '设置窗口超出可见区域');
          closeDialog('#settings-dialog'); await frames();
          assert(state.reader.id === first && readerScrollActive(), '原生尺寸变化或设置导致阅读状态丢失');
        }
        checks.push('real native wide and short/narrow window: reachable sidebar/header, no outer scrolling, settings preserve reader');
      }
      assert(!document.querySelector('.reader-back'), '旧返回文献库按钮未移除');
      assert(readerTabs.ids.length === 1, '重复打开产生了重复标签');
      await wait(() => document.querySelector('.reader-page-section .reader-pdf-page')?.dataset.pdfState === 'loaded', '第一篇 PDF 未真实加载');
      readerSetZoom(125); setReaderPage(2, true); await frames();
      toggleReaderNotes(true);
      let notes = document.querySelector('#reader-notes-text');
      notes.value = 'Session note 1'; notes.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('.workspace > .reader-tabs .reader-tab-library').click();
      await wait(() => !readerScrollActive(), '文献库标签未返回文献库');
      assert(readerTabs.ids.length === 1, '返回文献库关闭了文献标签');
      await openReader(second);
      await wait(() => document.querySelector('.reader-page-section .reader-pdf-page')?.dataset.pdfState === 'loaded', '第二篇 PDF 未真实加载');
      readerSetZoom(150); setReaderPage(3, true); await frames();
      toggleReaderNotes(true); notes = document.querySelector('#reader-notes-text');
      notes.value = 'Session note 2'; notes.dispatchEvent(new Event('input', { bubbles: true }));
      await switchReader(first);
      assert(state.readerPage === 2 && Number(state.zoom) === 125, '第一篇页码或缩放被第二篇覆盖');
      assert(readerRuntime.notes.value === 'Session note 1', '切换丢失第一篇未提交笔记');
      await switchReader(second);
      assert(state.readerPage === 3 && Number(state.zoom) === 150, '第二篇页码或缩放未恢复');
      assert(readerRuntime.notes.value === 'Session note 2', '切换丢失第二篇未提交笔记');
      document.querySelector('#main-nav [data-view="favorites"]').click();
      await wait(() => !readerScrollActive() && state.view === 'favorites', '侧栏收藏未保存后返回文献库');
      assert(readerTabs.ids.length === 2, '侧栏导航丢失阅读标签');
      await openReader(second);
      assert(state.readerPage === 3 && readerRuntime.notes.value === 'Session note 2', '侧栏往返丢失阅读状态');
      document.querySelector('#collections-nav .collection-nav').click();
      await wait(() => !readerScrollActive() && state.view === 'collection', '侧栏合集未返回文献库');
      await openReader(second);
      assert(readerTabs.ids.join(',') === [first, second].join(',') && readerTabs.active === second, '标签顺序或活跃标签错误');
      checks.push('two real PDFs, independent pages/zoom/notes, library keeps tabs; switches retain content and modal geometry without open/close mutations');
    } else if (phase === 2) {
      await wait(() => state.reader?.id === second && readerScrollActive(), '重开没有恢复最后活跃文献');
      await frames();
      assert(readerTabs.ids.join(',') === [first, second].join(','), '重开丢失标签顺序');
      assert(state.readerPage === 3 && Number(state.zoom) === 150, '重开没有恢复第二篇页码/缩放');
      assert(readerRuntime.notes.value === 'Session note 2', '重开丢失第二篇笔记');
      await openReader(first);
      assert(state.readerPage === 2 && Number(state.zoom) === 125, '重开后第一篇阅读状态错误');
      await readerTabsClose(second);
      assert(readerTabs.ids.join(',') === first && state.reader.id === first, '关闭后台标签影响当前文献');
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      await wait(() => !readerScrollActive(), 'Escape 未保存并返回文献库');
      assert(readerTabs.ids.length === 1 && readerTabs.active === null, '返回文献库未保留剩余标签');
      checks.push('restart restores order/current paper and saved reading state; background tab closes independently');
    } else if (phase === 3) {
      assert(readerTabs.ids.join(',') === first && readerTabs.active === null && !readerScrollActive(), '重开未恢复关闭前的文献库页面');
      await openReader(first);
      assert(state.readerPage === 2 && Number(state.zoom) === 125, '从恢复标签打开后阅读状态错误');
      await readerTabsClose(first);
      assert(readerTabs.ids.length === 0 && !readerScrollActive(), '关闭最后一个标签未返回文献库');
      const stored = JSON.parse(localStorage.getItem('ezread-reader-tabs:session'));
      assert(stored.ids.length === 0 && stored.active === null, '关闭标签没有持久化');
      checks.push('restart restores library with remaining tabs; closing last tab persists empty session');
    } else throw Error('未知验收阶段');
    await post({ phase, checks });
  } catch (error) {
    await post({ phase, checks, error: String(error.stack || error) });
  }
})();
