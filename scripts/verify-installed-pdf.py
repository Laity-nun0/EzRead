"""用安装内运行时、真实 PDF 队列和回环模拟供应商验收；不修补安装依赖。"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit


def digest(path):
    with path.open('rb') as stream:
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
        return result.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def fixture_pdf(path):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>']
    for content in (6, 7):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] '
                       f'/Resources << /Font << /F1 5 0 R >> >> /Contents {content} 0 R >>'.encode())
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    for index in (1, 2):
        stream = (f'BT /F1 16 Tf 50 740 Td (Synthetic installed PDF verification page {index}) Tj ET\n'
                  + '\n'.join(f'BT /F1 10 Tf 50 {680-row*40} Td '
                              '(This synthetic paragraph checks native PDF processing and mock responses.) Tj ET'
                              for row in range(6))).encode()
        objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    raw = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f'{number} 0 obj\n'.encode() + value + b'\nendobj\n')
    start = len(raw)
    raw.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    raw.extend(b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:]))
    raw.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    path.write_bytes(raw)


def install_network_guard(network):
    # 审计钩子在导入安装代码前生效；BabelDOC 使用既有离线资产和管道。
    def audit(event, args):
        if event != 'socket.connect':
            return
        address = args[1]
        try:
            allowed = isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            network['blocked_external_connections'] += 1
            raise RuntimeError('离线验收禁止非回环网络连接。')
        network['loopback_connections'] += 1
    sys.addaudithook(audit)


def worker(args):
    app, output = args.app_root, args.output
    report = {'status': 'failed', 'app_root': str(app), 'data_dir': str(output / 'data'),
              'main_python': sys.executable, 'real_model_calls': 0, 'cases': [],
              'network': {'loopback_connections': 0, 'blocked_external_connections': 0}}
    records = []
    provider = queue_worker = server = engine = None
    install_network_guard(report['network'])
    try:
        if Path.cwd().resolve() != app or Path(sys.executable).resolve() != app / 'runtime/python.exe':
            raise RuntimeError('worker 必须由目标安装运行时在安装目录启动。')
        if Path(os.environ['EZREAD_DATA_DIR']).resolve() != output / 'data':
            raise RuntimeError('独立 data 目录不匹配。')
        sys.path.insert(0, str(app))
        import server
        import pdf_layout_engine as engine
        import pypdfium2 as pdfium
        from ezread import api_settings, translation_api
        from ezread.data_location import data_use_lock
        from ezread.pdf_native_translation import request_units, encode_response
        if (server.ROOT.resolve() != app or Path(server.__file__).resolve() != app / 'server.py'
                or Path(engine.__file__).resolve() != app / 'pdf_layout_engine.py'
                or server.DATA.resolve() != output / 'data'):
            raise RuntimeError('实际加载源码或文献目录不属于目标安装/独立输出。')
        report.update(server_file=server.__file__, engine_file=engine.__file__,
                      engine_python=str(engine._paths(app)[1]))
        engine.processes.attach_job()

        class Provider(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    prompt = body['messages'][-1]['content']
                    batch = '## Here is the input:\n\n' in prompt
                    request = {'prompt': prompt,
                               'mode': 'llm' if batch or 'Now translate the following text:\n\n' in prompt else 'text',
                               'rate_limit_params': {'request_json_mode': batch}}
                    units, batch = request_units(request)
                    # 原文中的标签、公式与数字原样保留，响应继续经过产品校验器。
                    values = {item['id']: '模拟译文 ' + item['input'] for item in units}
                    response = encode_response(units, batch, values)
                    records.append({'units': len(units), 'batch': batch,
                                    'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()})
                    raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': response}}],
                                      'usage': {'prompt_tokens': 10, 'completion_tokens': 5}},
                                     ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as exc:
                    records.append({'error': f'{type(exc).__name__}: {exc}'})
                    self.send_error(500, 'Offline mock failed')

        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        provider.daemon_threads = True
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        send = translation_api.ApiTranslationSession._send_json

        def local_send(session, *values, **kwargs):
            session._url = urlsplit(f'http://127.0.0.1:{provider.server_port}')
            return send(session, *values, **kwargs)

        translation_api.ApiTranslationSession._send_json = local_send
        with data_use_lock(server.DATA):
            server.init_db()
            if server.all_docs():
                raise RuntimeError('独立验收库必须为空。')
            api_settings.save_settings(server.DATA, {'purpose': 'translation', 'api_key': 'synthetic-offline-key'})
            queue_worker = threading.Thread(target=server.worker, daemon=True)
            queue_worker.start()
            for index, source in enumerate([None, *([args.source] if args.source else [])], 1):
                pid = f'{index:016x}'
                folder = server.LIBRARY / pid
                folder.mkdir(parents=True)
                pdf = folder / 'original.pdf'
                source_hash = digest(source) if source else None
                if source:
                    shutil.copy2(source, pdf)
                else:
                    fixture_pdf(pdf)
                original_hash = digest(pdf)
                with pdfium.PdfDocument(pdf) as document:
                    pages = [{'number': i + 1, 'width': document[i].get_width(),
                              'height': document[i].get_height()} for i in range(len(document))]
                preserved = {'notes': '离线验收保留笔记', 'reader_state': {'page': 1}, 'read_page': 1}
                server.put_doc({'id': pid, 'hash': original_hash, 'title': 'Offline PDF verification',
                                'authors': [], 'deleted': False, 'blocks': [], 'figures': [],
                                'pages': pages, **preserved})
                before = len(records)
                case = {'case': pid, 'source_pages': len(pages), 'source_sha256': original_hash,
                        'source_path': str(source) if source else 'synthetic', 'outputs': {}}
                report['cases'].append(case)
                try:
                    server.enqueue(pid, 'pdf_translate', {'thinking': 'disabled'})
                    deadline, previous = time.monotonic() + args.timeout, None
                    while time.monotonic() < deadline:
                        saved = server.get_doc(pid)
                        task = saved['pdf_layout_translation']
                        state = (task['status'], task.get('stage'))
                        if state != previous:
                            print(json.dumps({'case': pid, 'status': state[0], 'stage': state[1],
                                              'mock_requests': len(records) - before}, ensure_ascii=True), flush=True)
                            previous = state
                        if task['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.25)
                    else:
                        server.CANCEL[(pid, 'pdf_translate')].set()
                        raise TimeoutError(f'任务超过 {args.timeout} 秒。')
                    case['task'] = {key: task.get(key) for key in
                                    ('status', 'run_id', 'error', 'error_code', 'stage', 'model_calls', 'warnings')}
                    run = folder / 'pdf-layout' / task['run_id']
                    case['native_stderr'] = str(run / 'native-stderr.log')
                    artifact_file = run / 'artifact.json'
                    if artifact_file.is_file():
                        artifact = json.loads(artifact_file.read_text(encoding='utf-8'))
                        for name in ('mono', 'dual'):
                            path = (run / artifact[name]).resolve()
                            if not path.is_relative_to(run.resolve()) or not path.is_file():
                                raise RuntimeError('派生 PDF 路径越界或不存在。')
                            with pdfium.PdfDocument(path) as document:
                                count = len(document)
                            case['outputs'][name] = {'path': str(path), 'bytes': path.stat().st_size,
                                                     'pages': count, 'sha256': digest(path)}
                    if task['status'] != 'completed':
                        raise RuntimeError(f"任务未完成：{task['status']} / {task.get('error')}")
                    if not records[before:] or any('error' in r for r in records[before:]):
                        raise RuntimeError('模拟供应商未收到有效请求。')
                    if any(case['outputs'].get(name, {}).get('pages') != len(pages) for name in ('mono', 'dual')):
                        raise RuntimeError('mono/dual 产物缺失或页数不符。')
                finally:
                    case['mock_requests'] = len(records) - before
                    case['source_unchanged'] = digest(pdf) == original_hash
                    case['source_file_unchanged'] = source is None or digest(source) == source_hash
                    saved = server.get_doc(pid)
                    case['notes_and_reading_preserved'] = all(saved.get(k) == v for k, v in preserved.items())
                    write_json(output / 'report.json', report)
                if not all(case[k] for k in ('source_unchanged', 'source_file_unchanged', 'notes_and_reading_preserved')):
                    raise RuntimeError('原件、笔记或阅读状态发生变化。')
            if report['network']['blocked_external_connections']:
                raise RuntimeError('曾尝试非回环网络连接。')
            report['status'] = 'passed'
        return 0
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        traceback.print_exc()
        return 1
    finally:
        if server is not None and queue_worker is not None:
            server.SHUTDOWN.set()
            for event in list(server.CANCEL.values()):
                event.set()
            server.JOBS.put(None)
            queue_worker.join(timeout=20)
            report['queue_worker_stopped'] = not queue_worker.is_alive()
            if queue_worker.is_alive():
                report['status'] = 'failed'
        if provider is not None:
            provider.shutdown()
            provider.server_close()
        report['mock_requests'] = len(records)
        write_json(output / 'mock-request-summary.json', records)
        write_json(output / 'report.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='项目 work/ 或 output/ 下尚不存在的目录')
    parser.add_argument('--source', type=Path, help='额外只读复制一份 PDF，不修改来源')
    parser.add_argument('--timeout', type=int, default=420, help='每篇任务期限（秒）')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.app_root, args.output = args.app_root.resolve(), args.output.resolve()
    args.source = args.source.resolve() if args.source else None
    if args.timeout < 1:
        parser.error('--timeout 必须大于零')
    root = Path(__file__).resolve().parent.parent
    if not any(args.output.is_relative_to(root / name) and args.output != root / name for name in ('work', 'output')):
        parser.error('--output 必须位于此项目 work/ 或 output/ 内')
    if args.worker:
        if (not args.output.is_dir() or (args.output / 'data').exists()
                or args.output.is_relative_to(args.app_root)):
            parser.error('worker 仅接受独立新输出目录，data 必须尚不存在')
        result = worker(args)
        report = json.loads((args.output / 'report.json').read_text(encoding='utf-8'))
        return result if report['status'] == 'passed' else 1
    if args.output.exists() or args.output.is_relative_to(args.app_root):
        parser.error('--output 必须是安装目录之外的新目录')
    runtime = args.app_root / 'runtime/python.exe'
    if not runtime.is_file() or not (args.app_root / 'server.py').is_file():
        parser.error('--app-root 缺少安装内 runtime/python.exe 或 server.py')
    if args.source and not args.source.is_file():
        parser.error('--source 必须是现存 PDF 文件')
    args.output.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('EZREAD_', 'TUDU_', 'PYTHON'))
           and k.upper() not in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    env.update(PATH=os.pathsep.join(map(str, (runtime.parent, windows / 'System32', windows))),
               EZREAD_DATA_DIR=str(args.output / 'data'), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               NO_PROXY='127.0.0.1,localhost,::1')
    command = [str(runtime), '-B', str(Path(__file__).resolve()), '--worker', '--app-root', str(args.app_root),
               '--output', str(args.output), '--timeout', str(args.timeout)]
    if args.source:
        command.extend(['--source', str(args.source)])
    launch = {'app_root': str(args.app_root), 'output': str(args.output), 'real_model_calls': 0}
    try:
        with (args.output / 'stdout.log').open('wb') as stdout, (args.output / 'stderr.log').open('wb') as stderr:
            result = subprocess.run(command, cwd=args.app_root, env=env, stdout=stdout, stderr=stderr,
                                    timeout=args.timeout * (2 if args.source else 1) + 90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        launch['returncode'] = result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        launch.update(returncode=1, error=f'{type(exc).__name__}: {exc}')
    write_json(args.output / 'launch-report.json', launch)
    report_path = args.output / 'report.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
    passed = launch['returncode'] == 0 and report.get('status') == 'passed'
    print(json.dumps({'status': 'passed' if passed else 'failed', 'report': str(report_path),
                      'real_model_calls': 0, 'mock_requests': report.get('mock_requests', 0)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
