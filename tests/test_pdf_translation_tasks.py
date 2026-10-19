"""版式任务使用隔离库、可读取的合成 PDF 与模拟模型/引擎。"""
import copy
import hashlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import codex_bridge
import paper_ai
import pdf_layout_engine
import pypdfium2 as pdfium
import server
from ezread import pdf_translations as layout
from ezread import api_settings, translation_api, pdf_layout_quality
from ezread import pdf_native_translation as native

ROOT = Path(__file__).resolve().parents[1]
PID = '0123456789abcdef'
LEGACY_CONFIG = {'model': 'test-model', 'reasoning_effort': 'low'}
CONFIG = {'provider': 'deepseek', 'base_url': 'http://127.0.0.1:1',
          'model': 'test-model', 'max_output_tokens': 8192}


def write_pdf(path, width=600, height=800):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pdfium.PdfDocument.new() as document:
        page = document.new_page(width=width, height=height)
        page.close()
        document.save(path)


def unit(text):
    return {'id': hashlib.sha256(text.encode()).hexdigest(), 'text': text, 'page': 1}


def validate_response(units, translations, *, reviews=None):
    inputs = [{'id': i, 'input': u['text']} for i, u in enumerate(units)]
    output = [{'id': i, 'output': translations[u['id']]} for i, u in enumerate(units) if u['id'] in translations]
    return native.response_values(inputs, True, json.dumps(output), reviews=reviews)


class PdfTranslationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix='pdf-task-tests-', dir=ROOT / 'work')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        replacements = {'DATA': self.folder, 'LIBRARY': self.folder / 'library', 'JOBS': queue.Queue(),
                        'CANCEL': {}, 'QUEUED': set(), 'RESUME_REQUESTED': set(),
                        'SHUTDOWN': threading.Event(), 'QUOTA_PAUSED': threading.Event()}
        for name, value in replacements.items():
            patched = patch.object(server, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch.object(pdf_layout_engine, 'availability', return_value={
            'available': True, 'engine': 'BabelDOC', 'version': '0.6.4', 'message': ''})
        patched.start()
        self.addCleanup(patched.stop)
        api_settings.save_settings(self.folder, {**CONFIG, 'api_key': 'synthetic-test-key'})
        server.init_db()
        self.original = server.LIBRARY / PID / 'original.pdf'
        write_pdf(self.original)
        self.doc = {'id': PID, 'hash': 'synthetic-pdf-layout', 'title': 'Synthetic layout task', 'deleted': False,
                    'pages': [{'number': 1, 'width': 600, 'height': 800}], 'figures': [], 'notes': '保留笔记',
                    'reader_state': {'page': 1}, 'text_annotations': [{'id': 'saved', 'ranges': []}],
                    'text_revision_history': [{'id': 'history'}],
                    'blocks': [{'id': 'old-source-id', 'page': 1, 'text': 'Original source 20.',
                                'translation': '人工译文', 'translation_manually_edited': True}],
                    'translation': {'status': 'completed', 'config': CONFIG},
                    'translation_versions': [{'id': 'old-version', 'blocks': {'old-source-id': {'translation': '旧译文'}}}],
                    'translation_drafts': [{'id': 'old-draft'}], 'read_page': 1}
        server.put_doc(self.doc)
        self.units = [unit('Text 20 with <b0>formula</b0>.'), unit('Second text [3].')]
        self.calls = []

    def request(self, units):
        return {'mode': 'llm', 'prompt': 'Translate each input faithfully.\n## Here is the input:\n\n' + json.dumps(
            [{'id': self.units.index(u), 'input': u['text']} for u in units]),
            'rate_limit_params': {'request_json_mode': True}}

    def engine(self, root, source, folder, **kwargs):
        self.calls.append('native')
        kwargs['on_progress']({'type': 'progress', 'stage': 'translate'})
        for batch in getattr(self, 'request_groups', [self.units]):
            request = self.request(batch)
            response = kwargs['translate'](request)
            native.response_values(json.loads(request['prompt'].rsplit('## Here is the input:\n\n', 1)[1]),
                                   True, response)
            self.assertEqual({x['id'] for x in json.loads(response)}, {x['id'] for x in json.loads(request['prompt'].split('## Here is the input:\n\n')[1])})
        write_pdf(folder / 'result.pdf')
        write_pdf(folder / 'bilingual.pdf', width=1200)
        return {'mono_pdf': 'result.pdf', 'dual_pdf': 'bilingual.pdf', 'page_count': 1,
                'quality': 'passed', 'warnings': []}

    def model(self, prompt, context, **kwargs):
        self.assertIsInstance(context, list)
        units = json.loads(prompt.split('## Here is the input:\n\n')[1])
        return json.dumps([{'id': u['id'], 'output': '模拟译文 ' + u['input']} for u in units])

    def run_queue(self, model=None, engine=None, quality=None):
        server.JOBS.put(None)
        transport = MagicMock()
        transport.__enter__.return_value = transport
        transport.translate.side_effect = model or self.model
        with patch.object(pdf_layout_engine, 'run_native', side_effect=engine or self.engine), \
             patch.object(pdf_layout_quality, 'inspect', return_value=quality or {
                 'status': 'passed', 'failures': [], 'warnings': []}), \
             patch.object(translation_api, 'ApiTranslationSession', return_value=transport) as session, \
             patch.object(paper_ai, 'PdfTranslationSession', side_effect=AssertionError('禁止 CLI')):
            server.worker()
        self.session_factory = session
        return transport.translate

    def start(self, **options):
        # 排版、缓存与错误边界均使用模拟 API，不访问外部服务。
        server.enqueue(PID, 'pdf_translate', {**CONFIG, **options})

    def test_upstream_success_clears_pending_format_rejection_for_completed_v3_task(self):
        def model(prompt, context, **kwargs):
            return '```json\n' + self.model(prompt, context, **kwargs) + '\n```'

        def completed_engine(root, source, folder, **kwargs):
            translate = kwargs['translate']
            def accepted(request):
                raw = translate(request)
                self.assertTrue(raw.startswith('```json\n'))
                self.assertGreater(server.get_doc(PID)[layout.TASK]['rejected'], 0)
                return raw.removeprefix('```json\n').removesuffix('\n```')
            return self.engine(root, source, folder, **{**kwargs, 'translate': accepted})

        self.start()
        self.run_queue(model=model, engine=completed_engine)
        task = server.get_doc(PID)[layout.TASK]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['protocol'], layout.NATIVE_PROTOCOL)
        self.assertEqual(task['rejected'], 0)

    def test_completed_pdf_keeps_source_translation_annotations_and_reading_state(self):
        source_hash = layout.file_hash(self.original)
        self.start()
        call = self.run_queue()
        saved = server.get_doc(PID)
        for key in self.doc:
            self.assertEqual(saved[key], self.doc[key], key)
        self.assertEqual(layout.file_hash(self.original), source_hash)
        self.assertEqual(self.calls, ['native'])
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        self.assertEqual(saved[layout.CURRENT]['page_count'], 1)
        self.assertEqual(call.call_args.args[1], [])
        timing = layout._read_json(layout.run_folder(server, PID, saved[layout.TASK]['run_id']) /
                                   'translation-timing.json', {})
        self.assertEqual(timing['model_calls'], 1)
        self.assertGreaterEqual(timing['engine_seconds'], timing['model_roundtrip_seconds'])
        public = layout.public_state(server, saved, full=True)
        self.assertEqual(public['current']['quality'], 'passed')
        self.assertEqual((public['received'], public['done'], public['review_pending'], public['model_calls']), (2, 2, 0, 1))
        self.assertIn('/pdf-layout/', public['current']['pages'][0]['image_url'])
        self.assertNotIn('thread_id', public)
        self.assertNotIn('source_hash', public['current'])

    def test_new_pdf_session_receives_application_usage_collector(self):
        collector = object()
        with patch.object(server, 'API_USAGE', collector, create=True):
            self.start()
            self.run_queue()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'completed')
        task = server.get_doc(PID)[layout.TASK]
        self.session_factory.assert_called_once_with({**CONFIG, 'thinking': 'disabled'}, 'synthetic-test-key',
            usage=collector, diagnostics_dir=layout.run_folder(server, PID, task['run_id']) / 'api-diagnostics')

    def working_files(self, folder):
        working = folder / 'native/working/original'
        working.mkdir(parents=True, exist_ok=True)
        names = ('input.pdf', 'mig_toc_temp.pdf', 'temp_subset_input_mono.pdf', 'temp_subset_output_mono.pdf',
                 'temp_save_input_dual.pdf', 'temp_save_output_dual.pdf', 'translate_tracking.json', 'unknown.pdf')
        for name in names:
            (working / name).write_bytes(name.encode())
        (folder / 'native-input').mkdir(exist_ok=True)
        (folder / 'native-input/original.pdf').write_bytes(b'normalized working input')
        (folder / 'rotation-normalization.json').write_text('{}', encoding='utf-8')
        return working

    def test_success_cleans_only_known_intermediate_pdfs_and_keeps_evidence(self):
        def engine(root, source, folder, **kwargs):
            self.working_files(folder)
            (folder / 'native-text-audit.json').write_text('{}', encoding='utf-8')
            return self.engine(root, source, folder, **kwargs)
        self.start()
        self.run_queue(engine=engine, quality={'status': 'needs_review', 'failures': [], 'warnings': []})
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        folder = layout.run_folder(server, PID, saved[layout.CURRENT]['id'])
        self.assertEqual(sorted(p.name for p in (folder / 'native/working/original').iterdir()),
                         ['translate_tracking.json', 'unknown.pdf'])
        self.assertTrue((folder / 'native-replies').is_dir())
        self.assertFalse((folder / 'native-input/original.pdf').exists())
        for name in ('result.pdf', 'bilingual.pdf', 'artifact.json', 'native-session.json',
                     'native-text-audit.json', 'body-quality.json', 'api-usage.json', 'rotation-normalization.json'):
            self.assertTrue((folder / name).is_file(), name)
        self.assertTrue(self.original.is_file())
        self.assertEqual(layout.cleanup_completed_run(server, PID, saved[layout.CURRENT]['id']),
                         {'removed_files': 0, 'removed_bytes': 0})

    def test_failed_cancelled_and_uncommitted_tasks_keep_working_pdfs(self):
        for mode in ('failed', 'cancelled', 'uncommitted'):
            with self.subTest(mode=mode):
                self.start(retranslate=True)
                run_id = server.get_doc(PID)[layout.TASK]['run_id']
                folder = layout.run_folder(server, PID, run_id)
                def engine(root, source, folder, **kwargs):
                    self.working_files(folder)
                    if mode == 'failed':
                        raise ValueError('模拟排版中断')
                    result = self.engine(root, source, folder, **kwargs)
                    if mode == 'cancelled':
                        kwargs['cancel_event'].set()
                    else:
                        server.update_doc(PID, lambda d: d['blocks'][0].update(text_override='用户新校订'))
                    return result
                self.run_queue(engine=engine)
                self.assertIn(server.get_doc(PID)[layout.TASK]['status'], ('error', 'paused'))
                self.assertTrue((folder / 'native/working/original/input.pdf').is_file())
                self.assertTrue((folder / 'native-input/original.pdf').is_file())
                with self.assertRaisesRegex(ValueError, '已成功提交'):
                    layout.cleanup_completed_run(server, PID, run_id)
                server.update_doc(PID, lambda d: d['blocks'][0].pop('text_override', None))

    def test_cleanup_keeps_referenced_output_even_if_it_has_a_temporary_name(self):
        def engine(root, source, folder, **kwargs):
            working = self.working_files(folder)
            result = self.engine(root, source, folder, **kwargs)
            (working / 'input.pdf').write_bytes((folder / result['mono_pdf']).read_bytes())
            result['mono_pdf'] = 'native/working/original/input.pdf'
            return result
        self.start()
        self.run_queue(engine=engine)
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        folder = layout.run_folder(server, PID, saved[layout.CURRENT]['id'])
        self.assertTrue((folder / saved[layout.CURRENT]['mono']).is_file())
        self.assertFalse((folder / 'native/working/original/mig_toc_temp.pdf').exists())

    def test_cleanup_verifies_source_outputs_and_links_before_deleting(self):
        self.start()
        self.run_queue()
        saved = server.get_doc(PID)
        run_id = saved[layout.CURRENT]['id']
        folder = layout.run_folder(server, PID, run_id)
        working = self.working_files(folder)
        for path in (self.original, folder / saved[layout.CURRENT]['mono'], folder / saved[layout.CURRENT]['dual']):
            content = path.read_bytes()
            path.write_bytes(content + b'changed')
            with self.assertRaises(ValueError):
                layout.cleanup_completed_run(server, PID, run_id)
            self.assertTrue((working / 'input.pdf').is_file())
            path.write_bytes(content)
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == working):
            with self.assertRaisesRegex(ValueError, '包含链接'):
                layout.cleanup_completed_run(server, PID, run_id)
        self.assertTrue((working / 'input.pdf').is_file())

    def test_cleanup_io_failure_does_not_turn_published_translation_into_error(self):
        def engine(root, source, folder, **kwargs):
            self.working_files(folder)
            return self.engine(root, source, folder, **kwargs)
        unlink = Path.unlink
        def blocked(path, *args, **kwargs):
            if path.name == 'input.pdf':
                raise PermissionError('模拟文件占用')
            return unlink(path, *args, **kwargs)
        self.start()
        with patch.object(Path, 'unlink', autospec=True, side_effect=blocked):
            self.run_queue(engine=engine)
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        folder = layout.run_folder(server, PID, saved[layout.CURRENT]['id'])
        self.assertTrue((folder / 'native/working/original/input.pdf').is_file())
        self.assertEqual(layout.cleanup_completed_run(server, PID, saved[layout.CURRENT]['id'])['removed_files'], 7)

    def test_uncertain_quantity_publishes_as_needs_review_and_reuses_response(self):
        self.units = [unit('90 MT')]
        self.start()
        called = self.run_queue(model=lambda *args, **kwargs: '[{"id":0,"output":"9000万吨"}]')
        called.assert_called_once()
        saved = server.get_doc(PID)
        public = layout.public_state(server, saved)
        self.assertEqual(public['status'], 'completed')
        self.assertEqual((public['received'], public['done'], public['review_pending'], public['rejected']), (1, 1, 1, 0))
        self.assertEqual(public['current']['quality'], 'needs_review')
        self.assertTrue(public['current']['warnings'])
        self.start()
        self.run_queue().assert_not_called()
        public = layout.public_state(server, server.get_doc(PID))
        self.assertEqual((public['reused'], public['review_pending'], public['model_calls']), (1, 1, 0))

    def test_completed_and_paused_published_tasks_continue_in_new_directory(self):
        self.start()
        self.run_queue()
        for status in ('completed', 'paused'):
            with self.subTest(status=status):
                before = server.get_doc(PID)
                artifact = copy.deepcopy(before[layout.CURRENT])
                folder = layout.run_folder(server, PID, artifact['id'])
                original = {name: (folder / artifact[name]).read_bytes() for name in ('mono', 'dual')}
                server.update_doc(PID, lambda d: d[layout.TASK].update(status=status))
                self.start()
                new_task = server.get_doc(PID)[layout.TASK]
                self.assertNotEqual(new_task['run_id'], artifact['id'])
                self.assertEqual(new_task['reuse_from']['run_id'], artifact['id'])
                called = self.run_queue()
                called.assert_not_called()
                saved = server.get_doc(PID)
                self.assertEqual(saved[layout.TASK]['status'], 'completed')
                self.assertIn(artifact, saved[layout.VERSIONS])
                self.assertEqual(saved[layout.CURRENT]['id'], new_task['run_id'])
                for name, content in original.items():
                    self.assertEqual((folder / artifact[name]).read_bytes(), content)

    def test_restored_version_continue_does_not_write_current_or_historical_directory(self):
        self.start()
        self.run_queue()
        first = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        self.start(retranslate=True)
        self.run_queue()
        second = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        server.restore_pdf_translation(PID, first['id'])
        server.update_doc(PID, lambda d: d[layout.TASK].update(status='error'))
        originals = {(v['id'], name): (layout.run_folder(server, PID, v['id']) / v[name]).read_bytes()
                     for v in (first, second) for name in ('mono', 'dual')}
        self.start()
        task = server.get_doc(PID)[layout.TASK]
        self.assertNotIn(task['run_id'], (first['id'], second['id']))
        self.assertEqual(task['reuse_from']['run_id'], second['id'])
        call = self.run_queue()
        call.assert_not_called()
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        for v in (first, second):
            for name in ('mono', 'dual'):
                self.assertEqual((layout.run_folder(server, PID, v['id']) / v[name]).read_bytes(), originals[v['id'], name])

    def test_failed_continue_preserves_published_pdf_bytes(self):
        self.start()
        self.run_queue()
        before = server.get_doc(PID)
        artifact = copy.deepcopy(before[layout.CURRENT])
        folder = layout.run_folder(server, PID, artifact['id'])
        original = {name: (folder / artifact[name]).read_bytes() for name in ('mono', 'dual')}
        def invalid(*args, **kwargs):
            result = self.engine(*args, **kwargs)
            write_pdf(args[2] / result['mono_pdf'], width=500)
            return result
        self.start()
        call = self.run_queue(engine=invalid)
        call.assert_not_called()
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'error')
        self.assertEqual(saved[layout.CURRENT], artifact)
        for name, content in original.items():
            self.assertEqual((folder / artifact[name]).read_bytes(), content)
        self.assertEqual(saved['blocks'], before['blocks'])
        self.assertEqual(saved['notes'], before['notes'])

    def test_execute_rejects_any_published_run_before_writing_or_starting_engine(self):
        self.start()
        self.run_queue()
        first = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        self.start(retranslate=True)
        self.run_queue()
        saved = server.get_doc(PID)
        second = copy.deepcopy(saved[layout.CURRENT])
        for artifact in (first, second):
            with self.subTest(run_id=artifact['id']):
                server.update_doc(PID, lambda d: d[layout.TASK].update(run_id=artifact['id'], status='queued'))
                with patch.object(pdf_layout_engine, 'run_native') as engine, \
                        patch.object(layout, '_write_json') as write, \
                        self.assertRaisesRegex(ValueError, '目录已发布'):
                    server.execute_pdf_translation(PID, threading.Event())
                engine.assert_not_called()
                write.assert_not_called()
                self.assertEqual(server.get_doc(PID)[layout.CURRENT], second)

    def test_geometry_warnings_publish_and_preserve_history_annotations_and_evidence(self):
        self.start()
        self.run_queue()
        previous = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        previous_folder = layout.run_folder(server, PID, previous['id'])
        original = (previous_folder / previous['mono']).read_bytes()
        self.start(retranslate=True)
        quality = {'status': 'failed', 'failures': [
            {'page': 1, 'kind': 'overlapping_body_math_or_latin'},
            {'page': 1, 'kind': 'small_body_font'}], 'warnings': []}
        called = self.run_queue(quality=quality)
        saved = server.get_doc(PID)
        called.assert_called_once()
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        self.assertEqual(saved[layout.CURRENT]['quality'], 'needs_review')
        self.assertTrue(saved[layout.CURRENT]['warnings'])
        self.assertIn(previous, saved[layout.VERSIONS])
        self.assertEqual((previous_folder / previous['mono']).read_bytes(), original)
        folder = layout.run_folder(server, PID, saved[layout.TASK]['run_id'])
        self.assertEqual(layout._read_json(folder / 'body-quality.json', {}), quality)
        self.assertTrue((folder / 'artifact.json').is_file())
        for key in self.doc:
            self.assertEqual(saved[key], self.doc[key], key)

    def test_changed_markers_or_missing_units_reject_publication(self):
        for changed in ('文本 20 <b0>公式</b1>.', '文本 20 公式.', ''):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_response([self.units[0]], {self.units[0]['id']: changed})
        with self.assertRaises(ValueError):
            validate_response(self.units, {self.units[0]['id']: self.units[0]['text']})
        self.start()
        self.run_queue(model=lambda *args, **kwargs: '[]')
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'error')
        self.assertNotIn(layout.CURRENT, server.get_doc(PID))

    def test_natural_chinese_word_order_moves_numbers_and_changed_counts_need_review(self):
        source = unit('The Al7075 material takes 8.4 h using 480 processors.')
        validate_response([source], {source['id']: '使用 480 个处理器时，Al7075 材料耗时 8.4 h。'})
        for changed in ('使用 480 个处理器时，Al7075 材料耗时 8.5 h。',
                        '使用 480 个处理器时，Al7075 材料耗时 8.4 h，重复 480。'):
            self.assert_numeric_review(source, changed)

    def test_written_quantity_and_month_name_are_not_added_numbers(self):
        source = unit('Split into 8 subsets, train on 7 folds and validate on the remaining one.')
        validate_response([source], {source['id']: '分成 8 个子集，使用 7 折训练，剩余 1 折验证。'})
        self.assert_numeric_review(source, '分成 8 个子集，使用 7 折训练，剩余 2 折验证。')
        dates = unit('Received 12 January 2026; revised 22 March 2026; accepted 19 April 2026.')
        validate_response([dates], {dates['id']: '2026 年 1 月 12 日收到；2026 年 3 月 22 日修订；2026 年 4 月 19 日接受。'})
        self.assert_numeric_review(dates, '2026 年 2 月 12 日收到；2026 年 3 月 22 日修订；2026 年 4 月 19 日接受。')

    def test_number_format_and_citation_spacing_keep_the_same_values(self):
        pairs = [('Load 12,000 cycles [18, 19].', '加载 12000 周次 [18,19]。'),
                 ('A coefficient of .5 and −2.5e−3.', '系数 0.5 和 -0.0025。'),
                 ('See Fig.1 and Fig.2 for details.', '详见图1和图2。'),
                 ('twenty-one specimens in May 2026.', '2026 年 5 月的 21 个试样。')]
        for source, translated in pairs:
            with self.subTest(source=source):
                item = unit(source)
                validate_response([item], {item['id']: translated})
        for source, translated in [('Values −5 and 7.', '数值 5 和 7。'),
                                   ('Reference [18,19].', '引文 [18,20]。'),
                                   ('one material with 20 samples.', '一种材料有 21 个样品。')]:
            with self.subTest(source=source):
                self.assert_numeric_review(unit(source), translated)

    def assert_numeric_review(self, source, translated):
        self.assertTrue(layout.translation_numbers.assess_numbers(source['text'], translated)['error'])
        assessment = layout.assess_translation(source['text'], translated)
        self.assertEqual(assessment['error'], '')
        self.assertTrue(assessment['warnings'])
        reviews = []
        validate_response([source], {source['id']: translated}, reviews=reviews)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['translation'], translated)

    def test_all_detected_numeric_categories_publish_for_review_and_reuse_without_model(self):
        pairs = [('Values 20 and 3.', '数值 21 和 3。'),
                 ('Reference [18,19].', '引文 [18,20]。'),
                 ('Alloy AA6061.', '合金 AA6063。'),
                 ('Length 20 mm.', '长度 21 mm。'),
                 ('Length 20 mm and 90 MT.', '长度 21 mm 和 90 MT。')]
        self.units = [unit(source) for source, _ in pairs]
        for source, translated in pairs:
            with self.subTest(source=source):
                self.assert_numeric_review(unit(source), translated)
        def changed(*args, **kwargs):
            return json.dumps([{'id': i, 'output': translated}
                                       for i, (_, translated) in enumerate(pairs)])
        self.start()
        self.run_queue(model=changed).assert_called_once()
        saved = server.get_doc(PID)
        public = layout.public_state(server, saved)
        self.assertEqual(public['status'], 'completed')
        self.assertEqual(public['current']['quality'], 'needs_review')
        self.assertEqual((public['received'], public['done'], public['review_pending'], public['rejected']), (5, 5, 5, 0))
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.start()
        self.run_queue().assert_not_called()
        public = layout.public_state(server, server.get_doc(PID))
        self.assertEqual((public['reused'], public['review_pending'], public['model_calls']), (5, 5, 0))

    def test_changed_formula_marker_is_rejected_before_rendering(self):
        with self.assertRaisesRegex(ValueError, '占位符'):
            validate_response(self.units, {self.units[0]['id']: '数值 20 <b0>公式</b8>',
                                           self.units[1]['id']: '第二段 [3]。'})
        validate_response(self.units, {self.units[0]['id']: '数值 20 <b0>公式</b0>',
                                       self.units[1]['id']: '第二段 [3]。'})

    def test_marker_reordering_passes_but_missing_added_and_repeated_markers_fail(self):
        source = unit('First <b0>20</b0> then {v1} and <b2>3</b2>.')
        reordered = '<b2>3</b2> 与 {v1}，随后 <b0>20</b0>。'
        validate_response([source], {source['id']: reordered})
        for changed in (reordered.replace('{v1}', ''), reordered + ' {v9}', reordered + ' {v1}'):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, '占位符'):
                validate_response([source], {source['id']: changed})
        with self.assertRaisesRegex(ValueError, '占位符'):
            validate_response([source], {source['id']: '【原文截断】20 和 3。'})

    def test_model_diagnostic_notes_are_accepted_for_review(self):
        source = unit('Material 20 retains its strength.')
        for value in ('材料 20 保留……【原文截断】', '材料 20【原文识别残缺；无法可靠翻译完整片段】',
                      '材料 20。[原文为句子片段]', '材料 20【无法可靠翻译：damaged text】'):
            with self.subTest(value=value):
                self.assertEqual(layout.translation_issue(source['text'], value), '')
                assessment = layout.assess_translation(source['text'], value)
                self.assertTrue(any('原文残缺' in warning for warning in assessment['warnings']))
                reviews = []
                validate_response([source], {source['id']: value}, reviews=reviews)
                self.assertEqual(len(reviews), 1)
        literal = unit('The label 【原文截断】 describes a failure with 20 words.')
        validate_response([literal], {literal['id']: '标签【原文截断】描述了包含 20 个词的失败。'})

    def test_diagnostic_reply_publishes_whole_pdf_for_review(self):
        def partial(*args, **kwargs):
            response = self.model(*args, **kwargs)
            values = json.loads(response)
            values[1]['output'] = '第二段 [3]……【原文截断】'
            return json.dumps(values)
        self.start()
        self.run_queue(model=partial)
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        self.assertEqual(saved[layout.CURRENT]['quality'], 'needs_review')
        self.assertEqual(saved[layout.TASK]['review_pending'], 1)
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.assertTrue(any('原文残缺' in warning for warning in saved[layout.CURRENT]['warnings']))

    def test_delivered_reply_is_reused_after_pause_without_another_model_call(self):
        self.start()
        event = server.CANCEL[(PID, 'pdf_translate')]
        def pause(*args, **kwargs):
            result = self.model(*args, **kwargs)
            event.set()
            return result
        first = self.run_queue(model=pause)
        self.assertEqual(first.call_count, 1)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'paused')
        self.start()
        second = self.run_queue()
        second.assert_not_called()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['done'], len(self.units))

    def test_session_with_wrong_model_snapshot_is_never_reused(self):
        self.start()
        def invalid(*args, **kwargs):
            result = self.engine(*args, **kwargs)
            write_pdf(args[2] / result['mono_pdf'], width=500)
            return result
        self.run_queue(engine=invalid)
        task = server.get_doc(PID)[layout.TASK]
        folder = layout.run_folder(server, PID, task['run_id'])
        identity = layout._read_json(folder / 'native-session.json', {})
        identity['config'] = {**CONFIG, 'model': 'other-model'}
        layout._write_json(folder / 'native-session.json', identity)
        self.start()
        call = self.run_queue()
        call.assert_not_called()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'error')
        self.assertNotIn(layout.CURRENT, server.get_doc(PID))

    def test_paused_requests_resume_only_unsaved_prompts_with_frozen_configuration(self):
        self.start()
        self.request_groups = [[u] for u in self.units]
        event = server.CANCEL[(PID, 'pdf_translate')]
        count = 0
        def pause(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                event.set()
                raise codex_bridge.TranslationError('暂停', code='cancelled')
            return self.model(*args, **kwargs)
        self.run_queue(model=pause)
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'paused')
        self.assertEqual(saved[layout.TASK]['done'], 1)
        run_id = saved[layout.TASK]['run_id']
        server.init_db()
        self.start()
        call = self.run_queue()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], self.request([self.units[1]])['prompt'])
        self.assertEqual(call.call_args.args[1], [])
        self.assertEqual(server.get_doc(PID)[layout.CURRENT]['id'], run_id)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['reused'], 1)

    def test_retranslation_failure_keeps_readable_current_then_version_can_restore(self):
        self.start()
        self.run_queue()
        first = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        self.start(retranslate=True)
        self.run_queue(model=lambda *args, **kwargs: '[]')
        self.assertEqual(server.get_doc(PID)[layout.CURRENT], first)
        self.start(retranslate=True)
        self.run_queue()
        second = server.get_doc(PID)[layout.CURRENT]
        self.assertNotEqual(first['id'], second['id'])
        server.restore_pdf_translation(PID, first['id'])
        self.assertEqual(server.get_doc(PID)[layout.CURRENT], first)
        self.assertEqual(server.get_doc(PID)['blocks'], self.doc['blocks'])

    def test_source_changes_or_english_edits_block_stale_pdf_commit(self):
        self.start()
        def edit(*args, **kwargs):
            server.update_doc(PID, lambda d: d['blocks'][0].update(text_override='User corrected text 20.'))
            return self.model(*args, **kwargs)
        self.run_queue(model=edit)
        self.assertNotIn(layout.CURRENT, server.get_doc(PID))
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'error')
        with self.assertRaisesRegex(ValueError, '手动修订'):
            self.start(retranslate=True)

    def test_published_pdf_warns_if_the_english_text_is_later_corrected(self):
        self.start()
        self.run_queue()
        before = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        server.update_doc(PID, lambda d: d['blocks'][0].update(text_override='Corrected English 20.'))
        saved = server.get_doc(PID)
        visible = layout.public_state(server, saved, full=True)
        self.assertEqual(visible['current']['quality'], 'needs_review')
        self.assertTrue(any('未包含修订' in x for x in visible['current']['warnings']))
        self.assertEqual(saved[layout.CURRENT], before)

    def test_source_and_engine_configuration_cannot_mix_on_resume(self):
        self.start()
        server.QUEUED.clear()
        before = server.get_doc(PID)
        with self.assertRaisesRegex(ValueError, '固定模型'):
            self.start(model='other-model')
        self.assertEqual(server.get_doc(PID), before)
        write_pdf(self.original, width=601)
        with self.assertRaisesRegex(ValueError, '原 PDF'):
            self.start()

    def test_engine_update_creates_new_session_and_preserves_prior_artifact_and_replies(self):
        available = {'available': True, 'version': '0.6.4', 'fingerprint': 'old'}
        with patch.object(pdf_layout_engine, 'availability', return_value=available):
            self.start()
            self.run_queue()
        before = server.get_doc(PID)
        artifact = copy.deepcopy(before[layout.CURRENT])
        old_folder = layout.run_folder(server, PID, artifact['id'])
        old_reply = next((old_folder / 'native-replies').glob('*.json'))
        old_hash = layout.file_hash(old_reply)
        self.units.append(unit('New body text 5.'))
        with patch.object(pdf_layout_engine, 'availability', return_value={**available, 'fingerprint': 'new'}):
            self.start()
            self.assertNotEqual(server.get_doc(PID)[layout.TASK]['run_id'], artifact['id'])
            self.assertEqual(server.get_doc(PID)[layout.CURRENT], artifact)
            call = self.run_queue()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0], self.request(self.units)['prompt'])
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['total'], 3)
        self.assertEqual(saved[layout.TASK]['config'], before[layout.TASK]['config'])
        self.assertEqual(layout.file_hash(old_reply), old_hash)
        self.assertIn(artifact, saved[layout.VERSIONS])
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.assertEqual(saved['text_annotations'], self.doc['text_annotations'])

    def test_local_engine_upgrade_reuses_unchanged_native_prompts_without_model(self):
        available = {'available': True, 'version': '0.6.4', 'fingerprint': 'old'}
        with patch.object(pdf_layout_engine, 'availability', return_value=available):
            self.start()
            self.run_queue()
        before = server.get_doc(PID)
        old = copy.deepcopy(before[layout.CURRENT])
        folder = layout.run_folder(server, PID, old['id'])
        reply = next((folder / 'native-replies').glob('*.json'))
        original = reply.read_bytes()
        with patch.object(pdf_layout_engine, 'availability', return_value={**available, 'fingerprint': 'new'}):
            self.start()
            new_id = server.get_doc(PID)[layout.TASK]['run_id']
            self.assertNotEqual(new_id, old['id'])
            call = self.run_queue()
        call.assert_not_called()
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.TASK]['status'], 'completed')
        self.assertEqual(saved[layout.TASK]['reused'], len(self.units))
        self.assertEqual(saved[layout.TASK]['model_calls'], 0)
        self.assertEqual(saved[layout.CURRENT]['id'], new_id)
        self.assertIn(old, saved[layout.VERSIONS])
        self.assertEqual(reply.read_bytes(), original)
        self.assertEqual((layout.run_folder(server, PID, new_id) / 'native-replies' / reply.name).read_bytes(), original)
        self.assertEqual(saved['blocks'], before['blocks'])

    def test_changed_source_during_model_call_cannot_replace_current_pdf(self):
        self.start()
        self.run_queue()
        previous = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        self.start(retranslate=True)
        def changed(*args, **kwargs):
            result = self.model(*args, **kwargs)
            with self.original.open('ab') as stream:
                stream.write(b'\n% changed source fixture\n')
            return result
        self.run_queue(model=changed)
        saved = server.get_doc(PID)
        self.assertEqual(saved[layout.CURRENT], previous)
        self.assertEqual(saved[layout.TASK]['status'], 'error')
        self.assertIn('文献或任务已变化', saved[layout.TASK]['error'])
        self.assertEqual(saved['blocks'], self.doc['blocks'])

    def test_deleted_paper_during_model_call_keeps_existing_pdf_and_notes(self):
        self.start()
        self.run_queue()
        previous = copy.deepcopy(server.get_doc(PID)[layout.CURRENT])
        self.start(retranslate=True)
        def deleted(*args, **kwargs):
            result = self.model(*args, **kwargs)
            server.update_doc(PID, lambda d: d.update(deleted=True))
            return result
        self.run_queue(model=deleted)
        saved = server.get_doc(PID, include_deleted=True)
        self.assertEqual(saved[layout.CURRENT], previous)
        self.assertEqual(saved['blocks'], self.doc['blocks'])
        self.assertEqual(saved['notes'], self.doc['notes'])
        self.assertTrue(saved['deleted'])

    def test_api_upgrade_ignores_legacy_unit_manifest_and_reuses_verified_replies(self):
        self.start()
        self.run_queue()
        saved = server.get_doc(PID)
        artifact = copy.deepcopy(saved[layout.CURRENT])
        task = saved[layout.TASK]
        folder = layout.run_folder(server, PID, artifact['id'])
        manifest = {k: task.get(k) for k in ('source_hash', 'config', 'engine_version', 'engine_fingerprint')}
        manifest.update(config={**CONFIG, 'model': 'wrong-model'}, units=self.units)
        layout._write_json(folder / 'source-units.json', manifest)
        with patch.object(pdf_layout_engine, 'availability', return_value={
                'available': True, 'version': '0.6.4', 'fingerprint': 'new'}):
            self.start()
            call = self.run_queue()
        call.assert_not_called()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'completed')
        self.assertIn(artifact, server.get_doc(PID)[layout.VERSIONS])

    def test_abandoned_draft_can_be_restored_without_starting_a_model_call(self):
        self.start()
        first = copy.deepcopy(server.get_doc(PID)[layout.TASK])
        server.QUEUED.clear()
        server.update_doc(PID, lambda d: d[layout.TASK].update(status='paused', done=1))
        self.start(retranslate=True)
        second = server.get_doc(PID)[layout.TASK]['run_id']
        server.QUEUED.clear()
        self.assertNotEqual(first['run_id'], second)
        with patch.object(paper_ai, 'translate_pdf_request') as model:
            server.restore_pdf_translation_draft(PID, first['run_id'])
        model.assert_not_called()
        doc = server.get_doc(PID)
        self.assertEqual(doc[layout.TASK]['run_id'], first['run_id'])
        self.assertEqual(doc[layout.TASK]['status'], 'paused')
        self.assertEqual(doc[layout.TASK]['done'], 1)
        self.assertEqual(doc['translation'], self.doc['translation'])
        public = server.public_doc(doc)
        self.assertNotIn(layout.DRAFTS, public)
        self.assertTrue(any(t['id'] == second for t in public[layout.TASK]['drafts']))

    def test_wrong_page_geometry_is_not_published(self):
        def invalid(*args, **kwargs):
            result = self.engine(*args, **kwargs)
            write_pdf(args[2] / result['mono_pdf'], width=500)
            return result
        self.start()
        self.run_queue(engine=invalid)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'error')
        self.assertNotIn(layout.CURRENT, server.get_doc(PID))

    def test_page_geometry_rounding_does_not_reject_valid_pdf(self):
        def rounded(*args, **kwargs):
            result = self.engine(*args, **kwargs)
            write_pdf(args[2] / result['mono_pdf'], width=600.005)
            return result
        self.start()
        self.run_queue(engine=rounded)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'completed')

    def test_queue_deduplicates_and_retranslation_waits_for_current_worker(self):
        self.start()
        original_id = server.get_doc(PID)[layout.TASK]['run_id']
        self.start()
        self.assertEqual(server.JOBS.qsize(), 1)
        with self.assertRaisesRegex(ValueError, '等待任务停止'):
            self.start(retranslate=True)
        self.assertEqual(server.get_doc(PID)[layout.TASK]['run_id'], original_id)

    def test_shutdown_and_startup_pause_pdf_tasks_without_losing_current(self):
        self.start()
        event = server.CANCEL[(PID, 'pdf_translate')]
        server.prepare_shutdown({'instance_id': server.INSTANCE_ID})
        self.assertTrue(event.is_set())
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'paused')
        server.SHUTDOWN.clear()
        server.update_doc(PID, lambda d: d[layout.TASK].update(status='running'))
        server.init_db()
        self.assertEqual(server.get_doc(PID)[layout.TASK]['status'], 'paused')
        self.assertEqual(server.get_doc(PID)['blocks'], self.doc['blocks'])

    def test_pdf_media_does_not_expose_units_or_unpublished_outputs(self):
        self.start()
        run_id = server.get_doc(PID)[layout.TASK]['run_id']
        with self.assertRaises(ValueError):
            layout.media_file(server, PID, f'pdf-layout/{run_id}/mono.pdf')
        self.run_queue()
        self.assertEqual(layout.media_file(server, PID, f'pdf-layout/{run_id}/mono.pdf').name, 'result.pdf')
        self.assertTrue(layout.media_file(server, PID, f'pdf-layout/{run_id}/page-001.jpg').is_file())
        for relative in (f'pdf-layout/{run_id}/translated-units.json', f'pdf-layout/{run_id}/page-002.jpg',
                         f'pdf-layout/{run_id}/last-model-reply.json', f'pdf-layout/{run_id}/validation-review.json',
                         f'pdf-layout/{run_id}/../original.pdf'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                layout.media_file(server, PID, relative)

    def test_page_symlink_cannot_expose_an_outside_file(self):
        self.start()
        self.run_queue()
        artifact = server.get_doc(PID)[layout.CURRENT]
        folder = layout.run_folder(server, PID, artifact['id'])
        outside = self.folder / 'private-fixture.txt'
        outside.write_text('private fixture', encoding='utf-8')
        image = folder / 'page-001.jpg'
        try:
            image.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f'本机未启用创建符号链接权限：{exc.__class__.__name__}')
        with self.assertRaisesRegex(ValueError, '页面路径'):
            layout.media_file(server, PID, f"pdf-layout/{artifact['id']}/page-001.jpg")

    def test_http_requires_confirmation_and_safe_local_origin_without_login(self):
        listener = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        base = f'http://127.0.0.1:{listener.server_port}'
        def request(path, body=None, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)
        with patch.object(server, 'PORT', listener.server_port), \
             patch.object(server, 'codex_status', return_value={'authenticated': False}):
            self.assertTrue(request('/api/pdf-layout')['available'])
            self.assertEqual(server.JOBS.qsize(), 0)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request(f'/api/papers/{PID}/pdf-translate', CONFIG, origin='https://remote.invalid')
            self.assertEqual(error.exception.code, 403)
        with patch.object(server, 'PORT', listener.server_port), \
             patch.object(server, 'codex_status', side_effect=AssertionError('API 不检查 Codex 登录')):
            response = request(f'/api/papers/{PID}/pdf-translate', CONFIG)
            self.assertEqual(response['paper'][layout.TASK]['status'], 'queued')
            self.assertNotIn('thread_id', response['paper'][layout.TASK])
            request(f'/api/papers/{PID}/pdf-pause', {})
            self.assertTrue(server.CANCEL[(PID, 'pdf_translate')].is_set())


if __name__ == '__main__':
    unittest.main()
