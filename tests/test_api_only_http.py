"""API 状态与剩余入口的回环集成检查；不得访问 CLI、账户或真实模型。"""
import json
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch

import codex_models
import codex_usage
import server
from ezread import api_settings
from ezread.ai_api import ApiSessionPool
from ezread.api_usage import ApiUsage
from ezread.selection_requests import SelectionRequests
from ezread.selection_session import SelectionSession
from tests import test_shared_api_http as fixtures
from tests.test_translation_api import KEY, response_for


class ApiOnlyHttpTests(unittest.TestCase):
    close_server = fixtures.SharedApiHttpTests.close_server

    def post(self, action, payload):
        try:
            return fixtures.SharedApiHttpTests.post(self, action, payload)
        except urllib.error.HTTPError as exc:
            with exc:
                raise AssertionError(exc.read().decode('utf-8')) from None

    def setUp(self):
        fixtures.SharedApiHttpTests.setUp(self)
        usage = ApiUsage()
        pool = self.stack.enter_context(ApiSessionPool(usage=usage))
        request_json = pool.request_json
        self.production_configs = []
        def local_request(channel, config, *args, **kwargs):
            self.assertEqual(config['provider'], 'deepseek')
            self.assertEqual(config['base_url'], 'https://api.deepseek.com')
            self.assertEqual(config['model'], 'deepseek-flash')
            self.assertEqual(config['max_output_tokens'], 393216)
            self.production_configs.append(dict(config))
            return request_json(channel, {**config, 'base_url': self.config['base_url']}, *args, **kwargs)
        self.stack.enter_context(patch.object(pool, 'request_json', side_effect=local_request))
        for name, value in (('API_USAGE', usage), ('AI_API', pool),
                            ('SELECTION_REQUESTS', SelectionRequests()), ('SELECTION_SESSION', SelectionSession())):
            self.stack.enter_context(patch.object(server, name, value))
        for owner, name in ((codex_usage, 'get'), (codex_usage, 'snapshot'), (codex_models, 'get')):
            self.stack.enter_context(patch.object(owner, name, side_effect=AssertionError('不得访问 Codex')))

    def get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.httpd.server_port}{path}', timeout=3) as response:
            return json.load(response)

    def test_status_models_usage_and_prepare_never_test_connection(self):
        for route in ('/api/ai/status', '/api/status?refresh=1'):
            status = self.get(route)
            self.assertTrue(status['configuration']['configured'])
            self.assertNotIn('codex', status)
            self.assertNotIn(KEY, json.dumps(status))
            self.assertEqual(status['usage']['requests']['attempted'], 0)
            self.assertIsNone(status['usage']['tokens']['input'])
            self.assertIsNone(status['usage']['account_balance'])
        self.assertEqual(self.get('/api/models?refresh=1')['source'], 'local_settings')
        self.assertEqual(self.get('/api/usage?refresh=1')['scope'], 'application_run')
        request = urllib.request.Request(f'http://127.0.0.1:{self.httpd.server_port}/api/selection-session',
            data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(json.load(response)['status'], 'ready')
        self.assertEqual(self.records, [])
        api_settings.save_settings(server.DATA, {'purpose': 'translation', 'clear_key': True})
        status = self.get('/api/ai/status')
        self.assertFalse(status['configuration']['configured'])
        self.assertEqual(self.records, [])

    def test_selection_and_retranslation_use_api_and_report_real_attempts(self):
        self.doc['blocks'].append({'id': 'b2', 'page': 1, 'text': 'We measured force.',
                                   'bbox': [.1, .5, .8, .6], 'kind': 'paragraph'})
        server.put_doc(self.doc)
        self.reply.clear()
        self.reply.update(response_for('deepseek', {'translation': '测得力。'}))
        result = self.post('ai/highlight', {'block_id': 'b2', 'text': 'We measured force.'})
        self.assertEqual(result['translation'], '测得力。')
        self.assertEqual(server.get_doc(fixtures.PID)['blocks'], self.doc['blocks'])
        self.reply.clear()
        self.reply.update(response_for('deepseek', {'b1': '我们测量了力。'}))
        selected = {'block_id': 'b1', 'language': 'original', 'start': 0, 'end': 18,
                    'quote': 'We measured force.', 'base_text': 'We measured force.'}
        self.post('text-actions', {'action': 'retranslate', 'ranges': [selected],
                                  'client_id': 'a' * 32, 'request_id': 'b' * 32})
        saved = server.get_doc(fixtures.PID)
        self.assertEqual(saved['blocks'][0]['translation'], '我们测量了力。')
        self.assertEqual(saved['blocks'][0]['text'], self.doc['blocks'][0]['text'])
        self.assertEqual(saved['notes'], self.doc['notes'])
        self.assertTrue(saved['translation_versions'])
        usage = self.get('/api/ai/status')['usage']
        self.assertEqual(len(self.production_configs), 2)
        self.assertTrue(all(config['profile_id'] == 'deepseek_translation' for config in self.production_configs))
        self.assertEqual(usage['requests']['attempted'], 2)
        self.assertEqual(usage['requests']['succeeded'], 2)
        self.assertEqual(usage['usage_reported'], 2)
        self.assertEqual(usage['tokens']['output'], 20)
        self.assertIsNone(usage['tokens']['input'])
        self.assertNotIn(KEY, json.dumps(usage))
        for record in self.records:
            self.assertEqual(len(record['body']['messages']), 2)
            self.assertNotIn('历史问题', record['body']['messages'][1]['content'])


if __name__ == '__main__':
    unittest.main()
