"""术语的证据、边界和逐批恢复回归，不调用模型。"""
import json
import unittest
from unittest.mock import patch

from ezread import translation_glossary as glossary


def term(source='Al', target='铝'):
    return {'source': source, 'target': target}


class PaperGlossaryTests(unittest.TestCase):
    def test_native_seed_rejects_case_conflicts_and_table_delimiters_without_changing_legacy(self):
        conflicting = [term('Al', '铝'), term('AL', '另一缩写')]
        self.assertEqual(glossary.validate_seed(conflicting), conflicting)
        for entries in (conflicting, [term('A|B', '甲')], [term('Al', '甲|乙')]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                glossary.validate_native_seed(entries)
        entries = [term('Al', '铝'), term('AL', '铝'), term(' grain\n size ', '晶粒尺寸')]
        self.assertEqual(glossary.validate_native_seed(entries),
                         [term('Al', '铝'), term('AL', '铝'), term('grain size', '晶粒尺寸')])

    def test_seed_normalizes_whitespace_deduplicates_and_rejects_bad_input(self):
        self.assertEqual(glossary.validate_seed([term(' grain\n size ', ' 晶粒 尺寸 ')] * 2),
                         [term('grain size', '晶粒 尺寸')])
        for value in ({}, [None], [term('', '铝')], [term('Al', '')],
                      [term(), term('Al', '其他')], [term('x' * 161)], [term()] * 501):
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                glossary.validate_seed(value)

    def test_context_uses_exact_case_word_boundaries_and_no_stemming(self):
        table = glossary.PaperGlossary([term(), term('AL', '另一缩写'),
                                        term('grain size', '晶粒尺寸'), term('grains', '晶粒')])
        self.assertEqual(table.context([{'input': 'also al alloy grain sizes grainsize'}]), [])
        self.assertEqual(table.context([{'input': 'Al alloy grain\n size and grain.'}]),
                         [term(), term('grain size', '晶粒尺寸')])
        self.assertEqual(table.context([{'input': 'AL'}]), [term('AL', '另一缩写')])

    def test_model_terms_require_same_unit_and_never_use_prompt_rules(self):
        table = glossary.PaperGlossary()
        units = [{'id': 0, 'input': 'Al alloy'}, {'id': 1, 'input': 'Steel'}]
        response = json.dumps([{'id': 0, 'output': '铝合金'}, {'id': 1, 'output': '钢'}])
        table.accept(units, response, [term(), term('Al', '钢'), term('Instructions', '铝')])
        self.assertEqual(table.snapshot()['entries'], [{**term(), 'origin': 'model'}])
        self.assertEqual(len(table.warnings), 2)

    def test_conflict_and_invalid_terms_do_not_replace_seed_or_discard_valid_terms(self):
        table = glossary.PaperGlossary([term()])
        units = [{'id': 0, 'input': 'Al steel'}]
        table.accept(units, '铝 铝材 钢', [None, term('Al', '铝材'), term('steel', '钢')], batch=False)
        self.assertEqual(table.context(units), [term(), term('steel', '钢')])
        self.assertEqual(table.snapshot()['entries'][0]['origin'], 'user')
        table.accept(units, '铝', {'wrong': []}, batch=False)
        self.assertTrue(any('冲突' in value for value in table.warnings))
        self.assertTrue(any('格式无效' in value for value in table.warnings))

    def test_missing_existing_term_is_flagged_even_without_new_terms(self):
        table = glossary.PaperGlossary([term('grain boundary', '晶界')])
        table.accept([{'id': 0, 'input': 'grain boundary'}], '晶粒边界', [], batch=False)
        self.assertTrue(any('既定译法' in value for value in table.warnings))
        self.assertEqual(table.snapshot()['entries'][0]['target'], '晶界')

    def test_instances_and_returned_snapshots_are_isolated(self):
        first, second = glossary.PaperGlossary([term()]), glossary.PaperGlossary()
        snapshot = first.snapshot()
        snapshot['entries'][0]['target'] = '污染'
        snapshot['warnings'].append('污染')
        self.assertEqual(first.context([{'input': 'Al'}]), [term()])
        self.assertEqual(second.snapshot(), {'entries': [], 'warnings': []})
        self.assertEqual(first.warnings, [])

    def test_model_term_count_and_table_capacity_are_bounded_with_warning(self):
        with patch.object(glossary, 'MAX_ENTRIES', 2):
            table = glossary.PaperGlossary([term('a', '甲')])
            table.accept([{'id': 0, 'input': 'b c d'}], '乙 丙 丁',
                         [term('b', '乙'), term('c', '丙'), term('d', '丁')], batch=False)
        self.assertEqual(len(table.snapshot()['entries']), 2)
        self.assertTrue(any('超过' in warning for warning in table.warnings))
        self.assertTrue(any('上限' in warning for warning in table.warnings))


if __name__ == '__main__':
    unittest.main()
