"use strict";

// 仅在隔离原生验收服务注入；借用宿主 smoke 的实际窗口尺寸，不提交翻译。
(() => {
  const original = window.openSettings;
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const assert = (value, message) => { if (!value) throw Error(message); };
  const post = body => fetch("/__test/native-results", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let serial = 0;
  window.openSettings = async function (...args) {
    await original.apply(this, args);
    const result = { sequence: ++serial, passed: false };
    try {
      await openPdfLayoutSetup({ id: "native-confirm-fixture", title: "Synthetic native PDF translation confirmation. ".repeat(14), pdf_layout_translation: { status: "idle", glossary_seed: [{ source: "grain", target: "晶粒" }] } });
      window.chrome.webview.postMessage("ezread-fit-viewport");
      // 原生 viewport 定时器为 100ms；等待本次 modal 高度核算，再让布局稳定两帧。
      await new Promise(resolve => setTimeout(resolve, 180));
      await frame(); await frame();
      const dialog = document.querySelector("#translation-dialog");
      const heading = dialog.querySelector(".translation-confirm-heading");
      const footer = dialog.querySelector(".translation-confirm-footer");
      const body = dialog.querySelector(".translation-confirm-body");
      const action = [...footer.querySelectorAll("button")].find(node => node.textContent.includes("开始翻译"));
      const box = node => node.getBoundingClientRect().toJSON();
      const before = { heading: box(heading), footer: box(footer) };
      const visible = rectangle => rectangle.x >= -1 && rectangle.y >= -1 && rectangle.right <= innerWidth + 1 && rectangle.bottom <= innerHeight + 1;
      for (const selector of [".translation-glossary", ".translation-advanced", ".translation-context"]) {
        assert(!dialog.querySelector(selector).open, `${selector} 未默认折叠`);
      }
      assert(dialog.open && visible(box(heading)) && visible(box(action)), "原生可见区内标题或提交按钮不可达");
      assert(dialog.scrollWidth <= dialog.clientWidth + 1, "原生翻译确认发生横向溢出");
      assert(["auto", "scroll"].includes(getComputedStyle(body).overflowY), "正文没有独立滚动区域");
      body.scrollTop = body.scrollHeight;
      await frame();
      assert(Math.abs(box(heading).y - before.heading.y) < 1 && Math.abs(box(footer).y - before.footer.y) < 1, "内部滚动移动了标题或底部操作");
      const glossary = dialog.querySelector(".translation-glossary");
      glossary.open = true;
      dialog.querySelector(".translation-advanced").open = true;
      const originalFont = document.documentElement.style.getPropertyValue("--ui-font-size");
      try {
        document.documentElement.style.setProperty("--ui-font-size", "22px");
        await new Promise(resolve => setTimeout(resolve, 180));
        await frame(); await frame();
        body.scrollTop = body.scrollHeight;
        assert(visible(box(heading)) && visible(box(action)), "原生大字号展开后标题或提交按钮不可达");
        assert(dialog.scrollWidth <= dialog.clientWidth + 1, "原生大字号展开后横向溢出");
        result.largeText = { viewport: { innerWidth, innerHeight }, action: box(action), heading: box(heading) };
      } finally {
        if (originalFont) document.documentElement.style.setProperty("--ui-font-size", originalFont);
        else document.documentElement.style.removeProperty("--ui-font-size");
        glossary.open = false;
        dialog.querySelector(".translation-advanced").open = false;
        await new Promise(resolve => setTimeout(resolve, 180));
        await frame(); await frame();
      }
      const thinking = dialog.querySelector('[aria-label="思考模式"]');
      const strengths = [...dialog.querySelectorAll('input[name="pdf-thinking-effort"]')];
      thinking.click();
      assert(thinking.checked && strengths.find(node => node.checked)?.value === "high", "首次开启思考未默认高强度");
      strengths.find(node => node.value === "max").click();
      assert(strengths.filter(node => node.checked).length === 1 && strengths.find(node => node.checked).value === "max", "原生强度单选不能切换");
      thinking.click();
      assert(dialog.querySelector(".translation-effort").hidden, "关闭思考后强度没有隐藏");
      result.passed = true;
      result.viewport = { innerWidth, innerHeight, devicePixelRatio, visualWidth: visualViewport?.width, visualHeight: visualViewport?.height };
      result.shortViewport = { targetHeight: 540, exercised: innerHeight <= 540, limitation: innerHeight <= 540 ? null : "宿主 smoke 本次实际 viewport 高于 540px，未覆盖目标矮窗口；未伪造 viewport。" };
      result.geometry = { heading: before.heading, footer: before.footer, action: box(action), body: box(body), bodyScrollHeight: body.scrollHeight, bodyClientHeight: body.clientHeight, bodyScrollTop: body.scrollTop };
      result.checks = ["固定标题与底部按钮位于真实可见区", "术语、高级与背景说明默认折叠", "内滚动不移动标题和底部", "展开与22px大字号操作可达且无横向溢出", "思考开关和高/最高单选可用"];
    } catch (error) {
      result.error = String(error.stack || error);
      result.viewport = { innerWidth, innerHeight, devicePixelRatio };
    } finally {
      if (document.querySelector("#translation-dialog").open) closeDialog("#translation-dialog");
      await post(result);
    }
  };
})();
