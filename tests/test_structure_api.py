"""结构校对的公开 API 快照、保留边界和取消；不联网、不调用 CLI。"""
import copy
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import codex_bridge
import codex_models
import paper_structure
import server
from ezread import api_settings

PID = 'a' * 16
CONFIG = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
          'model': 'deepseek-flash', 'max_output_tokens': 393216,
          'profile_id': 'deepseek_translation', 'thinking': 'disabled'}
KEY = 'synthetic-structure-api-key'


class StructureApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.pool = Mock()
        for name, value in (('DATA', self.data), ('LIBRARY', self.data / 'library'),
                            ('STRUCTURE_JOBS', queue.Queue()), ('STRUCTURE_QUEUED', set()),
                            ('SHUTDOWN', threading.Event()), ('AI_API', self.pool)):
            patched = patch.object(server, name, value, create=True)
            patched.start()
            self.addCleanup(patched.stop)
        for owner, name in ((codex_bridge, '_run_json'), (codex_models, 'resolve_config'),
                            (server, 'codex_status')):
            patched = patch.object(owner, name, side_effect=AssertionError('结构 API 不得调用 Codex'))
            patched.start()
            self.addCleanup(patched.stop)
        server.init_db()
        self.doc = {'id': PID, 'hash': 'synthetic-structure', 'title': 'Synthetic structure paper',
                    'deleted': False, 'collection': '手动合集', 'notes': '原笔记',
                    'translation_versions': [{'id': 'old-translation'}],
                    'blocks': [
                        {'id': 'title', 'text': 'Synthetic structure paper', 'kind': 'heading', 'page': 1,
                         'bbox': [.1, .1, .8, .2], 'translation': '人工译文'},
                        {'id': 'body', 'text': 'We measured the force.', 'kind': 'paragraph', 'page': 1,
                         'bbox': [.1, .3, .4, .5], 'note': '原批注'},
                        {'id': 'formula', 'text': 'F = ma', 'kind': 'paragraph', 'page': 1,
                         'bbox': [.1, .6, .4, .7], 'is_formula': True}]}
        self.doc['reading_structure'] = paper_structure.organize(self.doc)
        server.put_doc(self.doc)
        self.pool.request_json.side_effect = self.valid_response

    def configure(self, **overrides):
        api_settings.save_settings(self.data, {'purpose': 'translation', 'api_key': KEY, **overrides})
        self.assertEqual(api_settings.resolve_config(self.data, purpose='structure')['model'], 'deepseek-flash')

    def valid_response(self, channel, config, key, system, prompt, **kwargs):
        self.assertEqual(channel, 'structure')
        self.assertEqual(key, KEY)
        self.assertIn('语义分类输出契约', system)
        self.assertIn('"required": ["roles"]', system)
        data = json.loads(prompt)
        self.assertEqual(set(data), {'candidates'})
        self.assertNotIn('手动合集', prompt)
        self.assertNotIn('原笔记', prompt)
        self.assertIs(kwargs['cancel_event'], server.SHUTDOWN)
        return {'roles': [{'id': b['id'], 'role': b['local_role']} for b in data['candidates']]}

    def worker(self):
        server.STRUCTURE_JOBS.put(None)
        server.structure_worker()
        self.assertFalse(server.STRUCTURE_QUEUED)

    def test_success_freezes_public_configuration_and_preserves_latest_user_data(self):
        self.configure()
        server.schedule_structure(PID)
        server.schedule_structure(PID)
        self.assertEqual(server.STRUCTURE_JOBS.qsize(), 1)
        queued = server.get_doc(PID)['reading_structure']
        self.assertEqual(queued['requested_config'], CONFIG)
        self.assertNotIn(KEY, json.dumps(queued))
        self.configure(thinking='enabled', reasoning_effort='low')
        def change_notes(*args, **kwargs):
            server.update_doc(PID, lambda d: d.update(notes='校对期间新笔记', collection='新合集'))
            return self.valid_response(*args, **kwargs)
        self.pool.request_json.side_effect = change_notes
        self.worker()
        saved = server.get_doc(PID)
        structure = saved['reading_structure']
        self.assertEqual(structure['status'], 'ready')
        self.assertEqual(structure['model_config'], CONFIG)
        self.assertEqual(structure['requested_config'], CONFIG)
        self.assertTrue(structure['audit']['complete'])
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.assertEqual(saved['translation_versions'], self.doc['translation_versions'])
        self.assertEqual((saved['notes'], saved['collection']), ('校对期间新笔记', '新合集'))
        self.pool.request_json.assert_called_once()
        self.assertEqual(self.pool.request_json.call_args.args[1], CONFIG)
        server.schedule_structure(PID)
        self.assertTrue(server.STRUCTURE_JOBS.empty())
        self.assertNotIn(KEY, json.dumps(saved))

    def test_missing_key_keeps_rule_draft_and_does_not_block_import(self):
        server.schedule_structure(PID)
        saved = server.get_doc(PID)
        self.assertEqual(saved['reading_structure']['status'], 'needs_review')
        self.assertIn('API Key', saved['reading_structure']['error'])
        self.assertEqual(saved['reading_structure']['sections'], self.doc['reading_structure']['sections'])
        self.assertTrue(server.STRUCTURE_JOBS.empty())
        self.assertFalse(server.STRUCTURE_QUEUED)
        self.pool.request_json.assert_not_called()
        extracted = {'metadata': {'title': 'Another synthetic paper'}, 'blocks': self.doc['blocks'], 'pages': []}
        def extract(source, assets):
            (assets / 'original.pdf').write_bytes(source.read_bytes())
            (assets / 'page-001.jpg').write_bytes(b'synthetic page')
            return copy.deepcopy(extracted)
        with patch('pdf_tools.extract_document', side_effect=extract):
            imported = server.import_pdf(b'%PDF-1.7 synthetic-import', 'synthetic.pdf')
        raw = server.get_doc(imported['id'])
        self.assertEqual(raw['blocks'], self.doc['blocks'])
        self.assertEqual(raw['reading_structure']['status'], 'needs_review')
        self.assertEqual(raw['collection'], '')

    def test_restart_queued_or_running_task_keeps_frozen_config_after_settings_change(self):
        self.configure()
        server.schedule_structure(PID)
        pending = server.get_doc(PID)['reading_structure']
        self.configure(thinking='enabled', reasoning_effort='high')
        for status in ('queued', 'running'):
            with self.subTest(status=status), \
                    patch.object(server, 'STRUCTURE_JOBS', queue.Queue()), \
                    patch.object(server, 'STRUCTURE_QUEUED', set()):
                server.update_doc(PID, lambda d: d.update(reading_structure={**copy.deepcopy(pending), 'status': status}))
                server.schedule_structure(PID)
                self.assertEqual(server.get_doc(PID)['reading_structure']['requested_config'], CONFIG)
                self.worker()
                self.assertEqual(self.pool.request_json.call_args.args[1], CONFIG)
                self.assertEqual(server.get_doc(PID)['reading_structure']['status'], 'ready')

    def test_legacy_requested_model_migrates_to_api_instead_of_cli(self):
        self.configure()
        server.update_doc(PID, lambda d: d['reading_structure'].update(status='queued', requested_model='old-codex'))
        server.schedule_structure(PID)
        self.assertEqual(server.get_doc(PID)['reading_structure']['requested_config'], CONFIG)
        self.worker()
        self.assertEqual(server.get_doc(PID)['reading_structure']['status'], 'ready')
        self.assertEqual(self.pool.request_json.call_args.args[1], CONFIG)

    def test_legacy_explicit_snapshot_is_restored_without_rewriting_its_parameters(self):
        legacy = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                  'model': 'legacy-synthetic-model', 'max_output_tokens': 8192}
        api_settings.save_settings(self.data, {**legacy, 'api_key': KEY})
        server.update_doc(PID, lambda d: d['reading_structure'].update(status='queued', requested_config=legacy))
        server.schedule_structure(PID)
        self.assertEqual(server.get_doc(PID)['reading_structure']['requested_config'], legacy)
        self.worker()
        self.assertEqual(self.pool.request_json.call_args.args[1], legacy)
        self.assertEqual(server.get_doc(PID)['reading_structure']['status'], 'ready')

    def test_invalid_private_settings_preserve_local_structure_without_queue(self):
        (self.data / 'translation-api.private.json').write_text('invalid', encoding='utf-8')
        server.schedule_structure(PID)
        saved = server.get_doc(PID)
        self.assertEqual(saved['reading_structure']['status'], 'needs_review')
        self.assertIn('无法读取', saved['reading_structure']['error'])
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.assertTrue(server.STRUCTURE_JOBS.empty())

    def test_api_failure_preserves_initial_structure_and_all_user_fields(self):
        self.configure()
        server.schedule_structure(PID)
        self.pool.request_json.side_effect = ValueError('模拟 API 不可用')
        self.worker()
        saved = server.get_doc(PID)
        self.assertEqual(saved['reading_structure']['status'], 'needs_review')
        self.assertEqual(saved['reading_structure']['error'], '模拟 API 不可用')
        self.assertEqual(saved['reading_structure']['sections'], self.doc['reading_structure']['sections'])
        for name in ('blocks', 'notes', 'collection', 'translation_versions'):
            self.assertEqual(saved[name], self.doc[name])

    def test_cancelled_response_is_not_committed(self):
        self.configure()
        server.schedule_structure(PID)
        def cancel(*args, **kwargs):
            result = self.valid_response(*args, **kwargs)
            server.SHUTDOWN.set()
            return result
        self.pool.request_json.side_effect = cancel
        self.worker()
        structure = server.get_doc(PID)['reading_structure']
        self.assertEqual(structure['status'], 'needs_review')
        self.assertIn('取消', structure['error'])
        self.assertEqual(structure['method'], 'local')
        self.assertEqual(structure['sections'], self.doc['reading_structure']['sections'])

    def test_source_change_during_request_never_commits_stale_roles(self):
        self.configure()
        server.schedule_structure(PID)
        def mutate(*args, **kwargs):
            server.update_doc(PID, lambda d: d['blocks'][1].update(text='New source text.'))
            return self.valid_response(*args, **kwargs)
        self.pool.request_json.side_effect = mutate
        self.worker()
        saved = server.get_doc(PID)
        self.assertEqual(saved['blocks'][1]['text'], 'New source text.')
        self.assertEqual(saved['reading_structure']['status'], 'needs_review')
        self.assertIn('源文已变化', saved['reading_structure']['error'])
        self.assertEqual(saved['reading_structure']['method'], 'local')

    def test_role_boundary_rejections_keep_rule_structure(self):
        self.configure()
        valid = [{'id': bid, 'role': role} for bid, role in
                 (('title', 'title'), ('body', 'paragraph'), ('formula', 'equation'))]
        invalid = [None, {'roles': [None]}, {'roles': [{'id': [], 'role': 'paragraph'}]},
                   {'roles': valid[:-1]}, {'roles': [*valid, valid[0]]},
                   {'roles': [{**item, 'role': 'furniture'} if item['id'] == 'body' else item for item in valid]},
                   {'roles': [{**item, 'role': 'paragraph'} if item['id'] == 'formula' else item for item in valid]},
                   {'roles': [{**item, 'id': 'unknown'} if item['id'] == 'body' else item for item in valid]}]
        for response in invalid:
            with self.subTest(response=response):
                server.schedule_structure(PID)
                self.pool.request_json.side_effect = None
                self.pool.request_json.return_value = response
                self.worker()
                saved = server.get_doc(PID)
                self.assertEqual(saved['reading_structure']['status'], 'needs_review')
                self.assertEqual(saved['reading_structure']['sections'], self.doc['reading_structure']['sections'])
                self.assertEqual(saved['blocks'], self.doc['blocks'])

    def test_refine_without_explicit_runner_never_falls_back_to_cli(self):
        with self.assertRaisesRegex(ValueError, '显式提供 API'):
            paper_structure.refine(self.doc, CONFIG)


if __name__ == '__main__':
    unittest.main()
