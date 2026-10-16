"""数字及物理量归一的离线回归。"""
from decimal import Decimal
import unittest

from ezread.translation_numbers import assess_numbers, parse_unit_aliases


class TranslationNumbersTests(unittest.TestCase):
    def assert_equivalent(self, source, target, aliases=None):
        result = assess_numbers(source, target, aliases)
        self.assertEqual(result, {'error': '', 'warnings': []}, result)

    def test_equivalent_units_and_scales(self):
        for source, target in [('90 million metric tons', '9000万吨'),
                               ('5 kg', '5000克'), ('1 metric ton', '1000 kg'),
                               ('50 mm', '0.05 m'), ('2 µm', '2000 nm'),
                               ('0.5 GPa', '500 MPa'), ('50 percent', '50%'),
                               ('50%', '百分之50'), ('50%', '0.5'),
                               ('90 million', '9000万')]:
            with self.subTest(source=source):
                self.assert_equivalent(source, target)
        self.assert_equivalent('7–9 million metric tons', '700–900万吨')
        self.assertTrue(assess_numbers('7–9 million metric tons', '700–901万吨')['error'])

    def test_values_dimensions_and_bare_numbers_reject(self):
        for source, target in [('90', '91'), ('5 kg', '6 kg'), ('5 kg', '500 g'),
                               ('5 kg', '5 m'), ('50%', '0.5%'),
                               ('0.5 GPa', '0.5 MPa'), ('5 kg and 3 kg', '5千克')]:
            with self.subTest(source=source):
                self.assertTrue(assess_numbers(source, target)['error'])

    def test_explicit_aliases_and_conflicts(self):
        aliases = parse_unit_aliases('Production in million metric tons (MT).')
        self.assertEqual(aliases['MT'], ('mass', Decimal('1000000000')))
        self.assertEqual(parse_unit_aliases('Million metric tons (MT)'), aliases)
        self.assertIsNone(parse_unit_aliases('trillion metric tons (MT)')['MT'])
        self.assert_equivalent('90 MT', '9000万吨', aliases)
        self.assertTrue(assess_numbers('90 MT', '9001万吨', aliases)['error'])
        conflicts = parse_unit_aliases('million metric tons (MT); metric tons (MT).')
        self.assertIsNone(conflicts['MT'])
        self.assertTrue(assess_numbers('90 MT', '9000万吨', conflicts)['warnings'])
        self.assertEqual(parse_unit_aliases('MT is not defined here.'), {})
        self.assertIsNone(parse_unit_aliases('kilograms (m)')['m'])

    def test_unknown_units_and_expressions_warn_without_retry(self):
        for source, target in [('90 MT', '9000万吨'), ('2 furlongs', '402.336 m'),
                               ('5 kg/m3', '5 kg/m3'), ('5 × 10^x', '0.005'),
                               ('5 foo', '6 bar'), ('5 m²', '5平方米'), ('5 kg m^-3', '5 kg m^-3'),
                               ('5英尺', '1.524 m'), ('5×10^1000000000', '0.005'),
                               ('5e999999999 kg', '5 kg'), ('5e-999999999 kg', '0 kg')]:
            with self.subTest(source=source):
                result = assess_numbers(source, target)
                self.assertFalse(result['error'], result)
                self.assertTrue(result['warnings'], result)
        result = assess_numbers('5 kg; 2 foo', '6 kg; 2 foo')
        self.assertTrue(result['error'])
        self.assertTrue(assess_numbers('90; 2 foo', '91; 2 foo')['error'])
        self.assertTrue(assess_numbers('5 kg; 2 foo', '5 m; 2 foo')['error'])
        self.assertTrue(assess_numbers('Value 90, flux 2 XYZ.', '数值91，通量2 XYZ。')['error'])

    def test_scientific_notation(self):
        for source in ['5×10⁻³', '5e-3', '5 × 10^-3', '0.005']:
            with self.subTest(source=source):
                self.assert_equivalent(source, '0.005')
                self.assert_equivalent(source + ' kg', '5 g')
        self.assert_equivalent('−2.5e−3', '-0.0025')
        self.assertTrue(assess_numbers('5×10⁻³', '0.05')['error'])

    def test_markers_citations_and_identifiers(self):
        self.assert_equivalent('AA6061: 5 kg <b12>x</b12> {v8}', 'AA6061: 5000 g')
        self.assertTrue(assess_numbers('Text 20 with <b0>formula</b0>.', '文本 21 <b0>公式</b0>.')['error'])
        self.assert_equivalent('See [18, 19].', '见 [18,19]。')
        self.assert_equivalent('See [1–3].', '见 [1-3]。')
        for source, target in [('AA6061', 'AA6062'),
                               ('[18,19]', '[18,20]'), ('[18-20]', '[18-21]')]:
            with self.subTest(source=source):
                self.assertTrue(assess_numbers(source, target)['error'])

    def test_identifiers_adjacent_to_chinese_and_uncertain_typography(self):
        for source, target in [('Ford F-150', '福特F-150的'), ('AA6005 alloy', '对AA6005合金'),
                               ('alloy AA6061', '铝合金AA6061应变'), ('AA6061', '铝合金AA6061'),
                               ('铝合金AA6061', 'AA6061 alloy')]:
            with self.subTest(source=source, target=target):
                self.assert_equivalent(source, target)
        for source, target in [('F-150', '福特F150'), ('AA6061', '6.061e3')]:
            result = assess_numbers(source, target)
            self.assertFalse(result['error'], result)
            self.assertTrue(result['warnings'])
        for source, target in [('AA6061', '铝合金AA6062'), ('F-150; 90', '福特F150；91'),
                               ('F-150; 5 kg', '福特F150；6 kg')]:
            self.assertTrue(assess_numbers(source, target)['error'])

    def test_months_written_numbers_and_numeric_formats(self):
        for source, target in [('twenty-one specimens in May 2026.', '2026 年 5 月的 21 个试样。'),
                               ('Received 12 January 2026.', '2026 年 1 月 12 日收到。'),
                               ('Load 12,000 cycles [18, 19].', '加载 12000 周次 [18,19]。'),
                               ('A coefficient of .5 and −2.5e−3.', '系数 0.5 和 -0.0025。'),
                               ('See Fig.1 and Fig.2.', '详见图1和图2。')]:
            with self.subTest(source=source):
                self.assertFalse(assess_numbers(source, target)['error'])
        self.assertTrue(assess_numbers('Received 12 January 2026.', '2026 年 2 月 12 日收到。')['error'])
        for source, target in [('Values −5 and 7.', '数值 5 和 7。'),
                               ('one material with 20 samples.', '一种材料有 21 个样品。'),
                               ('Split into 8 subsets, train on 7 folds and validate on the remaining one.',
                                '分成 8 个子集，使用 7 折训练，剩余 2 折验证。'),
                               ('The Al7075 material takes 8.4 h using 480 processors.',
                                '使用 480 个处理器时，Al7075 材料耗时 8.5 h。')]:
            with self.subTest(source=source):
                self.assertTrue(assess_numbers(source, target)['error'])


if __name__ == '__main__':
    unittest.main()
