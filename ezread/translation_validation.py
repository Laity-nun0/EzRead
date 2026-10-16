"""翻译响应的确定性结构检查；不以文本长度猜测翻译质量。"""
from collections import Counter
import json
import re


MARKERS = re.compile(
    r"</?b\d+>|\{\s*v\s*\d+\s*\}|"
    r"<\s*style\s*id\s*=\s*'\s*\d+\s*'\s*>|<\s*/\s*style\s*>", re.I)


def _marker_token(token):
    return re.sub(r'\s+', '', token).lower()


def _style_records(text, paired):
    stack, records = [], []
    for match in MARKERS.finditer(text):
        token = _marker_token(match.group())
        if token.startswith('{') or (token.startswith(('<b', '</b'))
                                     and token.replace('</', '<') not in paired):
            continue
        if token.startswith('</'):
            if not stack:
                return None
            opening, start = stack[-1]
            expected = '</style>' if opening.startswith('<style') else '</' + opening[1:]
            if token != expected:
                return None
            stack.pop()
            records.append((opening, tuple(item[0] for item in stack), text[start:match.start()]))
        else:
            stack.append((token, match.end()))
    return None if stack else records


def load_response_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('响应 JSON 存在重复字段。')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError('响应 JSON 包含非标准数值。')

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)


class ResponseValidationError(ValueError):
    def __init__(self, message, rejected, code='batch_placeholders'):
        super().__init__(message)
        self.rejected = rejected
        self.code = code
        self.details = {'stage': 'batch'}


def batch_values(units, response):
    """校验批次 JSON 与单元标识；正文和占位符由后续流程检查。"""
    try:
        values = load_response_json(response)
    except ValueError as exc:
        error = ResponseValidationError('上游响应不是有效 JSON。', len(units), 'batch_json')
        error.details.update({name: getattr(exc, attr) for name, attr in
                              (('json_line', 'lineno'), ('json_column', 'colno'), ('json_position', 'pos'))
                              if hasattr(exc, attr)})
        raise error from exc
    if (not isinstance(values, list) or len(values) != len(units)
            or any(not isinstance(v, dict) or type(v.get('id')) is not int for v in values)
            or len({v['id'] for v in values}) != len(values)
            or {v['id'] for v in values} != {u['id'] for u in units}):
        raise ResponseValidationError('译文批次缺项、重复或标识不一致。', len(units), 'batch_ids')
    return {v['id']: v.get('output') for v in values}


def markers_match(source, translated):
    """公式可换序、完整样式对可换序；样式不能交叉闭合或改变嵌套。

    上游的公式也可能使用单个 <bN>，仅将源文有闭合标记的 ID 视为样式对。
    """
    source_tokens = [_marker_token(token) for token in MARKERS.findall(source)]
    translated_tokens = [_marker_token(token) for token in MARKERS.findall(translated)]
    if Counter(source_tokens) != Counter(translated_tokens):
        return False
    paired = {token.replace('</', '<') for token in source_tokens if token.startswith('</b')}
    original = _style_records(source, paired)
    if original is None:
        # 原始论文可能含有字面标记；无法解析时仅接受原有样式标记顺序。
        def style_tokens(tokens):
            return [token for token in tokens if token.startswith(('<style', '</style'))
                    or token.replace('</', '<') in paired]
        return style_tokens(source_tokens) == style_tokens(translated_tokens)
    result = _style_records(translated, paired)
    return result is not None and Counter((tag, parents) for tag, parents, _ in original) == Counter(
        (tag, parents) for tag, parents, _ in result)


def native_marker_assessment(source, output):
    """原生排版可降级完整字体样式，公式和旧 b 标记仍须完整保留。"""
    error = {'error': '公式或字体占位符缺失、重复、新增或配对结构改变',
             'warnings': [], 'style_degraded': False}

    def without_styles(text):
        return MARKERS.sub(lambda match: '' if _marker_token(match.group()).startswith(
            ('<style', '</style')) else match.group(), text)

    def style_residue(text):
        # 未识别的标签只能沿用源文字面内容，不能作为字体降级后的正文漏入 PDF。
        return Counter(re.findall(r'<\s*/?\s*style\b[^<>]*>?', MARKERS.sub('', text), re.I))

    if style_residue(output) - style_residue(source):
        return error
    if not markers_match(without_styles(source), without_styles(output)):
        return error
    original = _style_records(source, set())
    result = _style_records(output, set())
    if original is None:
        # 源文字面代码可能本来就无法配对；只能严格沿用，不猜测可删除的字体对。
        return {'error': '', 'warnings': [], 'style_degraded': False} if markers_match(source, output) else error
    if result is None:
        return error
    original_tags = Counter(tag for tag, _, _ in original)
    result_tags = Counter(tag for tag, _, _ in result)
    if result_tags - original_tags:
        return error
    # 混用旧字体时也不能让通用 style 闭合越过尚未闭合的 b 字体。
    paired = {_marker_token(token).replace('</', '<') for token in MARKERS.findall(source)
              if _marker_token(token).startswith('</b')}
    if _style_records(source, paired) is not None and _style_records(output, paired) is None:
        return error
    degraded = not markers_match(source, output)
    return {'error': '', 'warnings': ['字体样式已降级，请对照原 PDF 核对。'] if degraded else [],
            'style_degraded': degraded}


def emptied_styles(source, translated):
    """字体片段可能只有 fi 连字或词尾；空样式只提示，不能据此断言漏译。"""
    paired = {_marker_token(token).replace('</', '<') for token in MARKERS.findall(source)
              if _marker_token(token).startswith('</b')}
    original = _style_records(source, paired) or []
    result = _style_records(translated, paired) or []
    nonempty = {tag for tag, _, content in original if content.strip()}
    return sorted({re.search(r'\d+', tag).group() for tag, _, content in result
                   if not content.strip() and tag in nonempty})
