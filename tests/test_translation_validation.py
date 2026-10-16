"""共享标记和严格 JSON 边界，不涉及模型或真实文献。"""
import unittest

from ezread.translation_validation import MARKERS, emptied_styles, load_response_json, markers_match, native_marker_assessment


class TranslationValidationTests(unittest.TestCase):
    def test_duplicate_keys_and_nonstandard_numbers_are_not_silently_overwritten(self):
        for text in ('{"id":999,"id":0}', '[{"id":0,"output":"a","output":"b"}]',
                     '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_response_json(text)
        self.assertEqual(load_response_json('[{"id":0,"output":"译文"}]'), [{'id': 0, 'output': '译文'}])

    def test_formula_singletons_and_whole_style_pairs_can_reorder(self):
        for source, target in [('{v0} with {v1}', '{v1}与{v0}'),
                               ('<b1>Al<b3>alloy</b3>', '<b3>合金</b3><b1>'),
                               ('<b0>A</b0><b1>B</b1>', '<b1>乙</b1><b0>甲</b0>'),
                               ('<b0><b1>A</b1>B</b0>', '<b0>乙<b1>甲</b1></b0>'),
                               ('<b1>{v0}</b1>', '<b1>{v0}</b1>')]:
            with self.subTest(source=source, target=target):
                self.assertTrue(markers_match(source, target))

    def test_crossed_reversed_and_changed_nesting_are_rejected(self):
        for source, target in [('<b0>A</b0><b1>B</b1>', '<b0>甲<b1>乙</b0></b1>'),
                               ('<b0>A</b0>', '</b0>甲<b0>'),
                               ('<b0><b1>A</b1></b0>', '<b1><b0>甲</b0></b1>'),
                               ('Formula <b2>', '公式'),
                               ('Plain text', '文本 {v0}')]:
            with self.subTest(source=source, target=target):
                self.assertFalse(markers_match(source, target))

    def test_empty_font_fragments_are_reviewable_not_structurally_invalid(self):
        source, target = '<b0>fi</b0><b1>cation</b1>', '<b0>化合</b0><b1></b1>'
        self.assertTrue(markers_match(source, target))
        self.assertEqual(emptied_styles(source, target), ['1'])
        self.assertEqual(emptied_styles('<b0> </b0>', '<b0></b0>'), [])

    def test_literal_invalid_source_is_not_misread_as_generated_styles(self):
        source = '</b0>literal<b0>'
        self.assertTrue(markers_match(source, '</b0>字面标记<b0>'))
        self.assertFalse(markers_match(source, '<b0>字面标记</b0>'))

    def test_standard_styles_and_mixed_legacy_markers_can_reorder(self):
        for source, target in [
            ("<style id='0'>A</style><style id='1'>B</style>",
             "<style id='1'>乙</style><style id='0'>甲</style>"),
            ("<style id='0'><style id='1'>A</style>B</style>",
             "<style id='0'>乙<style id='1'>甲</style></style>"),
            ("<style id='2'>A {v0}<b3>B</b3></style> {v1}<b4>",
             "{v1}<b4><style id='2'><b3>乙</b3>{v0}甲</style>"),
            ("<style id='12'>A {v0}</style>",
             "< STYLE ID = ' 12 ' >甲{ V 0 }< / STYLE >"),
        ]:
            with self.subTest(source=source, target=target):
                self.assertTrue(markers_match(source, target))

    def test_standard_styles_reject_missing_extra_duplicate_and_changed_structure(self):
        source = "<style id='0'>A</style><style id='1'>B</style>{v0}"
        for target in [
            "<style id='0'>甲</style><style id='1'>乙{v0}",
            "<style id='0'>甲</style><style id='1'>乙</style></style>{v0}",
            "<style id='0'>甲</style><style id='9'>乙</style>{v0}",
            source + "<style id='9'>新增</style>",
            source + "<style id='0'>重复</style>",
            source + '{v0}',
            source.replace('{v0}', ''),
            "<style id='0'>甲<style id='1'>乙</style></style>{v0}",
            "</style><style id='0'>甲<style id='1'>乙</style>{v0}",
            source.replace("id='0'", 'id="0"'),
        ]:
            with self.subTest(target=target):
                self.assertFalse(markers_match(source, target))
        self.assertFalse(markers_match(
            "<style id='0'><style id='1'>A</style></style>",
            "<style id='1'><style id='0'>甲</style></style>"))
        self.assertFalse(markers_match(
            "<style id='0'><b1>A</b1></style>",
            "<style id='0'><b1>甲</style></b1>"))
        self.assertFalse(markers_match('Plain text', "<style id='9'>文本</style>"))

    def test_standard_empty_styles_remain_review_warnings(self):
        source = "<style id='0'>fi</style><style id='1'>cation</style>"
        target = "< STYLE ID = ' 1 ' > </ STYLE ><style id='0'>化合</style>"
        self.assertTrue(markers_match(source, target))
        self.assertEqual(emptied_styles(source, target), ['1'])
        self.assertEqual(emptied_styles("<style id='0'> </style>", "<style id='0'></style>"), [])

    def test_public_marker_regex_returns_strings_and_respects_literal_boundaries(self):
        text = "< STYLE ID = ' 2 ' >{ V 1 }</ STYLE ><b3></b3>"
        self.assertEqual(MARKERS.findall(text),
                         ["< STYLE ID = ' 2 ' >", '{ V 1 }', '</ STYLE >', '<b3>', '</b3>'])
        self.assertIsNotNone(MARKERS.search("<style id='9'>"))
        for literal in ('{value}', '{v1x}', "<style id='x'>", '<style id="1">', '<b1x>'):
            with self.subTest(literal=literal):
                self.assertIsNone(MARKERS.search(literal))
        source = '</b0>literal<b0> {v0}'
        self.assertTrue(markers_match(source, '{v0} </b0>字面<b0>'))
        self.assertFalse(markers_match(source, "</b0>字面<b0> {v0}<style id='9'></style>"))

    def test_native_style_degradation_keeps_formula_and_legacy_contract_strict(self):
        source = "<style id='6'>Fig. 9</style><style id='8'>strength {v3}</style> {v10}<b2>"
        target = '图 9 强度 {v3} {v10}<b2>'
        self.assertFalse(markers_match(source, target))
        assessment = native_marker_assessment(source, target)
        self.assertEqual(assessment['error'], '')
        self.assertTrue(assessment['style_degraded'])
        self.assertIn('字体样式已降级', assessment['warnings'][0])
        self.assertEqual(native_marker_assessment(source, source),
                         {'error': '', 'warnings': [], 'style_degraded': False})
        for bad in (target.replace('{v3}', ''), target + '{v3}', target + '{v99}',
                    target.replace('<b2>', ''), target + '<b3>',
                    target + "<style id='6'>", target + '</style>',
                    target + "<style id='99'>未知</style>",
                    target + "<style id='6'>甲</style><style id='6'>重复</style>"):
            with self.subTest(target=bad):
                self.assertTrue(native_marker_assessment(source, bad)['error'])

    def test_native_retained_styles_must_be_balanced_and_known_but_may_move(self):
        source = "<style id='0'>A<style id='1'>B</style></style>{v0}"
        for target in ("<style id='1'>乙</style>甲{v0}",
                       "<style id='1'>乙</style><style id='0'>甲</style>{v0}",
                       "< STYLE ID = ' 0 '>甲{ V 0 }</ STYLE >"):
            with self.subTest(target=target):
                assessment = native_marker_assessment(source, target)
                self.assertFalse(assessment['error'])
                self.assertTrue(assessment['style_degraded'])
        for source, target in (("<style id='0'><b1>A</b1></style>{v0}",
                                "<style id='0'><b1>甲</style></b1>{v0}"),
                               ('<b0>A</b0><b1>B</b1>{v0}', '<b0>甲<b1>乙</b0></b1>{v0}'),
                               ('<b0><b1>A</b1></b0>{v0}', '<b1><b0>甲</b0></b1>{v0}')):
            with self.subTest(source=source, target=target):
                self.assertTrue(native_marker_assessment(source, target)['error'])

    def test_native_rejects_new_unrecognized_or_partial_style_residue(self):
        source = "<style id='6'>A</style>{v3}"
        for residue in ('<style id="6">', '<style id=6>', '<style id="6"',
                        '< STYLE ID = 6', '</style extra>', '</style', '<style'):
            with self.subTest(residue=residue):
                self.assertTrue(native_marker_assessment(source, '甲{v3}' + residue)['error'])
        self.assertFalse(native_marker_assessment(source, '甲{v3}')['error'])

    def test_native_preserves_existing_literal_unknown_style_tags_without_adding_copies(self):
        for literal in ('<style id="6">', '<style id=6>', '<style id="6"',
                        '</style extra>', '<style id="6">literal</style>'):
            source, target = 'Source {v3}' + literal, '译文 {v3}' + literal
            with self.subTest(literal=literal):
                self.assertEqual(native_marker_assessment(source, target),
                                 {'error': '', 'warnings': [], 'style_degraded': False})
                self.assertTrue(native_marker_assessment(source, target + literal)['error'])
                if '6' in literal:
                    self.assertTrue(native_marker_assessment(source, target.replace('6', '7'))['error'])

    def test_native_invalid_literal_standard_source_requires_strict_original_marker_sequence(self):
        source = "Code <style id='6'> {v3}"
        self.assertEqual(native_marker_assessment(source, "代码 <style id='6'> {v3}"),
                         {'error': '', 'warnings': [], 'style_degraded': False})
        for target in ('代码 {v3}', "代码 <style id='7'> {v3}",
                       "代码 <style id='6'></style> {v3}"):
            with self.subTest(target=target):
                self.assertTrue(native_marker_assessment(source, target)['error'])


if __name__ == '__main__':
    unittest.main()
