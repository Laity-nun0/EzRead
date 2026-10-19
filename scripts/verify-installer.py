"""Verify a real per-user Windows installation using an isolated library and native identity."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\EzRead.Desktop.Installation_is1'


def test_path(value):
    """Reject both lexical escapes and junction/symlink escapes before writing."""
    absolute = Path(os.path.abspath(value))
    allowed = (ROOT / 'work', ROOT / 'output')
    if not any(absolute.is_relative_to(base) for base in allowed):
        raise ValueError('Verification paths must stay inside workspace/work or workspace/output.')
    current = absolute
    while current != ROOT:
        if current.is_symlink() or (current.exists() and current.is_junction()):
            raise ValueError(f'Verification paths cannot traverse links: {current}')
        current = current.parent
    resolved = absolute.resolve()
    if not any(resolved.is_relative_to(base.resolve()) for base in allowed):
        raise ValueError('Resolved verification path escaped the workspace.')
    return resolved


def installations():
    import winreg
    found = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as key:
                try:
                    location = winreg.QueryValueEx(key, 'InstallLocation')[0]
                except FileNotFoundError:
                    location = ''
                found.append({'view': view, 'location': location})
        except FileNotFoundError:
            pass
    return found


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError('Timed out waiting for isolated installer verification.')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def verify_installed_inventory(installed):
    """核对实际落盘文件，捕获安装器误排除依赖资源等 payload 之后的遗漏。"""
    installed = installed.resolve()
    manifest = json.loads((installed / 'release-manifest.json').read_text(encoding='utf-8'))
    files = manifest['files']
    require(isinstance(files, dict) and bool(files), 'Installed inventory is empty or invalid.')
    mismatches = []
    for name, expected in files.items():
        path = (installed / name).resolve()
        if (not path.is_relative_to(installed) or not path.is_file()
                or path.stat().st_size != expected['size'] or digest(path) != expected['sha256']):
            mismatches.append(name)
    require(not mismatches, f'Installed inventory differs ({len(mismatches)} files): {mismatches[:20]}')
    return {'installed_inventory': True, 'file_count': len(files), 'version': manifest['version']}


class NativeProcesses:
    """Hold stable handles, verify image paths, and operate only on owned HWNDs."""
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.handles = []

    def launcher_child(self, launcher, executable):
        """Retain pythonw before HTTP startup, so failed startup can cancel its lease."""
        class ProcessEntry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                        ('name', wintypes.WCHAR * 260)]

        require(launcher.poll() is None, 'Launcher exited before its embedded Python child was captured.')
        self.kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        self.kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            more = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.parent == launcher.pid and entry.name.lower() == executable.name.lower():
                    return self.retain(entry.pid, executable)
                more = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        return None

    def retain(self, pid, executable):
        handle = self.kernel.OpenProcess(0x101001, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(handle)
        name = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(name))
        if not self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            self.handles.remove(handle)
            self.kernel.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if Path(name.value).resolve() != executable.resolve():
            self.handles.remove(handle)
            self.kernel.CloseHandle(handle)
            raise AssertionError('Process image does not belong to the isolated installation.')
        return handle

    def window(self, pid):
        found = []

        @self.callback
        def collect(hwnd, unused):
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(hwnd, name, len(name))
            if owner.value == pid and self.user.IsWindowVisible(hwnd) and name.value.startswith('WindowsForms10.Window.'):
                found.append(hwnd)
            return True

        self.user.EnumWindows(collect, 0)
        return found[0] if found else None

    def close_window(self, pid):
        hwnd = wait_for(lambda: self.window(pid))
        # 再核对 HWND 的所属进程，避免枚举后窗口句柄复用。
        owner = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        require(owner.value == pid, 'Native window ownership changed before close.')
        require(self.user.PostMessageW(hwnd, 0x10, 0, 0), 'Could not post WM_CLOSE to the owned test host.')

    def exited(self, handle, seconds):
        require(self.kernel.WaitForSingleObject(handle, seconds * 1000) == 0,
                'Owned native/backend process did not actually exit.')

    def cleanup(self):
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 0) != 0:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 10000)
            self.kernel.CloseHandle(handle)
        self.handles.clear()


def command(command_line, log, env, cwd, timeout=900):
    with log.open('wb') as output:
        process = subprocess.Popen(command_line, env=env, cwd=cwd, stdout=output, stderr=output,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            raise
    require(code == 0, f'Command failed ({code}); inspect {log.name}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/installer-verification')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('This verification requires Windows and an interactive WebView2 desktop.')
    installer = test_path(args.installer)
    require(installer.is_file(), 'Installer executable is missing.')
    require(not installations(), 'A real EzRead installation is already registered; verification refused.')
    output = test_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    run = test_path(output / uuid.uuid4().hex)
    run.mkdir()
    installed = test_path(run / '安装 中文 空格' / 'EzRead')
    data = test_path(run / 'data')
    data.mkdir()
    report = {'status': 'running', 'installer': str(installer), 'installer_sha256': digest(installer),
              'run': str(run), 'checks': [], 'real_library_writes': 0, 'real_model_calls': 0,
              'taskbar_visual_inspection': 'not performed; checks use native identity and API logs'}
    native = NativeProcesses()
    launchers = []
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('EZREAD_', 'TUDU_', 'PYTHON'))}
    windows = Path(os.environ['WINDIR'])
    env.update(PATH=str(windows / 'System32') + os.pathsep + str(windows),
               EZREAD_DATA_DIR=str(data), EZREAD_PORT=str(port), PYTHONHOME='NO_GLOBAL_PYTHON_PATH',
               PYTHONPATH='NO_GLOBAL_PYTHON_PATH', NO_PROXY='127.0.0.1,localhost')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f'http://127.0.0.1:{port}'

    def request(path):
        with opener.open(origin + path, timeout=2) as response:
            return json.load(response)

    def health():
        try:
            value = request('/api/health')
        except (OSError, ValueError, urllib.error.URLError):
            return None
        require(Path(value.get('app_root', '')).resolve() == installed, 'Health app_root is not the test install.')
        require(Path(value.get('data_dir', '')).resolve() == data, 'Health data_dir is not the isolated library.')
        return value

    def install(stage):
        require(not installations() or all(Path(x['location']).resolve() == installed for x in installations()),
                'Refusing to replace a different registered installation.')
        command([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS',
                 '/TASKS=!desktopicon', '/DIR=' + str(installed), '/LOG=' + str(run / (stage + '-setup.log'))],
                run / (stage + '-output.log'), env, run)
        entries = installations()
        require(entries and all(Path(x['location']).resolve() == installed for x in entries),
                'Installer registration does not match the isolated installation.')
        require((installed / 'EzRead.exe').is_file(), 'Installation did not create EzRead.exe.')
        report['checks'].append(verify_installed_inventory(installed))

    def application_cycle(stage):
        session = uuid.uuid4().hex
        log_path = data / 'desktop.log'
        old_log = log_path.read_text(encoding='utf-8-sig') if log_path.exists() else ''
        with (run / (stage + '-launcher.log')).open('wb') as log:
            launcher = subprocess.Popen([str(installed / 'EzRead.exe'), '--verification-session', session],
                                        cwd=run, env=env, stdout=log, stderr=log,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            launchers.append(launcher)
            startup_python = wait_for(lambda: native.launcher_child(launcher, installed / 'runtime/pythonw.exe'), 10)
            state = wait_for(health, 60)
            backend = native.retain(state['pid'], installed / 'runtime/python.exe')
            report['checks'].append({'stage': stage, 'backend_pid': state['pid'], 'health': state})

            def host_pid():
                if not log_path.exists():
                    return None
                lines = log_path.read_text(encoding='utf-8-sig')[len(old_log):].splitlines()
                for line in lines:
                    if 'window-mode app-id=EzRead.Desktop.Verification.' + session + ' taskbar=False' in line:
                        match = re.search(r'pid=(\d+) child=False ', line)
                        if match:
                            return int(match[1])
                return None

            pid = wait_for(host_pid)
            host = native.retain(pid, installed / 'desktop/bin/EzRead.Desktop.exe')
            require(launcher.wait(timeout=60) == 0, 'Installed Launcher failed to initialize the application.')
            native.exited(startup_python, 5)
            version = json.loads((installed / 'release-manifest.json').read_text(encoding='utf-8'))['version']
            require(state['version'] == version, 'Installed health version does not match release manifest.')
            require(request('/api/papers')['papers'] == [], 'Isolated library is not empty.')
            require(request('/api/pdf-layout')['available'] is True, 'Installed PDF layout engine is unavailable.')
            with opener.open(origin + '/', timeout=5) as response:
                require(response.status == 200 and b'<html' in response.read().lower(), 'Installed UI did not load.')
            native.close_window(pid)
            native.exited(host, 30)
            native.exited(backend, 15)
            lines = [line for line in log_path.read_text(encoding='utf-8-sig').splitlines()
                     if f'pid={pid} ' in line and 'taskbar-remove hwnd=' in line]
            require(lines and all(line.endswith(' hr=0') for line in lines), 'Test taskbar removal was not confirmed.')
            report['checks'].append({'stage': stage, 'host_pid': pid, 'verification_session': session,
                                     'native_close': True, 'taskbar_removal': True,
                                     'embedded_backend_image_verified': True, 'system_python_removed_from_path': True})

    try:
        install('initial')
        application_cycle('initial')
        sentinel = data / 'user-data-sentinel.txt'
        sentinel.write_text('独立验收数据：升级与卸载必须保留。\n', encoding='utf-8')
        sentinel_hash = digest(sentinel)
        code = (
            'import json,pathlib,subprocess,sys; import pdf_layout_engine as engine; '
            'root=pathlib.Path.cwd(); env=engine.runtime_environment(root); '
            'env.pop("PYTHONHOME",None); '
            'exe=root/".deps/babeldoc/runtime/Scripts/python.exe"; '
            'p=subprocess.run([str(exe),"-B",str(root/"pdf_layout_engine.py"),"--probe"],'
            'env=env,capture_output=True,text=True,encoding="utf-8",timeout=240); '
            'pathlib.Path(sys.argv[1]).write_text(p.stdout,encoding="utf-8"); '
            'pathlib.Path(sys.argv[2]).write_text(p.stderr,encoding="utf-8"); '
            'print(json.dumps({"main_python":sys.executable,"engine_python":str(exe),'
            '"home":env["HOME"],"returncode":p.returncode})); sys.exit(p.returncode)'
        )
        probe_env = dict(env)
        probe_env.pop('PYTHONHOME', None)
        probe_env.pop('PYTHONPATH', None)
        command([str(installed / 'runtime/python.exe'), '-B', '-c', code,
                 str(run / 'engine-probe.json'), str(run / 'engine-probe-stderr.log')],
                run / 'engine-environment.json', probe_env, installed, timeout=300)
        probe = json.loads((run / 'engine-probe.json').read_text(encoding='utf-8'))
        engine_environment = json.loads((run / 'engine-environment.json').read_text(encoding='utf-8'))
        require(Path(engine_environment['main_python']).resolve() == installed / 'runtime/python.exe',
                'Engine probe helper used a different Python interpreter.')
        require(Path(engine_environment['home']).resolve() == installed / '.deps/babeldoc/home',
                'Engine probe cache home is not inside the isolated installation.')
        require(probe['status'] == 'verified' and len(probe['assets']) == 182,
                'PDF engine probe did not verify all 182 official assets.')
        report['checks'].append({'engine_probe': True, 'asset_count': len(probe['assets']), 'engine_version': probe['version']})
        # --probe 不执行 PDF 解析。必须使用真实安装运行时完成模拟翻译和产物核验。
        command([sys.executable, str(ROOT / 'scripts/verify-installed-pdf.py'),
                 '--app-root', str(installed), '--output', str(run / 'pdf-pipeline')],
                run / 'pdf-pipeline.log', probe_env, ROOT, timeout=900)
        pipeline = json.loads((run / 'pdf-pipeline/report.json').read_text(encoding='utf-8'))
        require(pipeline['status'] == 'passed' and pipeline['real_model_calls'] == 0,
                'Installed PDF pipeline did not complete offline.')
        report['checks'].append({'installed_pdf_pipeline': True, 'real_model_calls': 0})
        install('upgrade')
        require(digest(sentinel) == sentinel_hash, 'Upgrade modified user data.')
        application_cycle('upgrade')
        report['checks'].append({'same_version_upgrade': True, 'user_data_sha256': sentinel_hash})
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        # 仅结束经过映像路径核验并保留句柄的本测试进程，不按名称或端口杀进程。
        try:
            native.cleanup()
            for launcher in launchers:
                if launcher.poll() is None:
                    launcher.terminate()
                    launcher.wait(timeout=10)
        except Exception as error:
            report.update(status='failed', process_cleanup_error=f'{type(error).__name__}: {error}')
        try:
            entries = installations()
            own_registration = entries and all(Path(x['location']).resolve() == installed for x in entries)
            uninstaller = installed / 'unins000.exe'
            if uninstaller.is_file() and (not entries or own_registration):
                command([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                         '/LOG=' + str(run / 'uninstall.log')], run / 'uninstall-output.log', env, run)
                wait_for(lambda: not (installed / 'EzRead.exe').exists() and not installations(), 30)
                if 'sentinel_hash' in locals():
                    require(digest(sentinel) == sentinel_hash, 'Uninstall changed or deleted user data.')
                report['checks'].append({'uninstall': True, 'registered_app_id_removed': True, 'user_data_retained': data.exists()})
            elif own_registration or report['status'] == 'passed':
                raise RuntimeError('Owned installer registration remains but its uninstaller is missing.')
        except Exception as error:
            report.update(status='failed', cleanup_error=f'{type(error).__name__}: {error}')
        (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(run / 'report.json')}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
