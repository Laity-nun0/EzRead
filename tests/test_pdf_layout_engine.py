"""原生 BabelDOC 通道、失败审计与隔离进程测试；不调用真实模型。"""
import asyncio
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pdf_layout_engine as engine
from codex_bridge import TranslationError

ROOT = Path(__file__).resolve().parents[1]


def tracking():
    attempt = {'input': 'native prompt', 'output': '[{"id":0,"output":"测试译文"}]',
               'has_error': False, 'fallback_to_translate': False, 'placeholder_full_match': True}
    paragraph = {'input': 'A synthetic sentence.', 'output': '这是合成句子。',
                 'pdf_unicode': 'A synthetic sentence.', 'llm_translate_trackers': [attempt]}
    return {'page': [{'paragraph': [paragraph]}], 'cross_page': [], 'cross_column': []}


class PdfLayoutEngineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='pdf-engine-test-', dir=ROOT / 'work')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_native_channel_probe_and_complete_prompt_roundtrip(self):
        prompt = 'native context\n## Here is the input:\n\n[{"id":0,"input":"source"}]'
        incoming = io.StringIO(json.dumps({'id': '1', 'response': '[{"id":0,"output":"译文"}]'}) + '\n')
        outgoing = io.StringIO()
        channel = engine._TranslationChannel(incoming, outgoing)
        self.assertEqual(channel.request(None), '')
        self.assertEqual(channel.requests, [])
        self.assertEqual(channel.request(prompt, {'request_json_mode': True}), '[{"id":0,"output":"译文"}]')
        self.assertEqual(json.loads(outgoing.getvalue()),
                         {'type': 'translation_request', 'id': '1', 'mode': 'llm',
                          'prompt': prompt, 'rate_limit_params': {'request_json_mode': True}})
        self.assertEqual(channel.failures, [])

    def test_channel_rejects_missing_empty_wrong_id_and_malformed_response(self):
        for reply in ('', 'bad', '{}', '{"id":"wrong","response":"x"}',
                      '{"id":"1","response":""}', '{"id":"1","response":" "}'):
            with self.subTest(reply=reply):
                channel = engine._TranslationChannel(io.StringIO(reply), io.StringIO())
                with self.assertRaises((ValueError, TypeError)):
                    channel.request('source')
                self.assertEqual(len(channel.failures), 1)

    def test_text_mode_is_explicit(self):
        channel = engine._TranslationChannel(io.StringIO('{"id":"1","response":"译文"}'), io.StringIO())
        self.assertEqual(channel.request('source', mode='text'), '译文')
        self.assertEqual(channel.requests[0]['mode'], 'text')

    def test_tracking_covers_cross_page_and_cross_column(self):
        data = tracking()
        data['cross_page'] = copy.deepcopy(data['page'])
        data['cross_column'] = copy.deepcopy(data['page'])
        paragraphs, fallbacks = engine._audit_tracking(data)
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual(fallbacks, 0)
        self.assertEqual({p['group'] for p in paragraphs}, {'page', 'cross_page', 'cross_column'})

    def test_tracking_rejects_incomplete_final_attempt_but_accepts_successful_recovery(self):
        for field, value in [('output', None), ('output', '')]:
            data = tracking()
            data['page'][0]['paragraph'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                engine._audit_tracking(data)
        for field, value in [('llm_translate_trackers', []), ('removed_hallucinated_placeholders', {'<b9>': 1})]:
            data = tracking()
            data['page'][0]['paragraph'][0][field] = value
            with self.assertRaises(ValueError):
                engine._audit_tracking(data)
        for field, value in [('fallback_to_translate', True),
                             ('placeholder_full_match', False), ('output', '')]:
            data = tracking()
            data['page'][0]['paragraph'][0]['llm_translate_trackers'][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    engine._audit_tracking(data)

    def test_shared_batch_error_flag_is_reviewed_after_valid_final_application(self):
        data, warnings = tracking(), []
        data['page'][0]['paragraph'][0]['llm_translate_trackers'][-1]['has_error'] = True
        self.assertEqual(len(engine._audit_tracking(data, warnings=warnings)[0]), 1)
        self.assertTrue(warnings)

    def test_failed_channel_never_sends_another_request(self):
        outgoing = io.StringIO()
        channel = engine._TranslationChannel(io.StringIO('{"id":"1","response":" "}\n'), outgoing)
        for _ in range(3):
            with self.assertRaises(ValueError):
                channel.request('source')
        self.assertEqual(len(channel.requests), 1)
        self.assertEqual(len(outgoing.getvalue().splitlines()), 1)
        self.assertEqual(len(channel.failures), 1)

    def test_tracking_accepts_successful_native_single_paragraph_recovery(self):
        data = tracking()
        attempts = data['page'][0]['paragraph'][0]['llm_translate_trackers']
        attempts.insert(0, {**attempts[0], 'has_error': True, 'fallback_to_translate': True})
        self.assertEqual(engine._audit_tracking(data)[1], 1)

    def test_tracking_rejects_placeholder_loss(self):
        data = tracking()
        paragraph = data['page'][0]['paragraph'][0]
        paragraph['input'] += '<b1>'
        paragraph['output'] = '这是合成句子。'
        with self.assertRaisesRegex(ValueError, 'placeholders'):
            engine._audit_tracking(data)

    def test_tracking_accepts_verified_base_style_application_but_not_formula_loss(self):
        data, warnings = tracking(), []
        paragraph = data['page'][0]['paragraph'][0]
        paragraph['input'] = "<style id='1'>Source</style>{v3}<style id='4'>Fig. 9</style>"
        paragraph['output'] = '译文{v3}图9'
        self.assertEqual(len(engine._audit_tracking(data, warnings=warnings)[0]), 1)
        self.assertTrue(any('字体' in item for item in warnings))
        paragraph['output'] = '译文图9'
        with self.assertRaisesRegex(ValueError, 'placeholders'):
            engine._audit_tracking(data)

    def test_tracking_allows_placeholder_reordering_but_not_added_objects(self):
        data = tracking()
        paragraph = data['page'][0]['paragraph'][0]
        paragraph['input'] = 'Use {v0} before {v1}.'
        paragraph['output'] = '{v1}之前使用{v0}。'
        self.assertEqual(len(engine._audit_tracking(data)[0]), 1)
        paragraph['output'] += '{v2}'
        with self.assertRaisesRegex(ValueError, 'placeholders'):
            engine._audit_tracking(data)

    def test_unchanged_author_name_is_allowed_without_untranslated_prose_warning(self):
        data = tracking()
        item = data['page'][0]['paragraph'][0]
        item['input'] = item['output'] = 'John Smith'
        paragraphs, _ = engine._audit_tracking(data)
        self.assertEqual(engine._unchanged_paragraphs(paragraphs), (1, 0))

    def test_unchanged_long_prose_is_counted_for_review_not_claimed_translated(self):
        data = tracking()
        item = data['page'][0]['paragraph'][0]
        item['input'] = item['output'] = 'This synthetic paragraph verifies native prompt processing and complete response handling.'
        paragraphs, _ = engine._audit_tracking(data)
        self.assertEqual(engine._unchanged_paragraphs(paragraphs), (1, 1))

    def test_tracking_short_skipped_text_is_not_missing_translation(self):
        data = tracking()
        data['page'][0]['paragraph'].append({'input': 'abc', 'llm_translate_trackers': []})
        self.assertEqual(len(engine._audit_tracking(data)[0]), 1)
        with self.assertRaises(ValueError):
            engine._audit_tracking({'page': [], 'cross_page': [], 'cross_column': []})
        with self.assertRaises(ValueError):
            engine._audit_tracking({})

    def test_output_character_check_catches_missing_rendered_translation(self):
        paragraphs = [{'output': '模拟译文模拟译文'}]
        page = SimpleNamespace(get_text=lambda: '模拟译文模拟译文')
        self.assertEqual(engine._verify_output_characters([page], paragraphs), [])
        page.get_text = lambda: '模拟译文模拟译'
        self.assertTrue(engine._verify_output_characters([page], paragraphs))
        # 跨页、跨栏审计组可以重复包含同一最终段落，计数并非字形丢失证明。
        self.assertTrue(engine._verify_output_characters([page], paragraphs * 2))

    def test_rotation_output_blocks_missing_text_even_when_large_image_keeps_ink(self):
        pixmap = SimpleNamespace(width=80, height=80, samples=bytes([100]) * 6400)
        source = SimpleNamespace(get_text=lambda: 'Synthetic body text ' * 10,
                                 get_pixmap=lambda **kwargs: pixmap)
        output = SimpleNamespace(get_text=lambda: '', get_pixmap=lambda **kwargs: pixmap)
        runtime = SimpleNamespace(Matrix=lambda *args: None, csGRAY=None)
        with patch.dict(sys.modules, {'pymupdf': runtime}):
            result = engine._rotation_output_coverage([source], [output], [1])[0]
            self.assertEqual(result['occupied_region_retention'], 1)
            self.assertTrue(result['missing_text'])
            self.assertTrue(result['blocked'])
            output.get_text = lambda: '完整的模拟译文'
            self.assertFalse(engine._rotation_output_coverage([source], [output], [1])[0]['blocked'])

    def test_source_audit_requires_unique_exact_block_match(self):
        block = (10, 20, 100, 40, 'A synthetic\nsentence.', 0, 0)
        page = SimpleNamespace(rect=SimpleNamespace(height=200), get_text=lambda kind: [block])
        paragraphs = engine._audit_tracking(tracking())[0]
        self.assertEqual(engine._source_audit([page], paragraphs)[0]['bbox'], [10, 160, 100, 180])
        self.assertEqual(engine._source_audit([page, page], paragraphs), [])

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 PDF 运行时未安装。')
    def test_source_audit_applies_pdf_page_rotation_to_text_boxes(self):
        script = """import pymupdf,pdf_layout_engine as e
with pymupdf.open() as doc:
    page=doc.new_page(width=600,height=800)
    page.insert_text((50,100),'Unique synthetic paragraph.',fontsize=10)
    page.set_rotation(90)
    block=page.get_text('blocks')[0]
    expected=pymupdf.Rect(block[:4])*page.rotation_matrix
    audit=e._source_audit(doc,[{'pdf_unicode':block[4],'input':block[4]}])
    assert audit[0]['bbox']==[expected.x0,600-expected.y1,expected.x1,600-expected.y0],audit
"""
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        result = subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script],
                                cwd=ROOT, env=engine.runtime_environment(ROOT), capture_output=True,
                                text=True, encoding='utf-8', timeout=20, **flags)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 PDF 运行时未安装。')
    def test_rotation_normalization_preserves_visual_vector_image_and_source_identity(self):
        script = """import pathlib,sys,pymupdf,pdf_layout_engine as e
folder=pathlib.Path(sys.argv[1]);source=folder/'rotated.pdf'
with pymupdf.open() as doc:
    for rotation in (90,180,270):
        page=doc.new_page(width=600,height=800)
        page.insert_text((50,100),'Unique vector text on page '+str(rotation),fontsize=15)
        page.draw_rect(pymupdf.Rect(45,120,500,650),color=(0,0,0),width=2)
        page.draw_line((45,300),(500,300),color=(0,0,1),width=1)
        pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,20,20),False);pix.clear_with(120)
        page.insert_image(pymupdf.Rect(80,150,220,260),pixmap=pix)
        page.set_rotation(rotation)
    doc.save(source)
before=e._sha(source)
path,report=e._normalize_source_rotation(source,folder)
assert e._sha(source)==before and path!=source
assert [p['original_rotation'] for p in report['pages']]==[90,180,270]
assert report['normalized']
assert all(p['text_characters_equal'] and p['source_vectors']==p['normalized_vectors'] and p['source_images']==p['normalized_images'] for p in report['pages'])
with pymupdf.open(source) as a,pymupdf.open(path) as b:
    for item in report['pages']:
        b[item['page']-1].set_rotation(item['restore_rotation'])
    result=e._rotation_output_coverage(a,b,[1,2,3])
    assert all(r['occupied_region_retention']==1 for r in result)
    bad=pymupdf.open()
    for page in a:bad.new_page(width=page.rect.width,height=page.rect.height)
    assert e._rotation_output_coverage(a,bad,[1])[0]['needs_review']
    assert e._rotation_output_coverage(a,bad,[1])[0]['blocked']
"""
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        result = subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script, str(self.root)],
                                cwd=ROOT, env=engine.runtime_environment(ROOT), capture_output=True,
                                text=True, encoding='utf-8', timeout=25, **flags)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_stages_are_not_a_production_fallback(self):
        for mode in ('collect', 'render'):
            with self.assertRaises(TranslationError) as caught:
                engine.run_stage(self.root, self.root / 'source.pdf', self.root / 'run', mode)
            self.assertEqual(caught.exception.code, 'pdf_engine_legacy')

    def test_removed_batch_policy_is_rejected_before_starting_worker(self):
        for policy in ('source-800-max-8-v1', 'unknown', None):
            with self.subTest(policy=policy), patch.object(engine.processes, 'spawn') as spawn:
                with self.assertRaises(TypeError):
                    engine.run_native(self.root, self.root / 'source.pdf', self.root / 'run',
                                      translate=lambda request: 'synthetic', batch_policy=policy)
                with self.assertRaises(TypeError):
                    engine.run_stage(self.root, self.root / 'source.pdf', self.root / 'run', 'native',
                                     translate=lambda request: 'synthetic', batch_policy=policy)
                with self.assertRaisesRegex(ValueError, '实验已停用.*重新翻译'):
                    asyncio.run(engine._worker({'batch_policy': policy}))
                spawn.assert_not_called()
        self.assertFalse((self.root / 'run').exists())

    def _run_fixture_worker(self, body, translate, cancel=None):
        source = self.root / 'source.pdf'
        source.write_bytes(b'fixture')
        script = "import json,pathlib,sys\nr=json.loads(pathlib.Path(sys.argv[2]).read_text())\n" + body
        (self.root / 'pdf_layout_engine.py').write_text(script, encoding='utf-8')
        with patch.object(engine, 'availability', return_value={'available': True}), \
                patch.object(engine, '_paths', return_value=(self.root / '.deps/babeldoc', Path(sys.executable))):
            return engine.run_native(self.root, source, self.root / 'run', translate=translate, cancel_event=cancel)

    def test_parent_full_protocol_and_output_validation(self):
        script = ("print(json.dumps({'type':'translation_request','id':'1','mode':'llm','prompt':'full native prompt','rate_limit_params':{'request_json_mode':True}}),flush=True)\n"
                  "a=json.loads(sys.stdin.readline());assert a=={'id':'1','response':'synthetic response'}\n"
                  "d=pathlib.Path(r['run_dir'])\n"
                  "(d/'mono.pdf').write_bytes(b'x'*101);(d/'dual.pdf').write_bytes(b'y'*101)\n"
                  "(d/'audit.json').write_text('[]')\n"
                  "out={'source_sha256':r['source_sha256'],'mono_pdf':'mono.pdf','dual_pdf':'dual.pdf','audit_path':'audit.json','translation_requests':1}\n"
                  "(d/'native-result.json').write_text(json.dumps(out))\n")
        calls = []
        result = self._run_fixture_worker(script, lambda event: calls.append(event) or 'synthetic response')
        self.assertEqual(result['translation_requests'], 1)
        self.assertEqual(calls[0]['prompt'], 'full native prompt')
        self.assertNotIn('batch_policy', engine._read(self.root / 'run' / 'native-request.json'))
        self.assertNotIn('batch_policy', result)

    def test_parent_model_error_propagates_and_stops_worker(self):
        script = "print(json.dumps({'type':'translation_request','id':'1','mode':'llm','prompt':'p','rate_limit_params':{}}),flush=True)\nsys.stdin.readline()\n"
        failure = TranslationError('模拟额度限制', code='quota')
        def translate(event):
            raise failure
        with self.assertRaises(TranslationError) as caught:
            self._run_fixture_worker(script, translate)
        self.assertIs(caught.exception, failure)

    def test_parent_cancellation_after_response_does_not_write_late_response(self):
        cancel = threading.Event()
        script = "print(json.dumps({'type':'translation_request','id':'1','mode':'llm','prompt':'p','rate_limit_params':{}}),flush=True)\nsys.stdin.readline()\n"
        def translate(event):
            cancel.set()
            return 'late'
        with self.assertRaises(TranslationError) as caught:
            self._run_fixture_worker(script, translate, cancel)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_worker_exit_without_result_is_not_success(self):
        with self.assertRaises(TranslationError) as caught:
            self._run_fixture_worker('print("not JSON")\n', lambda event: '译文')
        self.assertEqual(caught.exception.code, 'pdf_engine')

    def test_worker_asset_download_attempt_is_blocked_and_recorded(self):
        assets = SimpleNamespace()
        with patch.dict(sys.modules, {'babeldoc.assets': SimpleNamespace(assets=assets)}):
            attempts = engine._block_asset_downloads()
            with self.assertRaisesRegex(RuntimeError, 'cannot download'):
                asyncio.run(assets.get_fastest_upstream_for_font())
        self.assertEqual(attempts, [True])

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_actual_native_llm_pipeline_with_synthetic_pdf_and_mock_responses(self):
        source = self.root / 'synthetic.pdf'
        script = """import pymupdf,sys
with pymupdf.open() as doc:
    for index in range(2):
        page=doc.new_page(width=600,height=800)
        page.insert_text((50,60),'Synthetic native translation validation',fontsize=16)
        page.insert_text((50,380),'References',fontsize=14)
        for row in range(6):
            page.insert_text((50,110+40*row),'This synthetic paragraph verifies native prompt processing and complete response handling.',fontsize=10)
    doc.save(sys.argv[1])
"""
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script, str(source)],
                       cwd=ROOT, env=engine.runtime_environment(ROOT), check=True, capture_output=True, **flags)
        calls, reviews = [], []
        def translate(event):
            from ezread.pdf_native_translation import request_units, response_values
            calls.append(event)
            self.assertEqual(event['mode'], 'llm')
            inputs, batch = request_units(event)
            outputs = [{'id': item['id'], 'output': '参考文献' if item['input'] == 'References'
                                   else '模拟译文 ' + item['input'] + ' 90'
                                   if len(item['input']) > 50 else item['input']} for item in inputs]
            response = json.dumps(outputs, ensure_ascii=False) if batch else outputs[0]['output']
            # 模拟数字疑点穿过真实上游排版流程，不以此发起实际模型请求。
            response_values(inputs, batch, response, reviews=reviews)
            return response
        result = engine.run_native(ROOT, source, self.root / 'run', translate=translate)
        self.assertEqual(result['pipeline'], 'babeldoc-native-llm')
        self.assertEqual(result['page_count'], 2)
        self.assertEqual(result['translation_requests'], len(calls))
        self.assertGreater(result['translation_audit']['paragraphs'], 0)
        self.assertGreater(result['translation_audit']['fallback_attempts'], 0)
        self.assertGreater(result['translation_audit']['unchanged_paragraphs'], 0)
        self.assertTrue(any('References' in call['prompt'] for call in calls))
        tracking_file = next((self.root / 'run').rglob('translate_tracking.json'))
        paragraphs, _ = engine._audit_tracking(engine._read(tracking_file))
        self.assertTrue(any(p['input'] == 'References' and p['output'] == '参考文献'
                            for p in paragraphs))
        self.assertEqual(result['quality'], 'needs_review')
        self.assertTrue(result['warnings'])
        self.assertTrue(reviews)
        self.assertTrue((self.root / 'run' / result['audit_path']).is_file())
        self.assertTrue(all(call['prompt'].startswith('You are') for call in calls))

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_actual_native_pipeline_retains_body_on_rotated_pages(self):
        from ezread.pdf_native_translation import request_units, response_values
        source = self.root / 'varied.pdf'
        script = r'''import pymupdf,sys
with pymupdf.open() as doc:
    for index in range(2):
        page=doc.new_page(width=600,height=800)
        page.insert_text((50,55),'Synthetic materials translation validation',fontsize=15)
        for column in (50,310):
            for row in range(6):
                page.insert_text((column,100+row*38),'The alloy shows improved properties.',fontsize=9)
                page.insert_text((column,112+row*38),'Strength and corrosion were evaluated.',fontsize=9)
        page.insert_text((50,380),'References',fontsize=14)
        page.insert_text((50,420),'Stress ',fontsize=10)
        page.insert_text((83,420),'sigma',fontname='tiit',fontsize=10)
        page.insert_text((111,420),' = 200 MPa; Al',fontsize=10)
        page.insert_text((179,416),'2',fontsize=7)
        page.insert_text((184,420),'O',fontsize=10)
        page.insert_text((193,423),'3',fontsize=7)
        page.insert_text((200,420),' particles resist damage.',fontsize=10)
        for y,label in ((490,'Sample   Stress (MPa)   Time (h)'),(520,'AA6061   200   24'),(550,'AA7075   400   48')):
            page.insert_text((50,y),label,fontsize=10)
            page.draw_line((45,y+8),(290,y+8))
        page.draw_rect((45,470,290,560))
        page.draw_rect((350,430,500,560),fill=(.8,.8,.8))
        page.insert_text((350,580),'Figure 1. A synthetic specimen.',fontsize=9)
        if index:
            page.set_rotation(90)
    doc.save(sys.argv[1])
'''
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script, str(source)],
                       cwd=ROOT, env=engine.runtime_environment(ROOT), check=True, capture_output=True, **flags)
        calls, tagged = [], []
        def translate(request):
            units, batch = request_units(request)
            calls.append(request)
            tagged.extend(unit['input'] for unit in units if engine.markers_match(unit['input'], '') is False)
            outputs = [{'id': u['id'], 'output': '参考文献' if u['input'] == 'References'
                        else '模拟译文 ' + u['input']} for u in units]
            response = json.dumps(outputs, ensure_ascii=False) if batch else outputs[0]['output']
            response_values(units, batch, response)
            return response
        original = source.read_bytes()
        result = engine.run_native(ROOT, source, self.root / 'varied-run', translate=translate)
        self.assertEqual(result['page_count'], 2)
        self.assertTrue(result['rotation_normalization']['output_restored'])
        check = engine._read(self.root / 'varied-run/rotation-output-validation.json')
        self.assertEqual(check['blocked_pages'], [])
        self.assertFalse(check['pages'][0]['missing_text'])
        self.assertTrue(engine._read(self.root / 'varied-run/rotation-normalization.json')['normalized'])
        self.assertTrue(calls)
        self.assertTrue(tagged, '合成版式必须实际进入公式或样式占位符路径')
        self.assertTrue(any('{v' in text or '<style' in text for text in tagged), '采用标准 OpenAI 占位符。')
        self.assertFalse(any('<b' in text for text in tagged), '新任务不使用基类的 b 标记。')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(engine._read(self.root / 'varied-run/native-translation-validation.json')['failures'], [])

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_standard_native_batch_recovers_one_bad_unit_with_one_single_request(self):
        from ezread.pdf_native_translation import request_units
        source = self.root / 'recover.pdf'
        script = """import pymupdf,sys
with pymupdf.open() as doc:
    page=doc.new_page(width=600,height=800)
    for row in range(6):
        page.insert_text((50,80+40*row),'The synthetic material paragraph verifies standard recovery and complete output.',fontsize=10)
    doc.save(sys.argv[1])
"""
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script, str(source)],
                       cwd=ROOT, env=engine.runtime_environment(ROOT), check=True, capture_output=True, **flags)
        calls = []
        def translate(request):
            units, batch = request_units(request)
            first = not calls
            calls.append((batch, units))
            outputs = [{'id': u['id'], 'output': '这是离线模拟译文。' + u['input']} for u in units]
            if first:
                outputs[0]['output'] += ' {v999}'
            return json.dumps(outputs, ensure_ascii=False) if batch else outputs[0]['output']
        try:
            result = engine.run_native(ROOT, source, self.root / 'recover-run', translate=translate)
        except Exception:
            import shutil
            shutil.copytree(self.root / 'recover-run', ROOT / 'work/integration-repair-20261016/recovery-failure', dirs_exist_ok=True)
            raise
        self.assertTrue(calls[0][0])
        self.assertEqual(sum(not batch for batch, _ in calls), 1)
        self.assertEqual(result['translation_audit']['fallback_attempts'], 1)
        self.assertTrue(result['warnings'])
        self.assertTrue((self.root / 'recover-run' / result['mono_pdf']).is_file())

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_actual_worker_refuses_result_after_composition_failure_and_sends_no_more_requests(self):
        from ezread.pdf_native_translation import request_units
        source = self.root / 'failure.pdf'
        script = """import pymupdf,sys
with pymupdf.open() as doc:
    page=doc.new_page(width=600,height=800)
    for row in range(8):
        page.insert_text((50,80+40*row),'A synthetic paragraph verifies atomic native translation application.',fontsize=10)
    doc.save(sys.argv[1])
"""
        paths, environment, status = engine._paths(ROOT), engine.runtime_environment(ROOT), engine.availability(ROOT)
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        subprocess.run([str(paths[1]), '-X', 'utf8', '-c', script, str(source)],
                       cwd=ROOT, env=environment, check=True, capture_output=True, **flags)
        entry = (f'import sys\nsys.path.insert(0,{str(ROOT)!r})\n'
                 'import asyncio,pdf_layout_engine as engine\n'
                 'from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator\n'
                 'def fail(*args,**kwargs): raise RuntimeError("synthetic composition failure")\n'
                 'ILTranslator.parse_translate_output=fail\n'
                 'engine.processes.attach_job()\nsys.stdout=sys.stderr\n'
                 'asyncio.run(engine._worker(engine._read(sys.argv[2])))\n')
        (self.root / 'pdf_layout_engine.py').write_text(entry, encoding='utf-8')
        calls = []
        def translate(request):
            units, batch = request_units(request)
            calls.append(request)
            outputs = [{'id': u['id'], 'output': '模拟译文 ' + u['input']} for u in units]
            return json.dumps(outputs) if batch else outputs[0]['output']
        folder = self.root / 'failure-run'
        with patch.object(engine, '_paths', return_value=paths), \
                patch.object(engine, 'runtime_environment', return_value=environment), \
                patch.object(engine, 'availability', return_value=status):
            with self.assertRaises(TranslationError) as caught:
                engine.run_native(self.root, source, folder, translate=translate)
        self.assertEqual(caught.exception.code, 'pdf_engine')
        self.assertEqual(len(calls), 2, '批次失败后仅允许上游一次单段恢复；终态失败停止后续请求。')
        self.assertTrue(engine._read(folder / 'native-translation-validation.json')['failures'])
        self.assertFalse((folder / 'native-result.json').exists())

    def test_normal_setup_bootstraps_pip_after_an_offline_seed(self):
        spec = importlib.util.spec_from_file_location('pdf_setup_test', ROOT / 'scripts/setup-pdf-layout.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        setup.ROOT = self.root
        base, executable = engine._paths(self.root)
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.object(sys, 'argv', ['setup-pdf-layout.py']), \
                patch.object(setup.subprocess, 'run', side_effect=[SimpleNamespace(returncode=1),
                                                                  SimpleNamespace(returncode=0),
                                                                  SimpleNamespace(returncode=0)]) as run, \
                patch.object(setup, 'run_json', return_value={'version': '0.6.4'}), \
                patch.object(engine, '_write'), \
                patch.object(engine, 'availability', return_value={'available': True}):
            setup.main()
        self.assertEqual(run.call_args_list[1].args[0], [str(executable), '-m', 'ensurepip', '--upgrade'])
        self.assertEqual(run.call_args_list[2].args[0][1:4], ['-m', 'pip', 'install'])

    def test_cancel_kills_worker_and_its_child(self):
        source, marker = self.root / 'source.pdf', self.root / 'child-marker'
        source.write_bytes(b'fixture')
        child = self.root / 'child.py'
        child.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\n'
                         'while True:\n p.write_text(str(time.time()))\n time.sleep(.05)\n', encoding='utf-8')
        worker = ('import json,pathlib,subprocess,sys,time\n'
                  f'sys.path.insert(0,{str(ROOT)!r})\n'
                  'from ezread import processes\nprocesses.attach_job()\n'
                  f'p=subprocess.Popen([sys.executable,{str(child)!r},{str(marker)!r}])\n'
                  f'while not pathlib.Path({str(marker)!r}).exists(): time.sleep(.01)\n'
                  'print(json.dumps({"type":"child_ready"}),flush=True)\ntime.sleep(30)\n')
        (self.root / 'pdf_layout_engine.py').write_text(worker, encoding='utf-8')
        cancel = threading.Event()
        def progress(event):
            if event.get('type') == 'child_ready':
                cancel.set()
        with patch.object(engine, 'availability', return_value={'available': True}), \
                patch.object(engine, '_paths', return_value=(self.root / '.deps/babeldoc', Path(sys.executable))):
            with self.assertRaises(TranslationError) as caught:
                engine.run_stage(self.root, source, self.root / 'run', 'native', translate=lambda event: '模拟译文',
                                 cancel_event=cancel, on_progress=progress)
        self.assertEqual(caught.exception.code, 'cancelled')
        final_marker = marker.read_text()
        time.sleep(.2)
        self.assertEqual(marker.read_text(), final_marker)


if __name__ == '__main__':
    unittest.main()
