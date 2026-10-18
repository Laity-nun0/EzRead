"""数据目录选择和离线迁移；调用方负责停止后台与原生窗口。"""
from contextlib import closing, contextmanager
import ctypes
from ctypes import wintypes
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from .launching import _kernel


def _identity(root):
    return os.path.normcase(str(Path(root).resolve()))


def location_file(root, environment=None) -> Path:
    env = os.environ if environment is None else environment
    local = Path(env.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    if not local.is_absolute():
        raise ValueError('本机配置目录必须是绝对路径。')
    name = hashlib.sha256(_identity(root).encode('utf-8')).hexdigest() + '.json'
    result = _plain_path(local / 'EzRead' / 'locations' / name)
    if result.is_relative_to(Path(root).resolve()):
        raise ValueError('数据目录选择配置不能保存在程序目录中。')
    return result


def _plain_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('数据目录不能包含符号链接或重解析点。')
    return path.resolve()


def _validate_database(connection, full=True):
    if full and connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise ValueError('文献数据库完整性检查失败。')
    required = {'papers': {'id', 'hash', 'doc', 'deleted'}, 'settings': {'key', 'value'}}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required.keys() <= tables:
        raise ValueError('目录中没有有效的 EzRead 文献数据库。')
    for table, columns in required.items():
        actual = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not columns <= actual:
            raise ValueError('目录中没有有效的 EzRead 文献数据库。')


def _open_database(directory):
    database = _plain_path(directory / 'library.sqlite3')
    if not database.is_file():
        raise ValueError('文献数据库缺失，请恢复原目录后重试。')
    return sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)


