"""Regression checks use an isolated SQLite library and mock all model calls."""
import copy
import json
import queue
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import codex_bridge
import codex_models

PID = '0123456789abcdef'


def resolve(model='', reasoning_effort=''):
    model = model or 'model-a'
    effort = reasoning_effort or 'medium'
    if model not in ('model-a', 'model-b') or effort not in ('low', 'medium', 'high'):
        raise ValueError('模型或推理强度不可用')
    return {'model': model, 'reasoning_effort': effort}


class TranslationTests(unittest.TestCase):
    def setUp(self):
        work = (ROOT / 'work').resolve()
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='translation-tests-', dir=work)
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(work))
        self.patches = [patch.object(server, 'DATA', self.folder), patch.object(server, 'LIBRARY', self.folder / 'library'),
                        patch.object(server, 'JOBS', queue.Queue()), patch.object(server, 'QUEUED', set()),
                        patch.object(server, 'CANCEL', {}), patch.object(server, 'RESUME_REQUESTED', set()),
                        patch.object(server, 'QUOTA_PAUSED', threading.Event()),
                        patch.object(codex_models, 'resolve_config', side_effect=resolve)]
        for mock in self.patches:
            mock.start()
        server.init_db()
        self.doc = {'id': PID, 'hash': 'test-hash', 'title': 'Synthetic test document', 'deleted': False,
                    'pages': [{'number': 1}, {'number': 2}], 'figures': [], 'notes': 'paper note',
                    'blocks': [{'id': 'p1', 'page': 1, 'text': 'First synthetic paragraph.', 'note': 'first note', 'highlight': True},
                               {'id': 'p2', 'page': 2, 'text': 'Second synthetic paragraph.', 'note': 'second note'}],
                    'translation': {'status': 'idle', 'error': ''}, 'read_page': 1}
        server.put_doc(self.doc)

    def tearDown(self):
        for mock in reversed(self.patches):
            mock.stop()
        self.assertTrue(self.folder.is_relative_to((ROOT / 'work').resolve()))
        self.temp.cleanup()

    def defaults(self, model='model-b', effort='high'):
        with server.db() as con:
            for key, value in [('translation_model', model), ('translation_reasoning_effort', effort)]:
                con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, json.dumps(value)))

    def add_old_translations(self):
        self.doc['blocks'][0].update(translation='old manual p1', translation_manually_edited=True)
        self.doc['blocks'][1]['translation'] = 'old p2'
        self.doc['translation']['status'] = 'completed'
        server.put_doc(self.doc)

    def test_legacy_enqueue_is_rejected_without_model_or_data_change(self):
        before = server.get_doc(PID)
        with patch.object(codex_models, 'resolve_config', side_effect=AssertionError('must stay local')):
            with self.assertRaises(ValueError):
                server.enqueue(PID, 'translate', {'retranslate': True})
        self.assertEqual(server.get_doc(PID), before)
        self.assertFalse(server.QUEUED)
        self.assertTrue(server.JOBS.empty())

    def test_stale_legacy_queue_job_cannot_execute_or_resume(self):
        server.QUEUED.add((PID, 'translate'))
        server.RESUME_REQUESTED.add((PID, 'translate'))
        server.JOBS.put((PID, 'translate'))
        server.JOBS.put(None)
        with patch.object(codex_bridge, 'translate_selection', side_effect=AssertionError('must not call model')):
            server.worker()
        self.assertEqual(server.get_doc(PID), self.doc)
        self.assertFalse(server.QUEUED)
        self.assertFalse(server.RESUME_REQUESTED)
        self.assertTrue(server.JOBS.empty())

    def test_restart_retires_old_tasks_without_losing_source_or_drafts(self):
        self.add_old_translations()
        server.update_doc(PID, lambda d: d['translation'].update(
            status='running', config={'model': 'retired-model', 'reasoning_effort': 'high'},
            run_id='old-run', staging={'p1': 'saved chunk'}, thread_id='old-thread'))
        before = server.get_doc(PID)
        server.init_db()
        after = server.get_doc(PID)
        expected = copy.deepcopy(before)
        expected.update(paper_type='journal', conference_name='', conference_abbr='', conference_track='unknown')
        expected['translation'].update(status='retired', error='旧正文全文翻译已停用，历史译文和草稿已保留。')
        self.assertEqual(after, expected)
        self.assertTrue(server.JOBS.empty())
        server.init_db()
        self.assertEqual(server.get_doc(PID), after)

    def test_restore_manual_version_preserves_source_annotations_and_pending_draft(self):
        self.add_old_translations()
        doc = server.get_doc(PID)
        doc['text_annotations'] = [{'id': 'annotation', 'note': 'keep range note'}]
        server.archive_translation(doc)
        version = copy.deepcopy(doc['translation_versions'][0])
        doc['blocks'][0].update(translation='new manual edit', note='new note', text_override='corrected source')
        doc['translation'].update(staging={'p1': 'valuable draft'}, config={'model': 'removed-model'})
        server.put_doc(doc)
        with patch.object(codex_models, 'resolve_config', side_effect=AssertionError('must stay local')):
            server.restore_translation(PID, version['id'])
        restored = server.get_doc(PID)
        self.assertEqual(restored['blocks'][0]['translation'], 'old manual p1')
        self.assertTrue(restored['blocks'][0]['translation_manually_edited'])
        self.assertEqual(restored['blocks'][0]['text'], self.doc['blocks'][0]['text'])
        self.assertEqual(restored['blocks'][0]['text_override'], 'corrected source')
        self.assertEqual(restored['blocks'][0]['note'], 'new note')
        self.assertTrue(restored['blocks'][0]['highlight'])
        self.assertEqual(restored['text_annotations'], doc['text_annotations'])
        self.assertEqual(restored['notes'], 'paper note')
        self.assertEqual(restored['translation_drafts'][0]['task']['staging'], {'p1': 'valuable draft'})
        self.assertEqual(restored['translation_versions'][-1]['blocks']['p1']['translation'], 'new manual edit')
        self.assertEqual(restored['translation']['status'], 'completed')
        self.assertTrue(server.JOBS.empty())

    def test_draft_recovery_does_not_require_live_model_or_replace_readable_text(self):
        self.add_old_translations()
        doc = server.get_doc(PID)
        doc['translation'].update(staging={'p1': 'draft a'}, config={'model': 'missing-model'}, run_id='old')
        server.archive_translation_draft(doc)
        draft = copy.deepcopy(doc['translation_drafts'][0])
        doc['translation'].update(staging={'p2': 'draft b'})
        server.put_doc(doc)
        with patch.object(codex_models, 'resolve_config', side_effect=AssertionError('must stay local')):
            server.restore_translation_draft(PID, draft['id'])
        recovered = server.get_doc(PID)
        self.assertEqual(recovered['translation']['staging'], {'p1': 'draft a'})
        self.assertEqual(recovered['translation']['status'], 'retired')
        self.assertEqual(recovered['translation']['config'], {'model': 'missing-model'})
        self.assertEqual(recovered['translation_drafts'][-1]['task']['staging'], {'p2': 'draft b'})
        self.assertEqual(recovered['blocks'], self.doc['blocks'])
        self.assertTrue(server.JOBS.empty())

    def test_missing_history_recovery_leaves_document_unchanged(self):
        before = server.get_doc(PID)
        for restore in (server.restore_translation, server.restore_translation_draft):
            with self.subTest(restore=restore.__name__), self.assertRaises(ValueError):
                restore(PID, 'missing')
            self.assertEqual(server.get_doc(PID), before)

    def test_public_document_does_not_leak_staging_or_versions_content(self):
        self.add_old_translations()
        doc = server.get_doc(PID)
        server.archive_translation(doc)
        doc['translation'].update(staging={'p1': 'SECRET DRAFT'}, status='retired')
        server.archive_translation_draft(doc)
        server.put_doc(doc)
        public = server.public_doc(server.get_doc(PID))
        encoded = json.dumps(public)
        self.assertNotIn('SECRET DRAFT', encoded)
        self.assertNotIn('old manual', encoded)
        self.assertNotIn('translation_runs', encoded)
        self.assertTrue(public['translation']['has_staging'])

    def test_retired_http_routes_return_gone_without_login_or_enqueue(self):
        listener = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, 'PORT', listener.server_port), \
                    patch.object(server, 'codex_status', side_effect=AssertionError('must stay local')), \
                    patch.object(server, 'enqueue', side_effect=AssertionError('must not enqueue')):
                for action in ('translate', 'pause'):
                    req = urllib.request.Request(f'http://127.0.0.1:{listener.server_port}/api/papers/{PID}/{action}',
                                                 b'{}', {'Content-Type': 'application/json'}, method='POST')
                    with self.subTest(action=action), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(req)
                    self.assertEqual(error.exception.code, 410)
            self.assertEqual(server.get_doc(PID), self.doc)
        finally:
            listener.shutdown()
            thread.join(timeout=2)
            listener.server_close()

    def test_reader_state_validation_and_independent_settings(self):
        state = {'version': 1, 'page': 2, 'original_offset': .5, 'translation_block_id': 'p2',
                 'translation_offset': .25, 'zoom': 125, 'split_ratio': .6}
        self.assertEqual(server.validate_reader_state(state, self.doc), state)
        for legacy in ('original', 'translation', 'parallel'):
            self.assertEqual(server.validate_reader_state({**state, 'mode': legacy}, self.doc), state)
        for zoom in (40, 50, 60):
            self.assertEqual(server.validate_reader_state({**state, 'zoom': zoom}, self.doc)['zoom'], zoom)
            self.assertEqual(server.validate_settings({'reader_zoom': zoom}), {'reader_zoom': zoom})
        for zoom in (39, 201, True, float('nan')):
            with self.assertRaises(ValueError):
                server.validate_settings({'reader_zoom': zoom})
        for bad in ({'page': True}, {'page': 3}, {'original_offset': float('nan')},
                    {'translation_block_id': 'foreign'}, {'split_ratio': .9}, {'zoom': 99}, {'mode': 'bad'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.validate_reader_state(bad, self.doc)
        with patch.object(codex_models, 'resolve_config', side_effect=AssertionError('must not load model catalog')):
            self.assertEqual(server.validate_settings({'theme': 'sage', 'reader_split_ratio': .6}), {'theme': 'sage', 'reader_split_ratio': .6})

    def test_supported_themes_and_retired_saved_values(self):
        for theme in ('paper', 'sage', 'graphite'):
            with self.subTest(theme=theme):
                self.assertEqual(server.validate_settings({'theme': theme}), {'theme': theme})
                with server.db() as con:
                    con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('theme', json.dumps(theme)))
                self.assertEqual(server.settings()['theme'], theme)
        with server.db() as con:
            con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('ui_font_size', '20'))
        for retired in ('cream', 'light', 'unknown', None, True):
            with self.subTest(retired=retired):
                with self.assertRaises(ValueError):
                    server.validate_settings({'theme': retired})
                with server.db() as con:
                    con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('theme', json.dumps(retired)))
                self.assertEqual(server.settings()['theme'], 'paper')
                self.assertEqual(server.settings()['ui_font_size'], 20)
        self.assertEqual(server.get_doc(PID), self.doc)

    def test_selection_reasoning_settings_are_independent_and_validated(self):
        self.assertEqual(server.settings()['selection_translation_reasoning_effort'], '')
        self.defaults('model-b', 'high')
        with server.db() as con:
            con.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('selection_translation_model', json.dumps('model-a')))
        self.assertEqual(server.validate_settings({'selection_translation_reasoning_effort': 'low'}),
                         {'selection_translation_reasoning_effort': 'low'})
        for invalid in ('x' * 161, True, None, 123):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                server.validate_settings({'selection_translation_reasoning_effort': invalid})
        self.assertEqual(server.settings()['translation_model'], 'model-b')
        self.assertEqual(server.settings()['translation_reasoning_effort'], 'high')

    def test_selection_http_uses_api_independently_of_legacy_preferences(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(httpd.server_port)
        try:
            with patch.object(server, 'PORT', httpd.server_port), \
                    patch.object(server, 'codex_status', side_effect=AssertionError('不得查登录')), \
                    patch('ezread.selection_api.translate_selection', return_value='模拟划线译文') as translate:
                for effort, expected in (('', 'medium'), ('high', 'high')):
                    settings = {'selection_translation_model': 'model-a', 'selection_translation_reasoning_effort': effort}
                    req = urllib.request.Request(base + '/api/settings', json.dumps(settings).encode(),
                                                 {'Content-Type': 'application/json'}, method='PATCH')
                    with urllib.request.urlopen(req) as response:
                        self.assertEqual(json.load(response)['selection_translation_reasoning_effort'], effort)
                    req = urllib.request.Request(base + '/api/papers/' + PID + '/ai/highlight',
                        json.dumps({'block_id': 'p1', 'text': 'First synthetic paragraph.'}).encode(),
                        {'Content-Type': 'application/json'}, method='POST')
                    with urllib.request.urlopen(req) as response:
                        self.assertEqual(json.load(response)['translation'], '模拟划线译文')
                    translate.assert_called_with(server, 'First synthetic paragraph.', paper_id=PID, cancel_event=server.SHUTDOWN)
                self.assertFalse(server.get_doc(PID)['blocks'][0].get('translation'))
        finally:
            httpd.shutdown(); thread.join(timeout=2); httpd.server_close()

    def test_http_contract_health_reader_versions_and_model_refresh(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port_patch = patch.object(server, 'PORT', httpd.server_port)
        port_patch.start()
        base = 'http://127.0.0.1:' + str(httpd.server_port)
        def request(path, data=None, method=None):
            req = urllib.request.Request(base + path, json.dumps(data).encode() if data is not None else None,
                                         {'Content-Type': 'application/json'}, method=method)
            with urllib.request.urlopen(req) as response:
                return json.load(response)
        try:
            with patch.object(server, 'codex_status', side_effect=AssertionError('health must stay local')):
                health = request('/api/health')
            self.assertEqual(health['app_root'], str(ROOT))
            self.assertEqual(health['data_dir'], str(self.folder))
            with patch.object(codex_models, 'get', side_effect=AssertionError('不得查目录')):
                self.assertEqual(request('/api/models?refresh=1')['source'], 'local_settings')
            with patch.object(server, 'codex_status', side_effect=AssertionError('不得查登录')):
                self.assertFalse(request('/api/status?refresh=1')['configuration']['configured'])
                self.assertNotIn('codex', request('/api/status'))
            server.update_doc(PID, lambda doc: doc.update(read_state='read'))
            self.assertNotIn('read_state', request('/api/papers/' + PID)['paper'])
            paper = request('/api/papers/' + PID, {'reader_state': {'page': 2, 'original_offset': .6, 'mode': 'parallel'}}, 'PATCH')['paper']
            self.assertEqual(paper['read_page'], 2)
            self.assertEqual(paper['reader_state']['original_offset'], .6)
            self.assertNotIn('read_state', paper)
            self.assertEqual(server.get_doc(PID)['read_state'], 'read', 'Reading position must not alter legacy read flags.')
            request('/api/papers/' + PID + '/blocks/p1', {'translation': 'manual', 'note': 'kept'}, 'PATCH')
            self.assertTrue(server.get_doc(PID)['blocks'][0]['translation_manually_edited'])
            versions = request('/api/papers/' + PID + '/translation-versions')
            self.assertTrue(versions['versions'][0]['current'])
            self.assertNotIn('blocks', versions['versions'][0])
        finally:
            httpd.shutdown()
            thread.join(timeout=2)
            httpd.server_close()
            port_patch.stop()


if __name__ == '__main__':
    unittest.main()
