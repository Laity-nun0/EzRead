"""永久删除边界；所有文件、数据库和任务均为临时夹具。"""
from pathlib import Path
import json
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from ezread import storage


class PaperDeleteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        for name, value in [('DATA', self.data), ('LIBRARY', self.data / 'library'),
                            ('DELETED_PAPERS', set()), ('CANCEL', {}), ('QUEUED', set()),
                            ('RESUME_REQUESTED', set()), ('STRUCTURE_QUEUED', set()),
                            ('JOBS', queue.Queue())]:
            mock = patch.object(server, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        server.init_db()
        self.pid = 'a' * 16
        self.other = 'b' * 16
        for pid in (self.pid, self.other):
            server.put_doc({'id': pid, 'hash': pid, 'notes': '私人笔记', 'blocks': [{'translation': '译文'}]})
            root = server.LIBRARY / pid
            root.mkdir()
            (root / 'original.pdf').write_bytes(b'pdf')
            (root / 'translated.pdf').write_bytes(b'translation')
            with server.db() as con:
                con.execute('INSERT INTO paper_ai(paper_id,generation) VALUES(?,1)', (pid,))
                con.execute("INSERT INTO paper_ai_generations(paper_id,generation,created_at) VALUES(?,1,'now')", (pid,))
                con.execute("INSERT INTO paper_ai_messages(paper_id,generation,role,kind,text,created_at) VALUES(?,1,'user','question','会话','now')", (pid,))

    def test_complete_delete_and_stale_writes_cannot_revive(self):
        old = server.get_doc(self.pid)
        clipboard = self.data / 'clipboard' / self.pid
        clipboard.mkdir(parents=True)
        (clipboard / 'copy.pdf').write_bytes(b'pdf')
        self.assertTrue(server.delete_paper(self.pid)['ok'])
        self.assertFalse((server.LIBRARY / self.pid).exists())
        self.assertFalse(clipboard.exists())
        self.assertEqual(list((self.data / 'tmp').iterdir()), [])
        self.assertEqual(server.get_doc(self.other)['notes'], '私人笔记')
        self.assertTrue((server.LIBRARY / self.other / 'original.pdf').is_file())
        with server.db() as con:
            for table, column in [('papers', 'id'), ('paper_ai', 'paper_id'),
                                  ('paper_ai_generations', 'paper_id'), ('paper_ai_messages', 'paper_id')]:
                self.assertEqual(con.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', (self.pid,)).fetchone()[0], 0)
                self.assertEqual(con.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', (self.other,)).fetchone()[0], 1)
        with self.assertRaises(ValueError):
            server.put_doc(old)
        with self.assertRaises(ValueError):
            server.update_doc(self.pid, lambda d: d.update(notes='late'))
        import paper_ai
        with server.db() as con, self.assertRaises(ValueError):
            paper_ai.ensure(con, self.pid)

    def test_partial_file_failure_restores_files_and_database(self):
        import shutil
        original = shutil.rmtree
        root = server.LIBRARY / self.pid
        def fail(path, *args, **kwargs):
            if Path(path) == root:
                (root / 'original.pdf').unlink()
                raise PermissionError('文件占用')
            return original(path, *args, **kwargs)
        with patch('ezread.library.shutil.rmtree', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '已保留'):
                server.delete_paper(self.pid)
        self.assertEqual((root / 'original.pdf').read_bytes(), b'pdf')
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id=?', (self.pid,)).fetchone()[0], 1)

    def test_database_failure_rolls_back_and_restores_files(self):
        with patch('ezread.storage.delete_paper_records', side_effect=sqlite3.OperationalError('database busy')):
            with self.assertRaisesRegex(ValueError, '已保留'):
                server.delete_paper(self.pid)
        self.assertEqual((server.LIBRARY / self.pid / 'original.pdf').read_bytes(), b'pdf')
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')

    def test_active_task_requests_cancel_but_does_not_claim_delete(self):
        key = (self.pid, 'pdf_translate')
        event = threading.Event()
        server.CANCEL[key] = event
        server.QUEUED.add(key)
        server.RESUME_REQUESTED.add(key)
        with self.assertRaisesRegex(ValueError, '尚未停止'):
            server.delete_paper(self.pid)
        self.assertTrue(event.is_set())
        self.assertNotIn(key, server.RESUME_REQUESTED)
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        server.QUEUED.clear()
        self.assertTrue(server.delete_paper(self.pid)['ok'])

    def test_path_escape_and_link_are_rejected(self):
        with self.assertRaises(ValueError):
            server.delete_paper('../escape')
        root = server.LIBRARY / self.pid
        with patch.object(Path, 'is_symlink', lambda p: p == root):
            with self.assertRaisesRegex(ValueError, '链接'):
                server.delete_paper(self.pid)
        self.assertEqual((root / 'original.pdf').read_bytes(), b'pdf')

    def test_record_helper_supports_old_schema_and_caller_rollback(self):
        with sqlite3.connect(':memory:') as con:
            con.execute('CREATE TABLE papers(id TEXT)')
            con.execute('INSERT INTO papers VALUES(?)', (self.pid,))
            con.commit()
            storage.delete_paper_records(con, self.pid)
            con.rollback()
            self.assertEqual(con.execute('SELECT id FROM papers').fetchone()[0], self.pid)

    def test_same_pdf_after_delete_is_fresh_without_old_chat(self):
        content = b'%PDF-1.7 synthetic delete fixture'
        def extract(source, assets):
            (assets / 'original.pdf').write_bytes(source.read_bytes())
            (assets / 'page-001.jpg').write_bytes(b'page')
            return {'metadata': {'title': 'Synthetic'}, 'pages': [], 'figures': [],
                    'blocks': [{'id': 'p1-b1', 'text': 'source', 'page': 1, 'kind': 'paragraph'}]}
        with patch('pdf_tools.extract_document', side_effect=extract), patch.object(server, 'schedule_structure'):
            pid = server.import_pdf(content, 'fixture.pdf')['id']
            server.update_doc(pid, lambda doc: doc.update(notes='old notes'))
            with server.db() as con:
                con.execute("INSERT INTO paper_ai_messages(paper_id,generation,role,kind,text,created_at) VALUES(?,1,'user','question','old history','now')", (pid,))
            server.delete_paper(pid)
            fresh = server.import_pdf(content, 'fixture.pdf')
        self.assertNotEqual(fresh['id'], pid)
        self.assertEqual(server.get_doc(fresh['id'])['notes'], '')
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id IN (?,?)', (pid, fresh['id'])).fetchone()[0], 0)

    def seed_snapshots(self):
        values = {f'ezread-reader-notes:{self.pid}': 'delete notes',
                  f'ezread-reader-position:{self.pid}': 'delete position',
                  f'ezread-reader-notes:{self.other}': 'keep sibling',
                  'ezread-preferences': 'keep global',
                  f'other-prefix:{self.pid}': 'keep unrelated'}
        documents = {'desktop-close-drafts.json': {'id': 'close-id', 'origin': 'http://localhost:1234', 'values': values},
                     'webview2-migration.json': values}
        for filename, document in documents.items():
            (self.data / filename).write_text(json.dumps(document), encoding='utf-8')
        return {filename: (self.data / filename).read_bytes() for filename in documents}

    def test_delete_removes_only_own_keys_from_both_desktop_snapshots(self):
        self.seed_snapshots()
        server.delete_paper(self.pid)
        for filename in ('desktop-close-drafts.json', 'webview2-migration.json'):
            document = json.loads((self.data / filename).read_text(encoding='utf-8'))
            values = document['values'] if filename == 'desktop-close-drafts.json' else document
            self.assertNotIn(f'ezread-reader-notes:{self.pid}', values)
            self.assertNotIn(f'ezread-reader-position:{self.pid}', values)
            self.assertEqual(values[f'ezread-reader-notes:{self.other}'], 'keep sibling')
            self.assertEqual(values['ezread-preferences'], 'keep global')
            self.assertEqual(values[f'other-prefix:{self.pid}'], 'keep unrelated')
            if filename == 'desktop-close-drafts.json':
                self.assertEqual(document['id'], 'close-id')
                self.assertEqual(document['origin'], 'http://localhost:1234')

    def test_database_failure_restores_exact_desktop_snapshot_bytes(self):
        originals = self.seed_snapshots()
        with patch('ezread.storage.delete_paper_records', side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaisesRegex(ValueError, '已保留'):
                server.delete_paper(self.pid)
        for filename, content in originals.items():
            self.assertEqual((self.data / filename).read_bytes(), content)
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        self.assertTrue((server.LIBRARY / self.pid / 'original.pdf').is_file())

    def test_snapshot_replace_failure_restores_all_and_does_not_claim_success(self):
        from ezread import library
        originals = self.seed_snapshots()
        replace = library.os.replace
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if Path(target).name == 'webview2-migration.json' and not failed:
                failed = True
                raise PermissionError('snapshot locked')
            return replace(source, target)
        with patch('ezread.library.os.replace', side_effect=fail_once):
            with self.assertRaisesRegex(ValueError, '已保留'):
                server.delete_paper(self.pid)
        self.assertTrue(failed)
        for filename, content in originals.items():
            self.assertEqual((self.data / filename).read_bytes(), content)
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        self.assertTrue((server.LIBRARY / self.pid / 'original.pdf').is_file())

    def test_invalid_snapshot_aborts_before_file_or_database_deletion(self):
        (self.data / 'desktop-close-drafts.json').write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '永久删除未完成'):
            server.delete_paper(self.pid)
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        self.assertTrue((server.LIBRARY / self.pid / 'original.pdf').is_file())

    def test_snapshot_restore_failure_keeps_explicit_recovery_copy(self):
        originals = self.seed_snapshots()
        with patch('ezread.library.os.replace', side_effect=PermissionError('snapshot remains locked')):
            with self.assertRaisesRegex(ValueError, '恢复副本保留'):
                server.delete_paper(self.pid)
        self.assertEqual(server.get_doc(self.pid)['notes'], '私人笔记')
        recovery, = (self.data / 'tmp').glob('delete-recovery-*')
        for filename, content in originals.items():
            self.assertEqual((recovery / filename).read_bytes(), content)

    def test_copy_rechecks_document_inside_lock_before_creating_clipboard_folder(self):
        doc = server.get_doc(self.pid)
        with patch.object(server, 'get_doc', side_effect=[doc, ValueError('已删除')]) as get_doc, patch('desktop_files.copy_pdf') as copy_pdf:
            with self.assertRaisesRegex(ValueError, '已删除'):
                server.paper_file_action(self.pid, 'copy-pdf')
        self.assertEqual(get_doc.call_count, 2)
        copy_pdf.assert_not_called()
        self.assertFalse((self.data / 'clipboard' / self.pid).exists())


if __name__ == '__main__':
    unittest.main()
