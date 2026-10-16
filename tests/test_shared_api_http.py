"""真实本机 HTTP 链路与模拟供应商；所有 Codex 入口均禁止调用。"""
import contextlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import codex_bridge
import codex_models
import paper_ai
import paper_structure
import server
from ezread import api_settings
from ezread.ai_api import ApiSessionPool
from tests.test_overview import generated
from tests.test_translation_api import KEY, mock_api, response_for

PID = '0123456789abcdef'


class SharedApiHttpTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path.cwd() / 'work')))
        self.pool = self.stack.enter_context(ApiSessionPool())
        for name, value in (('DATA', data), ('LIBRARY', data / 'library'), ('AI_API', self.pool),
                            ('JOBS', queue.Queue()), ('STRUCTURE_JOBS', queue.Queue()),
                            ('QUEUED', set()), ('STRUCTURE_QUEUED', set()), ('CANCEL', {}),
                            ('RESUME_REQUESTED', set()), ('SHUTDOWN', threading.Event()),
                            ('QUOTA_PAUSED', threading.Event())):
            self.stack.enter_context(patch.object(server, name, value))
        for owner, name in ((server, 'codex_status'), (codex_bridge, 'status'),
                            (codex_bridge, '_run_json'), (codex_bridge, '_cli'),
                            (codex_models, 'resolve_config')):
            self.stack.enter_context(patch.object(owner, name, side_effect=AssertionError('不得调用 CLI')))
        self.reply = response_for('deepseek', {'answer': '测得力。', 'citations': ['b1']})
        self.config, self.records, _, _ = self.stack.enter_context(mock_api(reply=self.reply))
        api_settings.save_settings(data, {'purpose': 'chat', 'api_key': KEY})
        api_settings.save_settings(data, {'purpose': 'translation', 'api_key': KEY})
        request_json = self.pool.request_json
        def local_request(channel, config, *args, **kwargs):
            self.assertEqual(config['provider'], 'deepseek')
            self.assertEqual(config['base_url'], 'https://api.deepseek.com')
            return request_json(channel, {**config, 'base_url': self.config['base_url']}, *args, **kwargs)
        self.stack.enter_context(patch.object(self.pool, 'request_json', side_effect=local_request))
        server.init_db()
        self.doc = {'id': PID, 'hash': 'synthetic-http', 'title': 'Synthetic API paper',
                    'deleted': False, 'notes': '保留笔记', 'collection': '人工合集', 'pages': [],
                    'blocks': [{'id': 'b1', 'page': 1, 'text': 'We measured force.',
                                'translation': '既有人工译文', 'note': '保留批注',
                                'bbox': [.1, .2, .8, .4], 'kind': 'paragraph'}]}
        self.doc['reading_structure'] = paper_structure.organize(self.doc)
        server.put_doc(self.doc)
        with server.db() as con:
            paper_ai.ensure(con, PID)
            con.execute("UPDATE paper_ai_generations SET model='legacy',main_thread_id='old-thread' WHERE paper_id=?", (PID,))
            paper_ai.add_message(con, PID, 1, 'user', 'question', '历史问题')
            paper_ai.add_message(con, PID, 1, 'assistant', 'answer', '历史回答')
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.stack.enter_context(patch.object(server, 'PORT', self.httpd.server_port))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def post(self, action, payload):
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.httpd.server_port}/api/papers/{PID}/{action}',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            if 'ndjson' in response.headers['Content-Type']:
                return [json.loads(line) for line in response]
            return json.load(response)

    def test_chat_overview_and_structure_use_shared_configuration_and_preserve_old_data(self):
        events = self.post('ai/chat-stream', {'question': '测了什么？'})
        self.assertEqual(events[-1]['type'], 'completed')
        self.assertEqual(events[-1]['generation'], 1)
        result = self.post('ai/chat', {'question': '刚才的量是什么？'})
        self.assertEqual(result['citations'], [{'block_id': 'b1', 'page': 1}])
        self.assertEqual(self.records[0]['peer'], self.records[1]['peer'])
        self.assertIn('历史问题', self.records[0]['body']['messages'][1]['content'])
        self.assertIn('测了什么', self.records[1]['body']['messages'][1]['content'])
        for request in self.records:
            self.assertEqual(len(request['body']['messages']), 2)
            self.assertNotIn('previous_response_id', request['body'])
            self.assertNotIn('AGENTS.md', json.dumps(request['body']))
        with server.db() as con:
            old = paper_ai.history(con, PID, 1)
            self.assertEqual([m['text'] for m in old['messages']][:2], ['历史问题', '历史回答'])
            self.assertEqual(len(old['messages']), 6)
            self.assertEqual(old['thread_id'], 'old-thread')
            self.assertIsNone(paper_ai.conversation(con, PID)['thread_id'])

        self.reply.clear()
        self.reply.update(response_for('deepseek', generated()))
        self.post('summarize', {})
        server.JOBS.put(None)
        server.worker()
        self.assertEqual(server.get_doc(PID)['summarize_status'], 'completed')
        overview_prompt = self.records[-1]['body']['messages'][1]['content']
        self.assertNotIn('历史问题', overview_prompt)
        self.assertNotIn('既有人工译文', overview_prompt)

        roles = [{'id': b['id'], 'role': b['local_role']} for b in paper_structure._workflow.candidates(self.doc)]
        self.reply.clear()
        self.reply.update(response_for('deepseek', {'roles': roles}))
        self.post('structure', {})
        server.STRUCTURE_JOBS.put(None)
        server.structure_worker()
        saved = server.get_doc(PID)
        self.assertEqual(saved['reading_structure']['status'], 'ready')
        self.assertEqual(saved['reading_structure']['requested_config'],
                         api_settings.resolve_config(server.DATA, purpose='structure'))
        for name in ('blocks', 'notes', 'collection'):
            self.assertEqual(saved[name], self.doc[name])
        self.assertEqual(len(self.records), 4)

    def test_invalid_chat_response_is_terminal_and_does_not_save_messages(self):
        self.reply.clear()
        self.reply.update(response_for('deepseek', {'answer': '无效引用', 'citations': ['foreign-id']}))
        events = self.post('ai/chat-stream', {'question': '结果？'})
        self.assertEqual(events[-1]['type'], 'error')
        with server.db() as con:
            self.assertEqual(len(paper_ai.conversation(con, PID)['messages']), 2)
            self.assertEqual(len(paper_ai.history(con, PID, 1)['messages']), 2)
        self.assertEqual(server.get_doc(PID), self.doc)


if __name__ == '__main__':
    unittest.main()
