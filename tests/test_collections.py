"""合集删除使用独立库，覆盖事务、内容保留与真实本机 HTTP。"""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import server


class CollectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data = Path(folder.name)
        for key, value in (('DATA', data), ('LIBRARY', data / 'library')):
            replacement = patch.object(server, key, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        server.init_db()
        with server.db() as con:
            con.execute('INSERT INTO settings VALUES(?,?)', ('collections', json.dumps(['空合集', '目标合集', '其他合集'])))
            con.execute('INSERT INTO settings VALUES(?,?)', ('theme', json.dumps('sage')))
        self.originals = []
        for index, (collection, deleted) in enumerate([('目标合集', False), ('目标合集', True), ('其他合集', False)]):
            doc = {'id': f'{index:016x}', 'hash': str(index), 'title': 'Synthetic paper',
                   'collection': collection, 'deleted': deleted, 'notes': '保留笔记',
                   'reader_state': {'page': 3}, 'translation_versions': [{'id': 'saved'}],
                   'blocks': [{'id': 'b1', 'text': 'Source', 'translation': '译文', 'note': '批注'}]}
            server.put_doc(doc)
            self.originals.append(copy.deepcopy(doc))
            folder = server.LIBRARY / doc['id']
            folder.mkdir()
            (folder / 'original.pdf').write_bytes(b'synthetic PDF evidence')

    def test_delete_clears_membership_and_preserves_all_other_content(self):
        result = server.delete_collection('目标合集')
        self.assertEqual(result['settings']['collections'], ['空合集', '其他合集'])
        self.assertEqual(result['settings']['theme'], 'sage')
        for original in self.originals:
            saved = server.get_doc(original['id'], include_deleted=True)
            if original['collection'] == '目标合集':
                self.assertEqual(saved.pop('collection'), '')
                self.assertEqual(saved.pop('collection_assignment'), {'status': 'manual', 'name': '', 'error': ''})
                self.assertTrue(saved.pop('updated_at'))
                self.assertEqual(saved, {k: v for k, v in original.items() if k != 'collection'})
            else:
                self.assertEqual(saved, original)
            self.assertEqual((server.LIBRARY / original['id'] / 'original.pdf').read_bytes(), b'synthetic PDF evidence')
        self.assertEqual(len(server.all_docs()), 2)
        server.delete_collection('目标合集')  # 重复请求不影响其他合集。
        self.assertEqual(server.settings()['collections'], ['空合集', '其他合集'])

    def test_empty_and_implicit_collection(self):
        server.delete_collection('空合集')
        self.assertNotIn('空合集', server.settings()['collections'])
        server.update_doc(self.originals[0]['id'], lambda doc: doc.update(collection='仅由论文归属产生'))
        server.delete_collection('仅由论文归属产生')
        self.assertEqual(server.get_doc(self.originals[0]['id'])['collection'], '')

    def test_failure_rolls_back_settings_and_every_paper(self):
        with server.db() as con:
            con.execute("""CREATE TRIGGER reject_second BEFORE UPDATE ON papers
                           WHEN OLD.id='0000000000000001'
                           BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        before = server.settings()
        with self.assertRaises(sqlite3.IntegrityError):
            server.delete_collection('目标合集')
        self.assertEqual(server.settings(), before)
        self.assertEqual(server.all_docs(include_deleted=True), self.originals)

    def test_invalid_names_leave_data_unchanged(self):
        before = server.settings()
        for name in (None, [], '', '   ', 'a\nb', 'x' * 101):
            with self.subTest(name=name), self.assertRaises(ValueError):
                server.delete_collection(name)
        self.assertEqual(server.settings(), before)
        self.assertEqual(server.all_docs(include_deleted=True), self.originals)

    def test_http_encoded_name_and_cross_origin_protection(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        replacement = patch.object(server, 'PORT', httpd.server_port)
        replacement.start()
        self.addCleanup(replacement.stop)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        def close():
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)
        self.addCleanup(close)
        name = '界面 / 疲劳? #合集'
        server.update_doc(self.originals[0]['id'], lambda doc: doc.update(collection=name))
        url = f'http://127.0.0.1:{httpd.server_port}/api/collections/{urllib.parse.quote(name, safe="")}'
        blocked = urllib.request.Request(url, method='DELETE', headers={'Origin': 'https://example.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(blocked, timeout=5)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.assertEqual(server.get_doc(self.originals[0]['id'])['collection'], name)
        with urllib.request.urlopen(urllib.request.Request(url, method='DELETE'), timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('settings', json.load(response))
        self.assertEqual(server.get_doc(self.originals[0]['id'])['collection'], '')
