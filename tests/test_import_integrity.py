"""重复导入仅补缺失资源，所有数据库与文件均使用临时目录。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import pdf_tools
import paper_ai


class ImportIntegrityTests(unittest.TestCase):
    content = b'%PDF-1.7 synthetic integrity fixture'

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        for name, value in [('DATA', self.data), ('LIBRARY', self.data / 'library'),
                            ('STRUCTURE_JOBS', queue.Queue()), ('STRUCTURE_QUEUED', set())]:
            mock = patch.object(server, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        server.init_db()
        self.real_extract = pdf_tools.extract_document
        self.extract = patch('pdf_tools.extract_document', side_effect=self.extract_document).start()
        self.addCleanup(patch.stopall)
        patch.object(server, 'schedule_structure').start()

    def extract_document(self, source, assets):
        (assets / 'original.pdf').write_bytes(source.read_bytes())
        (assets / 'page-001.jpg').write_bytes(b'new page')
        (assets / 'figure-001.jpg').write_bytes(b'new figure')
        return {'metadata': {'title': 'Synthetic'}, 'cover': 'page-001.jpg',
                'pages': [{'number': 1, 'image': 'page-001.jpg'}, {'number': 2, 'image': 'page-002.jpg'}],
                'figures': [{'path': 'figure-001.jpg'}],
                'blocks': [{'id': 'p1-b1', 'text': 'source', 'page': 1, 'kind': 'paragraph'}]}

    def seed(self, deleted=False):
        self.pid = server.import_pdf(self.content, 'fixture.pdf')['id']
        self.root = self.data / 'library' / self.pid
        def history(doc):
            doc.update(deleted=deleted, notes='保留笔记', collection='手动合集',
                       translation={'status': 'done'}, pdf_layout_translation={'status': 'done', 'path': 'translated.pdf'})
            doc['blocks'][0].update(translation='保留译文', text_override='corrected', note='保留批注')
        server.update_doc(self.pid, history)
        self.saved = server.get_doc(self.pid, include_deleted=True)
        with server.db() as con:
            con.execute("INSERT INTO paper_ai_messages(paper_id,generation,role,kind,text,created_at) VALUES(?,1,'user','chat','history','now')", (self.pid,))
        self.extract.reset_mock()

    def assert_history(self):
        doc = server.get_doc(self.pid)
        for key in ('id', 'blocks', 'notes', 'collection', 'translation', 'pdf_layout_translation'):
            self.assertEqual(doc[key], self.saved[key])
        with server.db() as con:
            self.assertEqual(con.execute('SELECT text FROM paper_ai_messages WHERE paper_id=?', (self.pid,)).fetchone()[0], 'history')

    def test_deleted_intact_duplicate_preserves_all_history_without_extraction(self):
        self.seed(deleted=True)
        result = server.import_pdf(self.content, 'renamed.pdf')
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['id'], self.pid)
        self.extract.assert_not_called()
        self.assert_history()

    def test_deleted_missing_directory_is_fresh_import_without_history(self):
        self.seed(deleted=True)
        shutil.rmtree(self.root)
        result = server.import_pdf(self.content, 'renamed.pdf')
        self.assertNotEqual(result['id'], self.pid)
        doc = server.get_doc(result['id'])
        self.assertEqual(doc['notes'], '')
        self.assertEqual(doc['collection'], '')
        self.assertNotIn('translation', doc['blocks'][0])
        self.assertEqual(doc['translation']['status'], 'idle')
        self.assertEqual((self.data / 'library' / doc['id'] / 'original.pdf').read_bytes(), self.content)
        with server.db() as con:
            for table, column in [('papers', 'id'), ('paper_ai', 'paper_id'),
                                  ('paper_ai_generations', 'paper_id'), ('paper_ai_messages', 'paper_id')]:
                self.assertEqual(con.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', (self.pid,)).fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT generation FROM paper_ai WHERE paper_id=?', (doc['id'],)).fetchone()[0], 1)

    def test_active_orphan_is_also_fresh_import(self):
        self.seed()
        shutil.rmtree(self.root)
        result = server.import_pdf(self.content, 'new.pdf')
        self.assertNotEqual(result['id'], self.pid)
        self.assertFalse(result.get('duplicate', False))
        self.assertEqual(server.get_doc(result['id'])['notes'], '')
        with self.assertRaises(ValueError):
            server.get_doc(self.pid, include_deleted=True)

    def test_incomplete_new_extraction_is_not_committed(self):
        self.extract.side_effect = lambda source, assets: {'metadata': {}, 'blocks': [], 'pages': []}
        with self.assertRaisesRegex(ValueError, '提取后缺少资源'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual(server.all_docs(), [])
        self.assertEqual(list((self.data / 'library').iterdir()), [])

    def test_active_missing_original_only_repaired_without_extraction(self):
        self.seed()
        (self.root / 'original.pdf').unlink()
        server.import_pdf(self.content, 'renamed.pdf')
        self.extract.assert_not_called()
        self.assertEqual(hashlib.sha256((self.root / 'original.pdf').read_bytes()).hexdigest(), self.saved['hash'])
        self.assert_history()

    def test_missing_asset_does_not_overwrite_existing_or_translation_files(self):
        self.seed()
        (self.root / 'page-001.jpg').write_bytes(b'old page')
        (self.root / 'translated.pdf').write_bytes(b'old translation')
        (self.root / 'figure-001.jpg').unlink()
        server.import_pdf(self.content, 'renamed.pdf')
        self.assertEqual((self.root / 'page-001.jpg').read_bytes(), b'old page')
        self.assertEqual((self.root / 'translated.pdf').read_bytes(), b'old translation')
        self.assert_history()

    def test_failed_rebuild_keeps_deleted_state_and_existing_directory(self):
        self.seed(deleted=True)
        (self.root / 'figure-001.jpg').unlink()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.extract.side_effect = ValueError('extraction failed')
        with self.assertRaisesRegex(ValueError, 'extraction failed'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertTrue(server.get_doc(self.pid, include_deleted=True)['deleted'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_unreconstructable_reference_keeps_original_missing_and_deleted(self):
        self.seed(deleted=True)
        server.update_doc(self.pid, lambda d: d.update(cover='custom-cover.jpg'))
        (self.root / 'original.pdf').unlink()
        with self.assertRaisesRegex(ValueError, '无法重建缺失资源'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertFalse((self.root / 'original.pdf').exists())
        self.assertTrue(server.get_doc(self.pid, include_deleted=True)['deleted'])

    def test_corrupt_original_is_not_overwritten_or_activated(self):
        self.seed(deleted=True)
        (self.root / 'original.pdf').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '指纹不一致'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual((self.root / 'original.pdf').read_bytes(), b'corrupt')
        self.assertTrue(server.get_doc(self.pid, include_deleted=True)['deleted'])

    def test_unsafe_paths_are_rejected_before_repair(self):
        self.seed(deleted=True)
        for name in ('../outside.jpg', '..\\outside.jpg', 'C:\\outside.jpg', '/outside.jpg'):
            server.update_doc(self.pid, lambda d: d.update(cover=name))
            with self.assertRaisesRegex(ValueError, '路径无效'):
                server.import_pdf(self.content, 'fixture.pdf')
        self.assertTrue(server.get_doc(self.pid, include_deleted=True)['deleted'])

    def test_mocked_symlink_guard_runs_without_windows_symlink_privilege(self):
        self.seed(deleted=True)
        cover = self.root / 'page-001.jpg'
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == cover):
            with self.assertRaisesRegex(ValueError, '包含链接'):
                server.import_pdf(self.content, 'fixture.pdf')
        self.assertTrue(server.get_doc(self.pid, include_deleted=True)['deleted'])

    def test_actual_extractor_repairs_synthetic_pdf_page(self):
        import pypdfium2 as pdfium
        source = self.data / 'fixture.pdf'
        with pdfium.PdfDocument.new() as document:
            page = document.new_page(width=240, height=320)
            page.close()
            document.save(source)
        content = source.read_bytes()
        self.extract.side_effect = self.real_extract
        first = server.import_pdf(content, 'fixture.pdf')
        root = self.data / 'library' / first['id']
        page_bytes = (root / 'page-001.jpg').read_bytes()
        (root / 'page-001.jpg').unlink()
        duplicate = server.import_pdf(content, 'renamed.pdf')
        self.assertEqual(duplicate['id'], first['id'])
        self.assertEqual((root / 'original.pdf').read_bytes(), content)
        self.assertEqual((root / 'page-001.jpg').read_bytes(), page_bytes)

    def test_symlink_is_rejected(self):
        self.seed(deleted=True)
        outside = self.data / 'outside.jpg'
        outside.write_bytes(b'outside')
        cover = self.root / 'page-001.jpg'
        cover.unlink()
        try:
            cover.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f'当前平台不允许创建符号链接: {exc}')
        with self.assertRaisesRegex(ValueError, '包含链接'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_post_commit_failure_preserves_readable_resources(self):
        with patch.object(server, 'schedule_structure', side_effect=RuntimeError('post commit')):
            with self.assertRaisesRegex(RuntimeError, 'post commit'):
                server.import_pdf(self.content, 'fixture.pdf')
        doc = server.all_docs()[0]
        root = self.data / 'library' / doc['id']
        self.assertEqual((root / 'original.pdf').read_bytes(), self.content)
        self.assertTrue((root / 'page-001.jpg').is_file())

    def test_active_chat_prevents_orphan_replacement_without_waiting(self):
        self.seed()
        shutil.rmtree(self.root)
        waiting = threading.Event()
        finish = threading.Event()
        config = {'provider': 'deepseek', 'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
                  'max_output_tokens': 393216, 'profile_id': 'deepseek_chat',
                  'thinking': 'enabled', 'reasoning_effort': 'high'}
        def reply(*args, **kwargs):
            waiting.set()
            if not finish.wait(5):
                raise RuntimeError('模拟问答未释放')
            return {'answer': '合成回答', 'citations': []}
        client = SimpleNamespace(request_json=reply)
        with patch('ezread.api_settings.resolve_config', return_value=config), \
                patch('ezread.api_settings.get_api_key', return_value='synthetic'), \
                ThreadPoolExecutor(max_workers=2) as pool:
            chat = pool.submit(paper_ai.ask, server.db, self.pid, self.saved, '问题',
                               data_dir=self.data, api_client=client)
            try:
                self.assertTrue(waiting.wait(2), '模拟问答应持有真实 paper_lock')
                importing = pool.submit(server.import_pdf, self.content, 'new.pdf')
                with self.assertRaisesRegex(ValueError, '旧文献对话正在执行'):
                    importing.result(timeout=2)
                self.assertEqual(server.get_doc(self.pid), self.saved)
                self.assertEqual(list((self.data / 'library').iterdir()), [])
            finally:
                finish.set()
            self.assertEqual(chat.result(timeout=2)['answer'], '合成回答')
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id=?',
                                         (self.pid,)).fetchone()[0], 3)
        result = server.import_pdf(self.content, 'new.pdf')
        self.assertNotEqual(result['id'], self.pid)
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id=?',
                                         (self.pid,)).fetchone()[0], 0)

    def test_orphan_lock_is_released_after_transaction_failure(self):
        self.seed()
        shutil.rmtree(self.root)
        with patch('paper_ai.ensure', side_effect=RuntimeError('ensure failed')):
            with self.assertRaisesRegex(RuntimeError, 'ensure failed'):
                server.import_pdf(self.content, 'new.pdf')
        def available():
            lock = paper_ai.paper_lock(self.pid)
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            return acquired
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertTrue(pool.submit(available).result(timeout=2))
        self.assertEqual(server.get_doc(self.pid), self.saved)

    def test_orphan_extraction_failure_keeps_old_record_and_history(self):
        self.seed(deleted=True)
        shutil.rmtree(self.root)
        self.extract.side_effect = ValueError('extraction failed')
        with self.assertRaisesRegex(ValueError, 'extraction failed'):
            server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual(server.get_doc(self.pid, include_deleted=True), self.saved)
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id=?', (self.pid,)).fetchone()[0], 1)
        self.assertEqual(list((self.data / 'library').iterdir()), [])

    def test_orphan_transaction_failure_rolls_back_old_records_and_cleans_new_assets(self):
        self.seed(deleted=True)
        shutil.rmtree(self.root)
        with patch('paper_ai.ensure', side_effect=RuntimeError('ensure failed')):
            with self.assertRaisesRegex(RuntimeError, 'ensure failed'):
                server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual(server.get_doc(self.pid, include_deleted=True), self.saved)
        with server.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM paper_ai_messages WHERE paper_id=?', (self.pid,)).fetchone()[0], 1)
        self.assertEqual(list((self.data / 'library').iterdir()), [])

    def test_new_transaction_failure_leaves_no_record_or_assets(self):
        with patch('paper_ai.ensure', side_effect=RuntimeError('ensure failed')):
            with self.assertRaisesRegex(RuntimeError, 'ensure failed'):
                server.import_pdf(self.content, 'fixture.pdf')
        self.assertEqual(server.all_docs(), [])
        self.assertEqual(list((self.data / 'library').iterdir()), [])

    def test_concurrent_identical_imports_share_one_record_and_assets(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: server.import_pdf(self.content, 'fixture.pdf'), range(2)))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(len(server.all_docs()), 1)
        self.assertEqual(self.extract.call_count, 1)

    def test_failed_new_extraction_removes_only_new_directory(self):
        self.seed()
        self.extract.side_effect = ValueError('bad extraction')
        with self.assertRaisesRegex(ValueError, 'bad extraction'):
            server.import_pdf(self.content + b'other', 'other.pdf')
        self.assertEqual([p.name for p in (self.data / 'library').iterdir()], [self.pid])
        self.assertEqual(len(server.all_docs()), 1)
        self.assert_history()


if __name__ == '__main__':
    unittest.main()
