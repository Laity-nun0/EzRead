"""Local body-text geometry quality checks; no rendering, network or model calls."""
import ctypes
import hashlib
import json
import math
import re
from collections import Counter
from contextlib import ExitStack, closing
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

import pypdfium2 as pdfium

BODY_LAYOUTS = frozenset(('plain text', 'text', 'caption', 'figure_caption', 'table_caption', 'fallback_line'))
THRESHOLDS = {'tiny_font_source_ratio': .7, 'tiny_cjk_min_count': 8,
              'severely_tiny_font_source_ratio': .45, 'severely_tiny_cjk_min_count': 3,
              'glyph_overlap_fraction': .35, 'overlap_min_pairs': 3,
              'letter_order_min_similarity': .85, 'protected_body_min_ink_retained_ratio': .8}


def contains(box, glyph):
    x = (glyph['box'][0] + glyph['box'][2]) / 2
    y = (glyph['box'][1] + glyph['box'][3]) / 2
    return box[0] - .5 <= x <= box[2] + .5 and box[1] - .5 <= y <= box[3] + .5


def cjk(text):
    return any('\u3400' <= char <= '\u9fff' or '\U00020000' <= char <= '\U0002fa1f' for char in text)


def overlap_fraction(a, b):
    ax0, ay0, ax1, ay1 = a['box']
    bx0, by0, bx1, by1 = b['box']
    area = max(0, min(ax1, bx1) - max(ax0, bx0)) * max(0, min(ay1, by1) - max(ay0, by0))
    minimum = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return area / minimum if minimum > 0 else 0


def paragraph_contains(paragraph, glyph):
    if not contains(paragraph['bbox'], glyph):
        return False
    boxes = [g['box'] for g in paragraph.get('glyphs', [])]
    return not boxes or any(contains(box, glyph) for box in boxes)


def glyphs(page):
    from pdf_tools import _PDF_LOCK
    result = []
    with _PDF_LOCK, closing(page.get_textpage()) as text:
        for i in range(text.count_chars()):
            value = text.get_text_range(i, 1)
            if not value or value.isspace():
                continue
            matrix = pdfium.raw.FS_MATRIX()
            pdfium.raw.FPDFText_GetMatrix(text.raw, i, matrix)
            x, y = ctypes.c_double(), ctypes.c_double()
            pdfium.raw.FPDFText_GetCharOrigin(text.raw, i, x, y)
            # Some PDFs use a 1 pt font with an 8x text matrix. Raw font size is not physical size.
            size = pdfium.raw.FPDFText_GetFontSize(text.raw, i) * math.hypot(matrix.c, matrix.d)
            result.append({'text': value, 'box': list(text.get_charbox(i)),
                           'size': size, 'baseline': y.value, 'index': i})
    return result


def source_words(chars):
    """Group physical baselines, then words using native spaces and glyph gaps."""
    lines = []
    for char in sorted(chars, key=lambda g: (-g['baseline'], g['box'][0])):
        line = next((line for line in lines if abs(line[0]['baseline'] - char['baseline'])
                     <= .23 * max(line[0]['size'], char['size'])), None)
        if line is None:
            lines.append([char])
        else:
            line.append(char)
    words = []
    for line in lines:
        word, previous = [], None
        for char in sorted(line, key=lambda g: g['box'][0]):
            if not re.fullmatch('[A-Za-z]', char['text']):
                if word:
                    words.append(word)
                word, previous = [], None
                continue
            if previous and (char['index'] != previous['index'] + 1
                             or char['box'][0] - previous['box'][2] > .28 * char['size']):
                words.append(word)
                word = []
            word.append(char)
            previous = char
        if word:
            words.append(word)
    return words


def body_groups(body):
    """Join same-row fragments for typography checks, retaining original blocks for cut-word checks."""
    groups = []
    for p in body:
        box = list(p['bbox'])
        merged = True
        while merged:
            merged = False
            for previous in list(groups):
                overlap = min(box[3], previous[3]) - max(box[1], previous[1])
                gap = max(box[0], previous[0]) - min(box[2], previous[2])
                if overlap >= .5 * min(box[3] - box[1], previous[3] - previous[1]) and gap < 12:
                    box = [min(box[0], previous[0]), min(box[1], previous[1]),
                           max(box[2], previous[2]), max(box[3], previous[3])]
                    groups.remove(previous)
                    merged = True
        groups.append(box)
    return groups


