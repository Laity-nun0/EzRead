"""隔离验证纯 PDF 阅读、自动双栏和保存恢复，不访问真实文献库或模型。"""
import json
import os
from pathlib import Path
import shutil
import secrets
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import codex_models
import codex_usage
import codex_bridge
import paper_ai
import paper_structure
from ezread.selection_output import diagnostic

PID = '0123456789abcdef'
calls = []
preparations = []
slow_model = False
progress_mode = ''


def write_fixture_pdf(path, width=595, count=2):
    """真正的矢量 PDF，供浏览器与原生窗口核验清晰度和文字选择。"""
    font_id = count + 3
    kids = ' '.join(f'{number} 0 R' for number in range(3, font_id))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Count {count} /Kids [{kids}] >>'.encode()]
    for number in range(font_id + 1, font_id + count + 1):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} 842] '
                       f'/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {number} 0 R >>'.encode())
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    for number in range(1, count + 1):
        stream = (f'BT /F1 18 Tf 60 700 Td (Synthetic PDF selectable text page {number}) Tj '
                  '0 -40 Td (First sentence. Another sentence.) Tj ET '
                  '0.2 0.4 0.6 RG 0.7 w 60 500 300 80 re S 60 520 m 360 520 l S').encode()
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    raw, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f'{number} 0 obj\n'.encode() + value + b'\nendobj\n')
    start = len(raw)
    raw.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    raw.extend(b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:]))
    raw.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    path.write_bytes(raw)


