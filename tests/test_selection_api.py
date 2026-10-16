"""API 选区与段落翻译仅携带本轮、本篇相关术语。"""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_bridge import TranslationError
from ezread import selection_api as api

CONFIG = {'provider': 'deepseek', 'base_url': 'https://example.invalid',
          'model': 'synthetic-model', 'max_output_tokens': 8192}


class SelectionApiTests(unittest.TestCase):
    def setUp(self):
        self.pool = Mock()
        self.app = SimpleNamespace(DATA=Path('unused'), SHUTDOWN=threading.Event(), AI_API=self.pool)
        self.app.get_doc = Mock(side_effect=lambda pid: {'pdf_layout_translation': {'glossary': {'entries': [
            {'source': 'Al', 'target': '铝' if pid == 'first' else '另一论文译法'},
            {'source': 'steel', 'target': '钢'}, None]}}})
        for patched in (patch('ezread.api_settings.resolve_config', return_value=CONFIG),
                        patch('ezread.api_settings.get_api_key', return_value='synthetic-key'),
                        patch('codex_bridge.translate_selection', side_effect=AssertionError('不得调用 CLI'))):
            patched.start()
            self.addCleanup(patched.stop)

    def test_requests_only_include_current_text_and_current_paper_terms(self):
        self.pool.request_json.return_value = {'translation': '译文'}
        for pid, expected in (('first', '铝'), ('second', '另一论文译法')):
            api.translate_selection(self.app, 'Al alloy', paper_id=pid)
            call = self.pool.request_json.call_args
            self.assertEqual(call.args[:3], ('selection', CONFIG, 'synthetic-key'))
            self.assertEqual(json.loads(call.args[4]), {
                'selected_text': 'Al alloy', 'terms': [{'source': 'Al', 'target': expected}]})
        api.translate_selection(self.app, 'steel')
        self.assertEqual(json.loads(self.pool.request_json.call_args.args[4])['terms'], [])

    def test_language_and_placeholder_contract(self):
        self.pool.request_json.return_value = {'translation': 'English <b0>text</b0> {v0}'}
        api.translate_selection(self.app, '中文 <b0>文字</b0> {v0}', target_language='en')
        self.assertIn('学术英语', self.pool.request_json.call_args.args[3])
        for bad in ({}, {'translation': ''}, {'translation': '丢失占位符'}, {'translation': '译文', 'extra': 'x'}):
            self.pool.request_json.return_value = bad
            with self.subTest(bad=bad), self.assertRaises(TranslationError):
                api.translate_selection(self.app, '<b0>text</b0> {v0}')

    def test_cancel_before_and_after_response_and_local_configuration_error(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(TranslationError) as caught:
            api.translate_selection(self.app, 'text', cancel_event=event)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.pool.request_json.assert_not_called()
        event.clear()
        self.pool.request_json.side_effect = lambda *args, **kwargs: event.set() or {'translation': '晚返回'}
        with self.assertRaises(TranslationError):
            api.translate_selection(self.app, 'text', cancel_event=event)
        event.clear()
        with patch('ezread.api_settings.get_api_key', side_effect=ValueError('未保存密钥')):
            with self.assertRaises(TranslationError) as caught:
                api.translate_selection(self.app, 'text')
        self.assertEqual(caught.exception.code, 'configuration')

    def test_blocks_require_exact_ids_nonempty_values_and_markers(self):
        blocks = [{'id': 'first', 'text': 'Al <b0>alloy</b0>'}, {'id': 'second', 'text': 'Steel'}]
        self.pool.request_json.return_value = {'first': '铝<b0>合金</b0>', 'second': '钢'}
        api.translate_blocks(self.app, blocks, paper_id='first')
        self.assertEqual(self.pool.request_json.call_args.args[0], 'retranslate')
        self.assertEqual(json.loads(self.pool.request_json.call_args.args[4])['blocks'], blocks)
        for bad in ({'first': '铝'}, {'first': '铝', 'second': '钢'},
                    {'first': '铝<b0>合金</b0>', 'second': ''}, {'foreign': '其他'}):
            self.pool.request_json.return_value = bad
            with self.subTest(bad=bad), self.assertRaises(TranslationError):
                api.translate_blocks(self.app, blocks)


    def test_same_count_but_crossed_or_reversed_styles_are_rejected(self):
        source = '<b0>source</b0><b1>other</b1>'
        for value in ('<b0>甲<b1>乙</b0></b1>', '</b0>甲<b0><b1>乙</b1>'):
            self.pool.request_json.return_value = {'translation': value}
            with self.subTest(value=value), self.assertRaises(TranslationError):
                api.translate_selection(self.app, source)
        self.pool.request_json.return_value = {'translation': '<b1>乙</b1><b0>甲</b0>'}
        self.assertEqual(api.translate_selection(self.app, source), '<b1>乙</b1><b0>甲</b0>')


if __name__ == '__main__':
    unittest.main()
