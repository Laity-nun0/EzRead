"""标准 BabelDOC 流程的提交边界；不复制分组、提示词或翻译回退循环。"""
from contextlib import contextmanager
from collections import Counter
from functools import wraps
import re

from .translation_validation import MARKERS, native_marker_assessment


def _strip_styles(text, prepared):
    """只去掉引擎生成的字体包裹，保留正文、公式和原文中的字面标签。"""
    generated = {re.sub(r'\s+', '', p.left_placeholder).lower()
                 for p in prepared.placeholders if hasattr(p, 'left_placeholder')}
    stack = []
    def replace(match):
        token = re.sub(r'\s+', '', match.group()).lower()
        if token.startswith('<style'):
            stack.append(token in generated)
            return '' if stack[-1] else match.group()
        if token == '</style>' and stack:
            return '' if stack.pop() else match.group()
        return match.group()
    return MARKERS.sub(replace, text)


def _verify_compositions(paragraph, prepared, text):
    formulas = [p for p in prepared.placeholders if hasattr(p, 'formula')]
    actual = [c.pdf_formula for c in paragraph.pdf_paragraph_composition if getattr(c, 'pdf_formula', None) is not None]
    if Counter(map(id, actual)) != Counter(id(p.formula) for p in formulas):
        raise ValueError('Native formula objects were not fully restored.')
    visible = _strip_styles(text, prepared)
    for p in formulas:
        visible = re.sub(p.regex_pattern, '', visible, flags=re.I)
    applied = []
    for comp in paragraph.pdf_paragraph_composition:
        if getattr(comp, 'pdf_formula', None) is not None:
            continue
        unicode = getattr(comp, 'pdf_same_style_unicode_characters', None)
        characters = getattr(comp, 'pdf_same_style_characters', None)
        if unicode is not None:
            applied.append(unicode.unicode)
        elif characters is not None:
            applied.append(''.join(c.char_unicode for c in characters.pdf_character))
        else:
            raise ValueError('Native translated composition is invalid.')
    if re.sub(r'\s+', '', visible) != re.sub(r'\s+', '', ''.join(applied)):
        raise ValueError('Native translated text was not fully applied.')


def _apply_translation(original, translator, paragraph, tracker, prepared, output):
    tracker.ezread_apply_failed = True
    assessment = native_marker_assessment(prepared.unicode, output) if isinstance(output, str) else {'error': True}
    if not isinstance(output, str) or not output.strip() or assessment['error']:
        raise ValueError('Native translation placeholders are incomplete.')
    output_tokens = Counter(re.sub(r'\s+', '', token).lower() for token in MARKERS.findall(output))
    for token, count in getattr(prepared, 'original_placeholder_tokens', {}).items():
        token = re.sub(r'\s+', '', token).lower()
        if token.startswith('<style') and output_tokens[token] != count:
            raise ValueError('Literal source tags cannot be removed as font styles.')
    before = paragraph.unicode, paragraph.pdf_paragraph_composition, getattr(tracker, 'output', None)
    removed = dict(tracker.removed_hallucinated_placeholders)
    try:
        result = original(translator, paragraph, tracker, prepared, output)
        attempt = tracker.last_llm_translate_tracker()
        if (not paragraph.pdf_paragraph_composition or not attempt
                or tracker.removed_hallucinated_placeholders != removed):
            raise ValueError('Native postprocessing did not fully apply the translation.')
        _verify_compositions(paragraph, prepared, output)
        degraded = assessment['style_degraded']
        if not attempt.placeholder_full_match:
            if not prepared.placeholders:
                raise ValueError('Native postprocessing did not fully match placeholders.')
            from babeldoc.format.pdf.document_il.midend.il_translator import RichTextPlaceholder
            if not any(isinstance(p, RichTextPlaceholder) for p in prepared.placeholders):
                raise ValueError('Native postprocessing did not fully match placeholders.')
            # 标准解析后的实际文字与公式对象已完整核验；仅允许字体样式降级。
            attempt.set_placeholder_full_match()
            degraded = True
        tracker.ezread_style_degraded = degraded
        tracker.ezread_apply_failed = False
        return result
    except Exception:
        paragraph.unicode, paragraph.pdf_paragraph_composition, tracker.output = before
        tracker.removed_hallucinated_placeholders = removed
        raise


@contextmanager
def native_translation_scope(failures):
    from babeldoc.const import __version__
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    if __version__ != '0.6.4':
        raise ValueError('Native validation adapter requires BabelDOC 0.6.4.')
    post, single = ILTranslator.post_translate_paragraph, ILTranslator.translate_paragraph
    audit = {'failures': failures, 'warnings': []}

    @wraps(post)
    def checked_post(self, paragraph, tracker, prepared, output):
        # 失败由原生批次流程安排单段回退；不能把本次部分回填留给下一次尝试。
        result = _apply_translation(post, self, paragraph, tracker, prepared, output)
        if tracker.ezread_style_degraded:
            audit['warnings'].append('部分字体样式已按段落基础字体排版；译文文字与公式对象已核验，请对照原文核对。')
        return result

    @wraps(single)
    def checked_single(self, *args, **kwargs):
        tracker = kwargs.get('tracker') or (args[3] if len(args) > 3 else None)
        result = single(self, *args, **kwargs)
        attempt = tracker.last_llm_translate_tracker() if tracker else None
        if getattr(tracker, 'ezread_apply_failed', False) or (attempt and (
                not getattr(tracker, 'output', None) or not attempt.placeholder_full_match
                or attempt.has_error or tracker.removed_hallucinated_placeholders)):
            # 上游单段方法吞掉异常；终态失败必须阻止后续请求和 PDF 发布。
            message = 'Native single-paragraph recovery failed.'
            if not failures:
                failures.append(message)
            raise ValueError(message)
        return result

    ILTranslator.post_translate_paragraph = checked_post
    ILTranslator.translate_paragraph = checked_single
    try:
        yield audit
    finally:
        ILTranslator.post_translate_paragraph = post
        ILTranslator.translate_paragraph = single
