"""用合成文献验证标签切换及浏览器/真实 WebView2 关闭重开，不调用模型。"""
import argparse
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--executable', type=Path, default=ROOT / 'work/desktop-build/EzRead.Desktop.exe')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('reader_fixture', ROOT / 'scripts/verify-reader-text.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    evidence = ROOT / 'work' / ('reader-session-native' if args.native else 'reader-session-browser')
    evidence.mkdir(parents=True, exist_ok=True)
    results, failures = [], []
    phase, finished = 1, threading.Event()
    backend_owner, owner_stop, owner_instance = None, None, None
    resize_native = None
    with tempfile.TemporaryDirectory(prefix='reader-session-', dir=ROOT / 'work', ignore_cleanup_errors=True) as folder:
        data = Path(folder)
        server.DATA, server.LIBRARY = data, data / 'library'
        server.init_db()
        for number, count in ((1, 2), (2, 3)):
            pid = f'{number:016x}'
            library = server.LIBRARY / pid
            library.mkdir(parents=True)
            fixture.write_fixture_pdf(library / 'original.pdf', count=count)
            server.put_doc({'id': pid, 'hash': f'synthetic-reader-session-{number}',
                            'title': f'Synthetic session paper {number} — reading tabs and saved notes',
                            'authors': [], 'deleted': False, 'notes': '', 'blocks': [], 'figures': [],
                            'favorite': number == 1, 'collection': 'Session collection' if number == 1 else '',
                            'pages': [{'number': index, 'width': 595, 'height': 842}
                                      for index in range(1, count + 1)]})

        class Handler(server.Handler):
            def log_message(self, *unused):
                pass

            def do_GET(self):
                if self.path == '/':
                    raw = (ROOT / 'static/index.html').read_text(encoding='utf-8').replace(
                        '</head>', '<script src="/__test/session-driver.js" defer></script></head>').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(raw)
                elif self.path == '/__test/session-driver.js':
                    self.send_file(ROOT / 'tests/browser_reader_session.js')
                elif self.path == '/__test/phase':
                    self.send_json({'phase': phase, 'native': args.native})
                elif self.path == '/__test/results':
                    self.send_json({'results': results, 'failures': failures})
                elif self.path == '/api/health':
                    self.send_json({'app_root': str(ROOT), 'data_dir': str(data),
                                    'pid': backend_owner.pid if backend_owner else os.getpid(),
                                    'instance_id': owner_instance, 'version': server.VERSION})
                else:
                    super().do_GET()

            def do_POST(self):
                nonlocal phase
                if self.path == '/__test/resize' and resize_native:
                    resize_native(self.read_json()['size'])
                    self.send_json({'ok': True})
                elif self.path == '/__test/phase':
                    phase = self.read_json()['phase']
                    finished.clear()
                    self.send_json({'ok': True})
                elif self.path == '/__test/result':
                    value = self.read_json()
                    (failures if value.get('error') else results).append(value)
                    finished.set()
                    self.send_json({'ok': True})
                elif self.path in ('/api/desktop-attach', '/api/shutdown'):
                    body = self.read_json()
                    if backend_owner is None or body.get('instance_id') != owner_instance:
                        self.send_json({'error': '验收后台身份不匹配。'}, 409)
                    else:
                        if self.path == '/api/shutdown':
                            owner_stop.touch()
                        self.send_json({'ok': True})
                else:
                    self.send_json({'error': '验收禁止模型、队列及其他 POST 操作。'}, 409)

        listener = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.PORT = listener.server_port
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{listener.server_port}'
        try:
            if args.native:
                executable = args.executable.resolve()
                if os.name != 'nt' or not executable.is_relative_to(ROOT) or not executable.is_file():
                    raise RuntimeError('需要工作区内已构建的 Windows 桌面组件。')
                user = ctypes.WinDLL('user32', use_last_error=True)
                callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                user.EnumWindows.argtypes = [callback, wintypes.LPARAM]
                user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user.IsWindowVisible.argtypes = [wintypes.HWND]
                user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
                user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int, wintypes.UINT]

                def main_windows():
                    windows = []

                    @callback
                    def collect(hwnd, unused):
                        owner = wintypes.DWORD()
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                        name = ctypes.create_unicode_buffer(256)
                        user.GetClassNameW(hwnd, name, len(name))
                        if (owner.value == process.pid and user.IsWindowVisible(hwnd)
                                and name.value.startswith('WindowsForms10.Window.')):
                            windows.append(hwnd)
                        return True

                    user.EnumWindows(collect, 0)
                    if len(windows) != 1:
                        raise RuntimeError('未找到本次测试唯一主窗口。')
                    return windows

                def resize_native(size):
                    width, height = {'short': (850, 500), 'wide': (1300, 850)}[size]
                    hwnd = main_windows()[0]
                    user.ShowWindow(hwnd, 9)
                    if not user.SetWindowPos(hwnd, None, 0, 0, width, height, 0x16):
                        raise RuntimeError('测试窗口调整尺寸失败。')

                session = uuid.uuid4().hex
                for phase in (1, 2, 3):
                    finished.clear()
                    # 原生宿主只取得这个合成后台的归属，绝不持有验收器自身 PID。
                    owner_instance = uuid.uuid4().hex
                    owner_stop = data / f'owner-stop-{phase}'
                    owner_code = ('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\n'
                                  'while not p.exists(): time.sleep(.02)\n')
                    backend_owner = subprocess.Popen([getattr(sys, '_base_executable', sys.executable),
                                                      '-c', owner_code, str(owner_stop)],
                                                     creationflags=subprocess.CREATE_NO_WINDOW)
                    process = subprocess.Popen([str(executable), '--app-root', str(ROOT), '--data-dir', str(data),
                                                '--url', origin, '--verification-session', session], cwd=ROOT,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        deadline = time.monotonic() + 50
                        while not finished.wait(.1):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError(f'原生会话阶段 {phase} 未完成，退出码 {process.poll()}。')
                        if failures:
                            raise RuntimeError(failures[-1]['error'])
                        windows = main_windows()
                        if len(windows) != 1 or not user.PostMessageW(windows[0], 0x10, 0, 0):
                            raise RuntimeError('未找到本次测试唯一主窗口，不能提交关闭验收。')
                        if process.wait(timeout=25) != 0:
                            raise RuntimeError('原生主窗口未正常关闭。')
                        if backend_owner.wait(timeout=5) != 0:
                            raise RuntimeError('原生关闭没有正常退出所属合成后台。')
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)
                        owner_stop.touch()
                        backend_owner.wait(timeout=5)
                log = (data / 'desktop.log').read_text(encoding='utf-8-sig')
                if f'app-id=EzRead.Desktop.Verification.{session} taskbar=False' not in log:
                    raise RuntimeError('未证实原生验收身份隔离。')
                removals = [line for line in log.splitlines() if 'taskbar-remove hwnd=' in line]
                if len(removals) < 3 or not all(line.endswith(' hr=0') for line in removals):
                    raise RuntimeError('未证实各次关闭显式移除任务栏按钮。')
            else:
                environment = dict(os.environ, EZREAD_TEST_URL=origin, EZREAD_TEST_ROOT=str(ROOT))
                runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
                node = environment.get('EZREAD_NODE') or shutil.which('node') or str(runtime / 'bin/node.exe')
                environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
                subprocess.run([node, str(ROOT / 'tests/browser_reader_session.cjs')], cwd=ROOT,
                               env=environment, check=True)
            if failures or len(results) != 3:
                raise RuntimeError(f'会话验收结果不完整：{results!r}, {failures!r}')
            for number in (1, 2):
                document = server.get_doc(f'{number:016x}')
                if document.get('notes') != f'Session note {number}':
                    raise RuntimeError('切换或关闭时丢失笔记。')
            report = {'status': 'passed', 'native': args.native, 'results': results,
                      'real_model_calls': 0, 'real_library_writes': 0,
                      'taskbar_visual_inspection': 'not performed; native identity and API logs checked'}
            (evidence / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': 'passed', 'native': args.native, 'phases': len(results)}))
        finally:
            if (data / 'desktop.log').is_file():
                shutil.copy2(data / 'desktop.log', evidence / 'desktop.log')
            (evidence / 'results.json').write_text(json.dumps({'results': results, 'failures': failures},
                                                            ensure_ascii=False, indent=2), encoding='utf-8')
            listener.shutdown()
            listener.server_close()
            thread.join()


if __name__ == '__main__':
    main()
