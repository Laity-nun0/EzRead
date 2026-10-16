"""兼容准备接口只核验本地配置，不连接模型。"""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import codex_bridge
import server
from ezread import api_settings
from ezread.selection_session import SelectionSession

CONFIG = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
          'model': 'deepseek-flash', 'max_output_tokens': 393216,
          'profile_id': 'deepseek_translation', 'thinking': 'disabled'}


class SelectionSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = SelectionSession()
        self.app = SimpleNamespace(SHUTDOWN=threading.Event(), DATA=Path('unused'), AI_API=Mock())
        for owner, name in ((codex_bridge, 'prepare_selection'), (codex_bridge, 'translate_selection')):
            blocked = patch.object(owner, name, side_effect=AssertionError('禁止 CLI 与预热'))
            blocked.start()
            self.addCleanup(blocked.stop)
        configured = patch.object(api_settings, 'resolve_config', return_value=CONFIG)
        self.resolve = configured.start()
        self.addCleanup(configured.stop)
        key = patch.object(api_settings, 'get_api_key', return_value='synthetic-key')
        self.key = key.start()
        self.addCleanup(key.stop)

    def test_start_only_validates_local_settings_without_connection(self):
        result = self.session.start(self.app)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('未连接模型', result['message'])
        self.assertIsNone(self.session.worker)
        self.resolve.assert_called_once_with(self.app.DATA, purpose='selection')
        self.key.assert_called_once_with(self.app.DATA, CONFIG)
        self.app.AI_API.request_json.assert_not_called()
        self.assertNotIn('synthetic-key', json.dumps(result))

    def test_missing_key_can_recover_after_local_settings_change(self):
        self.key.side_effect = ValueError('请先保存 API Key。')
        self.assertEqual(self.session.start(self.app), {'status': 'failed', 'message': '请先保存 API Key。'})
        self.key.side_effect = None
        self.assertEqual(self.session.start(self.app)['status'], 'ready')
        self.app.AI_API.request_json.assert_not_called()

    def test_close_never_publishes_ready(self):
        self.app.SHUTDOWN.set()
        self.assertEqual(self.session.start(self.app)['status'], 'stopped')
        self.assertEqual(self.session.snapshot()['status'], 'stopped')
        self.resolve.assert_not_called()
        self.app.SHUTDOWN.clear()
        self.key.side_effect = lambda *args: self.app.SHUTDOWN.set() or 'synthetic-key'
        self.assertEqual(self.session.start(self.app)['status'], 'stopped')

    def test_http_local_status_rejects_paper_data_and_cross_site(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{httpd.server_port}/api/selection-session'
        def post(body, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            return opener.open(urllib.request.Request(url,
                data=b'' if origin else json.dumps(body).encode(), headers=headers), timeout=2)
        with patch.object(server, 'SHUTDOWN', self.app.SHUTDOWN), \
                patch.object(server, 'SELECTION_SESSION', self.session), patch.object(server, 'PORT', httpd.server_port):
            with post({}) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)['status'], 'ready')
            with opener.open(url, timeout=2) as response:
                self.assertEqual(json.load(response)['status'], 'ready')
            for body, origin, code in (({'selected_text': '不应发出'}, None, 400), ({}, 'https://remote.invalid', 403)):
                with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as caught:
                    post(body, origin)
                self.assertEqual(caught.exception.code, code)
        self.key.assert_called_once()
        self.app.AI_API.request_json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
