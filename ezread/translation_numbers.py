"""保守核对译文中的数字和常用物理量；无法证明的换算只报告待核对。"""
from collections import Counter
from decimal import Decimal
import re
import unicodedata

from .translation_validation import MARKERS as _MARKERS


_NUMBER = re.compile(r'(?<!\d)[+-]?(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+|(?<![A-Za-z])\.\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?')
_SCIENCE = re.compile(r'([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*[×·*]\s*10\s*(?:\^\s*)?([+-]?\d+)')
# 中文可以紧邻拉丁编号；Unicode 的 \b 会把“合金AA6061”视为同一个词。
_IDENTIFIER = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z_-]*\d+[A-Za-z0-9_-]*(?![A-Za-z0-9_-])')
_CITATION = re.compile(r'\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]')
_RANGE = re.compile(r'(?<![\d.])(?P<low>\d+(?:\.\d+)?)\s*[–—-]\s*(?P<high>\d+(?:\.\d+)?)')
_SMALL = dict(zip(('zero one two three four five six seven eight nine ten eleven twelve thirteen '
                   'fourteen fifteen sixteen seventeen eighteen nineteen').split(), range(20)))
_TENS = dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(), range(20, 100, 10)))
_WORDS = re.compile(r'\b(?:' + '|'.join(_TENS) + r')(?:[ -]+(?:' + '|'.join(list(_SMALL)[1:10])
                    + r'))?\b|\b(?:' + '|'.join(_SMALL) + r')\b', re.I)
_MONTHS = dict(zip('January February March April May June July August September October November December'.split(), range(1, 13)))
_DATE = re.compile(r'\b(?:\d{1,2}\s+)?(' + '|'.join(_MONTHS) + r')\s+(?:\d{1,2},?\s+)?\d{4}\b', re.I)
_PROSE = set(('and or to in of for with using at by is was were are specimens samples processors '
              'subsets folds cycles times years months days').split()) | {name.lower() for name in _MONTHS}
_SCALES = {'million': '1000000', 'billion': '1000000000', 'thousand': '1000', '百万': '1000000',
           '千万': '10000000', '亿': '100000000', '万': '10000', '千': '1000'}
_UNITS = {}
for _dimension, _groups in {
    'mass': [('1', 'kg kilogram kilograms 千克 公斤'), ('.001', 'g gram grams 克'),
             ('1000', 't tonne tonnes metric_ton metric_tons 吨 公吨')],
    'length': [('1', 'm meter meters metre metres 米'), ('.01', 'cm centimeter centimeters 厘米'),
               ('.001', 'mm millimeter millimeters 毫米'), ('.000001', 'µm μm um micrometer micrometers 微米'),
               ('1e-9', 'nm nanometer nanometers 纳米'), ('1000', 'km kilometer kilometers 千米 公里')],
    'pressure': [('1', 'Pa pascal pascals 帕 帕斯卡'), ('1000', 'kPa 千帕'),
                 ('1000000', 'MPa 兆帕'), ('1000000000', 'GPa 吉帕')],
    'ratio': [('.01', '% percent percentage 百分比')],
    'time': [('1', 's second seconds 秒'), ('60', 'min minute minutes 分钟'),
             ('3600', 'h hour hours 小时')],
}.items():
    for _factor, _names in _groups:
        for _name in _names.split():
            _UNITS[_name.replace('_', ' ')] = (_dimension, Decimal(_factor))


def _normalize(text):
    # 先处理上标，避免 NFKC 将 10⁻³ 展平为 10-3 后丢失指数边界。
    text = re.sub(r'10([⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+)', lambda m: '10^' + unicodedata.normalize('NFKC', m[1]), text)
    # 占位符是文字边界，移除时不可把另一侧正文误接成数值的单位。
    text = unicodedata.normalize('NFKC', _MARKERS.sub(';', text)).replace('−', '-')
    text = re.sub(r'百分之\s*(\d+(?:\.\d+)?)', r'\1%', text)
    def science(match):
        # 极端指数不展开，保留表达式供后续报告待核对。
        if len(match[2]) > 6:
            return match[0]
        exponent = int(match[2])
        try:
            return str(Decimal(match[1]) * Decimal(10) ** exponent) if abs(exponent) <= 10000 else match[0]
        except ArithmeticError:
            return match[0]
    return _SCIENCE.sub(science, text)


