"""Historical text translation snapshots and draft recovery."""
from __future__ import annotations
from .context import ApplicationContext
import copy
import secrets


def version_metadata(version):
    return {k: copy.deepcopy(version.get(k)) for k in ('id', 'created_at', 'config', 'mixed', 'block_count', 'incomplete')}


def translation_snapshot(app: ApplicationContext, doc):
    current = doc.get('translation_current', {})
    blocks = {b['id']: {k: copy.deepcopy(b[k]) for k in app.TRANSLATION_FIELDS if k in b} for b in doc.get('blocks', [])}
    return {'id': secrets.token_hex(8), 'created_at': app.now(), 'config': copy.deepcopy(current.get('config')),
            'mixed': current.get('mixed', bool(any(b.get('translation') for b in doc.get('blocks', [])) and not current)),
            'block_count': sum(bool(v.get('translation', '').strip()) for v in blocks.values()), 'blocks': blocks}


def archive_translation(app: ApplicationContext, doc):
    snapshot = app.translation_snapshot(doc)
    if snapshot['block_count']:
        doc.setdefault('translation_versions', []).append(snapshot)


def archive_translation_draft(app: ApplicationContext, doc):
    task = doc.get('translation', {})
    if isinstance(task.get('staging'), dict):
        doc.setdefault('translation_drafts', []).append({
            'id': secrets.token_hex(8), 'created_at': app.now(), 'config': copy.deepcopy(task.get('config')),
            'block_count': len(task['staging']), 'incomplete': True, 'task': copy.deepcopy(task)})


def restore_translation_draft(app: ApplicationContext, pid, draft_id):
    with app.LOCK:
        if (pid, 'translate') in app.QUEUED:
            raise ValueError('请先暂停翻译并等待当前批次停止，再恢复重译草稿。')
        doc = app.get_doc(pid)
        draft = next((v for v in doc.get('translation_drafts', []) if v['id'] == draft_id), None)
        if not draft:
            raise ValueError('重译草稿不存在。')
        app.archive_translation_draft(doc)
        doc['translation'] = copy.deepcopy(draft['task'])
        doc['translation'].update(status='retired', error='已恢复历史草稿存档；旧正文全文翻译已停用。')
        doc['updated_at'] = app.now()
        app.put_doc(doc)
        return doc


def restore_translation(app: ApplicationContext, pid, version_id):
    with app.LOCK:
        if (pid, 'translate') in app.QUEUED:
            raise ValueError('请先暂停翻译并等待当前批次停止，再恢复译文版本。')
        doc = app.get_doc(pid)
        version = next((v for v in doc.get('translation_versions', []) if v['id'] == version_id), None)
        if not version:
            raise ValueError('译文版本不存在。')
        app.archive_translation_draft(doc)
        app.archive_translation(doc)
        for block in doc.get('blocks', []):
            saved = version['blocks'].get(block['id'], {})
            for key in app.TRANSLATION_FIELDS:
                block.pop(key, None)
            block.update(copy.deepcopy(saved))
        doc['translation_current'] = {'config': copy.deepcopy(version.get('config')), 'mixed': version.get('mixed', False), 'restored_from': version_id, 'created_at': app.now()}
        config = copy.deepcopy(version.get('config'))
        doc['translation'] = {'status': 'retired', 'error': '', 'config': config}
        counts = app.translation_counts(doc)
        doc['translation']['status'] = 'completed' if counts['done'] == counts['total'] else 'retired'
        doc['updated_at'] = app.now()
        app.put_doc(doc)
        return doc