def main():
    native = '--native' in sys.argv
    run_id = secrets.token_hex(8)
    verification_session = uuid.uuid4().hex
    native_events = []
    work = ROOT / 'work'; work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reader-text-browser-', dir=work, ignore_cleanup_errors=True) as folder:
        data = Path(folder).resolve()
        assert data.is_relative_to(work.resolve())
        server.DATA, server.LIBRARY = data, data / 'library'
        server.init_db()
        server.codex_status = lambda **kwargs: {'authenticated': True, 'available': True}
        codex_models.get = lambda **kwargs: {'models': [], 'status': 'available'}
        codex_models.resolve_config = lambda *args, **kwargs: {'model': 'mock-model', 'reasoning_effort': 'low'}
        codex_usage.snapshot = codex_usage.get = lambda **kwargs: {'status': 'unavailable', 'buckets': []}
        def translate(text, **kwargs):
            record = {'text': text, 'target_language': kwargs.get('target_language'), 'cancelled': False}
            calls.append(record)
            if progress_mode:
                progress = kwargs.get('on_progress')
                event = kwargs.get('cancel_event')
                for value in (
                        {'text': '模型已接收请求，等待输出…'},
                        {'kind': 'diagnostic', 'text': diagnostic('Network is not available. Reconnecting... 1/5')},
                        {'kind': 'output', 'text': '模拟正在输出的部分译文'},
                        {'kind': 'diagnostic', 'text': diagnostic('Stream disconnected before completion; retrying 2/5')}):
                    if progress: progress(value)
                    if event is not None and event.wait(.7):
                        record['cancelled'] = True
                        raise codex_bridge.TranslationError('此翻译请求已取消。', code='cancelled')
                if progress_mode == 'failure':
                    raise codex_bridge.TranslationError('无法连接 Codex 服务，请检查网络。', code='network')
                if progress: progress({'text': '翻译完成。'})
            if slow_model:
                event = kwargs.get('cancel_event')
                for _ in range(100):
                    if event is not None and event.wait(.05):
                        record['cancelled'] = True
                        raise codex_bridge.TranslationError('此翻译请求已取消。', code='cancelled')
            return '模拟划线译文：' + text
        codex_bridge.translate_selection = translate
        codex_bridge.prepare_selection = lambda **kwargs: preparations.append({'model': kwargs.get('model')})
        paper_ai.translate_batch = lambda *args, **kwargs: ('mock-thread', {b['id']: '重译：' + b['text'] for b in args[3]})
        doc = {'id': PID, 'hash': 'synthetic-original-pdf', 'title': 'Reader interaction fixture', 'authors': ['Test Author'],
               'journal': 'Synthetic Journal', 'year': 2026, 'deleted': False, 'translation': {'status': 'completed'},
               'pages': [{'number': n, 'width': 595, 'height': 842, 'image': 'page-001.png'} for n in (1, 2)], 'figures': [],
               'blocks': [
                   {'id': 'one', 'kind': 'paragraph', 'page': 1, 'text': 'First 😀 sentence. Another sentence.', 'translation': '第一😀句。第二句话保留。', 'bbox': [.1,.2,.85,.25]},
                   {'id': 'two', 'kind': 'paragraph', 'page': 1, 'text': 'Second paragraph has selectable text.', 'translation': '另一个自然段可以自由选择文字。', 'bbox': [.1,.4,.85,.45]},
                   {'id': 'three', 'kind': 'paragraph', 'page': 1, 'text': 'Legacy note and highlight stay available.', 'translation': '旧批注和高亮继续保留。', 'note': '原有批注', 'highlight': True, 'bbox': [.1,.6,.85,.65]},
                   {'id': 'four', 'kind': 'paragraph', 'page': 1, 'text': 'A long selected passage starts here. ' + 'Annotation geometry follows natural wrapping as the reader changes width and font size. ' * 5,
                    'translation': '批注应当跟随自然换行，不改变文字节点。' * 8, 'bbox': [.1,.7,.85,.8]},
                   {'id': 'five', 'kind': 'paragraph', 'page': 1, 'text': 'The same cross-paragraph annotation continues here.',
                    'translation': '这条跨段批注在下一段继续。', 'bbox': [.1,.8,.85,.9]}]}
        doc['text_annotations'] = [{'id': 'legacy-annotation', 'kind': 'note',
                                    'quote': 'First', 'note': '旧范围批注',
                                    'ranges': [{'block_id': 'one', 'language': 'original',
                                                'start': 0, 'end': 5, 'quote': 'First',
                                                'text_hash': '', 'prefix': '', 'suffix': ''}]}]
        doc['text_revision_history'] = [{'id': 'legacy-revision', 'changes': [
            {'block_id': 'one', 'language': 'original', 'before': 'Original', 'after': 'First'}]}]
        doc['blocks'][0]['text_override'] = 'First 😀 sentence. Another sentence.'
        doc['reading_structure'] = {'version': paper_structure.VERSION, 'source_fingerprint': paper_structure.fingerprint(doc), 'status': 'ready',
            'sections': [{'id': 'intro', 'role': 'introduction', 'label': 'Introduction', 'entries': [{'id': b['id'], 'role': 'paragraph', 'source_ids': [b['id']]} for b in doc['blocks']]}]}
        server.put_doc(doc)
        from PIL import Image
        library = server.LIBRARY / PID; library.mkdir(parents=True)
        write_fixture_pdf(library / 'original.pdf')
        write_fixture_pdf(library / 'fixture-mono.pdf')
        write_fixture_pdf(library / 'fixture-dual.pdf', width=1190)
        write_fixture_pdf(library / 'fixture-many.pdf', count=6)
        Image.new('RGB', (595, 842), '#f7f7ef').save(library / 'page-001.png')
        Image.new('RGB', (595, 842), '#f7f7ef').save(library / 'page-001.jpg')
        Image.new('RGB', (11, 14), 'white').save(library / 'tiny-formula.png')
        artifact_id = '1234567890abcdef'
        artifact_dir = library / 'pdf-layout' / artifact_id
        artifact_dir.mkdir(parents=True)
        shutil.copy2(library / 'fixture-mono.pdf', artifact_dir / 'mono.pdf')
        shutil.copy2(library / 'fixture-dual.pdf', artifact_dir / 'dual.pdf')
        class Handler(server.Handler):
            def log_message(self, *args): pass
            def do_GET(self):
                if native and self.path == '/api/health':
                    # The fixture server is owned by this verifier, never by
                    # the desktop host's production backend lifetime manager.
                    self.send_json({'synthetic': True}); return
                if native and self.path == '/':
                    native_events.append({'event': 'document-requested'})
                    raw = (ROOT / 'static/index.html').read_text(encoding='utf-8').replace('</head>', '<script src="/__test/native-driver.js" defer></script></head>').encode('utf-8')
                    self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw); return
                if native and self.path == '/__test/native-driver.js':
                    native_events.append({'event': 'driver-requested'})
                    self.send_file(ROOT / 'tests/browser_reader_native.js'); return
                if native and self.path == '/__test/tiny-formula.png':
                    self.send_file(library / 'tiny-formula.png'); return
                if self.path in ('/__test/mono.pdf', '/__test/dual.pdf'):
                    self.send_file(library / ('fixture-mono.pdf' if self.path.endswith('/mono.pdf') else 'fixture-dual.pdf')); return
                if self.path == '/__test/many-pages.pdf':
                    self.send_file(library / 'fixture-many.pdf'); return
                if self.path == '/__test/state':
                    self.send_json({'calls': calls, 'preparations': preparations, 'doc': server.get_doc(PID)}); return
                super().do_GET()
            def do_POST(self):
                global slow_model, progress_mode
                if native and self.path in ('/api/desktop-attach', '/api/shutdown'):
                    self.send_json({'error': 'Synthetic server lifetime belongs to the verifier.'}, 409); return
                if self.path == '/__test/publish-pdf':
                    artifact = {'id': artifact_id, 'created_at': '2026-01-01',
                                'config': {'model': 'mock-model', 'reasoning_effort': 'low'},
                                'page_count': 2, 'quality': 'passed', 'warnings': [],
                                'mono': 'mono.pdf', 'dual': 'dual.pdf',
                                'pages': doc['pages']}
                    def publish(d):
                        d['pdf_layout_current'] = artifact
                        d['pdf_layout_versions'] = [artifact]
                        d['pdf_layout_translation'] = {'status': 'completed'}
                    server.update_doc(PID, publish)
                    self.send_json({'ok': True}); return
                if self.path == '/__test/model-delay':
                    slow_model = bool(self.read_json().get('enabled'))
                    self.send_json({'ok': True}); return
                if self.path == '/__test/model-progress':
                    progress_mode = self.read_json().get('mode', '')
                    self.send_json({'ok': True}); return
                if self.path == '/__test/native-results':
                    (work / 'reader-text-webview2-verification.json').write_text(json.dumps({**self.read_json(), 'run_id': run_id},ensure_ascii=False,indent=2),encoding='utf-8')
                    self.send_json({'ok':True}); return
                if self.path == '/__test/native-progress':
                    native_events.append(self.read_json())
                    self.send_json({'ok': True}); return
                if self.path == '/__test/append':
                    server.update_doc(PID, lambda d: d['blocks'][0].update(translation=d['blocks'][0]['translation'] + ' 后台更新。'))
                    self.send_json({'ok': True}); return
                super().do_POST()
        listener = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.PORT = listener.server_port
        thread = threading.Thread(target=listener.serve_forever, daemon=True); thread.start()
        environment = os.environ.copy()
        environment['EZREAD_TEST_URL'] = f'http://127.0.0.1:{listener.server_port}'
        environment['EZREAD_TEST_ROOT'] = str(ROOT)
        runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        node = environment.get('EZREAD_NODE') or shutil.which('node') or str(runtime / 'bin/node.exe')
        environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
        try:
            if native:
                report = work / f'reader-text-native-host-{run_id}.json'
                evidence = {'run_id': run_id, 'verification_session': verification_session,
                            'events': native_events, 'profile': str(data), 'status': 'running'}
                command = [str(ROOT/'desktop/bin/EzRead.Desktop.exe'),'--app-root',str(ROOT),'--data-dir',str(data),'--url',environment['EZREAD_TEST_URL'],'--verification-session',verification_session,'--smoke-test',str(report)]
                try:
                    # SmokeTest also exercises monitor geometry; offscreen frame
                    # throttling can exceed the old 45-second process deadline.
                    result = subprocess.run(command,cwd=ROOT,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    evidence['returncode'] = result.returncode
                    if result.returncode: raise RuntimeError(f'Native smoke-test exited with code {result.returncode}.')
                    value = json.loads((work / 'reader-text-webview2-verification.json').read_text(encoding='utf-8'))
                    if value.get('run_id') != run_id: raise RuntimeError('Native reader verification did not finish in this run.')
                    if 'error' in value: raise RuntimeError(value['error'])
                    log = (data / 'desktop.log').read_text(encoding='utf-8-sig')
                    if f'app-id=EzRead.Desktop.Verification.{verification_session} taskbar=False' not in log:
                        raise RuntimeError('Native verification identity/taskbar isolation was not confirmed.')
                    if not any('taskbar-remove hwnd=' in line and ' hr=0' in line for line in log.splitlines()) or 'closed' not in log:
                        raise RuntimeError('Native verification did not confirm taskbar removal and window closure.')
                    if not report.is_file(): raise RuntimeError('Native smoke-test report was not produced.')
                    host = json.loads(report.read_text(encoding='utf-8-sig'))
                    if host.get('error') or host.get('page', {}).get('errors'):
                        raise RuntimeError(f'Native smoke-test JavaScript/host error: {host}')
                    shutil.copy2(report, work / 'reader-text-native-host.json')
                    evidence.update(taskbar_hidden=True, taskbar_removed=True, window_closed=True,
                                    process_id=host.get('processId'), host_report=str(report))
                    evidence['status'] = 'passed'
                    # Windows redirected stdout may still be GBK; JSON escapes
                    # preserve selected emoji without making a passed test fail.
                    print(json.dumps(value,ensure_ascii=True))
                except subprocess.TimeoutExpired:
                    evidence['status'] = 'process-timeout'
                    raise
                except Exception as exc:
                    evidence.update(status='failed', error=str(exc))
                    raise
                finally:
                    desktop_log = data / 'desktop.log'
                    if desktop_log.is_file():
                        target = work / f'reader-text-native-{run_id}.desktop.log'
                        shutil.copy2(desktop_log, target)
                        evidence['desktop_log'] = str(target)
                    (work / f'reader-text-native-{run_id}.diagnostics.json').write_text(
                        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                result = subprocess.run([node, str(ROOT / 'tests/browser_reader_text.cjs')], env=environment, cwd=ROOT)
            if result.returncode: raise SystemExit(result.returncode)
        finally:
            listener.shutdown(); listener.server_close(); thread.join()
    print('PDF reader verification completed; real library/model calls: 0.')


if __name__ == '__main__': main()
