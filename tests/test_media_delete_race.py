"""媒体延迟渲染与永久删除使用同一文件锁；独立临时库和回环端口。"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_ai
import server

PID = '0123456789abcdef'
RUN = 'fedcba9876543210'


class MediaDeleteRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        for name, value in (('DATA', self.data), ('LIBRARY', self.data / 'library'),
                            ('FILE_ACTION_LOCK', threading.Lock()), ('LOCK', threading.RLock()),
                            ('QUEUED', set()), ('STRUCTURE_QUEUED', set()), ('DELETED_PAPERS', set()),
                            ('CANCEL', {}), ('RESUME_REQUESTED', set()), ('STRUCTURE_JOBS', queue.Queue()),
                            ('SHUTDOWN', threading.Event())):
            replacement = patch.object(server, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        server.init_db()
        self.root = server.LIBRARY / PID
        self.root.mkdir()
        (self.root / 'original.pdf').write_bytes(b'%PDF-1.7 synthetic source')
        translated = self.root / 'pdf-layout' / RUN
        translated.mkdir(parents=True)
        (translated / 'result.pdf').write_bytes(b'%PDF-1.7 synthetic translation')
        server.put_doc({'id': PID, 'hash': 'synthetic-media-delete', 'title': 'Synthetic media',
                        'pages': [{'number': 1}], 'blocks': [], 'deleted': False,
                        'pdf_layout_current': {'id': RUN, 'mono': 'result.pdf', 'dual': 'result.pdf',
                                               'page_count': 1}})
        with server.db() as con:
            paper_ai.ensure(con, PID)
            paper_ai.add_message(con, PID, 1, 'user', 'question', 'synthetic history')
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = patch.object(server, 'PORT', self.httpd.server_port)
        port.start()
        self.addCleanup(port.stop)
        self.listener = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.listener.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.listener.join(timeout=3)

    def request(self, relative, method='GET'):
        request = urllib.request.Request(f'http://127.0.0.1:{self.httpd.server_port}{relative}', method=method)
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, response.read()

    def run_race(self, relative, expected_source):
        rendering = threading.Event()
        finish_render = threading.Event()
        deleting = threading.Event()
        original_delete = server.delete_paper
        payload = b'synthetic rendered JPEG'
        def render(source, number, target):
            self.assertEqual(source, expected_source)
            self.assertEqual(number, 1)
            rendering.set()
            if not finish_render.wait(4):
                raise RuntimeError('模拟渲染未释放')
            # 对应 _save_jpeg：若删除没有等待，这里会重新创建旧目录。
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        def delete(pid):
            deleting.set()
            return original_delete(pid)
        with patch('pdf_tools.render_page', side_effect=render) as render_call, \
                patch.object(server, 'delete_paper', side_effect=delete), \
                ThreadPoolExecutor(max_workers=2) as pool:
            media = pool.submit(self.request, f'/media/{PID}/{relative}')
            try:
                self.assertTrue(rendering.wait(2), 'HTTP 应进入延迟渲染')
                removal = pool.submit(self.request, f'/api/papers/{PID}', 'DELETE')
                self.assertTrue(deleting.wait(2), 'HTTP 删除应已开始')
                with self.assertRaises(TimeoutError):
                    removal.result(timeout=.2)
                self.assertEqual(server.get_doc(PID)['id'], PID)
                self.assertTrue(self.root.is_dir())
            finally:
                finish_render.set()
            self.assertEqual(media.result(timeout=3), (200, payload))
            status, raw = removal.result(timeout=3)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)['ok'])
            render_call.assert_called_once()
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(f'/media/{PID}/{relative}')
            self.assertEqual(error.exception.code, 400)
            render_call.assert_called_once()
        with server.db() as con:
            for table, column in (('papers', 'id'), ('paper_ai', 'paper_id'),
                                  ('paper_ai_generations', 'paper_id'), ('paper_ai_messages', 'paper_id')):
                self.assertEqual(con.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?',
                                             (PID,)).fetchone()[0], 0)
        self.assertFalse(self.root.exists(), '永久删除后渲染不能重建旧文献目录')

    def test_missing_original_returns_404_without_rendering_or_recreating_directory(self):
        (self.root / 'original.pdf').unlink()
        for missing_directory in (False, True):
            with self.subTest(missing_directory=missing_directory):
                if missing_directory:
                    shutil.rmtree(self.root)
                with patch('pdf_tools.render_page') as render:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        self.request(f'/media/{PID}/page-001.jpg')
                    self.assertEqual(error.exception.code, 404)
                    self.assertIn('原 PDF 已缺失', json.loads(error.exception.read())['error'])
                    render.assert_not_called()
                self.assertFalse((self.root / 'page-001.jpg').exists())
                self.assertEqual(self.root.exists(), not missing_directory)
                self.assertEqual(server.get_doc(PID)['id'], PID)

    def test_original_lazy_page_finishes_before_permanent_delete(self):
        self.run_race('page-001.jpg', self.root / 'original.pdf')

    def test_translated_lazy_preview_finishes_before_permanent_delete(self):
        self.run_race(f'pdf-layout/{RUN}/page-001.jpg', self.root / 'pdf-layout' / RUN / 'result.pdf')


if __name__ == '__main__':
    unittest.main()
