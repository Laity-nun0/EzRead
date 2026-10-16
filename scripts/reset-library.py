"""离线清空文献、任务、缓存及浏览器草稿；保留 API 配置，默认只预览。"""
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import sys
from types import SimpleNamespace
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezread.config import data_directory, listen_port
from ezread.launching import startup_lock
from ezread.storage import init_db

DIRECTORIES = ('library', 'tmp', 'clipboard', 'webview2-profile', 'browser-profile')
FILES = ('library.sqlite3', 'library.sqlite3-wal', 'library.sqlite3-shm',
         'desktop-close-drafts.json', 'webview2-migration.json', 'webview2-migration.tmp',
         'desktop-window.json', 'desktop.log', 'server.log')


def safe_tree(path, parent):
    """拒绝链接及重解析点；递归删除或移动前始终核对实际边界。"""
    path, parent = Path(path).absolute(), Path(parent).resolve()
    if path == parent or not path.resolve().is_relative_to(parent):
        raise ValueError('清理路径越界，未操作。')
    for ancestor in (path, *path.parents):
        if ancestor == parent:
            break
        if ancestor.is_symlink() or (ancestor.exists() and
                getattr(ancestor.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('清理路径包含链接或重解析点，未操作。')
    if path.exists() and path.is_dir():
        for child in path.iterdir():
            safe_tree(child, parent)
    return path


def counts(data):
    with closing(sqlite3.connect((data / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性检查失败，未清理。')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if not {'papers', 'settings'}.issubset(tables):
            raise ValueError('目录不是 EzRead 文献库，未清理。')
        return {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name in tables}


def api_digest(data):
    path = data / 'translation-api.private.json'
    # 不解析、输出或复制凭据；只比较文件摘要。
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(data):
    data = Path(data).absolute()
    safe_tree(data, ROOT)
    if not data.is_dir():
        raise ValueError('文献目录不存在。')
    for name in DIRECTORIES:
        path = data / name
        if path.exists() and not path.is_dir():
            raise ValueError('资源目录被同名文件占用，未清理。')
    for name in FILES:
        if (data / name).exists() and not (data / name).is_file():
            raise ValueError('数据文件被同名目录占用，未清理。')
    return {'data_dir': str(data), 'rows': counts(data),
            'reset_paths': [name for name in (*DIRECTORIES, *FILES) if (data / name).exists()],
            'library_files': sum(path.is_file() for path in (data / 'library').rglob('*')),
            'api_configuration_preserved': True}


def ensure_offline(data):
    try:
        # Windows 回环连接的明确拒绝可能晚于两秒；超时仍不能当作已关闭。
        with urllib.request.urlopen(f'http://127.0.0.1:{listen_port()}/api/health', timeout=5) as response:
            health = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('服务端口正在使用，未清理。') from exc
    except (OSError, urllib.error.URLError) as exc:
        reason = getattr(exc, 'reason', exc)
        if isinstance(reason, ConnectionRefusedError):
            return
        raise RuntimeError('无法确认服务已关闭，未清理。') from exc
    if Path(health.get('data_dir', '')).resolve() == data.resolve():
        raise RuntimeError('请先通过主窗口正常关闭 EzRead，未清理运行中的文献库。')


def snapshot(data, folder):
    folder.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect((data / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(folder / 'library.sqlite3')) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库备份校验失败。')
    with zipfile.ZipFile(folder / 'resources.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('library', 'clipboard'):
            for path in sorted((data / name).rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(data))
    with zipfile.ZipFile(folder / 'resources.zip') as archive:
        if archive.testzip() is not None:
            raise RuntimeError('资源备份校验失败。')


def reset(data, folder, *, engine_cache=False):
    """先备份并生成空库；全部安装及核验完成前保留可回滚的原目录。"""
    data, folder = Path(data).absolute(), Path(folder).absolute()
    safe_tree(folder, ROOT)
    if folder.is_relative_to(data) or data.is_relative_to(folder):
        raise ValueError('备份与活动文献目录不能重叠。')
    lock = startup_lock(ROOT, data, f'http://127.0.0.1:{listen_port()}') if sys.platform == 'win32' else nullcontext()
    with lock:
        ensure_offline(data)
        before = inspect(data)
        targets = [data / name for name in (*DIRECTORIES, *FILES)]
        if engine_cache:
            base = ROOT / '.deps' / 'babeldoc'
            targets.extend([base / 'tmp', *(base / 'home' / '.cache' / 'babeldoc' / name
                            for name in ('cache.v1.db', 'cache.v1.db-wal', 'cache.v1.db-shm'))])
            for path in targets:
                safe_tree(path, ROOT)
            before['engine_cache_paths'] = [str(path.relative_to(ROOT)) for path in targets
                                            if not path.is_relative_to(data) and path.exists()]
        digest = api_digest(data)
        snapshot(data, folder)
        stage, retired = folder / 'empty', folder / 'retired'
        stage.mkdir()
        init_db(SimpleNamespace(DATA=stage, LIBRARY=stage / 'library', all_docs=lambda **kwargs: []))
        # 安装阶段保持单文件，避免只读核验生成 WAL/SHM 阻碍失败回滚；应用启动会启用 WAL。
        with closing(sqlite3.connect(stage / 'library.sqlite3')) as connection:
            connection.execute('PRAGMA journal_mode=DELETE')
        # 空迁移标记阻止旧 Chromium 草稿在新 profile 中重新出现。
        (stage / 'webview2-migration.json').write_text('{}', encoding='utf-8')
        retired.mkdir()
        moved, installed = [], []
        try:
            # 资源/profile 若仍被窗口占用，会先失败；不结束不明归属进程。
            for index, source in enumerate(targets):
                safe_tree(source, ROOT)
                if source.exists():
                    saved = retired / str(index)
                    source.rename(saved)
                    moved.append((source, saved))
            for source in stage.iterdir():
                source.rename(data / source.name)
                installed.append(source.name)
            after = inspect(data)
            if any(after['rows'].values()) or after['library_files'] or api_digest(data) != digest:
                raise RuntimeError('清空后核验失败，恢复原文献库。')
        except BaseException:
            for name in reversed(installed):
                safe_tree(data / name, data).rename(stage / name)
            for source, saved in reversed(moved):
                safe_tree(saved, retired).rename(source)
            raise
        report = {'before': before, 'after': after, 'backup': str(folder),
                  'real_model_calls': 0, 'api_configuration_unchanged': True}
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        for path in (retired, stage):
            shutil.rmtree(safe_tree(path, folder))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=data_directory(ROOT))
    parser.add_argument('--apply', action='store_true', help='正常关闭应用后备份并清空。')
    parser.add_argument('--engine-cache', action='store_true', help='同时清空项目内 BabelDOC 翻译缓存及临时文件，保留公开资源。')
    args = parser.parse_args()
    data = args.data_dir.absolute()
    if args.apply:
        run = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        report = reset(data, ROOT / 'work' / 'backups' / f'library-reset-{run}', engine_cache=args.engine_cache)
    else:
        report = inspect(data)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
