// Publication search is initiated only by the user's affirmative action.
const PUBLICATION_LABELS = { journal: "期刊 / 出版来源", conference_name: "会议", authors: "作者", doi: "DOI",
  publication_date: "发表时间", year: "年份", page_range: "发表页码", article_number: "文章编号", volume: "卷", issue: "期", online_date: "在线发表", print_date: "出版日期" };
function publicationMissing(p) {
  if (Array.isArray(p.metadata_enrichment?.missing)) return p.metadata_enrichment.missing;
  const missing = [];
  if (!p.journal && !p.conference_name) missing.push({ key: "journal", label: PUBLICATION_LABELS.journal });
  if (!p.publication_date && !p.year) missing.push({ key: "publication_date", label: PUBLICATION_LABELS.publication_date });
  if (!p.doi || /^10\.48550\/arxiv\./i.test(p.doi)) missing.push({ key: "doi", label: "正式发表 DOI" });
  if (!p.page_range && !p.article_number) missing.push({ key: "page_range", label: "发表页码 / 文章编号" });
  if (!list(p.authors).length) missing.push({ key: "authors", label: PUBLICATION_LABELS.authors });
  return missing;
}
function publicationVerified(p) {
  const saved = p.metadata_enrichment || {};
  return Boolean(saved.checked_at && saved.status === "complete" && !publicationMissing(p).length
    && !saved.error && !saved.conflicts?.length);
}
function openPublicationLookup(p) {
  const dialog = $("#publication-dialog");
  if (dialog.open) return Promise.resolve();
  return new Promise(resolve => {
    let current = p, busy = false, searched = false, settled = false;
    const content = $("#publication-content");
    async function finish() {
      if (busy || settled) return;
      settled = true;
      if (!searched && publicationMissing(current).length) {
        try {
          const data = await api(`/api/papers/${encodeURIComponent(current.id)}/metadata-enrich`, { method: "POST", body: { consent: false } });
          if (data.paper) updatePaper(data.paper);
        } catch (error) { toast(error.message, "error"); }
      }
      dialog.close(); renderLibrary(); resolve();
    }
    const onCancel = event => { event.preventDefault(); void finish(); };
    dialog.addEventListener("cancel", onCancel);
    dialog.addEventListener("close", () => { dialog.removeEventListener("cancel", onCancel); resolve(); }, { once: true });
    function render() {
      const missing = publicationMissing(current), saved = current.metadata_enrichment || {};
      const ok = publicationVerified(current);
      const review = !missing.length;
      const heading = busy ? "正在核对出版信息" : ok ? "出版信息已核对" : missing.length ? "补全出版信息" : "核对出版信息";
      const description = busy ? "正在查询并核对论文标题与作者，请稍候。" : ok ? "已核对正式出版记录，结果已保存在本机。" : saved.error || (missing.length ? "这份 PDF 的部分出版信息未能可靠识别。是否联网查询并补全论文卡片？" : "当前出版信息已完整。是否联网核对正式出版记录？");
      const fields = review ? Object.keys(PUBLICATION_LABELS).filter(key => current[key]) : [];
      const details = fields.length ? el("dl", { class: "publication-result" }, fields.map(key => [el("dt", {}, PUBLICATION_LABELS[key]), el("dd", {}, asText(current[key]))])) : null;
      const cancel = button(ok ? "完成" : "暂不搜索", () => void finish(), "secondary", "", { disabled: busy, autofocus: !busy });
      const search = button(busy ? "正在检索…" : missing.length ? "联网补全" : "联网核对", () => void searchNow(), "primary", "", { disabled: busy });
      const children = [
        el("div", { class: "publication-heading" }, el("div", { class: "publication-symbol", "aria-hidden": "true" }, icon(busy ? "clock" : ok ? "check" : "book")),
          iconButton("close", "关闭出版信息补全", () => void finish())),
        el("h2", { id: "publication-dialog-title" }, heading),
        el("p", { class: "publication-paper-title" }, titleOf(current)),
        el("p", { id: "publication-dialog-description", class: "publication-description", role: "status", "aria-live": "polite" }, description),
        missing.length && !busy ? el("div", { class: "publication-missing", "aria-label": "未识别的出版信息" }, missing.map(item => el("span", {}, item.label))) : null,
        busy ? el("div", { class: "publication-search-progress", "aria-hidden": "true" }, el("span", { class: "spinner" })) : details,
        !busy && !ok ? el("p", { class: "publication-privacy" }, "仅向 Crossref / arXiv 查询标题、作者或文献编号，不上传 PDF 全文。暂不搜索也可以继续阅读，之后可在论文信息中补全。") : null,
        el("div", { class: "form-actions publication-actions" }, cancel, ok ? null : search)
      ];
      content.replaceChildren(...children.filter(node => node !== null && node !== undefined && node !== false));
      $(".publication-heading button", content).disabled = busy;
      if (!busy) cancel.focus({ preventScroll: true });
    }
    async function searchNow() {
      if (busy || settled) return;
      busy = true; searched = true; render();
      try {
        const data = await api(`/api/papers/${encodeURIComponent(current.id)}/metadata-enrich`, { method: "POST", body: { consent: true } });
        if (!data.paper) throw new Error("未收到出版信息结果，请重试。");
        current = normalizeDetail(data); updatePaper(current); renderLibrary();
        if (state.detail?.id === current.id && $("#detail-dialog").open) renderDetail();
      } catch (error) {
        current = { ...current, metadata_enrichment: { ...current.metadata_enrichment, status: "error", error: error.message } };
      } finally { busy = false; }
      await finish();
      const saved = current.metadata_enrichment || {};
      if (saved.status === "error") toast(saved.error || "出版信息查询未完成，可在论文信息中重试。", "error", 9000);
      else if (publicationMissing(current).length || saved.conflicts?.length || saved.error) {
        toast("已保存可补全的出版信息，仍有差异或缺项，可在论文信息中核对。", "warn", 9000);
      } else toast("出版信息已核对并保存");
    }
    render(); openDialog("#publication-dialog");
  });
}
