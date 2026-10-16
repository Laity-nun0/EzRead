"""在隔离库与真实浏览器中验证 API 设置及确认流程，禁止远端模型与 CLI。"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import codex_models
import paper_ai
import pdf_layout_engine
from ezread import translation_api
from tests.test_pdf_translation_tasks import write_pdf


def verify_native(args, output, environment, data, reports):
    session = uuid.uuid4().hex
    executable = (ROOT / args.executable).resolve()
    if not executable.is_file():
        raise RuntimeError(f'独立桌面构建不存在：{executable}')
    report = output / f'host-{session}.json'
    evidence = {'verification_session': session, 'status': 'running', 'checks': reports, 'profile': str(data),
                'realModelCalls': 0, 'realLibraryWrites': 0}
    try:
        result = subprocess.run(
            [str(executable), '--app-root', str(ROOT), '--data-dir', str(data),
             '--url', environment['EZREAD_TEST_URL'], '--verification-session', session,
             '--smoke-test', str(report)], cwd=ROOT, timeout=120,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        evidence['returncode'] = result.returncode
        if result.returncode:
            raise RuntimeError(f'原生 smoke 返回失败：{result.returncode}')
        if len(reports) < 2 or any(not item.get('passed') for item in reports):
            raise RuntimeError(f'原生翻译窗口检查未全部完成：{reports}')
        widths = [item['viewport']['innerWidth'] for item in reports]
        heights = [item['viewport']['innerHeight'] for item in reports]
        if min(widths) == max(widths) or min(heights) == max(heights):
            raise RuntimeError('未取得不同实际宽度与高度的原生窗口证据。')
        host = json.loads(report.read_text(encoding='utf-8-sig'))
        if host.get('error') or host.get('page', {}).get('errors'):
            raise RuntimeError(f'原生宿主或页面错误：{host}')
        log = (data / 'desktop.log').read_text(encoding='utf-8-sig')
        if f'app-id=EzRead.Desktop.Verification.{session} taskbar=False' not in log:
            raise RuntimeError('未确认独立原生身份与任务栏隐藏。')
        if not any('taskbar-remove hwnd=' in line and ' hr=0' in line for line in log.splitlines()) or 'closed' not in log:
            raise RuntimeError('未确认显式任务栏移除与窗口退出。')
        evidence.update(status='passed', taskbar_hidden=True, taskbar_removed=True,
                        window_closed=True, host_report=str(report), process_id=host.get('processId'))
    except Exception as error:
        evidence.update(status='failed', error=str(error))
        raise
    finally:
        if (data / 'desktop.log').is_file():
            shutil.copy2(data / 'desktop.log', output / f'desktop-{session}.log')
        (output / 'native-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='work/api-translation-verification')
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--executable', default='work/desktop-build/EzRead.Desktop.exe')
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node = os.environ.get('EZREAD_NODE') or shutil.which('node') or str(runtime / 'bin/node.exe')
    profile = (nullcontext(str(output / f'profile-{uuid.uuid4().hex}')) if args.native else
               tempfile.TemporaryDirectory(prefix='api-browser-', dir=ROOT / 'work', ignore_cleanup_errors=True))
    with profile as temp:
        server.DATA, server.LIBRARY = Path(temp), Path(temp) / 'library'
        server.DATA.mkdir(parents=True, exist_ok=True)
        server.init_db()
        pid = '0123456789abcdef'
        write_pdf(server.LIBRARY / pid / 'original.pdf')
        server.put_doc({'id': pid, 'hash': 'synthetic-api-browser', 'title': 'Synthetic API translation paper',
                        'deleted': False, 'pages': [{'number': 1, 'width': 600, 'height': 800}],
                        'figures': [], 'blocks': [], 'notes': 'Preserved note', 'translation': {'status': 'idle'}})
        forbidden = []
        native_reports = []

        class Handler(server.Handler):
            def do_GET(self):
                if args.native and self.path == '/api/health':
                    self.send_json({'synthetic': True})
                    return
                if args.native and self.path == '/':
                    raw = (ROOT / 'static/index.html').read_text(encoding='utf-8').replace(
                        '</head>', '<script src="/__test/native-driver.js" defer></script></head>').encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if args.native and self.path == '/__test/native-driver.js':
                    self.send_file(ROOT / 'tests/browser_api_translation_native.js')
                    return
                super().do_GET()

            def do_POST(self):
                if args.native and self.path in ('/api/desktop-attach', '/api/shutdown'):
                    self.send_json({'error': 'Synthetic server lifetime belongs to the verifier.'}, 409)
                    return
                if args.native and self.path == '/__test/native-results':
                    native_reports.append(self.read_json())
                    self.send_json({'ok': True})
                    return
                super().do_POST()

        def no_cli(*args, **kwargs):
            forbidden.append('CLI access')
            raise AssertionError('隔离 API 浏览器验收禁止 CLI')

        listener = server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.PORT = listener.server_port
        worker = threading.Thread(target=listener.serve_forever, daemon=True)
        environment = {**os.environ, 'EZREAD_TEST_URL': f'http://127.0.0.1:{server.PORT}',
                       'EZREAD_TEST_ROOT': str(ROOT), 'EZREAD_TEST_OUTPUT': str(output)}
        environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
        try:
            with patch.object(server, 'codex_status', side_effect=no_cli), \
                 patch.object(codex_models, 'get', side_effect=no_cli), \
                 patch.object(codex_models, 'resolve_config', side_effect=no_cli), \
                 patch.object(paper_ai, 'CodexSession', side_effect=no_cli), \
                 patch.object(translation_api.ApiTranslationSession, '_send_json', side_effect=no_cli), \
                 patch.object(pdf_layout_engine, 'availability', return_value={
                     'available': True, 'version': '0.6.4', 'engine': 'BabelDOC', 'message': '离线组件夹具'}):
                worker.start()
                if args.native:
                    verify_native(args, output, environment, Path(temp), native_reports)
                else:
                    result = subprocess.run([node, str(ROOT / 'tests/browser_api_translation.cjs')],
                                            env=environment, cwd=ROOT, timeout=90)
                    if result.returncode:
                        raise SystemExit(result.returncode)
                if forbidden:
                    raise AssertionError('发生了禁止的 CLI 查询')
                doc = server.get_doc(pid)
                if not args.native:
                    assert doc['pdf_layout_translation']['config']['provider'] == 'deepseek'
                    assert doc['pdf_layout_translation']['status'] == 'queued'
                assert doc['notes'] == 'Preserved note'
                public = json.dumps(server.public_doc(doc), ensure_ascii=False)
                assert 'fixture-browser-key' not in public
                print('原生翻译确认验收通过；未提交翻译，真实模型调用 0，真实文献库写入 0。' if args.native else
                      'API 浏览器验收通过；仅入队未执行，真实模型调用 0，真实文献库写入 0。')
        finally:
            listener.shutdown()
            listener.server_close()
            worker.join(timeout=3)


if __name__ == '__main__':
    main()
