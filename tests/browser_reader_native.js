// 仅由隔离 WebView2 验证服务加载；夹具和模型均为合成数据。
(async () => {
  const assert = (value, message) => { if (!value) throw Error(message); };
  const pid = '0123456789abcdef', checks = [];
  const post = (path, body = {}) => fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const wait = async (predicate, message) => {
    for(let i=0;i<200;i++) {if(predicate())return;await new Promise(resolve=>setTimeout(resolve,25));}
    throw Error(message);
  };
  try {
    await post('/__test/native-progress',{event:'driver-started'});
    await wait(()=>!state.loading,'Library did not load');
    const before=(await (await fetch('/__test/state')).json()).doc;
    localStorage.setItem(`ezread-reader-block:${pid}`,JSON.stringify({value:'legacy draft'}));
    localStorage.setItem(`ezread-reader-text-action:${pid}`,JSON.stringify({value:'legacy revision draft'}));
    await openReader(pid);
    await wait(()=>document.querySelector('.reader-page-section .reader-pdf-page')?.dataset.pdfState==='loaded','Original PDF did not load');
    const layout=document.querySelector('#reader-layout'),original=document.querySelector('.original-pane');
    assert(Math.abs(layout.clientWidth-original.getBoundingClientRect().width)<1,'Original PDF must occupy the full width');
    assert(getComputedStyle(document.querySelector('.translation-pane')).display==='none','Untranslated pane must be hidden');
    assert(getComputedStyle(document.querySelector('#reader-divider')).display==='none','Untranslated divider must be hidden');
    assert(!document.querySelector('.reader-source-text,.translation-block,.block-overlay,[data-text-language]'),'Retired HTML reader is present');
    checks.push('full-width original PDF and retired controls absent');
    await post('/__test/publish-pdf'); await poll();
    await wait(()=>document.querySelector('.reader-pdf-layout-page .reader-pdf-page')?.dataset.pdfState==='loaded','Completed translated PDF did not open automatically');
    assert(!layout.classList.contains('reader-original-only'),'PDF completion did not switch to dual pane');
    const span=document.querySelector('.reader-pdf-layout-page .textLayer span'),range=document.createRange();range.selectNodeContents(span);
    window.getSelection().removeAllRanges();window.getSelection().addRange(range);
    assert(window.getSelection().toString().includes('Synthetic PDF'),'Native PDF text cannot be selected');
    await poll();assert(span.isConnected,'Polling replaced selected PDF text');window.getSelection().removeAllRanges();
    checks.push('automatic dual PDF completion and native text selection');
    toggleReaderNotes(true);
    const note=document.querySelector('#reader-notes-text');note.value='Native PDF reader note';note.dispatchEvent(new Event('input',{bubbles:true}));await readerSaveNotes();toggleReaderNotes(false);
    setReaderPage(2,true);
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const captured=readerCapturePosition();await saveReadPage();await closeReader();
    await wait(()=>readerPdf.documents.size===0&&readerPdf.panes.size===0,'Closing leaked PDF workers');
    await openReader(pid);assert(state.readerPage===captured.page,`Saved page was not restored: captured=${captured.page}, reopened=${state.readerPage}`);assert(document.querySelectorAll('.reader-pdf-layout-page').length===2,'Existing PDF translation was not opened');
    await wait(()=>[...readerPdf.panes.values()].every(p=>p.entries.some(e=>e.visible&&e.drawn===e.target&&e.drawn)&&p.entries.filter(e=>e.active).every(e=>e.drawn===e.target&&e.drawn)),'Reopened PDF did not finish rendering');
    const entry=readerPdf.panes.get('layout').entries.find(e=>e.visible),canvas=entry.canvas,beforeImage=canvas.toDataURL(),beforeWidth=canvas.width,text=entry.layer.firstChild;
    const pdfPage=await entry.pane.record.document.getPage(entry.number),render=pdfPage.render.bind(pdfPage);
    let finish,partialConnected;
    pdfPage.render=options=>{
      pdfPage.render=render;
      const buffer=options.canvasContext.canvas;
      options.canvasContext.fillStyle='#ff00ff';options.canvasContext.fillRect(0,0,buffer.width,buffer.height);
      partialConnected=buffer.isConnected;
      let task;
      return {promise:new Promise(resolve=>{finish=resolve;}).then(()=>{task=render(options);return task.promise;}),cancel(){task?.cancel();}};
    };
    const previousZoom=state.settings.reader_zoom||100;
    readerSetTextZoom(previousZoom===100?125:100);
    await wait(()=>finish,'Native PDF redraw did not start');
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    assert(partialConnected===false&&canvas.width===beforeWidth&&canvas.toDataURL()===beforeImage&&text.isConnected,'Native redraw exposed an incomplete or cleared PDF page');
    finish();
    await wait(()=>entry.drawn===entry.target&&entry.canvas.width!==beforeWidth,'Native redraw did not commit the finished raster');
    readerSetTextZoom(previousZoom);
    await wait(()=>entry.drawn===entry.target&&entry.canvas.width===beforeWidth,'Native PDF zoom was not restored');
    checks.push('native PDF redraw retains existing pixels and selectable text until the offscreen raster is complete');
    assert(JSON.parse(localStorage.getItem(`ezread-reader-block:${pid}`)).value==='legacy draft','Legacy paragraph draft was changed');
    assert(JSON.parse(localStorage.getItem(`ezread-reader-text-action:${pid}`)).value==='legacy revision draft','Legacy selection draft was changed');
    const after=(await (await fetch('/__test/state')).json()).doc;
    for(const key of ['blocks','text_annotations','text_revision_history'])assert(JSON.stringify(before[key])===JSON.stringify(after[key]),'Historical data changed: '+key);
    assert(after.notes==='Native PDF reader note','Notes were not saved');
    checks.push('notes, page, legacy drafts, historical data and close cleanup');
    await closeReader();await post('/__test/native-results',{checks,realModelCalls:0,realLibraryWrites:0});
  } catch(error) {
    await post('/__test/native-results',{error:String(error.stack||error),checks});
  }
})();
