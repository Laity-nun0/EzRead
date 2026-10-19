"""SQLite connection, documents and settings persistence."""
from __future__ import annotations
from .context import ApplicationContext
from .preferences import THEMES
import copy
import contextlib
import json
import re
import sqlite3


def init_db(app: ApplicationContext):
    app.LIBRARY.mkdir(parents=True, exist_ok=True)
    (app.DATA / 'tmp').mkdir(exist_ok=True)
    with contextlib.closing(sqlite3.connect(app.DATA / 'library.sqlite3')) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, hash TEXT UNIQUE, doc TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)')
        con.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        import paper_ai
        paper_ai.init(con)
        con.commit()
    for doc in app.all_docs(include_deleted=True):
        changed = False
        for key, value in {'paper_type': 'journal', 'conference_name': '', 'conference_abbr': '',
                           'conference_track': 'unknown'}.items():
            if key not in doc:
                doc[key] = value
                changed = True
        if doc.get('translation', {}).get('status') in ('running', 'queued', 'paused', 'error'):
            doc['translation']['status'] = 'retired'
            doc['translation']['error'] = '旧正文全文翻译已停用，历史译文和草稿已保留。'
            changed = True
        if doc.get('pdf_layout_translation', {}).get('status') in ('running', 'queued'):
            doc['pdf_layout_translation'].update(status='paused', stage='翻译已暂停', error='上次版式任务已保存，可继续翻译。')
            changed = True
        for kind in ('summarize', 'team'):
            if doc.get(kind + '_status') in ('running', 'queued'):
                doc[kind + '_status'] = 'error'
                doc[kind + '_error'] = '上次任务未完成，可重新启动。'
                changed = True
        if changed:
            app.put_doc(doc)


@contextlib.contextmanager
def db(app: ApplicationContext):
    con = sqlite3.connect(app.DATA / 'library.sqlite3', timeout=30)
    try:
        with con:
            yield con
    finally:
        con.close()


def all_docs(app: ApplicationContext, include_deleted=False):
    with app.LOCK, app.db() as con:
        rows = con.execute('SELECT doc FROM papers' + ('' if include_deleted else ' WHERE deleted=0')).fetchall()
        return [json.loads(r[0]) for r in rows]


def get_doc(app: ApplicationContext, pid, include_deleted=False):
    with app.LOCK, app.db() as con:
        row = con.execute('SELECT doc FROM papers WHERE id=?' + ('' if include_deleted else ' AND deleted=0'), (pid,)).fetchone()
        if not row:
            raise ValueError('这篇文献不存在或已在回收站中。')
        return json.loads(row[0])


def put_doc(app: ApplicationContext, doc):
    with app.LOCK, app.db() as con:
        if doc['id'] in getattr(app, 'DELETED_PAPERS', set()):
            raise ValueError('文献已永久删除，旧任务结果未保存。')
        con.execute('INSERT INTO papers(id,hash,doc,deleted) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc,deleted=excluded.deleted',
                    (doc['id'], doc['hash'], json.dumps(doc, ensure_ascii=False), int(doc.get('deleted', False))))


def delete_paper_records(con, pid):
    """在调用方事务内删除文献及全部会话；兼容尚未创建 AI 表的旧库。"""
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in (('paper_ai_messages', 'paper_id'), ('paper_ai_generations', 'paper_id'),
                          ('paper_ai', 'paper_id'), ('papers', 'id')):
        if table in tables:
            con.execute(f'DELETE FROM {table} WHERE {column}=?', (pid,))


def update_doc(app: ApplicationContext, pid, fn):
    with app.LOCK:
        doc = app.get_doc(pid, include_deleted=True)
        fn(doc)
        doc['updated_at'] = app.now()
        app.put_doc(doc)
        return doc


def settings(app: ApplicationContext):
    out = copy.deepcopy(app.DEFAULT_SETTINGS)
    with app.LOCK, app.db() as con:
        for k, v in con.execute('SELECT key,value FROM settings'):
            if k in app.DEFAULT_SETTINGS:
                out[k] = json.loads(v)
    if out.get('theme') not in THEMES:
        out['theme'] = app.DEFAULT_SETTINGS['theme']
    return out


def delete_collection(app: ApplicationContext, name):
    """原子移除合集及其归属，保留文献和历史回收站中的内容。"""
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or re.search(r'[\x00-\x1f]', name):
        raise ValueError('合集名称须为不超过 100 字的非空单行文字。')
    with app.LOCK, app.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT value FROM settings WHERE key='collections'").fetchone()
        collections = json.loads(row[0]) if row else app.DEFAULT_SETTINGS['collections']
        con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                    ('collections', json.dumps([item for item in collections if item != name], ensure_ascii=False)))
        for pid, raw in con.execute('SELECT id,doc FROM papers').fetchall():
            doc = json.loads(raw)
            if doc.get('collection') != name:
                continue
            doc['collection'] = ''
            doc['collection_assignment'] = {'status': 'manual', 'name': '', 'error': ''}
            doc['updated_at'] = app.now()
            con.execute('UPDATE papers SET doc=? WHERE id=?', (json.dumps(doc, ensure_ascii=False), pid))
    return {'settings': app.settings()}