def _unit_suffix(text, aliases):
    """返回量纲、倍率、已消费长度；未知别名保留为 None。"""
    units = {**_UNITS, **aliases}
    for name in sorted(units, key=len, reverse=True):
        if text.startswith(name) and (not name[-1].isascii() or not name[-1].isalpha()
                                     or len(text) == len(name) or not text[len(name)].isascii()
                                     or not text[len(name)].isalpha()):
            return units[name], len(name)
    return None, 0


def _scale_suffix(suffix):
    for scale in sorted(_SCALES, key=len, reverse=True):
        if suffix.lower().startswith(scale) and (not scale.isascii() or len(suffix) == len(scale)
                                        or not suffix[len(scale)].isalpha()):
            return Decimal(_SCALES[scale]), suffix[len(scale):].lstrip()
    return Decimal(1), suffix


def parse_unit_aliases(text):
    """只读取明确的“单位全称 (缩写)”定义，冲突定义映射为 None。

    返回 {缩写: (量纲, Decimal 倍率) 或 None}，可直接传给 assess_numbers。
    不为未定义的 MT 推测含义，也不允许文档重定义既有 SI 单位。
    """
    aliases = {}
    normalized = _normalize(text)
    names = '|'.join(re.escape(n) for n in sorted(_UNITS, key=len, reverse=True))
    scales = '|'.join(re.escape(n) for n in sorted(_SCALES, key=len, reverse=True))
    pattern = re.compile(r'(?<![A-Za-z])(?:(?P<scale>(?i:' + scales + r'))\s*)?(?P<unit>' + names
                         + r')\s*\((?P<alias>[A-Za-zµμ][A-Za-zµμ0-9_-]{0,15})\)')
    for match in pattern.finditer(normalized):
        name = match['alias']
        dimension, factor = _UNITS[match['unit']]
        value = (dimension, factor * Decimal(_SCALES.get((match['scale'] or '').lower(), '1')))
        prefix = normalized[:match.start()].lower()
        if re.search(r'\b(?:\w*illion|hundreds?|thousands?|millions?|billions?)\s+(?:of\s+)?$', prefix):
            value = None
        previous = aliases.get(name, _UNITS.get(name, value))
        aliases[name] = value if previous == value else None
    # 同一缩写还出现无法解析的定义时，不能只采纳其中可解析的一个。
    defined_spans = {match.end() for match in pattern.finditer(normalized)}
    for match in re.finditer(r'\(([A-Z][A-Za-zµμ0-9_-]{0,15})\)', normalized):
        if match[1] in aliases and match.end() not in defined_spans:
            aliases[match[1]] = None
    return aliases


def _allowance(source):
    result = Counter()
    for match in _WORDS.finditer(source):
        result[Decimal(sum(_SMALL.get(w, _TENS.get(w, 0)) for w in match[0].lower().replace('-', ' ').split()))] += 1
    months = {name.lower(): value for name, value in _MONTHS.items()}
    for match in _DATE.finditer(source):
        result[Decimal(months[match[1].lower()])] += 1
    return result