def normalize_paragraphs(paragraphs):
    """Accept the actual engine input audit as well as its older paragraph-box format."""
    result, seen = [], set()
    for paragraph in paragraphs:
        p = dict(paragraph)
        if 'input' in p and not p['input']:
            continue
        if 'bbox' not in p:
            boxes = [g['box'] for g in p.get('glyphs', [])]
            if not boxes:
                continue
            p['bbox'] = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[2] for b in boxes), max(b[3] for b in boxes)]
        key = (p.get('page'), tuple(p['bbox']), p.get('input') or p.get('text'))
        if key not in seen:
            result.append(p)
            seen.add(key)
    return result


def audit_page(source, translated, paragraphs, protected):
    paragraphs = normalize_paragraphs(paragraphs)
    body = [p for p in paragraphs if p.get('layout') in BODY_LAYOUTS]
    allowed = lambda g: not any(contains(r, g) for r in protected)
    source = [g for g in source if allowed(g)]
    translated = [g for g in translated if allowed(g)]
    findings = []
    body_sizes = [g['size'] for g in source if g['text'].isalpha()
                  and any(contains(p['bbox'], g) for p in body)]
    body_size = median(body_sizes) if body_sizes else 0
    # A source word crossing multiple extracted body blocks is a concrete broken-word boundary.
    for word in source_words(source):
        owners = []
        for g in word:
            matches = [i for i, p in enumerate(body) if paragraph_contains(p, g)]
            owners.append(matches[0] if len(matches) == 1 else None)
        distinct = {i for i in owners if i is not None}
        if (len(word) >= 4 and len(distinct) > 1 and None not in owners
                and median(g['size'] for g in word) >= .75 * body_size):
            findings.append({'kind': 'source_word_split', 'word': ''.join(g['text'] for g in word),
                             'paragraphs': sorted(distinct), 'bbox':
                             [min(g['box'][0] for g in word), min(g['box'][1] for g in word),
                              max(g['box'][2] for g in word), max(g['box'][3] for g in word)]})
    for i, p in enumerate(body):
        placeholder_boxes = [g['box'] for item in p.get('placeholders', []) for g in item.get('glyphs', [])]
        original = [g for g in source if paragraph_contains(p, g) and g['text'].isalpha()
                    and not any(contains(box, g) for box in placeholder_boxes)]
        # Compare letter order, not mere word/CJK presence. Formula placeholders are excluded.
        expected = ''.join(g['text'] for g in sorted(original, key=lambda g: g['index']))
        extracted = re.sub(r'<b\d+>', '', p.get('input') or p.get('text') or '')
        extracted = ''.join(c for c in extracted if c.isalpha())
        if len(expected) >= 20 and len(extracted) >= 20:
            ratio = SequenceMatcher(None, expected.casefold(), extracted.casefold(), autojunk=False).ratio()
            if ratio < .85:
                findings.append({'kind': 'source_text_order_mismatch', 'paragraph': i, 'bbox': p['bbox'],
                                 'letter_order_similarity': round(ratio, 3),
                                 'source_letters': expected[:160], 'extracted_letters': extracted[:160]})
    formula_boxes = [g['box'] for p in body for item in p.get('placeholders', []) for g in item.get('glyphs', [])]
    for i, box in enumerate(body_groups(body)):
        original = [g for g in source if contains(box, g) and g['text'].isalpha()
                    and not any(contains(b, g) for b in formula_boxes)]
        chinese = [g for g in translated if contains(box, g) and cjk(g['text'])]
        if not chinese:
            continue
        source_size = median(g['size'] for g in original) if len(original) >= 12 else 0
        small = [g for g in chinese if g['size'] < .7 * source_size]
        severe = [g for g in chinese if g['size'] < .45 * source_size]
        if len(small) >= 8 or len(severe) >= 3:
            findings.append({'kind': 'tiny_body_font', 'body_group': i, 'bbox': box,
                             'source_font_pt': round(source_size, 3),
                             'translated_median_pt': round(median(g['size'] for g in chinese), 3),
                             'tiny_count': len(small), 'severely_tiny_count': len(severe),
                             'cjk_count': len(chinese)})
        overlaps = []
        for a_index, a in enumerate(chinese):
            for b in chinese[a_index + 1:]:
                ratio = overlap_fraction(a, b)
                if ratio > .35:
                    overlaps.append({'text': a['text'] + b['text'], 'boxes': [a['box'], b['box']],
                                     'overlap_fraction': round(ratio, 3)})
        if len(overlaps) >= 3:
            findings.append({'kind': 'overlapping_body_glyphs', 'body_group': i, 'bbox': box,
                             'pair_count': len(overlaps), 'examples': overlaps[:8]})
        # Inline formula glyphs remain legitimate small text, but must not occupy Chinese strokes.
        mixed = [g for g in translated if contains(box, g) and not cjk(g['text']) and
                 (g['text'].isalnum() or g['text'] in '√∑∫=()[]+−-/')]
        collisions = []
        for a in chinese:
            for b in mixed:
                ratio = overlap_fraction(a, b)
                if ratio > .35:
                    collisions.append({'text': [a['text'], b['text']], 'boxes': [a['box'], b['box']],
                                       'overlap_fraction': round(ratio, 3)})
        if len(collisions) >= 3:
            findings.append({'kind': 'overlapping_body_math_or_latin', 'body_group': i, 'bbox': box,
                             'pair_count': len(collisions), 'examples': collisions[:8]})
    return findings


