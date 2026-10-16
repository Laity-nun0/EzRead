"""Paper metadata, public documents and native file actions."""
from __future__ import annotations
from .context import ApplicationContext
from contextlib import contextmanager
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import urllib.parse
from pathlib import Path


def _atomic_write(path, content):
    handle, temporary = tempfile.mkstemp(prefix=path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _paper_draft_snapshots(app, pid):
    snapshots = []
    try:
        for filename in ('desktop-close-drafts.json', 'webview2-migration.json'):
            path = app.DATA / filename
            if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError('桌面草稿快照包含链接，未删除。')
            if not path.exists():
                continue
            original = path.read_bytes()
            document = json.loads(original.decode('utf-8-sig'))
            values = document.get('values') if filename == 'desktop-close-drafts.json' and isinstance(document, dict) else document
            if not isinstance(document, dict) or not isinstance(values, dict):
                raise ValueError('桌面草稿快照格式无效，未删除。')
            keys = [key for key in values if key.startswith('ezread-reader-') and key.endswith(':' + pid)]
            if keys:
                for key in keys:
                    del values[key]
                snapshots.append((path, original, json.dumps(document, ensure_ascii=False).encode('utf-8')))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('桌面草稿快照无法读取，永久删除未完成。') from exc
    return snapshots


@contextmanager
def paper_cleanup_scope(app: ApplicationContext, pid):
    """调用方持有文件锁与文献锁；数据库事务须在本作用域内完成提交。"""
    if not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{16}', pid):
        raise ValueError('无效的文献 ID。')
    roots = []
    for parent in (app.LIBRARY, app.DATA / 'clipboard'):
        if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('文献父目录包含链接，未删除。')
        root = parent / pid
        if root.is_symlink() or root.resolve().parent != parent.resolve():
            raise ValueError('文献目录包含链接或越界，未删除。')
        if not root.exists():
            continue
        for path in (root, *root.rglob('*')):
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('文献目录包含链接，未删除。')
        if not root.is_dir():
            raise ValueError('文献目录不是文件夹，未删除。')
        roots.append(root)
    temporary = app.DATA / 'tmp'
    if temporary.is_symlink() or (temporary.exists() and getattr(temporary.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('临时目录包含链接，未删除。')
    temporary.mkdir(parents=True, exist_ok=True)
    snapshots = _paper_draft_snapshots(app, pid)
    backup = Path(tempfile.mkdtemp(prefix='delete-recovery-', dir=temporary))
    copies = []
    yielded = False
    try:
        for index, root in enumerate(roots):
            saved = backup / str(index)
            shutil.copytree(root, saved)
            copies.append((root, saved))
        for path, original, _ in snapshots:
            (backup / path.name).write_bytes(original)
        for path, _, content in snapshots:
            _atomic_write(path, content)
        for root, _ in copies:
            shutil.rmtree(root)
        yielded = True
        yield
    except Exception as exc:
        try:
            for root, saved in copies:
                shutil.copytree(saved, root, dirs_exist_ok=True)
            for path, original, _ in snapshots:
                _atomic_write(path, original)
        except OSError as restore_error:
            raise ValueError(f'永久删除失败，恢复副本保留在 {backup}；请勿再次删除：{restore_error}') from exc
        shutil.rmtree(backup, ignore_errors=True)
        if yielded:
            raise
        raise ValueError(f'永久删除失败，文献与数据库已保留：{exc}') from exc
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        raise ValueError(f'文献记录已删除，但恢复副本清理失败，永久删除未完成：{backup}') from exc


def delete_paper(app: ApplicationContext, pid):
    """永久删除；文件操作失败时保留数据库并从临时副本恢复。"""
    import paper_ai
    from .storage import delete_paper_records
    if not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{16}', pid):
        raise ValueError('无效的文献 ID。')
    paper_lock = paper_ai.paper_lock(pid)
    if not paper_lock.acquire(blocking=False):
        raise ValueError('论文对话正在执行，请等待其停止后重试永久删除。')
    try:
        with app.FILE_ACTION_LOCK, app.LOCK:
            app.get_doc(pid, include_deleted=True)
            for (paper_id, kind), event in list(app.CANCEL.items()):
                if paper_id == pid:
                    event.set()
                    app.RESUME_REQUESTED.discard((paper_id, kind))
            if any(paper_id == pid for paper_id, _ in app.QUEUED) or pid in app.STRUCTURE_QUEUED:
                raise ValueError('文献后台任务尚未停止，已请求取消可取消的任务；请等待停止后重试永久删除。')
            try:
                with paper_cleanup_scope(app, pid):
                    with app.db() as con:
                        delete_paper_records(con, pid)
                    app.DELETED_PAPERS.add(pid)
            except (OSError, sqlite3.Error) as exc:
                raise ValueError(f'永久删除失败，文献与数据库已保留：{exc}') from exc
            return {'ok': True, 'message': '已永久删除文献、笔记、译文和全部会话，无法在应用内恢复。'}
    finally:
        paper_lock.release()


def paper_file_action(app: ApplicationContext, pid, action):
    """Resolve an active paper internally; clients cannot submit arbitrary paths."""
    if not re.fullmatch(r'[a-f0-9]{16}', pid) or action not in ('copy-pdf', 'reveal-pdf'):
        raise ValueError('无效的文件操作。')
    doc = app.get_doc(pid)
    source = (app.LIBRARY / pid / 'original.pdf').resolve()
    if not source.is_relative_to(app.LIBRARY.resolve()) or not source.is_file():
        raise ValueError('原 PDF 文件不存在。')
    import desktop_files
    if action == 'reveal-pdf':
        desktop_files.reveal_pdf(source)
        return {'ok': True}
    # Keep the imported filename when pasted, without renaming the library original.
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(doc.get('filename') or 'paper.pdf')).strip(' .')
    filename = filename[:150] or 'paper.pdf'
    if not filename.lower().endswith('.pdf'):
        filename += '.pdf'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', filename, re.I):
        filename = '_' + filename
    folder = app.DATA / 'clipboard' / pid
    target = (folder / filename).resolve()
    if not target.is_relative_to((app.DATA / 'clipboard').resolve()):
        raise ValueError('无效的 PDF 文件名。')
    with app.FILE_ACTION_LOCK:
        # 删除与复制共用文件锁，锁外取得的文献可能已过期。
        app.get_doc(pid)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        desktop_files.copy_pdf(target)
    return {'ok': True, 'filename': filename}


def journal_abbr(name):
    cleaned = re.sub(r'[^a-z0-9]', '', str(name).lower())
    aliases = {
        'ieeetransactionsonrobotics': 'TRO',
        'ieeeroboticsandautomationletters': 'RAL',
        'ieeeasmetransactionsonmechatronics': 'T-MECH',
        'naturecommunications': 'Nat. Commun.',
        'sciencerobotics': 'Sci. Robot.',
        'ieeetransactionsonautomation scienceandengineering'.replace(' ', ''): 'T-ASE',
        'ieeesensorsjournal': 'IEEE Sens. J.',
        'advancedintelligentsystems': 'Adv. Intell. Syst.',
        'softrobotics': 'Soft Robot.',
        'nature': 'Nature', 'science': 'Science',
    }
    return aliases.get(cleaned, name)


def media_url(pid, filename):
    return '/media/' + pid + '/' + urllib.parse.quote(str(filename).replace('\\', '/'), safe='/')


def translation_counts(app: ApplicationContext, doc):
    from .text_actions import translatable_blocks
    target = translatable_blocks(doc)
    t = dict(doc.get('translation', {}))
    staging = t.pop('staging', None)
    current_done = sum(bool(b.get('translation', '').strip()) for b in target)
    done = sum(bool(staging.get(b['id'], '').strip()) for b in target) if isinstance(staging, dict) else current_done
    t.update(total=len(target), done=done, current_done=current_done)
    t['has_staging'] = isinstance(staging, dict)
    t['current_config'] = doc.get('translation_current', {}).get('config')
    t['current_mixed'] = doc.get('translation_current', {}).get('mixed', bool(current_done and not doc.get('translation_current')))
    t['versions'] = [app.version_metadata(v) for v in reversed(doc.get('translation_versions', []))]
    t['drafts'] = [app.version_metadata(v) for v in reversed(doc.get('translation_drafts', []))]
    t.setdefault('status', 'idle')
    t.setdefault('error', '')
    return t


def public_doc(app: ApplicationContext, doc, full=False):
    from . import journal_catalogue
    import paper_ai
    hidden = {'hash', 'blocks', 'pages', 'figures', 'translation_versions', 'translation_current',
              'translation_runs', 'translation_drafts', 'jif', 'jif_year', 'jif_source_url',
              'quartiles', 'metrics_source', 'metrics_sources', 'metrics_status', 'metrics_error',
              'metrics_note', 'conference_rankings', 'read_state', 'reading_structure', 'collection_assignment'}
    hidden.update({'text_annotations', 'text_revision_history', 'text_action_receipts'})
    hidden.update({'team', 'team_sources', 'team_status', 'team_error'})
    hidden.update({'pdf_layout_translation', 'pdf_layout_current', 'pdf_layout_versions', 'pdf_layout_drafts'})
    out = {k: v for k, v in doc.items() if k not in hidden}
    from .publication import assessment
    out['metadata_enrichment'] = assessment(doc)
    out.setdefault('paper_type', 'journal')
    out.setdefault('conference_name', '')
    out.setdefault('conference_abbr', '')
    out.setdefault('conference_track', 'unknown')
    import paper_metadata
    out = paper_metadata.normalize_publication(out)
    if out.get('journal') != doc.get('journal'):
        out['journal_abbr'] = app.journal_abbr(out.get('journal', ''))
    for key in ('journal_tier', 'tier_needs_review', 'catalogue_revision'):
        out.pop(key, None)
    tier_info = journal_catalogue.classify(app.DATA, out)
    out['journal_tier'] = tier_info.pop('tier')
    out.update(tier_info)
    for key in ('tier_basis', 'tier_source_url', 'tier_journal'):
        out.pop(key, None)
    out['cover_url'] = app.media_url(doc['id'], doc.get('cover') or 'page-001.jpg')
    out['translation'] = app.translation_counts(doc)
    from . import pdf_translations
    out['pdf_layout_translation'] = pdf_translations.public_state(app, doc, full)
    out['page_count'] = len(doc.get('pages', []))
    out['original_url'] = app.media_url(doc['id'], 'original.pdf')
    structure = doc.get('reading_structure', {})
    out['structure_status'] = structure.get('status', 'local')
    out['structure_error'] = structure.get('error', '')
    try:
        with app.LOCK, app.db() as con:
            out['paper_ai'] = paper_ai.summary(con, doc['id'])
    except sqlite3.OperationalError as exc:
        if 'no such table: paper_ai' not in str(exc):
            raise
        # Pure metadata previews may run before init_db (including isolated tests).
        out['paper_ai'] = {'generation': 1, 'model': None,
                           'reasoning_effort': None, 'has_chat': False}
    if full:
        import copy
        from . import text_actions
        import paper_structure
        if structure.get('version') != paper_structure.VERSION or structure.get('source_fingerprint') != paper_structure.fingerprint(doc):
            structure = paper_structure.organize(doc)
        out['reading_structure'] = structure
        out['blocks'] = [{**b, 'translation_needs_review': text_actions.review_needed(b), **({'image_url': app.media_url(doc['id'], b['image'])} if b.get('image') else {})} for b in doc.get('blocks', [])]
        out['text_annotations'] = text_actions.resolved_annotations(doc)
        out['text_revision_history'] = copy.deepcopy(doc.get('text_revision_history', []))
        out['pages'] = [{**p, 'image_url': app.media_url(doc['id'], p.get('image', f"page-{p['number']:03}.jpg"))} for p in doc.get('pages', [])]
        out['figures'] = [{**f, 'url': app.media_url(doc['id'], f.get('path', ''))} for f in doc.get('figures', [])]
    return out


def patch_paper_fields(doc, fields):
    import paper_metadata
    publication_changed = any(key in fields and fields[key] != doc.get(key)
                              for key in (*paper_metadata.PUBLICATION_FIELDS, 'title', 'paper_type', 'conference_abbr'))
    doc.update(fields)
    saved = doc.get('metadata_enrichment')
    if publication_changed and isinstance(saved, dict) and saved.get('checked_at'):
        from .publication import assessment
        checked = saved['checked_at']
        saved = assessment(doc)
        saved.update(checked_at=None, previous_checked_at=checked,
                     status='needs_consent' if saved['missing'] else 'complete')
        doc['metadata_enrichment'] = saved
    provenance = doc.setdefault('metadata_provenance', {})
    for key in (*paper_metadata.PUBLICATION_FIELDS, 'title', 'paper_type', 'conference_abbr'):
        if key in fields:
            provenance[key] = {'source': 'manual'}
    if 'collection' in fields:
        doc['collection_assignment'] = {'status': 'manual', 'name': fields['collection'], 'error': ''}