def saved_directory(root, environment=None) -> Path | None:
    config = location_file(root, environment)
    if not config.exists():
        return None
    try:
        record = json.loads(config.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('version') != 1 or record.get('app_root') != _identity(root):
            raise ValueError('数据目录选择配置版本或程序目录不匹配。')
        value = record.get('data_dir')
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('已保存的数据目录无效。')
        directory = _plain_path(value)
        with closing(_open_database(directory)) as connection:
            _validate_database(connection, full=False)
        return directory
    except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
        raise ValueError('无法读取已选择的文献库；请恢复目录或修复选择配置，未切换到空库。') from error


def save_directory(root, directory, environment=None):
    directory = _plain_path(directory)
    config = location_file(root, environment)
    config.parent.mkdir(parents=True, exist_ok=True)
    record = {'version': 1, 'app_root': _identity(root), 'data_dir': str(directory)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config.parent,
                                         prefix=config.stem + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def location_lock(root, timeout=45):
    """跨进程串行化同一程序目录的启动、目录读取和迁移。"""
    return _named_lock('Location', root, timeout)


def data_use_lock(directory, timeout=0):
    """按实际数据目录独占，防止不同程序副本同时使用同一个库。"""
    return _named_lock('DataUse', directory, timeout)


@contextmanager
def _named_lock(kind, path, timeout):
    kernel = _kernel()
    name = 'Local\\EzRead.' + kind + '.' + hashlib.sha256(_identity(path).encode('utf-8')).hexdigest()
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = False
    try:
        result = kernel.WaitForSingleObject(handle, max(0, min(int(timeout * 1000), 0xFFFFFFFE)))
        owned = result in (0, 0x80)
        if not owned:
            raise ValueError('文献目录正在被另一个启动或迁移操作使用，请稍后重试。')
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(directory):
    files, directories = {}, []
    def inaccessible(error):
        raise error

    for parent, folders, names in os.walk(directory, followlinks=False, onerror=inaccessible):
        for name in folders + names:
            path = Path(parent) / name
            _plain_path(path)
            info = path.stat()
            relative = path.relative_to(directory)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = (info.st_size, info.st_mtime_ns, _hash(path))
            else:
                raise ValueError('文献目录含有不支持的特殊文件。')
    return files, sorted(directories)


def _restrict_directory(directory):
    """拒绝不支持持久 ACL 的磁盘，副本仅由当前 Windows 用户访问。"""
    kernel = _kernel()
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    volume = ctypes.create_unicode_buffer(32768)
    flags = wintypes.DWORD()
    if not kernel.GetVolumePathNameW(str(directory), volume, len(volume)) or not kernel.GetVolumeInformationW(
            volume.value, None, 0, None, None, ctypes.byref(flags), None, 0) or not flags.value & 8:
        raise ValueError('目标磁盘不支持文献库所需的文件权限保护，未切换目录。')
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    identity = subprocess.run([str(system / 'whoami.exe'), '/user', '/fo', 'csv', '/nh'],
                              capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    rows = list(csv.reader(identity.stdout.decode('utf-8', errors='replace').splitlines()))
    sid = rows[0][-1] if len(rows) == 1 and len(rows[0]) == 2 else ''
    if identity.returncode or not re.fullmatch(r'S-1-\d+(?:-\d+)+', sid):
        raise ValueError('无法取得当前用户的文件权限身份，未切换目录。')
    # 新目录也可能自带 SYSTEM 等显式 ACE，先重置再移除继承，避免残留额外授权。
    reset = subprocess.run([str(system / 'icacls.exe'), str(directory), '/reset'], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if reset.returncode:
        raise ValueError('无法重置目标副本的文件权限，未切换目录。')
    result = subprocess.run([str(system / 'icacls.exe'), str(directory), '/inheritance:r',
                             '/grant:r', f'*{sid}:(OI)(CI)F'], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if result.returncode:
        raise ValueError('无法保护目标副本的文件权限，未切换目录。')


def migrate_library(root, source, target, environment=None) -> dict:
    """持有 location_lock、源和目标 data_use_lock 且所属进程停止后调用；源永不删除。"""
    if not Path(source).is_absolute() or not Path(target).is_absolute():
        raise ValueError('源文献目录和目标目录必须使用绝对路径。')
    source, target = _plain_path(source), _plain_path(target)
    root = Path(root).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('源目录和目标目录不能相同或互相包含。')
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('目标文献目录不能与程序目录相同或互相包含。')
    if not source.is_dir():
        raise ValueError('源文献目录不存在。')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('目标目录必须为空，不能合并或覆盖已有文件。')
    # 在产生目标副本前验证配置位置；最终保存失败仍保留完整副本。
    location_file(root, environment)
    try:
        with closing(_open_database(source)) as connection:
            _validate_database(connection)
            before, directories = _inventory(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            required = sum(item[0] for item in before.values()) + before[Path('library.sqlite3')][0]
            if shutil.disk_usage(target.parent).free < required:
                raise ValueError('目标磁盘空间不足，未切换文献目录。')
            staging = Path(tempfile.mkdtemp(prefix=target.name + '.ezread-migration-', dir=target.parent))
            _restrict_directory(staging)
            for directory in directories:
                (staging / directory).mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(staging / 'library.sqlite3')) as backup:
                connection.backup(backup)
                _validate_database(backup)
                backup.execute('PRAGMA journal_mode=DELETE')
            excluded = {Path('library.sqlite3'), Path('library.sqlite3-wal'),
                        Path('library.sqlite3-shm'), Path('library.sqlite3-journal')}
            for relative, expected in before.items():
                if relative in excluded:
                    continue
                destination = staging / relative
                shutil.copyfile(source / relative, destination)
                if destination.stat().st_size != expected[0] or _hash(destination) != expected[2]:
                    raise ValueError('迁移副本核验失败，未切换文献目录。')
                with destination.open('r+b') as stream:
                    os.fsync(stream.fileno())
            with (staging / 'library.sqlite3').open('r+b') as stream:
                os.fsync(stream.fileno())
            if _inventory(source) != (before, directories):
                raise ValueError('迁移期间源文献目录发生变化，请关闭相关程序后重试。')
        _plain_path(target)
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise ValueError('目标目录已发生变化，未覆盖已有文件。')
            target.rmdir()
        # Windows 的 rename 在目标已存在时失败，不覆盖竞争者创建的目录。
        staging.rename(target)
        copied_files = sum(1 for path in target.rglob('*') if path.is_file())
        copied_bytes = sum(path.stat().st_size for path in target.rglob('*') if path.is_file())
        save_directory(root, target, environment)
        return {'source': str(source), 'target': str(target), 'source_preserved': True,
                'copied_files': copied_files, 'bytes': copied_bytes}
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise ValueError('文献迁移或目录选择保存失败；原目录及旧选择保留，目标副本可能已保留。') from error
