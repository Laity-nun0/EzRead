"""原生迁移握手；不向 HTTP 暴露任意本机文件路径操作。"""
import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

from . import data_location
from .launching import _kernel


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path):
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('迁移控制记录过大。')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('迁移控制记录无效。')
    return value


def _hold_process(stack, kernel, pid, expected=None):
    if pid <= 0:
        raise ValueError('迁移关联进程标识无效。')
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        raise ValueError('迁移关联进程已退出，未开始复制。')
    stack.callback(kernel.CloseHandle, handle)
    if expected is not None:
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        size, image = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if (not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) or
                Path(image.value).resolve() != expected.resolve()):
            raise ValueError('迁移请求不属于当前程序的桌面窗口。')
    return handle


def _wait_exit(kernel, handle, seconds, label):
    if kernel.WaitForSingleObject(handle, int(seconds * 1000)) != 0:
        raise ValueError(f'{label}尚未退出，未迁移文献目录。')


def _health(options):
    origin = urllib.parse.urlsplit(options.url)
    if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or
            origin.username or origin.password or origin.query or origin.fragment or
            origin.path not in ('', '/')):
        raise ValueError('迁移仅连接当前本机服务。')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(options.url.rstrip('/') + '/api/health', timeout=2) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('后台身份响应无效。')
    state = json.loads(raw)
    if (not isinstance(state, dict) or
            Path(state.get('app_root', '')).resolve() != options.root or
            Path(state.get('data_dir', '')).resolve() != options.source or
            type(state.get('pid')) is not int or state.get('closing')):
        raise ValueError('后台或文献库与当前窗口不一致，未开始迁移。')
    return state, origin.port or 80


def migrate_after_close(options):
    control = options.control_dir
    control.mkdir(parents=True, exist_ok=True)
    with data_location.location_lock(options.root), ExitStack() as stack:
        kernel = _kernel()
        owner = _hold_process(stack, kernel, options.owner_pid,
                              options.root / 'desktop/bin/EzRead.Desktop.exe')
        browser = _hold_process(stack, kernel, options.browser_pid)
        state, port = _health(options)
        backend = _hold_process(stack, kernel, state['pid'])
        confirmation, _ = _health(options)
        if (confirmation['pid'] != state['pid'] or
                not state.get('instance_id') or confirmation.get('instance_id') != state['instance_id']):
            raise ValueError('后台实例已变化，未开始迁移。')
        # 持锁并绑定稳定句柄之后才能通知旧窗口关闭。
        _write_json(control / 'ready.json', {'ready': True, 'pid': os.getpid()})
        _wait_exit(kernel, owner, 120, '原窗口')
        approval = _read_json(control / 'approved.json')
        if approval != {'approved': True, 'owner_pid': options.owner_pid}:
            raise ValueError('窗口未确认保存关闭，未开始迁移。')
        _wait_exit(kernel, backend, 30, '原后台')
        _wait_exit(kernel, browser, 30, '原浏览器数据进程')
        # 不接管同端口的新后台，也不在另一份程序仍使用文献库时复制。
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise ValueError('端口已被另一个后台使用，请关闭相关程序后重新迁移。')
        with data_location.data_use_lock(options.source), data_location.data_use_lock(options.target):
            return data_location.migrate_library(options.root, options.source, options.target)


def _restart(options, *, original=False):
    environment = dict(os.environ)
    if original:
        environment['EZREAD_DATA_DIR'] = str(options.source)
    command = [sys.executable, '-B', str(options.root / 'launch.pyw')]
    if options.verification_session:
        command.extend(['--verification-session', options.verification_session])
    return subprocess.Popen(command, cwd=options.root, env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'source', 'target', 'control-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--owner-pid', type=int, required=True)
    parser.add_argument('--browser-pid', type=int, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--verification-session')
    options = parser.parse_args(argv)
    for name in ('root', 'source', 'target', 'control_dir'):
        path = getattr(options, name)
        if not path.is_absolute():
            parser.error('迁移路径必须为绝对路径。')
        # source/target 保留原路径，核心还要检查每一级的 reparse point。
        setattr(options, name, path.absolute())
    options.root = options.root.resolve()
    if options.verification_session:
        options.verification_session = uuid.UUID(options.verification_session).hex
    start = time.monotonic()
    report = {'status': 'failed', 'source_preserved': True}
    try:
        report.update(migrate_after_close(options), status='passed')
    except Exception as error:
        report['error'] = str(error)
    report['elapsed_seconds'] = round(time.monotonic() - start, 3)
    options.control_dir.mkdir(parents=True, exist_ok=True)
    _write_json(options.control_dir / 'report.json', report)
    success = report['status'] == 'passed'
    approved = (options.control_dir / 'approved.json').is_file()
    if not options.verification_session and not success:
        ctypes.windll.user32.MessageBoxW(0, report['error'] + '\n原文献目录保留，未删除任何文献。',
                                         'EzRead 文献迁移未完成', 0x10)
    # 释放位置锁后重新启动；没有获准关闭时不能再创建一个窗口。
    if success or approved:
        try:
            _restart(options, original=not success)
        except OSError:
            if not options.verification_session:
                ctypes.windll.user32.MessageBoxW(0, '文献已保留，请重新双击 EzRead 打开。', 'EzRead', 0x10)
    return 0 if success else 1
