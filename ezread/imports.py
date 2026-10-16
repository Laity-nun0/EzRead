"""PDF ingestion, deduplication and local source structure."""
from __future__ import annotations
from .context import ApplicationContext
from contextlib import nullcontext
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from pathlib import Path, PureWindowsPath


def _asset_path(root, name):
    name = str(name)
    windows = PureWindowsPath(name)
    if not name or windows.drive or windows.root or '..' in windows.parts:
        raise ValueError('文献资源路径无效，未修改已有数据。')
    path = root.joinpath(*windows.parts)
    for candidate in (root, *path.relative_to(root).parents, path):
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('文献资源包含链接，未修改已有数据。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('文献资源越界，未修改已有数据。')
    if path.exists() and not path.is_file():
        raise ValueError('文献资源不是普通文件，未修改已有数据。')
    return path


def _required_assets(doc):
    # 后续页面按需渲染，未缓存不表示缺失；首页、封面及正文图像必须可读。
    names = {'original.pdf', 'page-001.jpg', doc.get('cover') or 'page-001.jpg'}
    names.update(f['path'] for f in doc.get('figures', []) if f.get('path'))
    names.update(b['image'] for b in doc.get('blocks', []) if b.get('image'))
    return names


def _repair_duplicate(app, doc, content, fingerprint):
    import pdf_tools
    pid = doc['id']
    if not re.fullmatch(r'[a-f0-9]{16}', pid):
        raise ValueError('文献资源目录无效，未修改已有数据。')
    _asset_path(app.LIBRARY, f'{pid}/original.pdf')
    root = app.LIBRARY / pid
    paths = {name: _asset_path(root, name) for name in _required_assets(doc)}
    original = paths['original.pdf']
    if original.exists() and hashlib.sha256(original.read_bytes()).hexdigest() != fingerprint:
        raise ValueError('已有原 PDF 与导入指纹不一致，未覆盖已有数据。')
    missing = {name for name, path in paths.items() if not path.exists()}
    if not missing:
        return
    with tempfile.TemporaryDirectory(prefix='import-repair-', dir=app.DATA / 'tmp') as folder:
        stage = Path(folder)
        source = stage / 'upload.pdf'
        source.write_bytes(content)
        assets = stage / 'assets'
        assets.mkdir()
        if missing - {'original.pdf'}:
            pdf_tools.extract_document(source, assets)
        # 使用本次已核验哈希的上传补原件，不依赖提取器是否复制原件。
        (assets / 'original.pdf').write_bytes(content)
        for name in missing:
            if not _asset_path(assets, name).is_file():
                raise ValueError(f'无法重建缺失资源 {name}，文献状态未恢复。')
        # 全部准备完成后才补缺；硬链接原子创建，绝不替换现存文件。
        for name in missing:
            target = _asset_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(_asset_path(assets, name), target)


def import_pdf(app: ApplicationContext, content, filename):
    import pdf_tools
    if not content or b'%PDF-' not in content[:1024]:
        raise ValueError('文件不是有效的 PDF。')
    # 与永久删除共用文件锁；锁序保持 FILE_ACTION_LOCK → LOCK → 数据库。
    with app.FILE_ACTION_LOCK:
        return _import_pdf(app, content, filename, pdf_tools)


def _import_pdf(app, content, filename, pdf_tools):
    fingerprint = hashlib.sha256(content).hexdigest()
    with app.LOCK, app.db() as con:
        duplicate = con.execute('SELECT id,deleted FROM papers WHERE hash=?', (fingerprint,)).fetchone()
    orphan = None
    if duplicate:
        old_pid = duplicate[0]
        if not re.fullmatch(r'[a-f0-9]{16}', old_pid):
            raise ValueError('文献资源目录无效，未修改已有数据。')
        _asset_path(app.LIBRARY, f'{old_pid}/original.pdf')
        if (app.LIBRARY / old_pid).exists():
            doc = app.get_doc(old_pid, include_deleted=True)
            _repair_duplicate(app, doc, content, fingerprint)
            if duplicate[1]:
                app.update_doc(old_pid, lambda d: d.update(deleted=False))
            doc = app.public_doc(app.get_doc(old_pid))
            doc['duplicate'] = True
            return doc
        # 整目录已消失的孤儿记录按全新导入；提取成功前保留旧记录。
        orphan = old_pid
    pid = secrets.token_hex(8)
    _asset_path(app.LIBRARY, f'{pid}/original.pdf')
    asset_dir = app.LIBRARY / pid
    asset_dir.mkdir()
    source = app.DATA / 'tmp' / f'{pid}.pdf'
    persisted = False
    orphan_lock = None
    try:
        source.write_bytes(content)
        extracted = pdf_tools.extract_document(source, asset_dir)
        metadata = extracted.pop('metadata', {})
        # Accommodate top-level metadata without confusing document geometry.
        for key in ('title', 'doi', 'authors', 'journal', 'year', 'abstract'):
            if extracted.get(key) and not metadata.get(key):
                metadata[key] = extracted[key]
        import paper_metadata
        metadata = paper_metadata.normalize_publication(metadata)
        doc = {**extracted, **metadata, 'id': pid, 'hash': fingerprint, 'filename': Path(filename).name,
               'title': metadata.get('title') or Path(filename).stem,
               'title_zh': '', 'summary': '', 'overview_sections': [],
               'journal': metadata.get('journal', ''), 'journal_abbr': app.journal_abbr(metadata.get('journal', '')),
               'paper_type': metadata.get('paper_type') or ('journal' if metadata.get('journal') else 'other'),
               'conference_name': metadata.get('conference_name', ''), 'conference_abbr': metadata.get('conference_abbr', ''),
               'conference_track': metadata.get('conference_track', 'unknown'),
               'authors': metadata.get('authors', []), 'tags': [], 'collection': '', 'favorite': False,
               'created_at': app.now(), 'updated_at': app.now(), 'last_read': '', 'read_page': 1,
               'notes': '', 'deleted': False,
               'translation': {'status': 'idle', 'error': ''}}
        if not doc.get('blocks'):
            doc['import_warning'] = '未提取到可选文字，可能是扫描件。可查看原文和选封面，暂不能全文翻译。'
        import paper_structure
        doc['reading_structure'] = paper_structure.organize(doc)
        from .publication import assessment
        doc['metadata_enrichment'] = assessment(doc)
        for name in _required_assets(doc):
            if not _asset_path(asset_dir, name).is_file():
                raise ValueError(f'提取后缺少资源 {name}，未提交文献。')
        if hashlib.sha256((asset_dir / 'original.pdf').read_bytes()).hexdigest() != fingerprint:
            raise ValueError('提取后的原 PDF 指纹不一致，未提交文献。')
        import paper_ai
        from .library import paper_cleanup_scope
        from .storage import delete_paper_records
        if orphan:
            lock = paper_ai.paper_lock(orphan)
            # 文件锁已持有；不能等待会话锁，以免与永久删除的相反锁序死锁。
            if not lock.acquire(blocking=False):
                raise ValueError('旧文献对话正在执行，请等待其停止后再作为新文献导入。')
            orphan_lock = lock
        # 旧记录清除、新记录和对话初始化必须一起提交或一起回滚。
        with app.LOCK:
            if orphan:
                if (any(paper_id == orphan for paper_id, _ in app.QUEUED)
                        or orphan in app.STRUCTURE_QUEUED):
                    raise ValueError('旧文献任务尚未停止，请停止后再作为新文献导入。')
            with paper_cleanup_scope(app, orphan) if orphan else nullcontext():
                with app.db() as con:
                    if orphan:
                        delete_paper_records(con, orphan)
                    con.execute('INSERT INTO papers(id,hash,doc,deleted) VALUES(?,?,?,0)',
                                (pid, fingerprint, json.dumps(doc, ensure_ascii=False)))
                    paper_ai.ensure(con, pid)
                # 数据库已提交；恢复副本清理失败也不能删除新文献资源。
                persisted = True
                if orphan:
                    app.DELETED_PAPERS.add(orphan)
        if doc.get('blocks'):
            app.schedule_structure(pid)
        result = app.public_doc(app.get_doc(pid))
        if orphan:
            result['replaced_paper_id'] = orphan
        return result
    except Exception:
        # Only clean this import's newly created directory, never existing papers.
        if not persisted and asset_dir.parent == app.LIBRARY and re.fullmatch(r'[a-f0-9]{16}', asset_dir.name):
            shutil.rmtree(asset_dir, ignore_errors=True)
        raise
    finally:
        if orphan_lock is not None:
            orphan_lock.release()
        source.unlink(missing_ok=True)
