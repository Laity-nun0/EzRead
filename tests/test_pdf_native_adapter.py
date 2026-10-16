"""标准 BabelDOC 提交边界与回填失败的离线回归。"""
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import pdf_layout_engine as engine
from ezread.pdf_native_adapter import _apply_translation

ROOT = Path(__file__).resolve().parents[1]


class NativeAdapterTests(unittest.TestCase):
    def fixture(self, source="Source {v1}"):
        paragraph = NS(unicode=source, pdf_paragraph_composition=['original'])
        attempt = NS(placeholder_full_match=False, has_error=False)
        attempt.set_placeholder_full_match = lambda: setattr(attempt, 'placeholder_full_match', True)
        tracker = NS(output=None, removed_hallucinated_placeholders={},
                     last_llm_translate_tracker=lambda: attempt,
                     set_output=lambda value: setattr(tracker, 'output', value))
        prepared = NS(unicode=source, placeholders=[], base_style=None)
        def post(translator, paragraph, tracker, prepared, text):
            paragraph.unicode = text
            paragraph.pdf_paragraph_composition = [NS(pdf_formula=None, pdf_same_style_characters=None,
                pdf_same_style_unicode_characters=NS(unicode=re.sub(r"</?style\b[^>]*>", '', text)))]
            tracker.set_output(text)
            attempt.set_placeholder_full_match()
        return paragraph, tracker, prepared, attempt, Mock(side_effect=post)

    def test_standard_post_is_used_and_success_is_committed(self):
        paragraph, tracker, prepared, attempt, post = self.fixture()
        _apply_translation(post, None, paragraph, tracker, prepared, '译文 {v1}')
        post.assert_called_once()
        self.assertEqual(paragraph.unicode, '译文 {v1}')
        self.assertEqual(tracker.output, paragraph.unicode)
        self.assertTrue(attempt.placeholder_full_match)

    def test_bad_markers_never_reach_postprocessing(self):
        for output in ('', '译文', '译文 {v1} {v1}', '译文 {v2}'):
            paragraph, tracker, prepared, _, post = self.fixture()
            with self.subTest(output=output), self.assertRaises(ValueError):
                _apply_translation(post, None, paragraph, tracker, prepared, output)
            post.assert_not_called()
            self.assertEqual(paragraph.unicode, prepared.unicode)
            self.assertIsNone(tracker.output)

    def test_parse_exception_or_partial_parse_rolls_back_every_mutation(self):
        for failure in ('exception', 'composition', 'placeholder', 'removed'):
            paragraph, tracker, prepared, attempt, post = self.fixture()
            original = paragraph.pdf_paragraph_composition
            apply = post.side_effect
            def incomplete(*args):
                apply(*args)
                if failure == 'exception':
                    raise RuntimeError('synthetic parse failure')
                if failure == 'composition':
                    paragraph.pdf_paragraph_composition = []
                if failure == 'placeholder':
                    attempt.placeholder_full_match = False
                if failure == 'removed':
                    tracker.removed_hallucinated_placeholders = {'{v9}': 1}
            post.side_effect = incomplete
            with self.subTest(failure=failure), self.assertRaises((ValueError, RuntimeError)):
                _apply_translation(post, None, paragraph, tracker, prepared, '译文 {v1}')
            self.assertEqual(paragraph.unicode, prepared.unicode)
            self.assertIs(paragraph.pdf_paragraph_composition, original)
            self.assertIsNone(tracker.output)
            self.assertEqual(tracker.removed_hallucinated_placeholders, {})

    def test_standard_post_receives_original_prepared_and_response_including_soft_line_breaks(self):
        source = "code <style id='1'>source</style> {v2}"
        output = "代码\n<style id='1'>模拟\n译文\r\n文字</style>\n公式 {v2}"
        paragraph, tracker, prepared, _, post = self.fixture(source)
        prepared.placeholders = [NS(left_regex_pattern=r"<style id='1'>", right_regex_pattern=r'</style>',
                                    left_placeholder="<style id='1'>")]
        _apply_translation(post, None, paragraph, tracker, prepared, output)
        self.assertIs(post.call_args.args[-2], prepared)
        self.assertIs(post.call_args.args[-1], output)
        self.assertEqual(prepared.unicode, source)
        self.assertEqual(paragraph.unicode, output)
        self.assertEqual(tracker.output, output)

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_scope_keeps_original_batch_loop_and_restores_hooks(self):
        script = r'''
import socket
socket.socket.connect = lambda *args: (_ for _ in ()).throw(AssertionError('offline only'))
from types import SimpleNamespace as NS
from babeldoc.format.pdf import high_level
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from ezread.pdf_native_adapter import native_translation_scope
batch = high_level.ILTranslatorLLMOnly.translate_paragraph
post, single = ILTranslator.post_translate_paragraph, ILTranslator.translate_paragraph
with native_translation_scope([]):
    assert high_level.ILTranslatorLLMOnly.translate_paragraph is batch
    assert ILTranslator.post_translate_paragraph is not post
assert ILTranslator.post_translate_paragraph is post
assert ILTranslator.translate_paragraph is single
# 标准单段方法吞异常后的终态仍须传递失败，不能发布部分结果。
def swallowed(self, *args, **kwargs): return None
ILTranslator.translate_paragraph = swallowed
failures = []
tracker = NS(output=None, removed_hallucinated_placeholders={},
             last_llm_translate_tracker=lambda: NS(placeholder_full_match=False, has_error=True))
with native_translation_scope(failures):
    try: ILTranslator.translate_paragraph(None, None, None, None, tracker)
    except ValueError: pass
    else: raise AssertionError('failed final recovery was ignored')
assert failures
tracker.output = 'previous valid output'
tracker.ezread_apply_failed = True
tracker.last_llm_translate_tracker = lambda: NS(placeholder_full_match=True, has_error=False)
with native_translation_scope([]):
    try: ILTranslator.translate_paragraph(None, None, None, None, tracker)
    except ValueError: pass
    else: raise AssertionError('previous output masked failed application')
ILTranslator.translate_paragraph = single
'''
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if __import__('sys').platform == 'win32' else {}
        result = subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script], cwd=ROOT,
                                env=engine.runtime_environment(ROOT), capture_output=True,
                                text=True, encoding='utf-8', timeout=30, **flags)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
    def test_actual_post_preserves_text_and_formula_objects_after_style_loss(self):
        script = r'''
import re, socket
socket.socket.connect = lambda *args: (_ for _ in ()).throw(AssertionError('offline only'))
from types import SimpleNamespace as NS
from babeldoc.format.pdf.document_il import PdfFormula, PdfStyle, PdfSameStyleCharacters
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator, ParagraphTranslateTracker, FormulaPlaceholder, RichTextPlaceholder
from babeldoc.translator.translator import OpenAITranslator
from ezread.pdf_native_adapter import _apply_translation
translator = ILTranslator.__new__(ILTranslator)
tokens = OpenAITranslator.__new__(OpenAITranslator)
translator._formula_placeholder_pattern = re.compile(tokens.get_formular_placeholder(r'\d+')[1], re.I)
translator._style_left_placeholder_pattern = re.compile(tokens.get_rich_text_left_placeholder(r'\d+')[1], re.I)
translator._style_right_placeholder_pattern = re.compile(tokens.get_rich_text_right_placeholder(r'\d+')[1], re.I)
style = PdfStyle()
formulas = [FormulaPlaceholder(n, PdfFormula(), *tokens.get_formular_placeholder(n)) for n in (3, 10)]
styles = [RichTextPlaceholder(n, PdfSameStyleCharacters(pdf_style=style),
           tokens.get_rich_text_left_placeholder(n)[0], tokens.get_rich_text_right_placeholder(n)[0],
           tokens.get_rich_text_left_placeholder(n)[1], tokens.get_rich_text_right_placeholder(n)[1]) for n in (1, 4, 6, 8, 11)]
source = "<style id='1'>Source</style>{v3}<style id='4'>caption</style><style id='6'>Fig. 9</style><style id='8'>end</style>{v10}<style id='11'>tail</style>"
output = "<style id='1'>译文</style>{v3}<style id='4'>图9及说明</style>{v10}<style id='11'>结束</style>"
def fixture():
    paragraph = NS(unicode=source, pdf_style=style, pdf_paragraph_composition=['original'])
    tracker = ParagraphTranslateTracker(); tracker.new_llm_translate_tracker()
    return paragraph, tracker, ILTranslator.TranslateInput(source, [*formulas, *styles], style)
for value, degraded in ((source, False), (output, True),
                        (source.replace('Source', '模拟\n译文\r\n文字'), True),
                        (output.replace('译文', '模拟\n译文'), True)):
    paragraph, tracker, prepared = fixture()
    def standard_post(*args):
        assert args[3] is prepared and args[4] is value
        return ILTranslator.post_translate_paragraph(*args)
    _apply_translation(standard_post, translator, paragraph, tracker, prepared, value)
    assert tracker.ezread_style_degraded is degraded
    assert {id(c.pdf_formula) for c in paragraph.pdf_paragraph_composition if c.pdf_formula is not None} == {id(f.formula) for f in formulas}
    assert tracker.last_llm_translate_tracker().placeholder_full_match
    assert paragraph.unicode is value and tracker.output is value
    assert prepared.unicode == source and prepared.placeholders == [*formulas, *styles]
for value in (output.replace('{v3}', ''), output + '{v99}',
              output.replace('{v3}', '').replace('图9', '{v3}图9')):
    paragraph, tracker, prepared = fixture()
    try: _apply_translation(ILTranslator.post_translate_paragraph, translator, paragraph, tracker, prepared, value)
    except ValueError: pass
    else: raise AssertionError('formula corruption was accepted')
    assert paragraph.unicode == source and paragraph.pdf_paragraph_composition == ['original']
    assert getattr(tracker, 'output', None) is None and tracker.ezread_apply_failed
paragraph, tracker, _ = fixture()
prepared = ILTranslator.TranslateInput('Source {v3}', [formulas[0]], style)
def formula_only_mismatch(*args):
    ILTranslator.post_translate_paragraph(*args)
    tracker.last_llm_translate_tracker().placeholder_full_match = False
try: _apply_translation(formula_only_mismatch, translator, paragraph, tracker, prepared, '译文 {v3}')
except ValueError: pass
else: raise AssertionError('a failed formula-only match was promoted without real rich text placeholders')
assert paragraph.unicode == source and paragraph.pdf_paragraph_composition == ['original']
assert not tracker.last_llm_translate_tracker().placeholder_full_match
for failure in ('formula', 'text'):
    paragraph, tracker, prepared = fixture()
    def incomplete(*args):
        ILTranslator.post_translate_paragraph(*args)
        if failure == 'formula':
            paragraph.pdf_paragraph_composition = [c for c in paragraph.pdf_paragraph_composition if c.pdf_formula is None]
        else:
            next(c.pdf_same_style_unicode_characters for c in paragraph.pdf_paragraph_composition if c.pdf_same_style_unicode_characters).unicode = ''
    try: _apply_translation(incomplete, translator, paragraph, tracker, prepared, output)
    except ValueError: pass
    else: raise AssertionError('partial postprocessing was accepted')
    assert paragraph.unicode == source and paragraph.pdf_paragraph_composition == ['original']
paragraph, tracker, prepared = fixture()
prepared.unicode += "<style id='80'>literal code</style>"
prepared.set_original_placeholder_tokens({"<style id='80'>": 1, '</style>': 1})
try: _apply_translation(ILTranslator.post_translate_paragraph, translator, paragraph, tracker, prepared, output + 'literal code')
except ValueError: pass
else: raise AssertionError('literal source tag was discarded as a font wrapper')
print('standard post, verified style degradation and rollback passed')
'''
        result = subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script], cwd=ROOT,
                                env=engine.runtime_environment(ROOT), capture_output=True,
                                text=True, encoding='utf-8', timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
