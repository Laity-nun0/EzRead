"""重新提取指定论文的版面；默认只预览，--apply 在线备份后保留原文与阅读数据提交。"""
from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_tools
from ezread.config import data_directory, listen_port
from ezread.extraction import repair_document
from ezread.launching import startup_lock


def ensure_offline(data):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{listen_port()}/api/health', timeout=2) as response:
            health = json.load(response)
    except (OSError, urllib.error.URLError):
        return
    if Path(health.get('data_dir', '')).resolve() == data:
        raise RuntimeError('请通过主窗口正常关闭当前 EzRead，完成保存后再应用补正。')


def backup(data, paper_ids, folder):
    folder.mkdir(parents=True)
    with closing(sqlite3.connect((data / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(folder / 'library.sqlite3')) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库备份校验失败，未补正文献。')
    archive = folder / 'resources.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
        for pid in paper_ids:
            for path in sorted((data / 'library' / pid).rglob('*')):
                if path.is_file():
                    target.write(path, path.relative_to(data / 'library'))
    with zipfile.ZipFile(archive) as target:
        if target.testzip() is not None:
            raise RuntimeError('资源备份校验失败，未补正文献。')


def commit(data, paper_ids, plans, observed, folder, snapshot):
    lock = startup_lock(ROOT, data, f'http://127.0.0.1:{listen_port()}') if sys.platform == 'win32' else nullcontext()
    with lock:
        ensure_offline(data)
        backup(data, paper_ids, snapshot)
        with closing(sqlite3.connect(data / 'library.sqlite3', timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            for pid, plan in plans.items():
                if connection.execute('SELECT doc FROM papers WHERE id=?', (pid,)).fetchone()[0] != observed[pid]:
                    raise RuntimeError('补正准备期间文献发生变化，未提交；请重新预览。')
                for old, new in plan['files'].items():
                    source, destination = folder / pid / old, data / 'library' / pid / new
                    if destination.exists():
                        raise RuntimeError('新资源名称已存在，未覆盖文件。')
                    shutil.copy2(source, destination)
                    if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
                        raise RuntimeError('资源复制校验失败，未提交数据库。')
                connection.execute('UPDATE papers SET doc=? WHERE id=?',
                                   (json.dumps(plan['doc'], ensure_ascii=False), pid))
            connection.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper', action='append', required=True, help='文献 ID，可重复指定。')
    parser.add_argument('--data-dir', type=Path, default=data_directory(ROOT))
    parser.add_argument('--apply', action='store_true', help='正常关闭应用后备份并提交；默认只生成预览。')
    args = parser.parse_args()
    data = args.data_dir.resolve()
    paper_ids = list(dict.fromkeys(args.paper))
    if any(len(pid) != 16 or any(char not in '0123456789abcdef' for char in pid) for pid in paper_ids):
        raise ValueError('无效的文献 ID。')
    if args.apply:
        ensure_offline(data)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    run = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    folder = ROOT / 'work' / 'layout-repair' / run
    folder.mkdir(parents=True)
    plans, observed = {}, {}
    with closing(sqlite3.connect((data / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)) as connection:
        for pid in paper_ids:
            row = connection.execute('SELECT doc FROM papers WHERE id=? AND deleted=0', (pid,)).fetchone()
            if row is None:
                raise ValueError('论文不存在或已进入回收站。')
            observed[pid] = row[0]
    for pid in paper_ids:
        doc = json.loads(observed[pid])
        if doc.get('translation', {}).get('status') in ('queued', 'running'):
            raise ValueError('存在未停止的翻译任务，未补正文献。')
        assets = data / 'library' / pid
        stage = folder / pid
        extracted = pdf_tools.extract_document(assets / 'original.pdf', stage)
        files = {b['image'] for b in extracted['blocks'] if b.get('image')}
        files.update(f['path'] for f in extracted['figures'])
        names = {name: f'layout-{run}-{name}' for name in files}
        for block in extracted['blocks']:
            if block.get('image'):
                block['image'] = names[block['image']]
        for figure in extracted['figures']:
            figure['path'] = names[figure['path']]
        updated, report = repair_document(doc, extracted, stamp)
        (stage / 'document-preview.json').write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        plans[pid] = {'doc': updated, 'report': report, 'files': names}
        print(json.dumps({'paper_id': pid, 'updated': len(report['updated']), 'added': len(report['added']),
                          'hidden': len(report['hidden']), 'warnings': report['warnings'],
                          'original_blocks': len(doc['blocks']), 'retained_blocks': len(updated['blocks'])}, ensure_ascii=False), flush=True)
    report_file = folder / 'report.json'
    report_file.write_text(json.dumps({'mode': 'preview', 'papers': {pid: plan['report'] for pid, plan in plans.items()}},
                                    ensure_ascii=False, indent=2), encoding='utf-8')
    if args.apply:
        snapshot = ROOT / 'work' / 'backups' / f'layout-repair-{run}'
        commit(data, paper_ids, plans, observed, folder, snapshot)
        report_file.write_text(json.dumps({'mode': 'applied', 'backup': str(snapshot.relative_to(ROOT)),
                                          'papers': {pid: plan['report'] for pid, plan in plans.items()}},
                                         ensure_ascii=False, indent=2), encoding='utf-8')
        print('补正已提交；备份：' + str(snapshot), flush=True)
    print('报告：' + str(report_file), flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