def removed_body_glyphs(before, after, paragraphs):
    """Protected-region restoration must not silently delete adjacent translated body glyphs."""
    boxes = [p['bbox'] for p in normalize_paragraphs(paragraphs) if p.get('layout') in BODY_LAYOUTS]
    remaining = {}
    for glyph in after:
        remaining.setdefault(glyph['text'], []).append(glyph)
    missing = []
    for glyph in before:
        if not cjk(glyph['text']) or not any(contains(box, glyph) for box in boxes):
            continue
        matches = remaining.get(glyph['text'], [])
        center = [(glyph['box'][0] + glyph['box'][2]) / 2, (glyph['box'][1] + glyph['box'][3]) / 2]
        match = next((g for g in matches if
                      abs((g['box'][0] + g['box'][2]) / 2 - center[0]) < 1 and
                      abs((g['box'][1] + g['box'][3]) / 2 - center[1]) < 1), None)
        if match is not None:
            matches.remove(match)
        else:
            missing.append({'text': glyph['text'], 'bbox': glyph['box'], 'font_pt': round(glyph['size'], 3)})
    return missing


def body_ink_loss(before_image, after_image, before_chars, after_chars, paragraphs, protected, height, scale=2):
    """Catch a surviving text object whose strokes were whitened by a nearby restored formula."""
    boxes = [p['bbox'] for p in normalize_paragraphs(paragraphs) if p.get('layout') in BODY_LAYOUTS]
    lost = []
    for g in before_chars:
        box = g['box']
        if not cjk(g['text']) or not any(contains(b, g) for b in boxes):
            continue
        if not any(min(box[2], b[2]) > max(box[0], b[0]) and
                   min(box[3], b[3]) > max(box[1], b[1]) for b in protected):
            continue
        if not any(a['text'] == g['text'] and abs(a['box'][0] - box[0]) < 1 and
                   abs(a['box'][1] - box[1]) < 1 for a in after_chars):
            continue  # Already reported as a removed body glyph.
        crop = [math.floor(box[0] * scale), math.floor((height - box[3]) * scale),
                math.ceil(box[2] * scale), math.ceil((height - box[1]) * scale)]
        before_ink = sum(before_image.crop(crop).convert('L').histogram()[:180])
        after_ink = sum(after_image.crop(crop).convert('L').histogram()[:180])
        if before_ink >= 12 and after_ink < .8 * before_ink:
            lost.append({'text': g['text'], 'bbox': box,
                         'before_ink_pixels': before_ink, 'after_ink_pixels': after_ink})
    return lost


def page_report(source, translated, paragraphs, protected, *, number, text=''):
    """Inspect native glyph data; a page without sufficient body evidence is not assessed."""
    findings = audit_page(source, translated, paragraphs, protected)
    boxes = [p['bbox'] for p in normalize_paragraphs(paragraphs) if p.get('layout') in BODY_LAYOUTS]
    in_body = lambda g: any(contains(box, g) for box in boxes) and not any(
        contains(box, g) for box in protected)
    coverage = {'body_regions': len(boxes),
                'source_body_letters': sum(g['text'].isalpha() and in_body(g) for g in source),
                'translated_body_cjk': sum(cjk(g['text']) and in_body(g) for g in translated)}
    for key, values in [('translated_body_font_pt', [g['size'] for g in translated if cjk(g['text']) and in_body(g)]),
                        ('source_body_font_pt', [g['size'] for g in source if g['text'].isalpha() and in_body(g)])]:
        if values:
            coverage[key] = {'min': round(min(values), 3), 'median': round(median(values), 3),
                             'max': round(max(values), 3)}
    coverage['assessable'] = coverage['source_body_letters'] >= 12 and coverage['translated_body_cjk'] >= 8
    if not coverage['assessable']:
        findings.append({'kind': 'body_audit_not_assessable', 'coverage': coverage,
                         'reason': '正文区域或中文正文不足，不能以空检查判定通过'})
    diagnostics = re.findall(r'【[^】]*(?:截断|残缺|片段|识别)[^】]*】', text)
    if diagnostics:
        findings.append({'kind': 'translation_failure_diagnostic', 'messages': diagnostics})
    return {'number': number, 'coverage': coverage, 'findings': findings}


