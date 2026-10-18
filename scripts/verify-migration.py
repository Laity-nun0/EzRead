"""用独立应用和合成库验证真实 WebView2 迁移、并发重开与保存退出。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PAPER_ID = '0123456789abcdef'
PROFILE_PROOF = f'ezread-reader-migration-proof:{PAPER_ID}'
PROFILE_VALUE = json.dumps({'synthetic': True, 'draft': '合成浏览器草稿应完整保留'}, ensure_ascii=False)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('等待超时：' + description)


class NativeProcesses:
    """仅保留已经绑定测试进程身份的句柄，不按全局进程名终止。"""
    def __init__(self, app):
        self.app = app
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.user.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.handles = {}

    def retain(self, pid, *, expected=None, descendant=False):
        if pid in self.handles:
            return self.handles[pid]
        handle = self.kernel.OpenProcess(0x100001 | 0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        size, image = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        try:
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            executable = Path(image.value).resolve()
            if expected is not None and executable != expected.resolve():
                raise AssertionError('进程可执行文件不属于预期测试对象')
            if not descendant and not executable.is_relative_to(self.app):
                raise AssertionError('拒绝绑定测试应用以外的进程')
            self.handles[pid] = handle
            return handle
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def exited(self, pid):
        return self.kernel.WaitForSingleObject(self.handles[pid], 0) == 0

    def windows(self, pid):
        result = []

        @self.callback
        def collect(hwnd, unused):
            owner, name = wintypes.DWORD(), ctypes.create_unicode_buffer(256)
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            self.user.GetClassNameW(hwnd, name, len(name))
            if (owner.value == pid and self.user.IsWindowVisible(hwnd)
                    and name.value.startswith('WindowsForms10.Window.')):
                result.append(hwnd)
            return True

        self.user.EnumWindows(collect, 0)
        return result

    def close_window(self, pid):
        if self.exited(pid):
            return False
        for hwnd in self.windows(pid):
            if not self.user.PostMessageW(hwnd, 0x10, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        return True

    def descendants(self, pid):
        class Entry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                        ('name', wintypes.WCHAR * 260)]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        self.kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        edges, entry = [], Entry()
        entry.size = ctypes.sizeof(entry)
        try:
            found = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                edges.append((entry.pid, entry.parent))
                found = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        owned = {pid}
        while True:
            following = owned | {child for child, parent in edges if parent in owned}
            if following == owned:
                return owned - {pid}
            owned = following

    def cleanup(self):
        forced = []
        for pid, handle in self.handles.items():
            if not self.exited(pid):
                if self.kernel.TerminateProcess(handle, 1):
                    forced.append(pid)
                    self.kernel.WaitForSingleObject(handle, 3000)
            self.kernel.CloseHandle(handle)
        self.handles.clear()
        return forced


def copy_application(payload, executable, app):
    app.mkdir()
    for path in payload.iterdir():
        if path.is_file() and path.suffix.lower() in {'.py', '.pyw'}:
            shutil.copyfile(path, app / path.name)
    for name in ('ezread', 'static', 'skills', 'assets/dictionaries', 'runtime'):
        shutil.copytree(payload / name, app / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    migration = payload / 'scripts/migrate-library.py'
    if not migration.is_file():
        raise AssertionError('发布副本缺少正式迁移 helper 入口')
    (app / 'scripts').mkdir()
    shutil.copyfile(migration, app / 'scripts/migrate-library.py')
    binary = app / 'desktop/bin'
    binary.mkdir(parents=True)
    for path in executable.parent.iterdir():
        if path.is_file() and path.suffix.lower() in {'.exe', '.dll', '.config', '.txt'}:
            shutil.copyfile(path, binary / path.name)
    for name in ('server.py', 'launch.pyw', 'runtime/python.exe', 'runtime/pythonw.exe',
                 'desktop/bin/EzRead.Desktop.exe', 'ezread/data_migration.py'):
        if not (app / name).is_file():
            raise AssertionError('隔离应用缺少文件：' + name)


def synthetic_pdf(path, label):
    content = f'BT /F1 16 Tf 50 750 Td ({label}) Tj ET'.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream']
    document, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f'{index} 0 obj\n'.encode() + value + b'\nendobj\n')
    xref = len(document)
    document.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        document.extend(f'{offset:010d} 00000 n \n'.encode())
    document.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document)


SEED = r'''
import json,pathlib,sqlite3,server,paper_ai
server.init_db()
doc=json.loads(pathlib.Path(__import__('sys').argv[1]).read_text(encoding='utf-8'))
server.put_doc(doc)
with server.db() as con:
    paper_ai.ensure(con,doc['id'])
    for role,kind,text in [('user','question','Synthetic migration question'),('assistant','answer','合成迁移对话；未调用模型。')]:
        con.execute('INSERT INTO paper_ai_messages(paper_id,generation,role,kind,text,created_at) VALUES(?,1,?,?,?,?)',(doc['id'],role,kind,text,'2026-10-18T00:00:00Z'))
    for key,value in [('theme','graphite'),('collections',['迁移验收合成合集']),('reader_zoom',100)]:
        con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
print(json.dumps({'app_root':str(server.ROOT),'data_dir':str(server.DATA),'seeded':True}))
'''


def seed(app, source, base, environment):
    pages = [{'number': 1, 'width': 600, 'height': 800}]
    doc = {'id': PAPER_ID, 'hash': 'synthetic-migration-verification', 'title': 'Synthetic migration verification',
           'authors': ['Synthetic Verification'], 'journal': 'Synthetic Journal', 'year': 2026,
           'deleted': False, 'pages': pages, 'figures': [],
           'blocks': [{'id': 'synthetic-paragraph', 'page': 1, 'kind': 'paragraph',
                       'text': 'Synthetic migration source paragraph.', 'translation': '合成迁移译文，完整保留。'}],
           'translation': {'status': 'idle'}, 'collection': '迁移验收合成合集',
           'notes': '需要完整保留的迁移验收笔记。', 'reader_state': {'page': 1, 'zoom': 100, 'original_progress': .25},
           'read_page': 1, 'last_read': '2026-10-18T00:00:00Z'}
    run_id = '1234567890abcdef'
    folder = source / 'library' / PAPER_ID
    synthetic_pdf(folder / 'original.pdf', 'Synthetic migration original PDF')
    for name in ('mono.pdf', 'dual.pdf'):
        synthetic_pdf(folder / 'pdf-layout' / run_id / name, 'Handwritten synthetic translated PDF')
    artifact = {'id': run_id, 'created_at': '2026-10-18', 'page_count': 1, 'pages': pages,
                'mono': 'mono.pdf', 'dual': 'dual.pdf', 'quality': 'passed', 'warnings': [],
                'config': {'model': 'synthetic-no-model'}}
    doc.update(pdf_layout_current=artifact, pdf_layout_versions=[artifact],
               pdf_layout_translation={'status': 'completed'})
    fixture = base / 'fixture-document.json'
    write_json(fixture, doc)
    result = subprocess.run([str(app / 'runtime/python.exe'), '-B', '-c', SEED, str(fixture)],
                            cwd=app, env=environment, check=True, capture_output=True,
                            encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    identity = json.loads(result.stdout)
    assert Path(identity['app_root']).resolve() == app
    assert Path(identity['data_dir']).resolve() == source
    stable = ['library/' + PAPER_ID + '/original.pdf',
              f'library/{PAPER_ID}/pdf-layout/{run_id}/mono.pdf',
              f'library/{PAPER_ID}/pdf-layout/{run_id}/dual.pdf',
              'translation-api.private.json', 'private/opaque-credential.bin',
              'reader-drafts/pending-synthetic.json', 'webview2-profile/migration-opaque-profile.bin',
              'webview2-migration.json']
    write_json(source / 'translation-api.private.json', {'selected_provider': 'deepseek', 'profiles': {},
                                                        'migration_fixture': 'synthetic-private-bytes-only'})
    write_json(source / 'reader-drafts/pending-synthetic.json', {'synthetic': True, 'text': '待恢复的合成草稿'})
    # 使用正式旧浏览器迁移入口，在真实 WebView2 中种入可恢复草稿标记。
    write_json(source / 'webview2-migration.json', {PROFILE_PROOF: PROFILE_VALUE})
    for name in ('private/opaque-credential.bin', 'webview2-profile/migration-opaque-profile.bin'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(range(256)) + b'EzRead migration synthetic opaque fixture')
    return doc, {name: digest(source / name) for name in stable}


def database_snapshot(directory):
    with sqlite3.connect((directory / 'library.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        assert connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        doc = json.loads(connection.execute('SELECT doc FROM papers WHERE id=?', (PAPER_ID,)).fetchone()[0])
        return {'doc': doc, 'settings': connection.execute('SELECT key,value FROM settings ORDER BY key').fetchall(),
                'messages': connection.execute('SELECT paper_id,generation,role,kind,text,citations,created_at FROM paper_ai_messages ORDER BY id').fetchall()}


def profile_storage(app, directory, output, origin):
    """只复制已经退出的本轮 profile 数据，复用既有有界 LevelDB 读取器。"""
    source = directory / 'webview2-profile/EBWebView/Default/Local Storage/leveldb'
    destination = output / 'browser-profile/Default/Local Storage/leveldb'
    shutil.copytree(source, destination)
    spec = importlib.util.spec_from_file_location('migration_browser_state', app / 'browser_state.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_legacy_storage(output, origin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--no-concurrent-launch', action='store_true')
    args = parser.parse_args()
    payload, executable = args.payload.resolve(), args.executable.resolve()
    if os.name != 'nt' or not payload.is_relative_to(ROOT) or not executable.is_relative_to(ROOT):
        parser.error('仅使用当前工作区内的 Windows payload 和原生构建。')
    if not (payload / 'release-manifest.json').is_file() or not executable.is_file():
        parser.error('请先完成 payload 和桌面组件构建。')
    base = ROOT / 'work/migration-verification' / uuid.uuid4().hex
    base.mkdir(parents=True)
    app, source, target = base / 'app', base / 'source', base / '目标中文 空格'
    temporary, local = base / 'temp', base / 'localappdata'
    temporary.mkdir()
    local.mkdir()
    session = uuid.uuid4().hex
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    environment = dict(os.environ)
    for key in ('EZREAD_DATA_DIR', 'TUDU_DATA_DIR', 'PYTHONHOME', 'PYTHONPATH'):
        environment.pop(key, None)
    environment.update(EZREAD_DEFAULT_DATA_DIR=str(source), LOCALAPPDATA=str(local),
                       EZREAD_PORT=str(port), TEMP=str(temporary), TMP=str(temporary),
                       PYTHONDONTWRITEBYTECODE='1', NO_PROXY='127.0.0.1,localhost')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {'status': 'running', 'app_root': str(app), 'source': str(source), 'target': str(target),
              'verification_session': session, 'realLibraryWrites': 0, 'checks': [], 'output': str(base)}
    processes, native, forced = [], None, []

    def request(path):
        try:
            with opener.open(origin + path, timeout=.6) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def log_text(directory):
        path = directory / 'desktop.log'
        return path.read_text(encoding='utf-8-sig', errors='replace') if path.is_file() else ''

    try:
        copy_application(payload, executable, app)
        doc, expected_hashes = seed(app, source, base, environment)
        initial = database_snapshot(source)
        native = NativeProcesses(app)
        with ExitStack() as stack:
            def spawn(command, name):
                log = stack.enter_context((base / name).open('w', encoding='utf-8'))
                process = subprocess.Popen(command, cwd=app, env=environment, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                processes.append(process)
                native.retain(process.pid)
                return process

            backend = spawn([str(app / 'runtime/python.exe'), '-B', str(app / 'server.py'), '--port', str(port)], 'initial-backend.log')
            health = wait_for(lambda: request('/api/health'), 20, '生产后台启动')
            assert health['pid'] == backend.pid and Path(health['app_root']).resolve() == app
            assert Path(health['data_dir']).resolve() == source
            report['initial_health'] = health
            host = spawn([str(app / 'desktop/bin/EzRead.Desktop.exe'), '--app-root', str(app), '--data-dir', str(source),
                          '--url', origin, '--verification-session', session, '--migration-test-target', str(target)], 'initial-native.log')
            control_root = temporary / 'EzRead.Migration'

            def controls_ready():
                for path in control_root.glob('*/ready.json'):
                    value = read_json(path)
                    if value and value.get('ready'):
                        return path.parent
                return None

            control = wait_for(controls_ready, 35, '真实迁移 helper ready')
            ready = read_json(control / 'ready.json')
            report.update(control_dir=str(control), ready=ready)
            native.retain(ready['pid'], expected=app / 'runtime/pythonw.exe')
            browser_pids = native.descendants(host.pid) - {ready['pid']}
            held_browsers = []
            for pid in browser_pids:
                try:
                    native.retain(pid, descendant=True)
                    held_browsers.append(pid)
                except OSError:
                    pass
            report['old_browser_descendants'] = held_browsers
            queued = None
            if not args.no_concurrent_launch:
                queued = spawn([str(app / 'runtime/python.exe'), '-B', str(app / 'launch.pyw'),
                                '--verification-session', session], 'concurrent-launch.log')
            approval = wait_for(lambda: read_json(control / 'approved.json'), 30, '原生保存关闭批准')
            assert approval == {'approved': True, 'owner_pid': host.pid}
            migration = wait_for(lambda: read_json(control / 'report.json'), 90, '真实 helper 迁移报告')
            report['migration'] = migration
            assert migration.get('status') == 'passed', migration
            assert migration.get('source_preserved') is True
            wait_for(lambda: native.exited(ready['pid']), 10, 'helper 完成自动重启调度')
            restart_launchers = []
            for pid in native.descendants(ready['pid']):
                try:
                    native.retain(pid, expected=app / 'runtime/pythonw.exe')
                    restart_launchers.append(pid)
                except (OSError, AssertionError):
                    pass
            wait_for(lambda: native.exited(host.pid) and native.exited(backend.pid), 10, '原窗口和后台退出')
            for pid in held_browsers:
                wait_for(lambda pid=pid: native.exited(pid), 10, '原 WebView2 后代退出')
            report['checks'].append({'helperReadyApprovedPassed': True, 'oldHostBackendBrowsersExited': True})

            def migrated_health():
                value = request('/api/health')
                return value if value and Path(value.get('data_dir', '')).resolve() == target else None

            new_health = wait_for(migrated_health, 45, '自动重启到新目录')
            assert Path(new_health['app_root']).resolve() == app
            assert new_health['instance_id'] != health['instance_id']
            native.retain(new_health['pid'], expected=app / 'runtime/python.exe')
            report['migrated_health'] = new_health
            if queued:
                assert queued.wait(timeout=45) == 0, '并发打开未成功复用迁移后的实例'

            def new_host_pid():
                ids = re.findall(r'pid=(\d+) child=False ready WebView2=', log_text(target))
                return int(ids[-1]) if ids and int(ids[-1]) != host.pid else None

            new_host = wait_for(new_host_pid, 30, '自动重启的独立原生窗口')
            native.retain(new_host, expected=app / 'desktop/bin/EzRead.Desktop.exe')
            wait_for(lambda: native.windows(new_host), 10, '新窗口 HWND')
            # ready 日志早于启动器释放启动租约。先等待正式启动事务提交，
            # 避免验收立即关窗，使另一条并发打开请求再次创建实例。
            for pid in restart_launchers:
                wait_for(lambda pid=pid: native.exited(pid), 30, '自动重启启动器提交并退出')
            report.update(restart_launcher_pids=restart_launchers, new_host_pid=new_host)
            new_children = native.descendants(new_host)
            for pid in new_children:
                try:
                    native.retain(pid, descendant=True)
                except OSError:
                    pass
            selected = list((local / 'EzRead/locations').glob('*.json'))
            assert len(selected) == 1
            selector = read_json(selected[0])
            assert Path(selector['app_root']).resolve() == app and Path(selector['data_dir']).resolve() == target
            report['selector'] = selector
            for directory in (source, target):
                for name, expected in expected_hashes.items():
                    assert (directory / name).is_file() and digest(directory / name) == expected, (str(directory), name)
            current, original = database_snapshot(target), database_snapshot(source)
            for field in ('notes', 'blocks', 'reader_state', 'pdf_layout_current'):
                assert current['doc'][field] == doc[field] == original['doc'][field], field
            assert current['messages'] == initial['messages'] == original['messages']
            assert current['settings'] == initial['settings'] == original['settings']
            for directory in (source, target):
                assert (directory / 'webview2-profile/EBWebView/Local State').is_file(), '未产生真实 WebView2 profile'
                assert (directory / 'webview2-profile/EBWebView/Default/Local Storage/leveldb').is_dir(), '缺少真实浏览器存储'
            report['stable_file_hashes'] = expected_hashes
            report['checks'].append({'notesChatReadPositionSettingsPreserved': True,
                                     'originalTranslatedPdfsPrivateDraftProfileHashesMatch': True,
                                     'sourcePreserved': True, 'selectorPointsToTarget': True,
                                     'automaticRestartUsesTarget': True})
            if queued:
                assert log_text(target).count(' child=False initializing') == 2, '并发打开创建了多余窗口'
                report['checks'].append({'concurrentOpenReusesOneNewHost': True})
            status = request('/api/ai/status')
            report['api_usage'] = status.get('usage') if status else None
            assert status and status['configuration']['configured'] is False
            assert status['usage']['requests']['attempted'] == 0, '出现了未授权模型传输尝试'
            # 仅向本次 app 的稳定 PID/句柄所对应 HWND 发关闭消息。
            native.close_window(new_host)
            wait_for(lambda: native.exited(new_host), 25, '新窗口正常保存退出')
            wait_for(lambda: native.exited(new_health['pid']), 10, '新后台退出')
            wait_for(lambda: all(native.exited(pid) for pid in new_children if pid in native.handles), 10, '新 WebView2 后代退出')
            for directory, pid in ((source, host.pid), (target, new_host)):
                log = '\n'.join(line for line in log_text(directory).splitlines()
                                if f' pid={pid} child=False ' in line)
                assert f'app-id=EzRead.Desktop.Verification.{session} taskbar=False' in log
                assert 'close-save-confirmed' in log and 'closed' in log
                assert any('taskbar-remove hwnd=' in line and ' hr=0' in line for line in log.splitlines())
            assert 'library-migration-ready' in log_text(source)
            assert 'library-migration-approved' in log_text(source)
            for label, directory in (('source', source), ('target', target)):
                storage = profile_storage(app, directory, base / ('profile-readback-' + label), origin)
                assert storage.get(PROFILE_PROOF) == PROFILE_VALUE, '真实浏览器草稿标记未保留'
            assert request('/api/health') is None
            report['checks'].append({'verificationTaskbarIdentityAndExplicitRemovalLogged': True,
                                     'newHostBackendBrowsersCleanlyExited': True,
                                     'actualWebViewLocalStorageDraftPreserved': True,
                                     'modelTransportAttempts': 0})
            report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=str(error), error_type=type(error).__name__)
        if 'control_root' in locals():
            report['control_records'] = {str(path.relative_to(base)): read_json(path)
                                         for path in control_root.glob('*/*.json')}
        raise
    finally:
        if native:
            # helper 失败后也可能按正式逻辑重新打开原库。绑定已知稳定进程
            # 的后代，清理时不遗漏本轮重启，也不按名称结束其他应用。
            for owner in list(native.handles):
                for pid in native.descendants(owner):
                    try:
                        native.retain(pid, descendant=True)
                    except OSError:
                        pass
            for pid in list(native.handles):
                if not native.exited(pid):
                    native.close_window(pid)
            time.sleep(.5)
            forced = native.cleanup()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        report['forced_cleanup_pids'] = forced
        if forced and report['status'] == 'passed':
            report.update(status='failed', error='验收后仍有测试所属进程需要强制清理')
        write_json(base / 'report.json', report)
        print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