def _extract(text, aliases):
    quantities, bare, unknown = Counter(), Counter(), Counter()
    citations = Counter(tuple(re.findall(r'\d+|-', m[0].replace('–', '-'))) for m in _CITATION.finditer(text))
    text = _CITATION.sub(' ', text)
    # 数值紧邻单位不是材料编号，科学记数法也不能当作字母数字标识。
    identifiers = Counter(m[0] for m in _IDENTIFIER.finditer(text)
                          if not re.fullmatch(r'[eE]\d+', m[0])
                          and not any(re.fullmatch(re.escape(name) + r'\d+', m[0]) for name in _UNITS))
    text = _IDENTIFIER.sub(lambda m: ' ' if m[0] in identifiers else m[0], text)
    def expand_range(match):
        tail = text[match.end():].lstrip()
        _, suffix = _scale_suffix(tail)
        unit, consumed = _unit_suffix(suffix, aliases)
        if consumed and unit is not None:
            unit_text = tail[:len(tail) - len(suffix) + consumed]
            return match['low'] + ' ' + unit_text + '; ' + match['high']
        return match['low'] + '; ' + match['high']
    text = _RANGE.sub(expand_range, text)
    matches = list(_NUMBER.finditer(text))
    for match in matches:
        value = Decimal(match[0].replace(',', ''))
        if abs(value.adjusted()) > 10000:
            raise ValueError('数值指数超出本地核验范围')
        suffix = text[match.end():].lstrip()
        factor, suffix = _scale_suffix(suffix)
        unit, consumed = _unit_suffix(suffix, aliases)
        # 复合单位/幂次暂不推演，不能误用其中单个 SI 单位证明等价。
        compound = consumed and re.match(r'\s*(?:[/^²³]|\d|[·⋅*]\s*[A-Za-zµμ]|[−-]\d)', suffix[consumed:])
        if consumed and suffix[consumed:].startswith(' '):
            compound = compound or _unit_suffix(suffix[consumed:].lstrip(), aliases)[1]
        next_word = re.match(r'[A-Za-z]+', suffix)
        unknown_word = next_word and next_word[0].lower() not in _PROSE
        unknown_chinese = re.match(r'[\u4e00-\u9fff]', suffix) and suffix[0] not in '和与及的个年月日份种组折次周天只条块项号中为是对所此等'
        if consumed and unit is not None and not compound:
            if unit[0] == 'ratio':
                bare[value * factor * unit[1]] += 1
            else:
                quantities[(unit[0], value * factor * unit[1])] += 1
        elif consumed or unknown_word or unknown_chinese or re.match(r'[µμ%°]', suffix) or re.match(r'\s*(?:[×*^]|/)', suffix):
            label = re.match(r'[A-Za-zµμ]+(?:[/^][A-Za-zµμ0-9+-]+)?|[\u4e00-\u9fff]+', suffix)
            unknown[(str(value * factor), label[0] if label else '?')] += 1
        else:
            bare[value * factor] += 1
    return quantities, bare, unknown, citations, identifiers


def assess_numbers(source, target, aliases=None):
    """硬错误只用于可证实的改变；未知表达式通过 warnings 交给人工核对。"""
    aliases = aliases or {}
    warnings = []
    try:
        source, target = _normalize(source), _normalize(target)
        original = _extract(source, aliases)
        returned = _extract(target, aliases)
    except (ArithmeticError, ValueError):
        return {'error': '', 'warnings': ['数字或物理量表达式无法可靠解析，待核对。']}
    sq, sb, su, sc, si = original
    tq, tb, tu, tc, ti = returned
    error = ''
    if sc != tc:
        error = '引文数值或出现次数不能对应原文'
    elif si != ti:
        def numbered_shapes(values):
            shapes = Counter()
            for key, count in values.items():
                shapes[re.sub(r'\d+', '#', key)] += count
            return shapes
        if numbered_shapes(si) == numbered_shapes(ti):
            error = '相同字母数字标识中的数值发生改变'
        else:
            warnings.append('字母数字标识的写法或数量不同，无法仅凭文本确定是否改义，待核对。')
            # 编号与普通数字之间的表达变化不应误报；其他数字仍独立核对。
            for identifiers, bare in ((si, sb), (ti, tb)):
                for identifier, count in identifiers.items():
                    for number in re.findall(r'\d+', identifier):
                        bare[Decimal(number)] += count
    if not error and sq != tq and not (su or tu):
        error = '物理量的数值、单位量纲或出现次数不能对应原文'
    elif not error and (sb - tb or (tb - sb) - _allowance(source)) and not (su or tu):
        error = '数值的值或出现次数不能对应原文'
    if su or tu:
        warnings.append('存在未知单位、冲突单位定义或无法解析的数量表达式，待核对。')
        # 已能独立匹配的量纲内发生改值，不能被无关未知单位掩盖。
        for dimension in {item[0] for item in sq} & {item[0] for item in tq}:
            if Counter({k: v for k, v in sq.items() if k[0] == dimension}) != Counter({k: v for k, v in tq.items() if k[0] == dimension}):
                error = error or '已知物理量的数值或出现次数不能对应原文'
        if su == tu and (sb - tb or (tb - sb) - _allowance(source)):
            error = error or '数值的值或出现次数不能对应原文'
        if su == tu and sq != tq:
            error = error or '物理量的数值、单位量纲或出现次数不能对应原文'
    return {'error': error, 'warnings': warnings}