def classify(findings, *, page):
    """Only strong overlap and severe body shrinkage block publication."""
    failures, warnings = [], []
    labels = {'overlapping_body_glyphs': '正文中文字形重叠',
              'overlapping_body_math_or_latin': '正文与行内公式或英文字形重叠',
              'tiny_body_font': '中文正文字号相对原文过小',
              'source_word_split': '源正文单词被拆分',
              'source_text_order_mismatch': '源正文字母顺序需核对',
              'body_audit_not_assessable': '本页缺少足够正文证据，未完成中文排版核验',
              'translation_failure_diagnostic': '译文含残缺或截断说明，需核对'}
    for finding in findings:
        kind = finding['kind']
        item = {**finding, 'page': page, 'message': f'第 {page} 页：{labels.get(kind, "正文排版需核对")}'}
        hard = (kind in ('overlapping_body_glyphs', 'overlapping_body_math_or_latin') and finding.get('pair_count', 0) >= 3
                or kind == 'tiny_body_font' and finding.get('severely_tiny_count', 0) >= 3)
        (failures if hard else warnings).append(item)
    return failures, warnings


def _records(value):
    records = json.loads(Path(value).read_text(encoding='utf-8')) if isinstance(value, (str, Path)) else value
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError('PDF layout quality metadata must be a JSON array of objects.')
    return records


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(source, mono, paragraphs, regions, *, pages=None, cancel_event=None):
    """Return private quality evidence; heuristic failures are review findings.

    Metadata can be JSON paths or decoded arrays. Coordinates are PDF points,
    bottom-left origin, pages one-based. No files are written and no raster is made.
    The publisher retains even severe findings as review warnings, not a veto.
    Structural PDF/metadata errors still raise; this report never proves full QA.
    """
    from pdf_tools import _PDF_LOCK
    source, mono = Path(source), Path(mono)
    paragraphs, regions = _records(paragraphs), _records(regions)
    report = {'schema_version': 1, 'source_sha256': _file_hash(source), 'translated_sha256': _file_hash(mono),
              'scope': '普通正文及图表题的本地字形几何检查，不证明全文语义或公式内部排版正确',
              'thresholds': dict(THRESHOLDS), 'pages': [], 'failures': [], 'warnings': []}
    with _PDF_LOCK, ExitStack() as stack:
        original = stack.enter_context(closing(pdfium.PdfDocument(source)))
        translated = stack.enter_context(closing(pdfium.PdfDocument(mono)))
        if len(original) != len(translated):
            raise ValueError('PDF layout quality page count differs from source.')
        numbers = sorted(set(pages)) if pages is not None else list(range(1, len(original) + 1))
        if not numbers or any(type(n) is not int or n < 1 or n > len(original) for n in numbers):
            raise ValueError('PDF layout quality page numbers are invalid.')
        for number in numbers:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('PDF layout quality inspection cancelled.')
            with closing(original[number - 1]) as a, closing(translated[number - 1]) as b:
                if any(abs(x - y) > .1 for x, y in zip(a.get_size(), b.get_size())):
                    raise ValueError('PDF layout quality page dimensions differ from source.')
                with closing(b.get_textpage()) as text_page:
                    text = text_page.get_text_range()
                result = page_report(glyphs(a), glyphs(b), [p for p in paragraphs if p.get('page') == number],
                                     [r['il_bbox'] for r in regions if r.get('page') == number], number=number, text=text)
                report['pages'].append(result)
                failures, warnings = classify(result['findings'], page=number)
                report['failures'].extend(failures)
                report['warnings'].extend(warnings)
        report['coverage'] = {'page_count': len(original), 'checked_pages': numbers,
                              'assessable_pages': [p['number'] for p in report['pages'] if p['coverage']['assessable']],
                              'unassessable_pages': [p['number'] for p in report['pages'] if not p['coverage']['assessable']],
                              'all_pages_checked': numbers == list(range(1, len(original) + 1))}
    if not report['coverage']['all_pages_checked']:
        report['warnings'].append({'kind': 'partial_page_coverage', 'page': None, 'message': '仅检查了部分页面，未完成全篇排版核验'})
    report['counts'] = dict(Counter(f['kind'] for p in report['pages'] for f in p['findings']))
    report['status'] = 'failed' if report['failures'] else 'needs_review' if report['warnings'] else 'passed'
    return report


