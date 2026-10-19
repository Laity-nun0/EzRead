"""Loopback HTTP contract; delegate business operations to the application context."""
from __future__ import annotations
from .context import ApplicationContext
from . import journal_catalogue
import contextlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler


def handler_for(app: ApplicationContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = f'EzRead/{app.VERSION}'

        def log_message(self, fmt, *args):
            if '/api/status' not in str(args) and '/api/papers' not in str(args):
                print(fmt % args, flush=True)

        def safe_origin(self):
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{app.PORT}', f'localhost:{app.PORT}'):
                return False
            origin = self.headers.get('Origin')
            return (not origin or origin in (f'http://127.0.0.1:{app.PORT}', f'http://localhost:{app.PORT}')) and self.headers.get('Sec-Fetch-Site') != 'cross-site'

        def send_json(self, value, code=200):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # AbortController intentionally disconnects superseded requests.
                self.close_connection = True

        def read_json(self):
            size = int(self.headers.get('Content-Length', 0))
            if size > 4 * 1024 * 1024:
                raise ValueError('提交内容过大。')
            if 'application/json' not in self.headers.get('Content-Type', ''):
                raise ValueError('请求必须使用 JSON。')
            body = self.rfile.read(size)
            value = json.loads(body or b'{}')
            if not isinstance(value, dict):
                raise ValueError('无效的请求。')
            return value

        def stream_paper_chat(self, pid, doc, data):
            import paper_ai
            question=data.get('question','')
            if not isinstance(question,str) or not 1<=len(question.strip())<=4000:
                raise ValueError('请输入 1–4000 字的问题。')
            # Reject a duplicate immediately, before sending streaming headers.
            lock=paper_ai.paper_lock(pid)
            if not lock.acquire(blocking=False):
                self.send_json({'error':'这篇论文正在回答，请等待本轮完成。'},409)
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type','application/x-ndjson; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Connection','close')
                self.end_headers()
                self.close_connection=True
                connected=True
                def emit(event):
                    nonlocal connected
                    if not connected: return
                    try:
                        self.wfile.write((json.dumps(event,ensure_ascii=False)+'\n').encode('utf-8'))
                        self.wfile.flush()
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                        connected=False
                try:
                    result=paper_ai.ask(app.db,pid,doc,question,data_dir=app.DATA,api_client=app.AI_API,
                                        on_progress=emit,cancel_event=app.SHUTDOWN)
                    emit({'type':'completed',**result})
                except Exception as exc:
                    emit({'type':'error','error':str(exc),'code':getattr(exc,'code','session')})
            finally:
                lock.release()

        def send_file(self, path, attachment=None):
            import mimetypes
            if not path.is_file():
                self.send_json({'error': '文件不存在。'}, 404)
                return
            self.send_response(200)
            # Windows registry mappings must not prevent scripts loading with nosniff.
            content_type = {'.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
                            '.wasm': 'application/wasm'}.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(path.stat().st_size))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if attachment:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(attachment))
            self.end_headers()
            with path.open('rb') as f:
                shutil.copyfileobj(f, self.wfile)

        def do_GET(self):
            if not self.safe_origin():
                self.send_json({'error': '仅允许本机访问。'}, 403)
                return
            try:
                parts = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(parts.path)
                if app.SHUTDOWN.is_set() and path != '/api/health':
                    self.send_json({'error': '应用正在关闭，任务已停止。'}, 503)
                    return
                if path == '/api/selection-session':
                    self.send_json(app.SELECTION_SESSION.snapshot())
                elif path == '/api/health':
                    self.send_json({'app_root': str(app.ROOT), 'data_dir': str(app.DATA), 'pid': os.getpid(), 'version': app.VERSION,
                                    'instance_id': app.INSTANCE_ID, 'lifecycle': 1, 'closing': app.SHUTDOWN.is_set()})
                elif path == '/api/pdf-layout':
                    import pdf_layout_engine
                    self.send_json(pdf_layout_engine.availability(app.ROOT))
                elif path == '/api/translation-api':
                    from . import api_settings
                    self.send_json(api_settings.public_settings(app.DATA))
                elif path == '/api/papers':
                    docs = app.all_docs()
                    collections = set(app.settings().get('collections', [])) | {d.get('collection') for d in docs if d.get('collection')}
                    catalogue = journal_catalogue.snapshot(app.DATA)
                    self.send_json({'papers': [app.public_doc(d) for d in docs], 'collections': sorted(collections),
                                    'journal_catalogue_revision': catalogue['revision'], 'journal_catalogue_error': catalogue['error']})
                elif path in ('/api/ai/status', '/api/status'):
                    from . import ai_status
                    self.send_json(ai_status.snapshot(app))
                elif path == '/api/usage':
                    self.send_json(app.API_USAGE.snapshot())
                elif path == '/api/settings':
                    self.send_json(app.settings())
                elif path == '/api/journal-catalogue':
                    self.send_json(journal_catalogue.snapshot(app.DATA))
                elif re.fullmatch(r'/api/journal-catalogue/proposals/[a-f0-9]{24}', path):
                    self.send_json(journal_catalogue.proposal(app.DATA, path.rsplit('/', 1)[-1]))
                elif path == '/api/models':
                    from . import ai_status
                    self.send_json(ai_status.models(app.DATA))
                elif path == '/api/trash':
                    self.send_json({'papers': [app.public_doc(d) for d in app.all_docs(True) if d.get('deleted')]})
                elif path == '/api/export':
                    export = app.DATA / 'tmp' / f'EzRead-backup-{int(time.time())}-{secrets.token_hex(3)}.zip'
                    snapshot = app.DATA / 'tmp' / f'backup-{secrets.token_hex(5)}.sqlite3'
                    with app.LOCK, app.db() as source, contextlib.closing(sqlite3.connect(snapshot)) as target:
                        source.backup(target)
                        docs = app.all_docs(include_deleted=True)
                    with zipfile.ZipFile(export, 'w', zipfile.ZIP_DEFLATED) as z:
                        z.write(snapshot, 'data/library.sqlite3')
                        z.writestr('library.json', json.dumps({'version': app.VERSION, 'papers': docs, 'settings': app.settings()}, ensure_ascii=False, indent=2))
                        z.writestr('data/journal_tiers.csv', journal_catalogue._encoded(journal_catalogue.snapshot(app.DATA)['rows']))
                        for file in app.LIBRARY.rglob('*'):
                            if (file.is_file() and 'api-diagnostics' not in file.relative_to(app.LIBRARY).parts
                                    and file.suffix.lower() in ('.pdf', '.jpg', '.png', '.json')):
                                z.write(file, 'data/library/' + file.relative_to(app.LIBRARY).as_posix())
                        z.writestr('RESTORE.txt', f'关闭 EzRead 窗口与对应后台服务后，将本备份中 data 文件夹的完整内容复制到文献库数据目录：\n{app.DATA}\n恢复前先完整备份目标数据目录，不直接合并两份数据库。安装版默认库在 %LOCALAPPDATA%\\EzRead\\data，源码版默认在程序目录的 data；设置中的“选择目录并迁移”可更改位置，EZREAD_DATA_DIR 可显式覆盖。library.json 可用于其他软件读取。')
                    snapshot.unlink(missing_ok=True)
                    try:
                        self.send_file(export, 'EzRead-backup.zip')
                    finally:
                        export.unlink(missing_ok=True)
                elif re.fullmatch(r'/api/papers/[a-f0-9]{16}/translation-versions', path):
                    doc = app.get_doc(path.split('/')[-2])
                    current = app.version_metadata(app.translation_snapshot(doc))
                    current.update(id='current', current=True)
                    self.send_json({'versions': [current] + [app.version_metadata(v) for v in reversed(doc.get('translation_versions', []))],
                                    'drafts': [app.version_metadata(v) for v in reversed(doc.get('translation_drafts', []))]})
                elif re.fullmatch(r'/api/papers/[a-f0-9]{16}/ai', path):
                    import paper_ai
                    pid = path.split('/')[-2]
                    app.get_doc(pid)
                    raw_generation = urllib.parse.parse_qs(parts.query).get('generation', [None])[0]
                    generation = int(raw_generation) if raw_generation is not None and raw_generation.isdigit() else None
                    with app.LOCK, app.db() as con:
                        result = (paper_ai.history(con, pid, generation) if raw_generation is not None
                                  else paper_ai.conversation(con, pid, app.DATA))
                        from . import api_settings
                        result['chat_models'] = api_settings.configured_chat_models(app.DATA)
                        if 'chat_config' not in result:
                            try:
                                result['chat_config'] = paper_ai.chat_config(con, pid, app.DATA)
                                api_settings.get_api_key(app.DATA, result['chat_config'])
                                result['chat_ready'] = True
                            except ValueError:
                                result['chat_ready'] = False
                        self.send_json(result)
                elif re.fullmatch(r'/api/papers/[a-f0-9]{16}/selection-progress', path):
                    query = urllib.parse.parse_qs(parts.query)
                    data = {key: query.get(key, [None])[0] for key in ('client_id', 'request_id')}
                    self.send_json(app.SELECTION_REQUESTS.snapshot(path.split('/')[-2], data))
                elif re.fullmatch(r'/api/papers/[a-f0-9]{16}', path):
                    self.send_json({'paper': app.public_doc(app.get_doc(path.split('/')[-1]), full=True)})
                elif path.startswith('/media/'):
                    # 延迟渲染与发送文件共用删除锁，防止删除后重新创建资源目录。
                    with app.FILE_ACTION_LOCK:
                        components = path.split('/')
                        if len(components) < 4 or not re.fullmatch(r'[a-f0-9]{16}', components[2]):
                            raise ValueError('无效文件路径。')
                        pid, relative = components[2], '/'.join(components[3:])
                        app.get_doc(pid)
                        folder = (app.LIBRARY / pid).resolve()
                        target = (folder / relative).resolve()
                        if not target.is_relative_to(folder):
                            raise ValueError('无效文件路径。')
                        if relative.startswith('pdf-layout/'):
                            from . import pdf_translations
                            self.send_file(pdf_translations.media_file(app, pid, relative))
                            return
                        match = re.fullmatch(r'page-(\d+)\.jpg', relative)
                        if match and not target.exists():
                            import pdf_tools
                            number = int(match.group(1))
                            if not 1 <= number <= len(app.get_doc(pid).get('pages', [])):
                                raise ValueError('页码超出范围。')
                            source = folder / 'original.pdf'
                            if not source.is_file():
                                self.send_json({'error': '原 PDF 已缺失，请重新导入。'}, 404)
                                return
                            pdf_tools.render_page(source, number, target)
                        self.send_file(target)
                else:
                    relative = 'index.html' if path in ('/', '/index.html') else path.lstrip('/')
                    if relative.startswith('static/'):
                        relative = relative[7:]
                    target = (app.STATIC / relative).resolve()
                    if not target.is_relative_to(app.STATIC.resolve()):
                        raise ValueError('无效文件路径。')
                    self.send_file(target)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self.send_json({'error': str(exc)}, 500)

        def do_POST(self):
            if not self.safe_origin():
                self.send_json({'error': '拒绝跨站请求。'}, 403)
                return
            try:
                path = urllib.parse.urlparse(self.path).path
                if path == '/api/shutdown':
                    result = app.prepare_shutdown(self.read_json())
                    self.send_json(result)
                    threading.Thread(target=self.server.shutdown, daemon=True, name='ezread-shutdown').start()
                    return
                if app.SHUTDOWN.is_set():
                    self.send_json({'error': '应用正在关闭，任务已停止。'}, 503)
                    return
                if path == '/api/desktop-attach':
                    self.send_json(app.attach_desktop(self.server, self.read_json()))
                    return
                if path == '/api/selection-session':
                    if self.read_json():
                        raise ValueError('翻译对话准备不接受正文或其他参数。')
                    self.send_json(app.SELECTION_SESSION.start(app), 202)
                    return
                if path == '/api/journal-catalogue/proposals':
                    self.send_json(journal_catalogue.start_proposal(app, self.read_json().get('instruction')), 202)
                    return
                text_match = re.fullmatch(r'/api/papers/([a-f0-9]{16})/text-actions', path)
                if text_match:
                    from . import text_actions
                    from codex_bridge import TranslationError
                    pid = text_match.group(1)
                    data = self.read_json()
                    try:
                        if data.get('action') == 'cancel_translation':
                            self.send_json(app.SELECTION_REQUESTS.cancel(pid, data))
                        elif data.get('action') == 'translate_word':
                            self.send_json(text_actions.translate_word(app, pid, data))
                        elif data.get('action') in {'translate', 'translate_sentence'}:
                            self.send_json(text_actions.translate(app, pid, data))
                        else:
                            doc = text_actions.retranslate(app, pid, data) if data.get('action') == 'retranslate' else text_actions.apply(app, pid, data)
                            self.send_json({'paper': app.public_doc(doc, True)})
                    except text_actions.TextConflict as exc:
                        self.send_json({'error': str(exc)}, 409)
                    except TranslationError as exc:
                        code = 409 if exc.code == 'cancelled' else 408 if exc.code == 'timeout' else 502
                        self.send_json({'error': str(exc), 'code': exc.code}, code)
                    return
                file_match = re.fullmatch(r'/api/papers/([a-f0-9]{16})/(copy-pdf|reveal-pdf)', path)
                if file_match:
                    self.read_json()
                    self.send_json(app.paper_file_action(*file_match.groups()))
                    return
                ai_match = re.fullmatch(r'/api/papers/([a-f0-9]{16})/ai/(chat|chat-stream|chat-model|open|model|highlight)', path)
                if ai_match:
                    import paper_ai
                    pid, action = ai_match.groups()
                    doc = app.get_doc(pid)
                    data = self.read_json()
                    if not isinstance(data, dict):
                        raise ValueError('请求内容无效。')
                    if action == 'chat-model':
                        if set(data) - {'model','thinking','reasoning_effort'}:
                            raise ValueError('问答模型配置包含未知字段。')
                        lock = paper_ai.paper_lock(pid)
                        if not lock.acquire(blocking=False):
                            self.send_json({'error': '这篇论文正在回答，请等待本轮完成。'}, 409)
                            return
                        try:
                            with app.LOCK, app.db() as con:
                                result = paper_ai.switch_chat_model(con, pid, app.DATA, data)
                            from . import api_settings
                            result['chat_models'] = api_settings.configured_chat_models(app.DATA)
                            self.send_json(result)
                        finally:
                            lock.release()
                    elif action == 'model':
                        raise ValueError('模型选择入口已停用，请在设置中配置 DeepSeek 翻译与问答。')
                    elif action == 'open':
                        if set(data)-{'generation'}:
                            raise ValueError('打开对话的请求包含未知字段。')
                        # 历史消息与线程编号保留在本机，不再从应用启动外部 Codex。
                        with app.LOCK, app.db() as con:
                            paper_ai.history(con,pid,data.get('generation'))
                        self.send_json({'error': '外部 Codex 打开入口已停用，请在论文对话版本中查看完整历史。'}, 410)
                    elif action in ('chat','chat-stream'):
                        if set(data) - {'question'}:
                            raise ValueError('问答请求包含未知字段。')
                        if action=='chat-stream':
                            self.stream_paper_chat(pid,doc,data)
                        else:
                            result = paper_ai.ask(app.db,pid,doc,data.get('question',''),
                                data_dir=app.DATA,api_client=app.AI_API,cancel_event=app.SHUTDOWN)
                            self.send_json(result)
                    else:
                        import paper_structure
                        block_id, selected = data.get('block_id'), data.get('text')
                        source = paper_structure.selection_source(doc, block_id, data.get('source_ids'))
                        if not isinstance(selected,str) or not 1<=len(selected.strip())<=1200:
                            raise ValueError('每次请选择不超过 1200 字符的连续正文。')
                        normalize = lambda s: ' '.join(s.split())
                        if normalize(selected) not in normalize(source):
                            raise ValueError('选中文字与该论文段落不一致。')
                        from .selection_api import translate_selection
                        result = translate_selection(app, selected, paper_id=pid, cancel_event=app.SHUTDOWN)
                        self.send_json({'translation': result})
                    return
                if path == '/api/import':
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 200 * 1024 * 1024:
                        raise ValueError('一次导入总大小请控制在 200 MB 以内。')
                    mime = self.headers.get('Content-Type', '')
                    if not mime.startswith('multipart/form-data'):
                        raise ValueError('请通过文件选择器上传 PDF。')
                    body = self.rfile.read(size)
                    message = BytesParser(policy=email_policy).parsebytes(('Content-Type: ' + mime + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
                    papers, errors = [], []
                    for part in message.iter_parts():
                        filename = part.get_filename()
                        if not filename:
                            continue
                        try:
                            if not filename.lower().endswith('.pdf'):
                                raise ValueError('仅支持 PDF 文件。')
                            papers.append(app.import_pdf(part.get_payload(decode=True), filename))
                        except Exception as exc:
                            errors.append({'filename': filename, 'error': str(exc)})
                    self.send_json({'papers': papers, 'errors': errors})
                    return
                if re.fullmatch(r'/api/papers/[a-f0-9]{16}/team', path):
                    self.send_json({'error': '作者与团队查询功能已取消。'}, 410)
                    return
                if re.fullmatch(r'/api/papers/[a-f0-9]{16}/(?:translate|pause)', path):
                    self.send_json({'error': '旧正文全文翻译已停用，请使用 PDF 保留版式翻译。'}, 410)
                    return
                match = re.fullmatch(r'/api/papers/([a-f0-9]{16})/(metadata-enrich|structure|pdf-translate|pdf-pause|pdf-restore|pdf-resume-draft|summarize|cover|restore|translation-restore|translation-resume-draft)', path)
                if not match:
                    self.send_json({'error': '接口不存在。'}, 404)
                    return
                pid, action = match.groups()
                data = self.read_json()
                if action == 'metadata-enrich':
                    app.enrich_publication(pid, data)
                elif action == 'structure':
                    app.schedule_structure(pid)
                elif action in ('pdf-translate', 'summarize'):
                    app.enqueue(pid, 'pdf_translate' if action == 'pdf-translate' else action,
                                data if action == 'pdf-translate' else None)
                elif action == 'pdf-pause':
                    if data:
                        raise ValueError('暂停版式翻译请求包含未知字段。')
                    app.get_doc(pid)
                    with app.LOCK:
                        app.RESUME_REQUESTED.discard((pid, 'pdf_translate'))
                        app.CANCEL.setdefault((pid, 'pdf_translate'), threading.Event()).set()
                        app.update_doc(pid, lambda d: d.setdefault('pdf_layout_translation', {}).update(status='paused', stage='翻译已暂停', error=''))
                elif action == 'pdf-restore':
                    if set(data) != {'version_id'}:
                        raise ValueError('版式译文恢复请求无效。')
                    app.restore_pdf_translation(pid, data.get('version_id'))
                elif action == 'pdf-resume-draft':
                    if set(data) != {'draft_id'}:
                        raise ValueError('版式翻译草稿恢复请求无效。')
                    app.restore_pdf_translation_draft(pid, data.get('draft_id'))
                elif action == 'restore':
                    app.update_doc(pid, lambda d: d.update(deleted=False))
                elif action == 'translation-restore':
                    app.restore_translation(pid, data.get('version_id'))
                elif action == 'translation-resume-draft':
                    app.restore_translation_draft(pid, data.get('draft_id'))
                elif action == 'cover':
                    doc = app.get_doc(pid)
                    figure = next((f for f in doc.get('figures', []) if f['id'] == data.get('figure_id')), None)
                    if not figure:
                        raise ValueError('请先选择已提取的论文图片。')
                    if 'bbox' in data:
                        import pdf_tools
                        cover = pdf_tools.cover_crop_figure(app.LIBRARY / pid, figure, data['bbox'])
                    else:
                        cover = figure['path']
                    app.update_doc(pid, lambda d: d.update(cover=str(cover)))
                self.send_json({'paper': app.public_doc(app.get_doc(pid), True)})
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self.send_json({'error': str(exc)}, 500)

        def do_PUT(self):
            if urllib.parse.urlparse(self.path).path != '/api/translation-api':
                self.send_json({'error': '接口不存在。'}, 404)
                return
            self.do_PATCH()

        def do_PATCH(self):
            if not self.safe_origin():
                self.send_json({'error': '拒绝跨站请求。'}, 403)
                return
            if app.SHUTDOWN.is_set():
                self.send_json({'error': '应用正在关闭，任务已停止。'}, 503)
                return
            try:
                path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
                data = self.read_json()
                if path == '/api/translation-api':
                    from . import api_settings
                    allowed = {'purpose', 'api_key', 'clear_key', 'thinking', 'reasoning_effort'}
                    if data.get('purpose') == 'chat':
                        allowed.add('model')
                    if data.get('purpose') not in ('translation', 'chat') or set(data) - allowed:
                        raise ValueError('请选择翻译或问答用途；当前仅支持固定 DeepSeek 配置。')
                    self.send_json(api_settings.save_settings(app.DATA, data))
                    return
                if path == '/api/journal-catalogue':
                    try:
                        result = journal_catalogue.apply_changes(app.DATA, data.get('changes'), data.get('revision'))
                    except journal_catalogue.CatalogueConflict as exc:
                        self.send_json({'error': str(exc)}, 409)
                        return
                    self.send_json(result)
                    return
                if path == '/api/settings':
                    data = app.validate_settings(data)
                    with app.LOCK, app.db() as con:
                        for k, v in data.items():
                            if k in app.DEFAULT_SETTINGS:
                                con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (k, json.dumps(v)))
                    self.send_json(app.settings())
                    return
                match = re.fullmatch(r'/api/papers/([a-f0-9]{16})(?:/blocks/([^/]+))?', path)
                if not match:
                    raise ValueError('接口不存在。')
                pid, block_id = match.groups()
                if block_id:
                    for key in ('translation', 'note'):
                        if key in data and not isinstance(data[key], str):
                            raise ValueError('译文与批注必须是文字。')
                    if 'highlight' in data and not isinstance(data['highlight'], bool):
                        raise ValueError('高亮状态必须为布尔值。')
                    def apply_block(doc):
                        block = next((b for b in doc['blocks'] if b['id'] == block_id), None)
                        if not block:
                            raise ValueError('段落不存在。')
                        for k in ('translation', 'highlight', 'note'):
                            if k in data:
                                block[k] = data[k]
                        if 'translation' in data:
                            block['translation_manually_edited'] = True
                            block['translation_modified_at'] = app.now()
                    doc = app.update_doc(pid, apply_block)
                else:
                    retired_metrics = {'jif', 'jif_year', 'quartiles', 'metrics_source', 'conference_rankings'}
                    if retired_metrics.intersection(data):
                        raise ValueError('已取消影响因子、分区和会议评级的编辑。')
                    allowed = {'title', 'title_zh', 'summary', 'journal', 'journal_abbr', 'year', 'authors', 'tags', 'collection', 'favorite', 'notes', 'read_page', 'reader_state', 'last_read', 'doi', 'paper_type', 'conference_name', 'conference_abbr', 'conference_track', 'publication_date', 'volume', 'issue', 'page_range', 'article_number'}
                    fields = {k: v for k, v in data.items() if k in allowed}
                    for k in ('title', 'title_zh', 'summary', 'journal', 'journal_abbr', 'collection', 'notes', 'last_read', 'doi', 'paper_type', 'conference_name', 'conference_abbr', 'conference_track', 'publication_date', 'volume', 'issue', 'page_range', 'article_number'):
                        if k in fields and not isinstance(fields[k], str):
                            raise ValueError(k + ' 必须是文字。')
                    if 'favorite' in fields and not isinstance(fields['favorite'], bool):
                        raise ValueError('收藏状态必须为布尔值。')
                    if fields.get('publication_date') and not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', fields['publication_date']):
                        raise ValueError('发表时间请使用 YYYY、YYYY-MM 或 YYYY-MM-DD。')
                    if fields.get('publication_date'):
                        from datetime import date
                        pieces = [int(s) for s in fields['publication_date'].split('-')]
                        date(*[pieces[i] if i < len(pieces) else 1 for i in range(3)])
                        if fields.get('year') not in (None, '', pieces[0]):
                            raise ValueError('发表时间与年份不一致，请核对。')
                        fields['year'] = pieces[0]
                    if 'collection' in fields:
                        fields['collection'] = fields['collection'].strip()
                        if len(fields['collection']) > 100 or re.search(r'[\x00-\x1f]', fields['collection']):
                            raise ValueError('合集名称须为不超过 100 字的单行文字。')
                    if 'paper_type' in fields and fields['paper_type'] not in ('journal', 'conference', 'preprint', 'other'):
                        raise ValueError('无效文献类型。')
                    if 'conference_track' in fields and fields['conference_track'] not in ('main', 'workshop', 'unknown'):
                        raise ValueError('无效会议论文类型。')
                    for k in ('year',):
                        if k in fields:
                            if isinstance(fields[k], bool):
                                raise ValueError(k + ' 数值无效。')
                            if fields[k] not in (None, '') and not (isinstance(fields[k], int) or (isinstance(fields[k], str) and re.fullmatch(r'\d{4}', fields[k]))):
                                raise ValueError(k + ' 必须是整数年份。')
                            fields[k] = None if fields[k] in (None, '') else int(fields[k])
                    for k in ('year',):
                        if fields.get(k) is not None and not app.valid_year(fields[k]):
                            raise ValueError(k + ' 年份无效。')
                    for k in ('tags', 'authors'):
                        if k in fields and not isinstance(fields[k], list):
                            raise ValueError(k + ' 必须是列表。')
                    for k in ('tags', 'authors'):
                        if k in fields and any(not isinstance(v, str) for v in fields[k]):
                            raise ValueError(k + ' 必须是文字列表。')
                    if 'reader_state' in fields:
                        fields['reader_state'] = app.validate_reader_state(fields['reader_state'], app.get_doc(pid))
                        fields['read_page'] = fields['reader_state']['page']
                    if 'read_page' in fields:
                        fields['read_page'] = max(1, min(int(fields['read_page']), len(app.get_doc(pid)['pages'])))
                        fields['last_read'] = app.now()
                    doc = app.update_doc(pid, lambda d: app.patch_paper_fields(d, fields))
                self.send_json({'paper': app.public_doc(doc, True)})
            except (ValueError, TypeError, KeyError) as exc:
                self.send_json({'error': str(exc)}, 400)
            except Exception as exc:
                traceback.print_exc()
                self.send_json({'error': str(exc)}, 500)

        def do_DELETE(self):
            if not self.safe_origin():
                self.send_json({'error': '拒绝跨站请求。'}, 403)
                return
            try:
                path = urllib.parse.urlparse(self.path).path
                if path.startswith('/api/collections/'):
                    name = urllib.parse.unquote(path[len('/api/collections/'):])
                    self.send_json(app.delete_collection(name))
                    return
                match = re.fullmatch(r'/api/papers/([a-f0-9]{16})', path)
                if not match:
                    raise ValueError('接口不存在。')
                pid = match.group(1)
                self.send_json(app.delete_paper(pid))
            except ValueError as exc:
                self.send_json({'error': str(exc)}, 400)

    return Handler
