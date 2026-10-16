import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from ezread import pdf_layout_quality as quality
from pdf_tools import _PDF_LOCK


spec = importlib.util.spec_from_file_location('pdf_layout_audit',
                                            Path(__file__).parents[1] / 'scripts' / 'audit-pdf-layout.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def glyph(text, x, y=20, size=10, index=0):
    return {'text': text, 'box': [x, y, x + size * .8, y + size],
            'baseline': y, 'size': size, 'index': index}


def matrix_pdf():
    stream = b'BT /F1 1 Tf 10 0 0 10 20 80 Tm (normalization) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources '
               b'<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    payload, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload += str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(payload)
    payload += b'xref\n0 6\n0000000000 65535 f \n'
    payload += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    payload += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode()
    return payload

class PdfLayoutAuditTest(unittest.TestCase):
    def test_actual_pdf_text_matrix_physical_font_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'matrix.pdf'
            path.write_bytes(matrix_pdf())
            document = pdfium.PdfDocument(path)
            page = document[0]
            chars = audit.glyphs(page)
            self.assertEqual(''.join(g['text'] for g in chars), 'normalization')
            self.assertAlmostEqual(chars[0]['size'], 10, places=4)
            page.close()
            document.close()

    def test_cli_explicitly_reexports_shared_geometry(self):
        for name in ('glyphs', 'audit_page', 'normalize_paragraphs', 'body_groups', 'overlap_fraction', 'page_report'):
            self.assertIs(getattr(audit, name), getattr(quality, name))

    def test_warning_vs_failure_severity_and_protected_formula(self):
        source = [glyph('a', i * 8, index=i) for i in range(25)]
        paragraphs = [{'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 220, 31]}]
        moderate = [glyph('中', i * 9, size=6.5, index=i) for i in range(20)]
        failures, warnings = quality.classify(quality.audit_page(source, moderate, paragraphs, []), page=1)
        self.assertEqual(failures, [])
        self.assertEqual([x['kind'] for x in warnings], ['tiny_body_font'])
        severe = [glyph('中', i * 9, size=4, index=i) for i in range(20)]
        failures, warnings = quality.classify(quality.audit_page(source, severe, paragraphs, []), page=1)
        self.assertEqual([x['kind'] for x in failures], ['tiny_body_font'])
        self.assertEqual(warnings, [])
        self.assertEqual(quality.audit_page(source, severe, paragraphs, [[-1, 19, 220, 31]]), [])
        failures, warnings = quality.classify([{'kind': 'source_word_split'}], page=1)
        self.assertEqual(failures, [])
        self.assertEqual(warnings[0]['kind'], 'source_word_split')

    def test_short_source_does_not_disable_mixed_overlap_gate(self):
        source = [glyph('a', i * 8, index=i) for i in range(4)]
        chinese = [glyph('中', i * 9, size=9, index=i) for i in range(3)]
        math = [glyph(char, 2 + i * 9, y=22, size=3, index=100 + i)
                for i, char in enumerate('πΔσ')]
        paragraphs = [{'layout': 'plain text', 'text': 'aaaa', 'bbox': [-1, 19, 50, 31]}]
        failures, _ = quality.classify(quality.audit_page(source, chinese + math, paragraphs, []), page=1)
        self.assertEqual([x['kind'] for x in failures], ['overlapping_body_math_or_latin'])
        # Two Chinese characters can still suffer three distinct math collisions.
        math[2]['box'] = list(math[1]['box'])
        failures, _ = quality.classify(quality.audit_page(source, chinese[:2] + math, paragraphs, []), page=1)
        self.assertEqual([x['kind'] for x in failures], ['overlapping_body_math_or_latin'])

    def test_inspect_paths_arrays_coverage_lock_cancellation_and_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'matrix.pdf'
            path.write_bytes(matrix_pdf())
            metadata = Path(temporary) / 'paragraphs.json'
            paragraphs = [{'page': 1, 'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 200, 31]}]
            metadata.write_text(json.dumps(paragraphs), encoding='utf-8')
            source = [glyph('a', i * 8, index=i) for i in range(25)]
            chinese = [glyph('中', i * 9, size=9, index=i) for i in range(20)]
            rows = iter([source, chinese])
            def locked_glyphs(page):
                self.assertTrue(_PDF_LOCK._is_owned())
                return next(rows)
            with patch.object(quality, 'glyphs', side_effect=locked_glyphs):
                result = quality.inspect(path, path, metadata, [])
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['failures'], [])
            self.assertEqual(result['coverage']['assessable_pages'], [1])
            json.dumps(result)
            self.assertEqual({p.name for p in Path(temporary).iterdir()}, {'matrix.pdf', 'paragraphs.json'})
            math = [glyph(char, 2 + i * 9, y=22, size=3, index=100 + i)
                    for i, char in enumerate('πΔσ')]
            rows = iter([source, chinese + math])
            with patch.object(quality, 'glyphs', side_effect=locked_glyphs):
                blocked = quality.inspect(path, path, paragraphs, [])
            self.assertEqual(blocked['status'], 'failed')
            self.assertEqual(blocked['failures'][0]['kind'], 'overlapping_body_math_or_latin')
            self.assertIn('重叠', blocked['failures'][0]['message'])
            # Real PDFium extraction contains only English, so it cannot claim Chinese QA.
            unassessed = quality.inspect(path, path, paragraphs, [])
            self.assertEqual(unassessed['status'], 'needs_review')
            self.assertEqual(unassessed['failures'], [])
            self.assertEqual(unassessed['coverage']['unassessable_pages'], [1])
            event = threading.Event()
            event.set()
            with self.assertRaises(InterruptedError):
                quality.inspect(path, path, paragraphs, [], cancel_event=event)
            self.assertFalse(_PDF_LOCK._is_owned())
            with self.assertRaises(ValueError):
                quality.inspect(path, path, {}, [])
            with self.assertRaises(FileNotFoundError):
                quality.inspect(path, path, Path(temporary) / 'missing-audit.json', [])
            different_size = Path(temporary) / 'different.pdf'
            different_size.write_bytes(matrix_pdf().replace(b'[0 0 200 100]', b'[0 0 210 100]'))
            with self.assertRaises(ValueError):
                quality.inspect(path, different_size, paragraphs, [])

    def test_split_source_word_and_scrambled_letter_order(self):
        source = [glyph(c, i * 8, index=i) for i, c in enumerate('normalization')]
        paragraphs = [{'layout': 'plain text', 'text': 'normali', 'bbox': [-1, 19, 56, 31]},
                      {'layout': 'fallback_line', 'text': 'zation', 'bbox': [56, 19, 110, 31]}]
        self.assertIn('source_word_split', {f['kind'] for f in audit.audit_page(source, [], paragraphs, [])})
        source = [glyph(c, i * 8, index=i) for i, c in enumerate('abcdefghijklmnopqrstuvwxyz')]
        paragraphs = [{'layout': 'plain text', 'text': 'zyxwvutsrqponmlkjihgfedcba', 'bbox': [-1, 19, 220, 31]}]
        self.assertIn('source_text_order_mismatch',
                      {f['kind'] for f in audit.audit_page(source, [], paragraphs, [])})

    def test_tiny_overlapping_body_and_legal_formula(self):
        source = [glyph('a', i * 8, index=i) for i in range(25)]
        translated = [glyph('中', i * 1.5, size=3, index=i) for i in range(25)]
        paragraphs = [{'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 220, 31]}]
        kinds = {f['kind'] for f in audit.audit_page(source, translated, paragraphs, [])}
        self.assertIn('tiny_body_font', kinds)
        self.assertIn('overlapping_body_glyphs', kinds)
        self.assertIn('tiny_body_font', {f['kind'] for f in audit.audit_page(
            source, translated[:3], paragraphs, [])})
        # Formula/figure/table regions, including tiny subscripts, are explicitly protected.
        self.assertEqual(audit.audit_page(source, translated, paragraphs, [[-1, 19, 220, 31]]), [])

    def test_normal_body_is_clear(self):
        source = [glyph('a', i * 8, index=i) for i in range(25)]
        translated = [glyph('中', i * 9, size=9, index=i) for i in range(20)]
        paragraphs = [{'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 220, 31]}]
        self.assertEqual(audit.audit_page(source, translated, paragraphs, []), [])

    def test_actual_input_audit_glyph_boxes_and_formula_placeholders(self):
        word = 'abcdefghijklmnopqrstuvwxyz'
        source = [glyph(c, i * 8, index=i) for i, c in enumerate(word)]
        paragraph = {'page': 1, 'layout': 'plain text', 'text': word,
                     'input': word[:13] + '<b1>' + word[15:],
                     'glyphs': [{'box': g['box']} for g in source],
                     'placeholders': [{'glyphs': [{'box': g['box']} for g in source[13:15]]}]}
        self.assertEqual(audit.audit_page(source, [], [paragraph], []), [])
        self.assertEqual(len(audit.normalize_paragraphs([paragraph, paragraph, {**paragraph, 'input': None}])), 1)

    def test_protected_restore_cannot_remove_adjacent_body_text(self):
        before = [glyph('经', 10), glyph('过', 20), glyph('这', 30)]
        paragraphs = [{'layout': 'plain text', 'text': 'source', 'bbox': [0, 19, 50, 31]}]
        self.assertEqual(audit.removed_body_glyphs(before, before, paragraphs), [])
        missing = audit.removed_body_glyphs(before, before[2:], paragraphs)
        self.assertEqual([g['text'] for g in missing], ['经', '过'])
        # Non-body figure/label glyphs are outside this check's scope.
        self.assertEqual(audit.removed_body_glyphs(before, [], []), [])
        caption = [{'layout': 'table_caption', 'text': 'Table 3', 'bbox': [0, 19, 50, 31]}]
        self.assertEqual(len(audit.removed_body_glyphs(before, [], caption)), 3)

    def test_surviving_text_object_with_whitened_strokes_is_not_clear(self):
        before = Image.new('RGB', (100, 100), 'white')
        ImageDraw.Draw(before).rectangle([20, 40, 35, 59], fill='black')
        after = before.copy()
        ImageDraw.Draw(after).rectangle([20, 40, 35, 50], fill='white')
        chars = [glyph('中', 10)]
        paragraphs = [{'layout': 'plain text', 'text': 'source', 'bbox': [0, 19, 50, 31]}]
        args = (chars, chars, paragraphs, [[9, 24, 19, 32]], 50)
        self.assertEqual(audit.body_ink_loss(before, before, *args), [])
        self.assertEqual(len(audit.body_ink_loss(before, after, *args)), 1)

    def test_small_formula_word_split_is_not_body_cut_word(self):
        source = [glyph('a', i * 8, index=i) for i in range(25)]
        source += [glyph(c, 20 + i * 4, y=5, size=5, index=100 + i)
                   for i, c in enumerate('average')]
        paragraphs = [{'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 220, 31]},
                      {'layout': 'fallback_line', 'text': 'aver', 'bbox': [19, 4, 35, 11]},
                      {'layout': 'fallback_line', 'text': 'age', 'bbox': [35, 4, 49, 11]}]
        self.assertEqual(audit.audit_page(source, [], paragraphs, []), [])

    def test_inline_formula_subscripts_must_not_overlap_chinese(self):
        source = [glyph('a', i * 8, index=i) for i in range(25)]
        chinese = [glyph('中', i * 9, size=9, index=i) for i in range(20)]
        math = [glyph(char, 2 + i * 9, y=22, size=3, index=100 + i)
                for i, char in enumerate('πΔσ')]
        paragraphs = [{'layout': 'plain text', 'text': 'a' * 25, 'bbox': [-1, 19, 220, 31]}]
        self.assertIn('overlapping_body_math_or_latin', {f['kind'] for f in audit.audit_page(
            source, chinese + math, paragraphs, [])})
        for g in math:
            g['box'][0] += 200
            g['box'][2] += 200
        self.assertEqual(audit.audit_page(source, chinese + math, paragraphs, []), [])


if __name__ == '__main__':
    unittest.main()
