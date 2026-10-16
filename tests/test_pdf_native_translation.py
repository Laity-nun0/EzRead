"""上游请求身份、已付费响应和旧译文迁移的离线缓存回归。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezread import pdf_native_translation as native
from ezread import pdf_translations as layout
from ezread.translation_glossary import PaperGlossary

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {'source_hash': 'source-hash', 'config': {'model': 'model-a', 'reasoning_effort': 'low'},
            'engine_version': '0.6.4', 'engine_fingerprint': 'local-old', 'protocol': 'native-llm-v1'}


def request(text='Source 20.', *, prefix='Translate faithfully.\n', unit_id=0):
    return {'id': 'transport-id', 'mode': 'llm', 'prompt': prefix + native.BATCH_INPUT + json.dumps(
        [{'id': unit_id, 'input': text}]), 'rate_limit_params': {'request_json_mode': True}}


def response(text='译文 20。', *, unit_id=0):
    return json.dumps([{'id': unit_id, 'output': text}], ensure_ascii=False)


class NativeRequestCacheTests(unittest.TestCase):
    def test_batch_values_checks_structure_without_assessing_content(self):
        from ezread.translation_validation import ResponseValidationError, batch_values
        self.assertIs(native.ResponseValidationError, ResponseValidationError)
        self.assertIs(native.batch_values, batch_values)
        units = [{'id': 0, 'input': 'Source 20 {v0}.'}]
        for text in ('', '译文 21。', None):
            self.assertEqual(batch_values(units, response(text)), {0: text})

    def test_v3_valid_ancestor_wins_over_current_recoverable_batch(self):
        identity = {**IDENTITY, 'protocol': 'babeldoc-native-v3'}
        bad_folder, good_folder = self.folder / 'bad', self.folder / 'good'
        self.assertEqual(self.cache(folder=bad_folder, identity=identity).resolve(
            request(), Mock(return_value='not JSON')), 'not JSON')
        self.cache(folder=good_folder, identity=identity).resolve(request(), Mock(return_value=response()))
        bad_path = next((bad_folder / 'native-replies').glob('*.json'))
        bad_bytes = bad_path.read_bytes()
        good_path = next((good_folder / 'native-replies').glob('*.json'))
        good_bytes = good_path.read_bytes()
        model = Mock(side_effect=AssertionError('合法祖先缓存不得重复调用模型'))
        cache = self.cache(folder=bad_folder, identity=identity, previous_folder=good_folder, retry_rejected=True)
        self.assertEqual(cache.resolve(request(), model), response())
        self.assertEqual((cache.reused, cache.received, cache.done, cache.model_calls), (1, 1, 1, 0))
        self.assertEqual(bad_path.read_bytes(), good_bytes)
        self.assertEqual(good_path.read_bytes(), good_bytes)
        self.assertEqual(next((bad_folder / 'native-replies-rejected').glob('*/*.json')).read_bytes(), bad_bytes)
        model.assert_not_called()

    def test_v3_invalid_batch_replays_original_bytes_for_upstream_without_new_calls(self):
        identity = {**IDENTITY, 'protocol': 'babeldoc-native-v3'}
        for index, (paid, code) in enumerate((
                ('not JSON', 'batch_json'), ('[]', 'batch_ids'),
                ('[{"output":"译文 20。"}]', 'batch_ids'),
                (response(unit_id=-1), 'batch_ids'),
                ('[{"id":0,"id":0,"output":"译文 20。"}]', 'batch_json'),
                ('[{"id":0,"output":"译文 20。","extra":NaN}]', 'batch_json'),
                (json.dumps(response()), 'batch_ids'))):
            with self.subTest(paid=paid):
                original_folder = self.folder / str(index)
                model = Mock(return_value={'response': paid, 'terms': [], 'usage': {'output_tokens': 7}})
                original_cache = self.cache(folder=original_folder, identity=identity, native_flow=True)
                self.assertEqual(original_cache.resolve(request(), model), paid)
                with self.assertRaises(native.ResponseValidationError) as caught:
                    native.batch_values(native.request_units(request())[0], paid)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(original_cache.rejected, 1)
                self.assertTrue(original_cache.warnings)
                self.assertEqual(original_cache.model_calls, 1)
                source = next((original_folder / 'native-replies').glob('*.json'))
                saved = json.loads(source.read_bytes())
                self.assertEqual(saved['response'], paid)
                original = json.dumps(saved, ensure_ascii=False, indent=3).encode() + b'\n'
                source.write_bytes(original)
                target = original_folder / 'restored'
                no_model = Mock(side_effect=AssertionError('坏批次不得再次付费'))
                for _ in range(2):
                    restored = self.cache(folder=target, identity=identity, previous_folder=original_folder,
                                          native_flow=True, retry_rejected=True)
                    for _ in range(2):
                        self.assertEqual(restored.resolve(request(), no_model), paid)
                        self.assertEqual(restored.last_metadata, saved['metadata'])
                    self.assertEqual((restored.reused, restored.done, restored.model_calls), (2, 2, 0))
                    self.assertEqual((target / 'native-replies' / source.name).read_bytes(), original)
                    self.assertEqual(source.read_bytes(), original)
                    self.assertFalse((target / 'native-replies-rejected').exists())
                no_model.assert_not_called()
                single = {'mode': 'llm', 'prompt': native.SINGLE_INPUT + 'Source 20.'}
                single_model = Mock(return_value='译文 20。')
                self.assertEqual(restored.resolve(single, single_model), '译文 20。')
                single_model.assert_called_once()

    def test_v3_batch_content_returns_raw_but_single_formula_loss_remains_terminal(self):
        identity = {**IDENTITY, 'protocol': 'babeldoc-native-v3'}
        for index, paid in enumerate(('', response(''), response('译文 20。'))):
            with self.subTest(paid=paid):
                cache = self.cache(folder=self.folder / str(index), identity=identity, native_flow=True)
                model = Mock(return_value=paid)
                with patch.object(native, 'normalize_batch_response', side_effect=AssertionError('v3 不解转义')):
                    self.assertEqual(cache.resolve(request('Source 20 {v0}.'), model), paid)
                    self.assertEqual(cache.resolve(request('Source 20 {v0}.'), model), paid)
                model.assert_called_once()
        single = {'mode': 'llm', 'prompt': native.SINGLE_INPUT + 'Source 20 {v0}.'}
        model = Mock(return_value='译文 20。')
        for _ in range(2):
            with self.assertRaises(native.ResponseValidationError):
                self.cache(identity=identity, native_flow=True).resolve(single, model)
        model.assert_called_once()

    def test_v2_v3_cache_isolation_preserves_v2_normalization_and_explicit_retry(self):
        v2 = {**IDENTITY, 'protocol': 'babeldoc-native-v2'}
        v3 = {**IDENTITY, 'protocol': 'babeldoc-native-v3'}
        paid = json.dumps(response())
        model = Mock(return_value=paid)
        self.assertEqual(self.cache(identity=v2).resolve(request(), model), response())
        self.assertEqual(self.cache(identity=v3).resolve(request(), model), paid)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.cache(identity=v2).resolve(request(), model), response())
        self.assertEqual(model.call_count, 2)
        bad_request = request('Other 3.')
        with self.assertRaises(native.ResponseValidationError):
            self.cache(identity=v2).resolve(bad_request, Mock(return_value='not JSON'))
        retry = Mock(return_value=response('其他 3。'))
        self.assertEqual(self.cache(identity=v2, retry_rejected=True).resolve(bad_request, retry), response('其他 3。'))
        retry.assert_called_once()

    def test_native_single_style_only_loss_reuses_paid_v2_response_without_rewriting_bytes(self):
        source = "<style id='6'>Fig. 9</style><style id='8'>strength {v3}</style> {v10}"
        paid = '图 9 强度 {v3} {v10}'
        req = {'mode': 'llm', 'prompt': 'Rules.\n' + native.SINGLE_INPUT + source}
        identity = {**IDENTITY, 'protocol': 'native-llm-v2'}
        with self.assertRaises(native.ResponseValidationError):
            self.cache(identity=identity).resolve(req, Mock(return_value=paid))
        path = next((self.folder / 'native-replies').glob('*.json'))
        saved = json.loads(path.read_bytes())
        original = json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
        path.write_bytes(original)
        callback = Mock(side_effect=AssertionError('仅样式降级不得再次调用模型'))
        cached = self.cache(identity=identity, native_flow=True)
        self.assertEqual(cached.resolve(req, callback), paid)
        callback.assert_not_called()
        self.assertEqual((cached.reused, cached.done, cached.rejected, cached.model_calls), (1, 1, 0, 0))
        self.assertEqual(cached.review_pending, 1)
        self.assertTrue(any('字体样式已降级' in warning for warning in cached.warnings))
        self.assertEqual(path.read_bytes(), original)
        copied = self.cache(folder=self.folder / 'copy', identity=identity, native_flow=True,
                            previous_folder=self.folder)
        self.assertEqual(copied.resolve(req, callback), paid)
        self.assertEqual((copied.folder / 'native-replies' / path.name).read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
        callback.assert_not_called()

    def test_native_style_degradation_does_not_relax_formula_or_invalid_style_rejection(self):
        source = "<style id='6'>A</style><style id='8'>B {v3}</style>{v10}"
        units = [{'id': 0, 'input': source}]
        paid = '<style id=\'8\'>甲乙 {v3}</style>{v10}'
        reviews = []
        self.assertEqual(native.response_values(units, True, response(paid), native_flow=True, reviews=reviews),
                         {0: paid})
        self.assertTrue(reviews)
        self.assertTrue(layout.assess_translation(source, paid, native_flow=True)['style_degraded'])
        self.assertTrue(layout.assess_translation(source, paid)['error'])
        for bad in (paid.replace('{v3}', ''), paid + '{v3}', paid + '{v99}',
                    paid.replace('</style>', ''), paid + '</style>',
                    paid + "<style id='99'>未知</style>", paid + "<style id='8'>重复</style>"):
            with self.subTest(bad=bad):
                with self.assertRaises(native.ResponseValidationError):
                    native.response_values(units, True, response(bad), native_flow=True)
                single = {'mode': 'llm', 'prompt': native.SINGLE_INPUT + source}
                folder = self.folder / str(len(list(self.folder.iterdir())))
                callback = Mock(return_value=bad)
                for _ in range(2):
                    with self.assertRaises(native.ResponseValidationError):
                        self.cache(folder=folder, native_flow=True).resolve(single, callback)
                callback.assert_called_once()

    def test_native_formula_failure_returns_cached_batch_to_upstream_recovery(self):
        req = request("<style id='6'>A {v3}</style>{v10}")
        paid = response('甲 {v10}')
        callback = Mock(return_value=paid)
        cached = self.cache(native_flow=True)
        self.assertEqual(cached.resolve(req, callback), paid)
        self.assertEqual(cached.rejected, 1)
        self.assertEqual(cached.resolve(req, callback), paid)
        callback.assert_called_once()
        self.assertEqual((cached.reused, cached.model_calls), (1, 1))

    def test_native_degradation_does_not_relax_batch_json_or_id_contract(self):
        units = [{'id': 0, 'input': "<style id='6'>A</style>{v3}"}]
        for paid in ('not JSON', '[]', response('甲{v3}', unit_id=1),
                     '[{"id":0,"id":0,"output":"甲{v3}"}]',
                     '[{"id":0,"output":"甲{v3}","extra":NaN}]',
                     '[{"id":0,"output":"甲{v3}"},{"id":0,"output":"甲{v3}"}]'):
            with self.subTest(paid=paid), self.assertRaises(native.ResponseValidationError):
                native.response_values(units, True, paid, native_flow=True)

    def test_native_flow_replays_failed_batch_for_upstream_recovery_without_repaying(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as directory:
            cache = native.RequestCache(Path(directory), IDENTITY, native_flow=True)
            req = request("<style id='1'>Source</style> {v2}")
            bad = response("<style id='1'>坏的模拟响应 {v2}")
            model = Mock(return_value=bad)
            self.assertEqual(cache.resolve(req, model), bad)
            self.assertEqual(cache.rejected, 1)
            self.assertEqual(cache.resolve(req, Mock(side_effect=AssertionError('不得重译整批'))), bad)
            self.assertEqual(cache.reused, 1)
            model.assert_called_once()
            with self.assertRaises(native.ResponseValidationError):
                native.response_values(*native.request_units(req), bad)

    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='pdf-native-cache-', dir=ROOT / 'work')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def cache(self, *, folder=None, identity=None, **kwargs):
        return native.RequestCache(folder or self.folder, copy.deepcopy(identity or IDENTITY), **kwargs)

    def test_batch_delimiter_inside_title_does_not_shadow_final_input(self):
        req = request(prefix='Title\n' + native.BATCH_INPUT + 'literal heading\n')
        units, batch = native.request_units(req)
        self.assertTrue(batch)
        self.assertEqual(units, [{'id': 0, 'input': 'Source 20.'}])

    def test_duplicate_response_fields_and_nonfinite_constants_are_rejected(self):
        for paid in ('[{"id":0,"id":0,"output":"valid"}]',
                     '[{"id":0,"output":"first","output":"second"}]',
                     '[{"id":0,"output":"valid","extra":NaN}]',
                     '[{"id":0,"output":"valid","extra":Infinity}]'):
            with self.subTest(paid=paid), self.assertRaises(native.ResponseValidationError):
                native.response_values([{'id': 0, 'input': 'source'}], True, paid)

    def test_crossed_and_reversed_style_pairs_rejected_but_whole_pairs_can_move(self):
        units = [{'id': 0, 'input': '<b0>Alpha</b0><b1>Beta</b1> {v0} <b2>'}]
        good = '<b1>乙</b1>{v0}<b2><b0>甲</b0>'
        self.assertEqual(native.response_values(units, True, response(good)), {0: good})
        for bad in ('<b0>甲<b1>乙</b0></b1> {v0} <b2>',
                    '</b0>甲<b0><b1>乙</b1> {v0} <b2>'):
            with self.subTest(bad=bad), self.assertRaises(native.ResponseValidationError):
                native.response_values(units, True, response(bad))
        reviews = []
        emptied = '<b0></b0>甲<b1>乙</b1> {v0} <b2>'
        self.assertEqual(native.response_values(units, True, response(emptied), reviews=reviews), {0: emptied})
        self.assertTrue(reviews)

    def test_multiple_ancestor_folders_recover_partial_replay_without_model_calls(self):
        a, b, c = [self.folder / name for name in ('a', 'b', 'c')]
        first, second = request('First source'), request('Second source')
        old = self.cache(folder=a)
        old.resolve(first, Mock(return_value=response('第一原文')))
        old.resolve(second, Mock(return_value=response('第二原文')))
        original = {p.name: p.read_bytes() for p in (a / 'native-replies').glob('*.json')}
        no_api = Mock(side_effect=AssertionError('祖先已有回复，禁止请求'))
        self.cache(folder=b, previous_folder=a).resolve(first, no_api)
        current = self.cache(folder=c, previous_folders=[b, a])
        self.assertEqual(current.resolve(second, no_api), response('第二原文'))
        self.assertEqual((current.received, current.done, current.reused, current.model_calls), (1, 1, 1, 0))
        self.assertEqual({p.name: p.read_bytes() for p in (a / 'native-replies').glob('*.json')}, original)

    def test_valid_ancestor_wins_over_bad_current_and_direct_cache_without_count_inflation(self):
        current, direct, older = [self.folder / name for name in ('current', 'direct', 'older')]
        for folder in (current, direct):
            with self.assertRaises(ValueError):
                self.cache(folder=folder).resolve(request(), Mock(return_value='bad JSON'))
        self.cache(folder=older).resolve(request(), Mock(return_value=response()))
        originals = {p: p.read_bytes() for p in (direct / 'native-replies').glob('*.json')}
        cached = self.cache(folder=current, previous_folders=[direct, older], retry_rejected=True)
        no_api = Mock(side_effect=AssertionError('有效祖先优先于重试'))
        self.assertEqual(cached.resolve(request(), no_api), response())
        self.assertEqual((cached.received, cached.done, cached.reused, cached.model_calls), (1, 1, 1, 0))
        archived = list((current / 'native-replies-rejected').glob('*/*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(layout._read_json(archived[0], {})['response'], 'bad JSON')
        self.assertTrue(all(p.read_bytes() == original for p, original in originals.items()))

    def test_corrupt_current_record_can_recover_from_good_ancestor_with_original_bytes_archived(self):
        older, current = self.folder / 'older', self.folder / 'current'
        self.cache(folder=older).resolve(request(), Mock(return_value=response()))
        older_reply = next((older / 'native-replies').glob('*.json'))
        current_reply = current / 'native-replies' / older_reply.name
        current_reply.parent.mkdir(parents=True)
        current_reply.write_bytes(b'corrupt paid evidence')
        cached = self.cache(folder=current, previous_folder=older)
        self.assertEqual(cached.resolve(request(), Mock(side_effect=AssertionError('禁止请求'))), response())
        archived = list((current / 'native-replies-rejected').glob('*/*.json'))
        self.assertEqual([p.read_bytes() for p in archived], [b'corrupt paid evidence'])
        self.assertTrue(cached.warnings)

    def test_unreadable_current_evidence_never_overwrites_or_calls_model(self):
        older, current = self.folder / 'older', self.folder / 'current'
        for folder in (older, current):
            self.cache(folder=folder).resolve(request(), Mock(return_value=response()))
        reply = next((current / 'native-replies').glob('*.json'))
        original = reply.read_bytes()
        read_bytes = Path.read_bytes
        def read(path):
            if path == reply:
                raise PermissionError('current evidence locked')
            return read_bytes(path)
        cached = self.cache(folder=current, previous_folder=older)
        model = Mock(side_effect=AssertionError('不可读证据不能触发模型'))
        with patch.object(Path, 'read_bytes', read), self.assertRaises(PermissionError):
            cached.resolve(request(), model)
        self.assertEqual(reply.read_bytes(), original)
        self.assertFalse((current / 'native-replies-rejected').exists())
        model.assert_not_called()

    def test_cache_source_manifests_are_same_paper_same_identity_and_only_for_resume(self):
        root = self.folder / 'pdf-layout'
        current = root / ('c' * 16)
        current.mkdir(parents=True)
        direct, older = root / ('b' * 16), root / ('a' * 16)
        for folder in (direct, older):
            layout._write_json(folder / 'native-session.json', IDENTITY)
        task = {'reuse_from': {'run_id': direct.name}}
        sources, _ = layout._cache_sources(current, task, {**IDENTITY, 'engine_fingerprint': 'upgraded'})
        self.assertEqual(sources, [direct, older])
        self.assertEqual(layout._cache_sources(current, {}, IDENTITY), ([], []))
        for field, value in (('source_hash', 'other'), ('config', {'model': 'other'}),
                             ('engine_version', 'other'), ('protocol', 'other'),
                             ('batch_policy', 'source-800-max-8-v1')):
            with self.subTest(field=field):
                layout._write_json(older / 'native-session.json', {**IDENTITY, field: value})
                self.assertEqual(layout._cache_sources(current, task, IDENTITY)[0], [direct])
        (older / 'native-session.json').write_text('bad JSON', encoding='utf-8')
        sources, diagnostics = layout._cache_sources(current, task, IDENTITY)
        self.assertEqual(sources, [direct])
        self.assertTrue(any(item['status'] == 'invalid_manifest' for item in diagnostics))

    def test_cache_source_directory_symlink_cannot_escape_same_paper(self):
        root = self.folder / 'pdf-layout'
        current, outside = root / ('c' * 16), self.folder / 'outside'
        current.mkdir(parents=True)
        layout._write_json(outside / 'native-session.json', IDENTITY)
        link = root / ('a' * 16)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'本机未启用创建符号链接权限：{type(exc).__name__}')
        sources, diagnostics = layout._cache_sources(current, {'reuse_from': {'run_id': link.name}}, IDENTITY)
        self.assertEqual(sources, [])
        self.assertEqual(diagnostics, [{'run_id': link.name, 'status': 'unsafe_path'}])

    def test_resolved_manifest_and_reply_paths_cannot_escape_without_symlink_privilege(self):
        root = self.folder / 'pdf-layout'
        current, linked = root / ('c' * 16), root / ('a' * 16)
        current.mkdir(parents=True)
        linked.mkdir()
        outside = self.folder / 'outside'
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return outside if path == linked else original_resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', resolve):
            sources, diagnostics = layout._cache_sources(current, {'reuse_from': {'run_id': linked.name}}, IDENTITY)
        self.assertEqual(sources, [])
        self.assertEqual(diagnostics, [{'run_id': linked.name, 'status': 'unsafe_path'}])
        self.cache(folder=current).resolve(request(), Mock(return_value=response()))
        ancestor = self.folder / 'valid-ancestor'
        self.cache(folder=ancestor).resolve(request(), Mock(return_value=response()))
        cached = self.cache(folder=current, previous_folders=[ancestor])
        reply = next((current / 'native-replies').glob('*.json'))
        def reply_resolve(path, *args, **kwargs):
            return outside if path == reply else original_resolve(path, *args, **kwargs)
        model = Mock(side_effect=AssertionError('越界缓存不能触发模型'))
        with patch.object(Path, 'resolve', reply_resolve), self.assertRaisesRegex(ValueError, '路径越界'):
            cached.resolve(request(), model)
        model.assert_not_called()

    def test_extra_json_string_escaping_returns_array_without_changing_paid_evidence(self):
        expected = response()
        escaped = json.dumps(expected, ensure_ascii=False)[1:-1]
        raw = {'response': escaped, 'terms': [{'source': 'Source', 'target': '译文'}],
               'usage': {'input_tokens': 12}}
        cache = self.cache()
        self.assertEqual(cache.resolve(request(), Mock(return_value=raw)), expected)
        saved = layout._read_json(next((self.folder / 'native-replies').glob('*.json')), {})
        self.assertEqual(saved['response'], escaped)
        self.assertEqual(saved['raw_response'], raw)
        self.assertEqual(cache.last_metadata, {'terms': raw['terms'], 'usage': raw['usage']})
        self.assertEqual((cache.received, cache.done, cache.rejected, cache.model_calls), (1, 1, 0, 1))

    def test_previously_failed_escaped_reply_is_recovered_locally_without_retry_archive(self):
        expected = response()
        raw = {'response': json.dumps(expected, ensure_ascii=False)[1:-1],
               'terms': [{'source': 'Source', 'target': '译文'}], 'usage': {'output_tokens': 5}}
        with patch.object(native, 'normalize_batch_response', side_effect=lambda value: value):
            with self.assertRaises(native.ResponseValidationError):
                self.cache().resolve(request(), Mock(return_value=raw))
        path = next((self.folder / 'native-replies').glob('*.json'))
        original = path.read_bytes()
        cache = self.cache(retry_rejected=True)
        no_api = Mock(side_effect=AssertionError('可本地恢复的缓存不得再次付费'))
        self.assertEqual(cache.resolve(request(), no_api), expected)
        no_api.assert_not_called()
        self.assertEqual((cache.received, cache.done, cache.reused, cache.rejected, cache.model_calls), (1, 1, 1, 0, 0))
        self.assertEqual(cache.last_metadata, {'terms': raw['terms'], 'usage': raw['usage']})
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.folder / 'native-replies-rejected').exists())
        # 缓存键沿用既有请求身份，不能通过新文件名绕过旧响应。
        self.assertEqual(len(list((self.folder / 'native-replies').glob('*.json'))), 1)

    def test_one_layer_preserves_nested_quotes_backslashes_latex_and_unicode_exactly(self):
        translated = '译文 20 "嵌套引号"；\\alpha + \\beta；C:\\tmp\\file；\\n 字面值；😀\n{v0} <b0>公式</b0>'
        req = request('Source 20 {v0} <b0>formula</b0>.')
        for ascii_only in (False, True):
            expected = json.dumps([{'id': 0, 'output': translated}], ensure_ascii=ascii_only)
            for wrapped in (False, True):
                with self.subTest(ascii_only=ascii_only, wrapped=wrapped):
                    encoded = json.dumps(expected, ensure_ascii=ascii_only)
                    paid = encoded if wrapped else encoded[1:-1]
                    folder = self.folder / f'{ascii_only}-{wrapped}'
                    result = self.cache(folder=folder).resolve(req, Mock(return_value={'response': paid}))
                    self.assertEqual(result, expected)
                    self.assertEqual(json.loads(result)[0]['output'], translated)
                    saved = layout._read_json(next((folder / 'native-replies').glob('*.json')), {})
                    self.assertEqual(saved['response'], paid)

    def test_normalization_never_repairs_truncation_extra_text_invalid_ids_or_markers(self):
        def escape(value):
            return json.dumps(value, ensure_ascii=False)[1:-1]
        bad_responses = [escape(response()[:-1]), escape(response() + ' extra text'),
                         escape(response() + response()), escape("[{'id':0,'output':'译文 20。'}]"),
                         escape(response(unit_id=1)), escape(response('译文 20 {v1}。')),
                         escape(response('译文 20 <b0>新增</b0>')),
                         escape(json.dumps(response(), ensure_ascii=False)),
                         escape('{"id":0,"output":"译文 20。"}'),
                         r'[{\"id\":0,\"output\":\"\q\"}]']
        for index, paid in enumerate(bad_responses):
            with self.subTest(response=paid):
                folder = self.folder / str(index)
                cache = self.cache(folder=folder, retry_rejected=True)
                call = Mock(return_value={'response': paid, 'terms': [], 'usage': {'input_tokens': 1}})
                for _ in range(2):
                    with self.assertRaises(native.ResponseValidationError):
                        cache.resolve(request(), call)
                call.assert_called_once()
                saved = layout._read_json(next((folder / 'native-replies').glob('*.json')), {})
                self.assertEqual(saved['response'], paid)
                self.assertEqual(saved['raw_response']['response'], paid)
                self.assertEqual((cache.received, cache.done, cache.model_calls), (2, 0, 1))
                self.assertEqual(cache.last_metadata, {})
                self.assertFalse((folder / 'native-replies-rejected').exists())

    def test_valid_array_bytes_and_nonbatch_text_are_not_normalized(self):
        expected = '[ { "id" : 0, "output" : "译文 20。" } ]\n'
        self.assertEqual(self.cache().resolve(request(), Mock(return_value=expected)), expected)
        literal = json.dumps(response(), ensure_ascii=False)[1:-1]
        req = {'mode': 'text', 'prompt': 'Source 20.'}
        self.assertEqual(self.cache().resolve(req, Mock(return_value=literal)), literal)

    def test_api_metadata_roundtrips_without_changing_engine_response(self):
        raw = {'response': response(), 'terms': [{'source': 'Source', 'target': '译文'}],
               'usage': {'input_tokens': 12}, 'provider_extra': 'evidence'}
        call = Mock(return_value=raw)
        cache = self.cache()
        self.assertEqual(cache.resolve(request(), call), response())
        self.assertEqual(cache.last_metadata, {'terms': raw['terms'], 'usage': raw['usage']})
        saved = layout._read_json(next((self.folder / 'native-replies').glob('*.json')), {})
        self.assertEqual(saved['raw_response'], raw)
        restored = self.cache()
        self.assertEqual(restored.resolve(request(), call), response())
        self.assertEqual(restored.last_metadata, cache.last_metadata)
        call.assert_called_once()
        restored.resolve(request('Other 3.'), Mock(return_value=response('其他 3。')))
        self.assertEqual(restored.last_metadata, {})

    def test_invalid_api_response_is_saved_with_usage_before_validation_without_stale_metadata(self):
        cache = self.cache()
        cache.resolve(request(), Mock(return_value={'response': response(), 'terms': []}))
        bad = {'response': ['not a string'], 'terms': 'invalid', 'usage': {'output_tokens': 3}}
        call = Mock(return_value=bad)
        for attempt in range(2):
            with self.assertRaises(ValueError):
                cache.resolve(request('Other 3.'), call)
            self.assertEqual(cache.last_metadata, {} if attempt else {
                'terms': 'invalid', 'usage': {'output_tokens': 3}})
        call.assert_called_once()
        records = [layout._read_json(path, {}) for path in (self.folder / 'native-replies').glob('*.json')]
        self.assertTrue(any(record.get('raw_response') == bad for record in records))

    def test_context_changes_cache_identity_and_old_requests_keep_old_key_shape(self):
        call = Mock(return_value=response())
        for extra in ({}, {'translation_context': []},
                      {'translation_context': [{'source': 'Source', 'target': '译文'}]}):
            self.cache().resolve({**request(), **extra}, call)
        self.assertEqual(call.call_count, 3)
        for extra in ({}, {'translation_context': []}):
            self.cache().resolve({**request(), **extra}, call)
        self.assertEqual(call.call_count, 3)

    def test_sequential_replay_rebuilds_glossary_with_zero_api_calls(self):
        requests = [request('Al steel'), request('Al alloy')]
        terms = [{'source': 'Al', 'target': '铝'}, {'source': 'steel', 'target': '钢'}]
        call = Mock(side_effect=[{'response': response('铝 钢'), 'terms': terms},
                                 {'response': response('铝合金'), 'terms': []}])

        def replay(cache, call):
            table = PaperGlossary()
            for original in requests:
                units, batch = native.request_units(original)
                req = {**original, 'translation_context': table.context(units)}
                translated = cache.resolve(req, call)
                table.accept(units, translated, cache.last_metadata.get('terms'), batch=batch)
            return table.snapshot()

        original = replay(self.cache(), call)
        self.assertEqual(call.call_args_list[1].args[0]['translation_context'], [terms[0]])
        no_api = Mock(side_effect=AssertionError('恢复不得调用 API'))
        cache = self.cache()
        self.assertEqual(replay(cache, no_api), original)
        no_api.assert_not_called()
        self.assertEqual((cache.reused, cache.model_calls), (2, 0))

    def test_full_prompt_and_parameters_define_identity_but_transport_id_does_not(self):
        call = Mock(return_value=response())
        cache = self.cache()
        first = request()
        self.assertEqual(cache.resolve(first, call), response())
        same = {**first, 'id': 'new-transport-id'}
        self.assertEqual(self.cache().resolve(same, call), response())
        call.assert_called_once()
        for changed in (request(prefix='Different context.\n'),
                        {**first, 'mode': 'text'},
                        {**first, 'rate_limit_params': {'request_json_mode': True, 'temperature': 0}}):
            self.cache().resolve(changed, call)
        self.assertEqual(call.call_count, 4)
        self.assertEqual(len(list((self.folder / 'native-replies').glob('*.json'))), 4)

    def test_source_model_effort_version_and_protocol_cannot_mix(self):
        call = Mock(return_value=response())
        self.cache().resolve(request(), call)
        for field, value in (('source_hash', 'different-source'), ('engine_version', '0.6.5'),
                             ('protocol', 'different-protocol'),
                             ('config', {'model': 'model-b', 'reasoning_effort': 'low'}),
                             ('config', {'model': 'model-a', 'reasoning_effort': 'high'})):
            with self.subTest(field=field, value=value):
                self.cache(identity={**IDENTITY, field: value}).resolve(request(), call)
        self.assertEqual(call.call_count, 6)

    def test_local_fingerprint_upgrade_reads_previous_reply_without_modifying_it(self):
        old = self.folder / 'old'
        new = self.folder / 'new'
        call = Mock(return_value=response())
        self.cache(folder=old).resolve(request(), call)
        source = next((old / 'native-replies').glob('*.json'))
        original = source.read_bytes()
        cache = self.cache(folder=new, identity={**IDENTITY, 'engine_fingerprint': 'local-new'}, previous_folder=old)
        self.assertEqual(cache.resolve(request(), call), response())
        call.assert_called_once()
        self.assertEqual(cache.reused, 1)
        self.assertEqual(cache.model_calls, 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual((new / 'native-replies' / source.name).read_bytes(), original)

    def test_rejected_delivered_reply_is_persisted_and_not_paid_again(self):
        for bad in ('not JSON', response('译文 20 {v1}。'), response('译文 20 <b0>公式</b0>'), '[]'):
            with self.subTest(response=bad):
                folder = self.folder / str(len(list(self.folder.iterdir())))
                call = Mock(return_value=bad)
                with self.assertRaises(ValueError):
                    self.cache(folder=folder).resolve(request(), call)
                saved = next((folder / 'native-replies').glob('*.json'))
                self.assertEqual(layout._read_json(saved, {})['response'], bad)
                with self.assertRaises(ValueError):
                    self.cache(folder=folder).resolve(request(), call)
                call.assert_called_once()

    def test_equivalent_unit_response_reuses_paid_cache_after_validator_upgrade(self):
        req = request('90 MT')
        paid = response('9000万吨')
        # 模拟旧校验器已拒绝、但持久化保存了响应。
        with patch.object(layout, 'assess_translation', return_value={'error': '旧数字比较失败', 'warnings': []}):
            with self.assertRaises(ValueError):
                self.cache().resolve(req, Mock(return_value=paid))
        aliases = layout.translation_numbers.parse_unit_aliases('million metric tons (MT)')
        cached = self.cache(aliases=aliases, retry_rejected=True)
        call = Mock(side_effect=AssertionError('已付费等价译文不能再次请求模型'))
        self.assertEqual(cached.resolve(req, call), paid)
        call.assert_not_called()
        self.assertEqual((cached.received, cached.done, cached.reused, cached.model_calls), (1, 1, 1, 0))
        self.assertEqual(cached.review_pending, 0)

    def test_previously_rejected_numeric_change_is_reused_with_review_without_model(self):
        req = request('Source 20.')
        paid = response('译文 21。')
        with patch.object(layout, 'assess_translation', return_value={'error': '旧数字比较失败', 'warnings': []}):
            with self.assertRaises(ValueError):
                self.cache().resolve(req, Mock(return_value=paid))
        path = next((self.folder / 'native-replies').glob('*.json'))
        original = path.read_bytes()
        cached = self.cache(retry_rejected=True)
        call = Mock(side_effect=AssertionError('数字核对告警不得重复付费'))
        self.assertEqual(cached.resolve(req, call), paid)
        call.assert_not_called()
        self.assertEqual((cached.done, cached.reused, cached.review_pending, cached.rejected, cached.model_calls),
                         (1, 1, 1, 0, 0))
        self.assertTrue(cached.warnings)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.folder / 'native-replies-rejected').exists())

    def test_unknown_quantity_retains_review_and_never_triggers_automatic_retranslation(self):
        req = request('90 MT')
        paid = response('9000万吨')
        self.cache().resolve(req, Mock(return_value=paid))
        cached = self.cache(retry_rejected=True)
        call = Mock(side_effect=AssertionError('不确定表达式不能盲目重译'))
        self.assertEqual(cached.resolve(req, call), paid)
        call.assert_not_called()
        self.assertEqual((cached.done, cached.review_pending, cached.rejected, cached.model_calls), (1, 1, 0, 0))
        review = layout._read_json(next((self.folder / 'numeric-reviews').glob('*.json')), [])
        self.assertEqual(review[0]['source'], '90 MT')
        self.assertEqual(review[0]['translation'], '9000万吨')
        self.assertTrue(cached.warnings)

    def test_chinese_adjacent_identifier_reuses_rejected_response_without_model(self):
        req = request('Ford F-150 and AA6005 alloy; alloy AA6061.')
        paid = response('福特F-150与AA6005合金；铝合金AA6061。')
        with patch.object(layout, 'assess_translation', return_value={'error': '旧编号边界误报', 'warnings': []}):
            with self.assertRaises(ValueError):
                self.cache().resolve(req, Mock(return_value=paid))
        cached = self.cache(retry_rejected=True)
        call = Mock(side_effect=AssertionError('中文边界误报不得重复付费'))
        self.assertEqual(cached.resolve(req, call), paid)
        call.assert_not_called()
        self.assertEqual((cached.reused, cached.done, cached.rejected, cached.model_calls), (1, 1, 0, 0))

    def test_invalid_batch_reports_received_units_separately_from_accepted_units(self):
        req = request()
        req['prompt'] = native.BATCH_INPUT + json.dumps([
            {'id': 0, 'input': '90 {v0}'}, {'id': 1, 'input': '3'}])
        cached = self.cache()
        with self.assertRaises(native.ResponseValidationError):
            cached.resolve(req, Mock(return_value=json.dumps([
                {'id': 0, 'output': '90'}, {'id': 1, 'output': '3'}])))
        self.assertEqual((cached.received, cached.done, cached.rejected, cached.model_calls), (2, 0, 1, 1))

    def test_explicit_retry_archives_bad_reply_once_and_preserves_successful_cached_requests(self):
        original = 'not JSON'
        with self.assertRaises(ValueError):
            self.cache().resolve(request(), Mock(return_value=original))
        successful = request('Other 3.')
        self.cache().resolve(successful, Mock(return_value=response('其他 3。')))
        call = Mock(return_value=response())
        retry = self.cache(retry_rejected=True)
        self.assertEqual(retry.resolve(request(), call), response())
        self.assertEqual(retry.resolve(request(), call), response())
        self.assertEqual(retry.resolve(successful, call), response('其他 3。'))
        call.assert_called_once()
        archived = list((self.folder / 'native-replies-rejected').glob('*/*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(layout._read_json(archived[0], {})['response'], original)
        self.assertEqual(retry.reused, 2)

    def test_failed_explicit_retry_is_bounded_per_instance_and_next_explicit_run_can_retry(self):
        with self.assertRaises(ValueError):
            self.cache().resolve(request(), Mock(return_value='not JSON'))
        call = Mock(return_value='[]')
        retry = self.cache(retry_rejected=True)
        for _ in range(3):
            with self.assertRaises(ValueError):
                retry.resolve(request(), call)
        call.assert_called_once()
        next_call = Mock(return_value=response())
        self.assertEqual(self.cache(retry_rejected=True).resolve(request(), next_call), response())
        next_call.assert_called_once()
        archived = list((self.folder / 'native-replies-rejected').glob('*/*.json'))
        self.assertEqual(len(archived), 2)
        self.assertEqual({json.loads(path.read_text(encoding='utf-8'))['response'] for path in archived},
                         {'not JSON', '[]'})

    def test_new_model_failure_is_not_automatically_retried_within_same_execution(self):
        call = Mock(return_value='not JSON')
        cache = self.cache(retry_rejected=True)
        for _ in range(3):
            with self.assertRaises(ValueError):
                cache.resolve(request(), call)
        call.assert_called_once()
        self.assertFalse((self.folder / 'native-replies-rejected').exists())

    def test_duplicate_or_invalid_input_ids_are_rejected_before_model_or_write(self):
        call = Mock(return_value=response())
        for units in ([{'id': 0, 'input': 'Source 20.'}] * 2,
                      [{'id': True, 'input': 'Source 20.'}], [{'id': 0, 'input': ' '}], []):
            value = request()
            value['prompt'] = native.BATCH_INPUT + json.dumps(units)
            with self.subTest(units=units), self.assertRaises(ValueError):
                self.cache().resolve(value, call)
        call.assert_not_called()
        self.assertFalse((self.folder / 'native-replies').exists())

    def test_duplicate_missing_or_unexpected_output_ids_are_rejected_and_saved(self):
        units = [{'id': 0, 'input': 'First 20.'}, {'id': 1, 'input': 'Second 3.'}]
        req = request()
        req['prompt'] = native.BATCH_INPUT + json.dumps(units)
        for outputs in ([{'id': 0, 'output': '第一 20。'}] * 2,
                        [{'id': 0, 'output': '第一 20。'}],
                        [{'id': 0, 'output': '第一 20。'}, {'id': 2, 'output': '第二 3。'}]):
            with self.subTest(outputs=outputs):
                call = Mock(return_value=json.dumps(outputs))
                folder = self.folder / str(len(list(self.folder.iterdir())))
                with self.assertRaises(ValueError):
                    self.cache(folder=folder).resolve(req, call)
                with self.assertRaises(ValueError):
                    self.cache(folder=folder).resolve(req, call)
                call.assert_called_once()

    def test_tampered_cache_request_cannot_reach_engine_or_model(self):
        call = Mock(return_value=response())
        self.cache().resolve(request(), call)
        path = next((self.folder / 'native-replies').glob('*.json'))
        value = layout._read_json(path, {})
        value['request']['identity']['config']['model'] = 'other-model'
        layout._write_json(path, value)
        with self.assertRaisesRegex(ValueError, '来源不匹配'):
            self.cache(retry_rejected=True).resolve(request(), call)
        call.assert_called_once()
        self.assertFalse((self.folder / 'native-replies-rejected').exists())

    def test_upstream_single_and_text_modes_preserve_exact_input(self):
        for req in ({'mode': 'llm', 'prompt': 'Rules.\n' + native.SINGLE_INPUT + 'Source 20.'},
                    {'mode': 'text', 'prompt': 'Source 20.'}):
            with self.subTest(request=req):
                call = Mock(return_value='译文 20。')
                result = self.cache().resolve(req, call)
                self.assertEqual(result, '译文 20。')
                self.assertEqual(call.call_args.args[0], req)
        with self.assertRaisesRegex(ValueError, '格式已变化'):
            self.cache().resolve({'mode': 'llm', 'prompt': 'Unknown input format'}, Mock())


if __name__ == '__main__':
    unittest.main()
