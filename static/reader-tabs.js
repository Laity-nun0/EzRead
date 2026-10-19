"use strict";

// 会话只保存文献身份；页码、缩放和笔记沿用各篇阅读草稿与服务器保存流程。
const readerTabs = { ids: [], active: null, ready: false };
function readerTabsPersist() {
  if (!readerTabs.ready) return true;
  const value = { ids: readerTabs.ids, active: readerTabs.active };
  if (!readerWriteLocal("tabs", "session", value) || JSON.stringify(readerReadLocal("tabs", "session")) !== JSON.stringify(value)) {
    toast("阅读标签未能保存到本机，请检查存储空间。", "warn");
    return false;
  }
  return true;
}
function readerTabsRender() {
  for (const root of $$(".reader-tabs")) {
    const library = el("button", { type: "button", class: "reader-tab-library", "aria-pressed": String(!readerTabs.active), onclick: () => act(() => closeReader()) }, icon("book"), "文献库");
    const papers = readerTabs.ids.map(id => {
      const paper = state.papers.find(p => p.id === id), title = paper ? titleOf(paper) : id;
      return el("div", { class: `reader-tab${id === readerTabs.active ? " active" : ""}`, dataset: { paperId: id } },
        el("button", { type: "button", class: "reader-tab-title", "aria-label": `阅读：${title}`, "aria-pressed": String(id === readerTabs.active), onclick: () => act(() => openReader(id)) }, title),
        el("button", { type: "button", class: "reader-tab-close", "aria-label": `关闭标签：${title}`, onclick: () => act(() => readerTabsClose(id)) }, icon("close")));
    });
    const content = $(".reader-tab-content", root) || root;
    content.replaceChildren(library, el("div", { class: "reader-tab-list" }, papers));
    const active = $(".reader-tab.active", root), row = $(".reader-tab-list", root);
    if (active && row) row.scrollLeft = Math.max(0, active.offsetLeft + active.offsetWidth - row.clientWidth);
  }
}
function readerTabsOpened(id) {
  readerTabs.ready = true;
  if (!readerTabs.ids.includes(id)) readerTabs.ids.push(id);
  readerTabs.active = id;
  readerTabsPersist(); readerTabsRender();
}
function readerTabsLibrary() {
  readerTabs.active = null;
  readerTabsPersist(); readerTabsRender();
}
async function readerTabsClose(id) {
  if (!readerTabs.ids.includes(id)) return;
  const index = readerTabs.ids.indexOf(id), active = readerTabs.active === id;
  if (active) {
    const remaining = readerTabs.ids.filter(value => value !== id);
    if (remaining.length) {
      const switched = await openReader(remaining[Math.min(index, remaining.length - 1)]);
      if (!switched && readerTabs.active === id) return;
    } else if (!(await closeReader())) return;
  } else if (readerRuntime.openingId === id) {
    ++readerRuntime.opening; readerRuntime.openingId = null;
    $("#reader-content").inert = false;
  }
  readerTabs.ids = readerTabs.ids.filter(value => value !== id);
  readerTabsPersist(); readerTabsRender();
}
function readerTabsDeleted(id) {
  if (readerRuntime.openingId === id) { ++readerRuntime.opening; readerRuntime.openingId = null; $("#reader-content").inert = false; }
  readerTabs.ids = readerTabs.ids.filter(value => value !== id);
  if (readerTabs.active === id) readerTabs.active = null;
  readerTabsPersist(); readerTabsRender();
}
async function readerTabsRestore() {
  if (readerTabs.ready) { readerTabsRender(); return; }
  readerTabs.ready = true;
  const cached = readerReadLocal("tabs", "session"), available = new Set(state.papers.map(p => p.id));
  readerTabs.ids = [...new Set(Array.isArray(cached?.ids) ? cached.ids : [])].filter(id => typeof id === "string" && available.has(id));
  const active = readerTabs.ids.includes(cached?.active) ? cached.active : null;
  readerTabs.active = active;
  readerTabsPersist(); readerTabsRender();
  if (active) {
    const restoring = openReader(active), request = readerRuntime.opening;
    try { await restoring; }
    catch (error) {
      if (request !== readerRuntime.opening) return;
      readerTabsLibrary(); toast(`阅读标签恢复失败：${error.message}`, "warn");
    }
  }
}
