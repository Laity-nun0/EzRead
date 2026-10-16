"""孤儿重导的附属文件、快照清理与事务恢复，仅使用临时库。"""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_import_integrity as fixtures
import server


class OrphanCleanupTests(unittest.TestCase):
    content = fixtures.ImportIntegrityTests.content
    setUp = fixtures.ImportIntegrityTests.setUp
    extract_document = fixtures.ImportIntegrityTests.extract_document
    seed = fixtures.ImportIntegrityTests.seed

    def seed_orphan(self):
        self.seed(deleted=True)
        shutil.rmtree(self.root)
        self.clipboard = self.data / 'clipboard' / self.pid
        self.clipboard.mkdir(parents=True)
        (self.clipboard / 'old.pdf').write_bytes(b'old original')
        self.sibling = 'f' * 16
        sibling = self.data / 'clipboard' / self.sibling
        sibling.mkdir()
        (sibling / 'keep.pdf').write_bytes(b'keep original')
        values = {f'ezread-reader-notes:{self.pid}': 'old notes',
                  f'ezread-reader-pdf-view:{self.pid}': 'old view',
                  f'ezread-reader-notes:{self.sibling}': 'keep notes',
                  'ezread-preferences': 'keep global'}
        snapshots = {'desktop-close-drafts.json': {'id': 'close-id', 'origin': 'http://localhost:1234', 'values': values},
                     'webview2-migration.json': values}
        self.originals = {}
        for name, data in snapshots.items():
            raw = json.dumps(data).encode('utf-8')
            (self.data / name).write_bytes(raw)
            self.originals[name] = raw

    def assert_restored(self):
        self.assertEqual(server.get_doc(self.pid, include_deleted=True), self.saved)
        self.assertEqual((self.clipboard / 'old.pdf').read_bytes(), b'old original')
        self.assertEqual(list(server.LIBRARY.iterdir()), [])
        for name, raw in self.originals.items():
            self.assertEqual((self.data / name).read_bytes(), raw)
        with server.db() as con:
            self.assertEqual(con.execute('SELECT text FROM paper_ai_messages WHERE paper_id=?', (self.pid,)).fetchone()[0], 'history')

    def test_success_removes_old_clipboard_snapshots_and_reports_replacement(self):
        self.seed_orphan()
        result = server.import_pdf(self.content, 'fresh.pdf')
        self.assertNotEqual(result['id'], self.pid)
        self.assertEqual(result['replaced_paper_id'], self.pid)
        self.assertNotIn('replaced_paper_id', server.get_doc(result['id']))
        self.assertFalse(self.clipboard.exists())
        self.assertEqual((self.data / 'clipboard' / self.sibling / 'keep.pdf').read_bytes(), b'keep original')
        for name in self.originals:
            document = json.loads((self.data / name).read_text(encoding='utf-8'))
            values = document['values'] if 'values' in document else document
            self.assertEqual(values, {f'ezread-reader-notes:{self.sibling}': 'keep notes', 'ezread-preferences': 'keep global'})
        self.assertEqual(server.get_doc(result['id'])['notes'], '')
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id IN (?,?)', (self.pid, result['id'])).fetchone()[0], 0)

    def test_transaction_failure_restores_old_clipboard_and_snapshots(self):
        self.seed_orphan()
        with patch('paper_ai.ensure', side_effect=RuntimeError('ensure failed')):
            with self.assertRaisesRegex(RuntimeError, 'ensure failed'):
                server.import_pdf(self.content, 'fresh.pdf')
        self.assert_restored()

    def test_partial_clipboard_delete_failure_restores_old_files_and_records(self):
        self.seed_orphan()
        remove = shutil.rmtree
        def fail(path, *args, **kwargs):
            if Path(path) == self.clipboard:
                (self.clipboard / 'old.pdf').unlink()
                raise PermissionError('clipboard locked')
            return remove(path, *args, **kwargs)
        with patch('ezread.library.shutil.rmtree', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '已保留'):
                server.import_pdf(self.content, 'fresh.pdf')
        self.assert_restored()

    def test_postcommit_backup_cleanup_failure_keeps_new_assets(self):
        self.seed_orphan()
        remove = shutil.rmtree
        def fail(path, *args, **kwargs):
            if Path(path).name.startswith('delete-recovery-'):
                raise PermissionError('recovery locked')
            return remove(path, *args, **kwargs)
        with patch('ezread.library.shutil.rmtree', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '永久删除未完成'):
                server.import_pdf(self.content, 'fresh.pdf')
        new, = server.all_docs()
        self.assertNotEqual(new['id'], self.pid)
        self.assertEqual((server.LIBRARY / new['id'] / 'original.pdf').read_bytes(), self.content)
        with self.assertRaises(ValueError):
            server.get_doc(self.pid, include_deleted=True)


if __name__ == '__main__':
    unittest.main()
