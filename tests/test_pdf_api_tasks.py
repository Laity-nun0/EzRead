"""临时库与本机 HTTP 供应商的全文 API 任务集成；禁止调用 Codex。"""
import copy
import http.client
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import patch
import zipfile

import codex_models
import paper_ai
import pdf_layout_engine
import server
from ezread import api_settings, pdf_layout_quality, pdf_translations as layout, translation_api
from tests import test_pdf_translation_tasks as fixtures

PID = fixtures.PID
KEY = 'synthetic-integration-secret'


class PdfApiTaskTests(unittest.TestCase):
    # 复用合成 PDF 夹具；本组验证实际回环 HTTP API。 
    request = fixtures.PdfTranslationTests.request
    engine = fixtures.PdfTranslationTests.engine

    def setUp(self):
        fixtures.PdfTranslationTests.setUp(self)
        for owner, name in ((paper_ai, 'CodexSession'), (paper_ai, 'PdfTranslationSession'), (codex_models, 'resolve_config'),
                            (paper_ai, 'lock_model'), (server, 'codex_status')):
            blocked = patch.object(owner, name, side_effect=AssertionError('API 任务不得调用 CLI'))
            blocked.start()
            self.addCleanup(blocked.stop)
        self.units = [fixtures.unit('Al steel'), fixtures.unit('Al alloy')]
        self.request_groups = [[self.units[0]], [self.units[1]]]
        self.provider_requests = []
        records = self.provider_requests
        self.reply_shape = 'string-envelope'
        self.override_outputs = None
        fixture = self

        class Provider(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_POST(handler):
                body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                records.append({'body': body, 'peer': handler.client_address, 'path': handler.path})
                prompt = body['messages'][-1]['content']
                units = json.loads(prompt.split('## Here is the input:\n\n')[1])
                outputs = [{'id': u['id'], 'output': '铝 钢' if 'steel' in u['input'] else '铝 合金'} for u in units]
                if fixture.override_outputs is not None:
                    outputs = fixture.override_outputs
                terms = [{'source': 'Al', 'target': '铝'}, {'source': 'steel', 'target': '钢'}] if 'steel' in prompt else []
                envelope = (outputs if fixture.reply_shape == 'direct-array' else
                            {'response': outputs if fixture.reply_shape == 'array-envelope' else
                             json.dumps(outputs, ensure_ascii=False), 'terms': terms})
                payload = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(envelope, ensure_ascii=False)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)

        self.provider = self.listener(Provider)
        local_base = f'http://127.0.0.1:{self.provider.server_port}'
        api_settings.save_settings(self.folder, {'purpose': 'translation', 'api_key': KEY})
        self.config = api_settings.resolve_config(self.folder)
        send_json = translation_api.ApiTranslationSession._send_json
        def local_send(session, *args, **kwargs):
            if session._config['base_url'] == 'https://api.deepseek.com':
                session._url = urlsplit(local_base)
            self.assertEqual(session._url.hostname, '127.0.0.1', '模拟测试不得连接真实供应商')
            return send_json(session, *args, **kwargs)
        redirect = patch.object(translation_api.ApiTranslationSession, '_send_json', local_send)
        redirect.start()
        self.addCleanup(redirect.stop)

    def listener(self, handler):
        listener = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        listener.daemon_threads = True
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener

    def start(self, **options):
        server.enqueue(PID, 'pdf_translate', options)
        # 这些既有夹具覆盖 v2 的响应包装与外层术语契约；新协议另用直接入队测试。
        server.update_doc(PID, lambda d: d[layout.TASK].update(protocol=layout.LEGACY_PROTOCOL))

    def run_queue(self, engine=None):
        server.JOBS.put(None)
        with patch.object(pdf_layout_engine, 'run_native', side_effect=engine or self.engine), \
                patch.object(pdf_layout_quality, 'inspect', return_value={
                    'status': 'passed', 'failures': [], 'warnings': []}):
            server.worker()

    def assert_completed(self):
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed', saved[layout.TASK].get('error'))
        return saved

    def app_http(self):
        listener = self.listener(server.Handler)
        patched = patch.object(server, 'PORT', listener.server_port)
        patched.start()
        self.addCleanup(patched.stop)
        base = f'http://127.0.0.1:{listener.server_port}'

        def fetch(path, body=None, *, method=None, origin=None, binary=False):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            request = urllib.request.Request(base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.read() if binary else json.load(response)
        return fetch

    def test_two_batches_reuse_connection_exact_prompts_and_only_relevant_terms(self):
        original_bytes = self.original.read_bytes()
        self.start(glossary=[{'source': 'grain', 'target': '晶粒'}])
        self.run_queue()
        saved = self.assert_completed()
        self.assertEqual(len(self.provider_requests), 2)
        self.assertEqual(self.provider_requests[0]['peer'], self.provider_requests[1]['peer'])
        for index, record in enumerate(self.provider_requests):
            self.assertEqual(record['path'], '/chat/completions')
            self.assertEqual(record['body']['thinking'], {'type': 'disabled'})
            messages = record['body']['messages']
            self.assertEqual([m['role'] for m in messages], ['system', 'user'])
            self.assertEqual(messages[1]['content'], self.request(self.request_groups[index])['prompt'])
            context = json.loads(messages[0]['content'].split('本批术语表（仅供术语一致性参考）：\n')[1])
            self.assertEqual(context, [] if index == 0 else [{'source': 'Al', 'target': '铝'}])
        self.assertEqual(self.original.read_bytes(), original_bytes)
        for key, value in self.doc.items():
            self.assertEqual(saved[key], value, key)
        self.assertEqual(saved[layout.TASK]['model_calls'], 2)
        self.assertEqual(saved[layout.TASK]['glossary_seed'], [{'source': 'grain', 'target': '晶粒'}])
        folder = layout.run_folder(server, PID, saved[layout.TASK]['run_id'])
        self.assertEqual(layout._read_json(folder / 'native-session.json', {})['protocol'], 'babeldoc-native-v2')
        for path in folder.rglob('*.json'):
            self.assertNotIn(KEY, path.read_text(encoding='utf-8'), str(path))
        public = json.dumps(layout.public_state(server, saved, full=True))
        for secret_field in (KEY, 'source_hash', 'raw_response', 'native-replies', 'thread_id'):
            self.assertNotIn(secret_field, public)
        for path in ('native-session.json', 'glossary.json', 'native-replies/example.json'):
            with self.assertRaises(ValueError):
                layout.media_file(server, PID, f"pdf-layout/{saved[layout.TASK]['run_id']}/{path}")

    def test_new_task_defaults_to_v3_with_native_glossary_and_no_outer_term_context(self):
        self.reply_shape = 'direct-array'
        seed = [{'source': 'Al', 'target': '铝'}]
        server.enqueue(PID, 'pdf_translate', {'glossary': seed})
        self.assertEqual(server.get_doc(PID)[layout.TASK]['protocol'], layout.NATIVE_PROTOCOL)
        engine_seeds = []
        def standard_engine(root, source, folder, **kwargs):
            engine_seeds.append(kwargs['glossary'])
            return self.engine(root, source, folder, **kwargs)
        self.run_queue(engine=standard_engine)
        saved = self.assert_completed()[layout.TASK]
        self.assertEqual(engine_seeds, [seed])
        self.assertEqual(saved['glossary_seed'], seed)
        self.assertEqual((saved['model_calls'], saved['reused']), (2, 0))
        folder = layout.run_folder(server, PID, saved['run_id'])
        self.assertEqual(layout._read_json(folder / 'native-session.json', {})['protocol'], layout.NATIVE_PROTOCOL)
        for index, record in enumerate(self.provider_requests):
            messages = record['body']['messages']
            self.assertEqual(messages[0]['content'], translation_api.STANDARD_NATIVE_SYSTEM)
            self.assertEqual(messages[1]['content'], self.request(self.request_groups[index])['prompt'])
            self.assertNotIn('response_format', record['body'])
        replies = list((folder / 'native-replies').glob('*.json'))
        self.assertEqual(len(replies), 2)
        for path in replies:
            reply = layout._read_json(path, {})
            self.assertNotIn('translation_context', reply['request'])
            self.assertEqual(reply['request']['identity']['protocol'], layout.NATIVE_PROTOCOL)
            self.assertEqual(reply['metadata']['terms'], [])
            self.assertIsInstance(json.loads(reply['response']), list)

    def test_old_task_without_protocol_keeps_v2_and_full_cache_after_component_upgrade(self):
        self.start()
        self.run_queue()
        previous = self.assert_completed()[layout.TASK]
        folder = layout.run_folder(server, PID, previous['run_id'])
        original = {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        server.update_doc(PID, lambda d: d[layout.TASK].pop('protocol'))
        with patch.object(pdf_layout_engine, 'availability', return_value={
                'available': True, 'engine': 'BabelDOC', 'version': '0.6.4', 'fingerprint': 'v3-upgraded-component'}):
            server.enqueue(PID, 'pdf_translate', {})
            queued = server.get_doc(PID)[layout.TASK]
            self.assertEqual(queued['protocol'], layout.LEGACY_PROTOCOL)
            self.assertEqual(queued['reuse_from']['run_id'], previous['run_id'])
            with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('v2全缓存不能重复付费')):
                self.run_queue()
        current = self.assert_completed()[layout.TASK]
        self.assertEqual((current['protocol'], current['reused'], current['model_calls']), (layout.LEGACY_PROTOCOL, 2, 0))
        self.assertEqual(len(self.provider_requests), 2)
        self.assertEqual({p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}, original)

    def test_explicit_retranslation_upgrades_v2_to_v3_without_mixing_old_caches(self):
        self.start()
        self.run_queue()
        previous = self.assert_completed()[layout.TASK]
        old_folder = layout.run_folder(server, PID, previous['run_id'])
        original = {p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}
        self.reply_shape = 'direct-array'
        server.enqueue(PID, 'pdf_translate', {'retranslate': True})
        queued = server.get_doc(PID)[layout.TASK]
        self.assertEqual(queued['protocol'], layout.NATIVE_PROTOCOL)
        self.assertNotEqual(queued['run_id'], previous['run_id'])
        self.assertNotIn('reuse_from', queued)
        self.run_queue()
        current = self.assert_completed()[layout.TASK]
        self.assertEqual((current['reused'], current['model_calls']), (0, 2))
        self.assertEqual(len(self.provider_requests), 4)
        self.assertTrue(all(record['body']['messages'][0]['content'] == translation_api.STANDARD_NATIVE_SYSTEM
                            for record in self.provider_requests[2:]))
        folder = layout.run_folder(server, PID, current['run_id'])
        self.assertFalse({p.name for p in (old_folder / 'native-replies').glob('*.json')} &
                         {p.name for p in (folder / 'native-replies').glob('*.json')})
        self.assertEqual({p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}, original)

    def test_v3_rejects_native_seed_conflicts_before_queue_or_provider_requests(self):
        original = server.get_doc(PID)
        for seed in ([{'source': 'Al', 'target': '铝'}, {'source': 'al', 'target': '铝元素'}],
                     [{'source': 'phase|grain', 'target': '相'}], [{'source': 'Al', 'target': '铝|金属'}]):
            with self.subTest(seed=seed), self.assertRaisesRegex(ValueError, '大小写|竖线'):
                server.enqueue(PID, 'pdf_translate', {'glossary': seed})
            self.assertEqual(server.get_doc(PID), original)
            self.assertTrue(server.JOBS.empty())
            self.assertEqual(self.provider_requests, [])

    def test_v2_can_resume_existing_seed_not_supported_by_native_glossary(self):
        self.start()
        seed = [{'source': 'Al', 'target': '铝'}, {'source': 'al', 'target': '铝元素'},
                {'source': 'phase|grain', 'target': '相|晶粒'}]
        server.update_doc(PID, lambda d: d[layout.TASK].update(glossary_seed=seed))
        server.enqueue(PID, 'pdf_translate', {})
        def legacy_engine(root, source, folder, **kwargs):
            self.assertNotIn('glossary', kwargs)
            return self.engine(root, source, folder, **kwargs)
        self.run_queue(engine=legacy_engine)
        current = self.assert_completed()[layout.TASK]
        self.assertEqual(current['protocol'], layout.LEGACY_PROTOCOL)
        self.assertEqual(current['glossary_seed'], seed)
        self.assertEqual(len(self.provider_requests), 2)

    def test_native_direct_arrays_complete_and_replay_without_new_api_calls(self):
        for shape in ('direct-array', 'array-envelope'):
            with self.subTest(shape=shape):
                self.reply_shape = shape
                self.start(retranslate=True)
                self.run_queue()
                saved = self.assert_completed()
                folder = layout.run_folder(server, PID, saved[layout.TASK]['run_id'])
                replies = {p.name: p.read_bytes() for p in (folder / 'native-replies').glob('*.json')}
                requests_before = len(self.provider_requests)
                self.start()
                with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('完整缓存不能付费重试')):
                    self.run_queue()
                self.assertEqual(self.assert_completed()[layout.TASK]['reused'], 2)
                self.assertEqual(len(self.provider_requests), requests_before)
                self.assertTrue(all((folder / 'native-replies' / name).read_bytes() == value
                                    for name, value in replies.items()))

    def test_direct_arrays_still_require_exact_ids_nonempty_outputs_and_placeholders(self):
        self.reply_shape = 'direct-array'
        cases = [([], 'batch_ids'), ([{'id': 9, 'output': '有效文字'}], 'batch_ids'),
                 ([{'id': True, 'output': '有效文字'}], 'batch_ids'),
                 ([{'id': 0, 'output': ''}], 'batch_empty'),
                 ([{'id': 0, 'output': '无公式'}], 'batch_placeholders')]
        for outputs, code in cases:
            with self.subTest(code=code, outputs=outputs):
                self.override_outputs = outputs
                self.units = [fixtures.unit('Al steel {v1}')]
                self.request_groups = [self.units]
                self.start(retranslate=True)
                self.run_queue()
                saved = server.get_doc(PID)
                self.assertEqual(saved[layout.TASK]['status'], 'error')
                self.assertEqual(saved[layout.TASK]['error_code'], code)
                self.assertNotIn(layout.CURRENT, saved)
                folder = layout.run_folder(server, PID, saved[layout.TASK]['run_id'])
                self.assertEqual(len(list((folder / 'native-replies').glob('*.json'))), 1)

    def test_old_api_task_without_thinking_resumes_and_reuses_cache_without_adding_default(self):
        self.start()
        # 模拟改动前已保存的 API 任务；旧快照与旧请求缓存均不带 thinking。
        server.update_doc(PID, lambda d: d[layout.TASK]['config'].pop('thinking'))
        old = copy.deepcopy(server.get_doc(PID)[layout.TASK])
        self.start()
        self.assertEqual(server.get_doc(PID)[layout.TASK], old)
        self.run_queue()
        saved = self.assert_completed()
        folder = layout.run_folder(server, PID, saved[layout.TASK]['run_id'])
        replies = {path.name: path.read_bytes() for path in (folder / 'native-replies').glob('*.json')}
        self.assertEqual(saved[layout.TASK]['config'], {k: v for k, v in self.config.items() if k != 'thinking'})
        self.assertTrue(all('thinking' not in record['body'] for record in self.provider_requests))
        self.start()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['config'], {k: v for k, v in self.config.items() if k != 'thinking'})
        with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('旧缓存不应重译')):
            self.run_queue()
        current = self.assert_completed()
        self.assertEqual((current[layout.TASK]['reused'], current[layout.TASK]['model_calls']), (2, 0))
        self.assertEqual(len(self.provider_requests), 2)
        for name, content in replies.items():
            self.assertEqual((folder / 'native-replies' / name).read_bytes(), content)
        with self.assertRaisesRegex(ValueError, '重新翻译'):
            self.start(thinking='disabled')
        self.start(retranslate=True)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['config']['thinking'], 'disabled')
        self.assertNotIn('reuse_from', server.get_doc(PID)[layout.TASK])
        self.run_queue()
        self.assertEqual(self.assert_completed()[layout.TASK]['reused'], 0)
        self.assertEqual(len(self.provider_requests), 4)

    def test_thinking_switch_requires_retranslation_and_does_not_change_shared_config(self):
        settings_path = self.folder / api_settings._FILENAME
        before_settings = settings_path.read_bytes()
        self.start()
        self.run_queue()
        old = self.assert_completed()
        with self.assertRaisesRegex(ValueError, '重新翻译'):
            self.start(thinking='enabled')
        self.assertEqual(server.get_doc(PID), old)
        self.start(thinking='enabled', retranslate=True)
        task = server.get_doc(PID)[layout.TASK]
        self.assertNotEqual(task['run_id'], old[layout.TASK]['run_id'])
        self.assertNotIn('reuse_from', task)
        self.run_queue()
        saved = self.assert_completed()
        self.assertEqual((saved[layout.TASK]['reused'], saved[layout.TASK]['model_calls']), (0, 2))
        self.assertEqual(self.provider_requests[-1]['body']['thinking'], {'type': 'enabled'})
        self.assertEqual(api_settings.resolve_config(self.folder), self.config)
        self.assertEqual(settings_path.read_bytes(), before_settings)

    def test_effort_snapshot_is_frozen_and_new_version_preserves_draft(self):
        self.start(thinking='enabled', reasoning_effort='low')
        old = copy.deepcopy(server.get_doc(PID)[layout.TASK])
        self.run_queue()
        self.assertEqual(self.provider_requests[0]['body']['reasoning_effort'], 'low')
        with self.assertRaisesRegex(ValueError, '重新翻译'):
            self.start(thinking='enabled', reasoning_effort='max')
        self.start(retranslate=True, thinking='enabled', reasoning_effort='max')
        self.run_queue()
        saved = self.assert_completed()
        self.assertNotEqual(saved[layout.TASK]['run_id'], old['run_id'])
        self.assertEqual(self.provider_requests[-1]['body']['reasoning_effort'], 'max')
        self.assertNotIn('reuse_from', saved[layout.TASK])
        self.assertEqual(saved[layout.VERSIONS][0]['id'], old['run_id'])

    def test_removed_batch_experiment_requires_explicit_retranslation_before_any_execution(self):
        self.start()
        self.run_queue()
        before = self.assert_completed()
        for policy in ('source-800-max-8-v1', 'unknown', None):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, '实验已停用.*重新翻译'):
                self.start(retranslate=True, batch_policy=policy)
            self.assertEqual(server.get_doc(PID), before)
        server.update_doc(PID, lambda d: d[layout.TASK].update(batch_policy='source-800-max-8-v1', protocol='unknown'))
        candidate = server.get_doc(PID)
        with patch.object(pdf_layout_engine, 'availability', side_effect=AssertionError('旧实验不能进入组件检查')), \
             patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('旧实验不能建立API连接')):
            for action in (lambda: layout.task_protocol(candidate[layout.TASK]), lambda: self.start(),
                           lambda: server.execute_pdf_translation(PID, threading.Event())):
                with self.assertRaisesRegex(ValueError, '实验已停用.*重新翻译'):
                    action()
        self.assertEqual(server.get_doc(PID), candidate)
        self.assertTrue(server.JOBS.empty())
        self.assertEqual(len(self.provider_requests), 2)
        self.reply_shape = 'direct-array'
        server.enqueue(PID, 'pdf_translate', {'retranslate': True})
        task = server.get_doc(PID)[layout.TASK]
        self.assertEqual(task['protocol'], layout.NATIVE_PROTOCOL)
        self.assertNotIn('batch_policy', task)
        self.assertNotIn('reuse_from', task)
        self.run_queue()
        saved = self.assert_completed()[layout.TASK]
        self.assertEqual((saved['reused'], saved['model_calls']), (0, 2))
        self.assertEqual(len(self.provider_requests), 4)

    def test_removed_experiment_cache_is_not_reused_by_ordinary_component_upgrade(self):
        self.start()
        self.run_queue()
        previous = self.assert_completed()[layout.TASK]
        old_folder = layout.run_folder(server, PID, previous['run_id'])
        manifest = layout._read_json(old_folder / 'native-session.json', {})
        layout._write_json(old_folder / 'native-session.json', {**manifest, 'batch_policy': 'source-800-max-8-v1'})
        original = {p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}
        with patch.object(pdf_layout_engine, 'availability', return_value={
                'available': True, 'engine': 'BabelDOC', 'version': '0.6.4', 'fingerprint': 'ordinary-upgrade'}):
            self.start()
            self.run_queue()
        saved = self.assert_completed()[layout.TASK]
        self.assertEqual((saved['reused'], saved['model_calls']), (0, 2))
        self.assertEqual(len(self.provider_requests), 4)
        folder = layout.run_folder(server, PID, saved['run_id'])
        self.assertEqual(layout._read_json(folder / 'cache-sources.json', []),
                         [{'run_id': previous['run_id'], 'status': 'identity_mismatch'}])
        self.assertEqual({p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}, original)

    def test_failed_outer_response_updates_stage_and_exposes_only_safe_diagnostics(self):
        from tests.test_translation_api import mock_api
        with mock_api(reply=b'bad JSON ' + KEY.encode()) as (config, _, _, _):
            api_settings.save_settings(self.folder, {**config, 'api_key': KEY})
            self.start(**config)
            self.run_queue()
        saved = server.get_doc(PID)
        task = saved[layout.TASK]
        self.assertEqual((task['status'], task['stage'], task['error_code']), ('error', '翻译已中断', 'api_json'))
        self.assertTrue(task['error_details']['diagnostic_saved'])
        self.assertEqual(task['error_details']['stage'], 'outer_json')
        public = json.dumps(layout.public_state(server, saved))
        self.assertNotIn(KEY, public)
        self.assertNotIn('raw_base64', public)
        self.assertNotIn('bad JSON', public)
        private = layout.run_folder(server, PID, task['run_id']) / 'api-diagnostics'
        self.assertEqual(len(list(private.glob('*.json'))), 1)

    def test_component_fingerprint_upgrade_replays_terms_without_legacy_unit_migration(self):
        self.start()
        self.run_queue()
        previous = self.assert_completed()
        old_folder = layout.run_folder(server, PID, previous[layout.TASK]['run_id'])
        old_pdf = (old_folder / 'result.pdf').read_bytes()
        with patch.object(pdf_layout_engine, 'availability', return_value={
                'available': True, 'engine': 'BabelDOC', 'version': '0.6.4', 'fingerprint': 'updated-local-code'}), \
             patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('全缓存不新建连接')):
            self.start()
            self.run_queue()
        saved = self.assert_completed()
        self.assertNotEqual(saved[layout.TASK]['run_id'], previous[layout.TASK]['run_id'])
        self.assertEqual(saved[layout.TASK]['model_calls'], 0)
        self.assertEqual(saved[layout.TASK]['glossary'], previous[layout.TASK]['glossary'])
        self.assertEqual(len(self.provider_requests), 2)
        self.assertEqual((old_folder / 'result.pdf').read_bytes(), old_pdf)

    def test_pause_replays_terms_then_only_calls_remaining_batch_and_full_cache_needs_no_session(self):
        def pause_after_first(root, source, folder, **kwargs):
            kwargs['translate'](self.request(self.request_groups[0]))
            kwargs['cancel_event'].set()
            kwargs['translate'](self.request(self.request_groups[1]))

        self.start()
        self.run_queue(engine=pause_after_first)
        paused = server.get_doc(PID)[layout.TASK]
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(len(self.provider_requests), 1)
        # 故意污染展示快照；恢复必须从冻结种子与缓存重放。
        server.update_doc(PID, lambda d: d[layout.TASK].update(glossary={'entries': [], 'warnings': ['stale']}))
        self.start()
        self.run_queue()
        saved = self.assert_completed()
        self.assertEqual(saved[layout.TASK]['run_id'], paused['run_id'])
        self.assertEqual((saved[layout.TASK]['reused'], saved[layout.TASK]['model_calls']), (1, 1))
        self.assertEqual(len(self.provider_requests), 2)
        system = self.provider_requests[-1]['body']['messages'][0]['content']
        self.assertIn('"Al"', system)
        self.assertNotIn('"steel"', system)
        expected = saved[layout.TASK]['glossary']
        self.start()
        with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('全缓存不得创建连接')):
            self.run_queue()
        saved = self.assert_completed()
        self.assertEqual((saved[layout.TASK]['reused'], saved[layout.TASK]['model_calls']), (2, 0))
        self.assertEqual(saved[layout.TASK]['glossary'], expected)
        self.assertEqual(len(self.provider_requests), 2)

    def test_two_component_upgrades_recover_ancestor_cache_after_partial_replay(self):
        self.start()
        self.run_queue()
        first = self.assert_completed()[layout.TASK]
        old_folder = layout.run_folder(server, PID, first['run_id'])
        original = {p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}
        self.assertEqual(len(self.provider_requests), 2)

        def pause_after_first(root, source, folder, **kwargs):
            kwargs['translate'](self.request(self.request_groups[0]))
            kwargs['cancel_event'].set()
            kwargs['translate'](self.request(self.request_groups[1]))

        status = {'available': True, 'version': '0.6.4', 'fingerprint': 'upgrade-1'}
        with patch.object(pdf_layout_engine, 'availability', return_value=status):
            self.start()
            self.run_queue(engine=pause_after_first)
        second = server.get_doc(PID)[layout.TASK]
        self.assertEqual(second['status'], 'paused')
        self.assertEqual(second['reuse_from']['run_id'], first['run_id'])
        status['fingerprint'] = 'upgrade-2'
        with patch.object(pdf_layout_engine, 'availability', return_value=status):
            self.start()
            with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('祖先全缓存不得建连接')):
                self.run_queue()
        saved = self.assert_completed()[layout.TASK]
        self.assertEqual(saved['reuse_from']['run_id'], second['run_id'])
        self.assertNotIn('reuse_from', saved['reuse_from'])  # 已丢链快照仍须恢复祖先。
        self.assertEqual((saved['reused'], saved['model_calls'], saved['received']), (2, 0, 2))
        self.assertEqual(len(self.provider_requests), 2)
        self.assertEqual({p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}, original)

    def test_explicit_retranslation_does_not_scan_or_reuse_other_run_caches(self):
        self.start()
        self.run_queue()
        self.assert_completed()
        self.start(retranslate=True)
        self.assertNotIn('reuse_from', server.get_doc(PID)[layout.TASK])
        self.run_queue()
        saved = self.assert_completed()[layout.TASK]
        self.assertEqual((saved['reused'], saved['model_calls']), (0, 2))
        self.assertEqual(len(self.provider_requests), 4)

    def test_old_component_draft_restores_paused_then_confirms_new_run_and_keeps_source_guard(self):
        def pause_after_first(root, source, folder, **kwargs):
            kwargs['translate'](self.request(self.request_groups[0]))
            kwargs['cancel_event'].set()
            kwargs['translate'](self.request(self.request_groups[1]))
        self.start()
        self.run_queue(engine=pause_after_first)
        draft = server.get_doc(PID)[layout.TASK]
        self.start(retranslate=True)
        self.run_queue()
        self.assert_completed()
        old_folder = layout.run_folder(server, PID, draft['run_id'])
        original = {p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}
        status = {'available': True, 'version': '0.6.4', 'fingerprint': 'upgraded'}
        with patch.object(pdf_layout_engine, 'availability', return_value=status):
            layout.restore_draft(server, PID, draft['run_id'])
            restored = server.get_doc(PID)[layout.TASK]
            self.assertEqual(restored['status'], 'paused')
            self.assertEqual(restored['run_id'], draft['run_id'])
            self.assertIn('新目录', restored['error'])
            self.start()
            self.assertNotEqual(server.get_doc(PID)[layout.TASK]['run_id'], draft['run_id'])
            with patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('已有完整缓存不得建连接')):
                self.run_queue()
        self.assert_completed()
        self.assertEqual({p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob('*') if p.is_file()}, original)
        doc = server.get_doc(PID)
        self.original.write_bytes(self.original.read_bytes() + b'changed source')
        with self.assertRaisesRegex(ValueError, '原 PDF'):
            layout.restore_draft(server, PID, draft['run_id'])
        self.assertEqual(server.get_doc(PID), doc)

    def test_provider_endpoint_model_and_seed_changes_require_explicit_retranslation(self):
        self.start()
        self.run_queue()
        before = server.get_doc(PID)
        for changed in ({'provider': 'openai_compatible'}, {'base_url': self.config['base_url'] + '/v1'},
                        {'model': 'different-model'}, {'glossary': [{'source': 'Al', 'target': '铝'}]}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, '重新翻译'):
                self.start(**changed)
            self.assertEqual(server.get_doc(PID), before)
        self.start(retranslate=True, glossary=[{'source': 'Al', 'target': '铝'}])
        task = server.get_doc(PID)[layout.TASK]
        self.assertNotEqual(task['run_id'], before[layout.TASK]['run_id'])
        self.assertNotIn('reuse_from', task)

    def test_http_settings_confirmation_origin_public_payload_and_export_exclude_private_key(self):
        self.reply_shape = 'direct-array'
        fetch = self.app_http()
        settings = fetch('/api/translation-api')
        self.assertTrue(settings['profiles']['deepseek']['has_key'])
        self.assertNotIn(KEY, json.dumps(settings))
        settings = fetch('/api/translation-api', {'purpose': 'translation', 'api_key': KEY}, method='PUT')
        self.assertNotIn(KEY, json.dumps(settings))
        for method, body in (('GET', None), ('PUT', self.config)):
            # 来源检查先于读取正文；先发头可避免 Windows 对未读请求体关闭时的 TCP reset 竞争。
            with self.subTest(method=method), closing(http.client.HTTPConnection('127.0.0.1', server.PORT, timeout=5)) as connection:
                connection.putrequest(method, '/api/translation-api')
                connection.putheader('Origin', 'https://remote.invalid')
                connection.putheader('Content-Type', 'application/json')
                if body is not None:
                    connection.putheader('Content-Length', str(len(json.dumps(body).encode())))
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
        # GET 只读，不暗中触发任务；确认以现有 POST 契约入队。
        self.assertTrue(server.JOBS.empty())
        result = fetch(f'/api/papers/{PID}/pdf-translate', {})
        self.assertEqual(result['paper'][layout.TASK]['status'], 'queued')
        self.assertEqual(server.get_doc(PID)[layout.TASK]['protocol'], layout.NATIVE_PROTOCOL)
        self.assertNotIn(KEY, json.dumps(result))
        self.run_queue()
        self.assert_completed()
        saved = server.get_doc(PID)
        private = layout.run_folder(server, PID, saved[layout.TASK]['run_id']) / 'api-diagnostics' / 'failure.json'
        layout._write_json(private, {'raw_base64': 'private-response'})
        with self.assertRaises(ValueError):
            layout.media_file(server, PID, f"pdf-layout/{saved[layout.TASK]['run_id']}/api-diagnostics/failure.json")
        papers = fetch('/api/papers')
        for private in (KEY, 'source_hash', 'raw_response', 'thread_id'):
            self.assertNotIn(private, json.dumps(papers))
        exported = fetch('/api/export', binary=True)
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            self.assertFalse(any('translation-api.private' in name for name in archive.namelist()))
            self.assertFalse(any('api-diagnostics' in name for name in archive.namelist()))
            self.assertIn('data/library.sqlite3', archive.namelist())
            for name in archive.namelist():
                self.assertNotIn(KEY.encode(), archive.read(name), name)

    def legacy_task(self):
        return {'run_id': 'a' * 16, 'status': 'paused', 'config': fixtures.LEGACY_CONFIG,
                'source_hash': layout.file_hash(self.original), 'engine_version': '0.6.4',
                'engine_fingerprint': None, 'done': 1, 'total': 2, 'thread_id': 'legacy-thread',
                'stage': '旧草稿', 'warnings': ['保留原诊断']}

    def test_explicit_cli_options_and_implicit_legacy_resume_are_rejected_without_changes(self):
        before = server.get_doc(PID)
        with self.assertRaisesRegex(ValueError, 'API'):
            self.start(model='old-cli-model', reasoning_effort='low')
        self.assertEqual(server.get_doc(PID), before)
        server.update_doc(PID, lambda d: d.update({layout.TASK: self.legacy_task()}))
        before = server.get_doc(PID)
        for options in ({}, {'provider': 'deepseek'}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, '重新翻译'):
                self.start(**options)
            self.assertEqual(server.get_doc(PID), before)
        self.assertTrue(server.JOBS.empty())

    def test_queued_legacy_task_stops_before_engine_or_either_transport_and_preserves_files(self):
        task = self.legacy_task()
        server.update_doc(PID, lambda d: d.update({layout.TASK: copy.deepcopy(task)}))
        folder = layout.run_folder(server, PID, task['run_id'])
        layout._write_json(folder / 'native-session.json', {'config': task['config']})
        before = (folder / 'native-session.json').read_bytes()
        with patch.object(paper_ai, 'PdfTranslationSession', side_effect=AssertionError('禁止旧会话')), \
             patch.object(translation_api, 'ApiTranslationSession', side_effect=AssertionError('禁止自动迁移')), \
             patch.object(pdf_layout_engine, 'run_native', side_effect=AssertionError('禁止自动排版')):
            with self.assertRaisesRegex(ValueError, 'API'):
                server.execute_pdf_translation(PID, threading.Event())
            server.QUEUED.add((PID, 'pdf_translate'))
            server.CANCEL[(PID, 'pdf_translate')] = threading.Event()
            server.JOBS.put((PID, 'pdf_translate'))
            server.JOBS.put(None)
            server.worker()
        saved = server.get_doc(PID)[layout.TASK]
        self.assertEqual(saved['status'], 'error')
        self.assertIn('API', saved['error'])
        for key, value in task.items():
            if key not in ('status', 'stage'):
                self.assertEqual(saved[key], value)
        self.assertEqual(saved['stage'], '翻译已中断')
        self.assertEqual((folder / 'native-session.json').read_bytes(), before)
        self.assertFalse((folder / 'translation-timing.json').exists())

    def test_restore_legacy_draft_is_local_even_without_api_settings_or_model_catalogue(self):
        old = self.legacy_task()
        server.update_doc(PID, lambda d: d.update({layout.DRAFTS: [copy.deepcopy(old)]}))
        with patch.object(api_settings, 'resolve_config', side_effect=AssertionError('恢复不读配置')), \
             patch.object(api_settings, 'get_api_key', side_effect=AssertionError('恢复不读密钥')):
            server.restore_pdf_translation_draft(PID, old['run_id'])
        restored = server.get_doc(PID)[layout.TASK]
        for key, value in old.items():
            if key != 'stage':
                self.assertEqual(restored[key], value)
        self.assertEqual(restored['stage'], '翻译已暂停')
        self.assertIn('API', restored['error'])
        self.assertTrue(server.JOBS.empty())

    def test_other_provider_is_rejected_without_changing_published_pdf_or_cache(self):
        self.start()
        self.run_queue()
        previous = self.assert_completed()
        with self.assertRaisesRegex(ValueError, 'DeepSeek'):
            self.start(provider='openai_compatible', retranslate=True)
        self.assertEqual(server.get_doc(PID)[layout.TASK], previous[layout.TASK])
        self.assertEqual(len(self.provider_requests), 2)

    def test_legacy_draft_to_api_archives_draft_and_keeps_published_version(self):
        old = self.legacy_task()
        old_folder = layout.run_folder(server, PID, old['run_id'])
        old_files = {'native-session.json': b'old manifest', 'native-replies/reply.json': b'old cache',
                     'source-units.json': b'old source units', 'page-001.jpg': b'old asset'}
        for name, content in old_files.items():
            path = old_folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        artifact = {'id': 'b' * 16, 'quality': 'passed', 'mono': 'result.pdf', 'dual': 'bilingual.pdf',
                    'page_count': 1, 'pages': [], 'warnings': []}
        folder = layout.run_folder(server, PID, artifact['id'])
        fixtures.write_pdf(folder / 'result.pdf')
        fixtures.write_pdf(folder / 'bilingual.pdf', width=1200)
        before_bytes = {name: (folder / artifact[name]).read_bytes() for name in ('mono', 'dual')}
        server.update_doc(PID, lambda d: d.update({layout.TASK: copy.deepcopy(old), layout.CURRENT: artifact}))
        with self.assertRaisesRegex(ValueError, '重新翻译'):
            self.start(provider='deepseek')
        self.start(provider='deepseek', retranslate=True)
        self.assertIn(old, server.get_doc(PID)[layout.DRAFTS])
        self.run_queue()
        saved = self.assert_completed()
        self.assertIn(artifact, saved[layout.VERSIONS])
        self.assertIn(old, saved[layout.DRAFTS])
        for name, value in before_bytes.items():
            self.assertEqual((folder / artifact[name]).read_bytes(), value)
        self.assertEqual(saved['translation_versions'], self.doc['translation_versions'])
        self.assertNotEqual(saved[layout.TASK]['run_id'], old['run_id'])
        self.assertNotIn('reuse_from', saved[layout.TASK])
        self.assertEqual(saved[layout.TASK]['config'], {**self.config, 'thinking': 'disabled'})
        self.assertEqual(saved[layout.TASK]['reused'], 0)
        for name, content in old_files.items():
            self.assertEqual((old_folder / name).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
