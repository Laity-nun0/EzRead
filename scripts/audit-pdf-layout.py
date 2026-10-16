"""Read-only body-text geometry audit; findings require visual/semantic review."""
import argparse
import hashlib
import json
from collections import Counter
from contextlib import ExitStack, closing
from pathlib import Path
import sys

import pypdfium2 as pdfium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ezread.pdf_layout_quality import (
    BODY_LAYOUTS as BODY_LAYOUTS,
    THRESHOLDS as THRESHOLDS,
    audit_page as audit_page,
    body_groups as body_groups,
    body_ink_loss as body_ink_loss,
    cjk as cjk,
    contains as contains,
    glyphs as glyphs,
    normalize_paragraphs as normalize_paragraphs,
    overlap_fraction as overlap_fraction,
    page_report as page_report,
    paragraph_contains as paragraph_contains,
    removed_body_glyphs as removed_body_glyphs,
    source_words as source_words,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--translated', type=Path, required=True)
    parser.add_argument('--before-protection', type=Path,
                        help='Unprotected translated mono PDF; auto-detect the engine sibling when present')
    parser.add_argument('--paragraphs', type=Path, required=True,
                        help='Source paragraph JSON with page, layout and PDF bottom-left bbox')
    parser.add_argument('--regions', type=Path, required=True,
                        help='Protected formula/figure/table region JSON with page and il_bbox')
    parser.add_argument('--pages', help='Comma-separated page numbers; default all pages')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    before_path = args.before_protection or args.translated.with_name('original.no_watermark.zh.mono.pdf')
    if args.before_protection and not before_path.is_file():
        raise FileNotFoundError(before_path)
    paragraphs = json.loads(args.paragraphs.read_text(encoding='utf-8'))
    regions = json.loads(args.regions.read_text(encoding='utf-8'))
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'scope': '正文切词及译文字形几何检查，不证明翻译语义完整；需逐页视觉核对',
              'thresholds': dict(THRESHOLDS),
              'source_sha256': hashlib.sha256(args.source.read_bytes()).hexdigest(),
              'translated_sha256': hashlib.sha256(args.translated.read_bytes()).hexdigest(), 'pages': []}
    with ExitStack() as stack:
        source = stack.enter_context(closing(pdfium.PdfDocument(args.source)))
        translated = stack.enter_context(closing(pdfium.PdfDocument(args.translated)))
        before = stack.enter_context(closing(pdfium.PdfDocument(before_path))) if before_path.is_file() else None
        report['protected_body_survival_checked'] = before is not None
        if before is not None:
            report['before_protection_sha256'] = hashlib.sha256(before_path.read_bytes()).hexdigest()
        if len(source) != len(translated):
            raise ValueError('Mono PDF page count must match source')
        pages = [int(n) for n in args.pages.split(',')] if args.pages else range(1, len(source) + 1)
        for number in pages:
            with closing(source[number - 1]) as a, closing(translated[number - 1]) as b:
                original, chinese = glyphs(a), glyphs(b)
                page_paragraphs = [p for p in paragraphs if p['page'] == number]
                page_regions = [r['il_bbox'] for r in regions if r['page'] == number]
                with closing(b.get_textpage()) as text:
                    result = page_report(original, chinese, page_paragraphs, page_regions,
                                         number=number, text=text.get_text_range())
                findings = result['findings']
                if before is not None:
                    with closing(before[number - 1]) as raw_page:
                        raw_chars = glyphs(raw_page)
                        missing = removed_body_glyphs(raw_chars, chinese, page_paragraphs)
                        raw_bitmap, final_bitmap = raw_page.render(scale=2), b.render(scale=2)
                        ink_loss = body_ink_loss(raw_bitmap.to_pil(), final_bitmap.to_pil(), raw_chars,
                                                chinese, page_paragraphs, page_regions, b.get_height())
                        raw_bitmap.close()
                        final_bitmap.close()
                    if missing:
                        findings.append({'kind': 'body_glyphs_removed_by_protection', 'count': len(missing),
                                         'examples': missing[:30]})
                    if ink_loss:
                        findings.append({'kind': 'body_strokes_clipped_by_protection', 'count': len(ink_loss),
                                         'examples': ink_loss[:30]})
                for label, page in [('source', a), ('translated', b)]:
                    bitmap = page.render(scale=2)
                    image = bitmap.to_pil()
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(image)
                    for item in findings:
                        if (box := item.get('bbox')) is not None:
                            draw.rectangle([box[0] * 2, (page.get_height() - box[3]) * 2,
                                            box[2] * 2, (page.get_height() - box[1]) * 2], outline='red', width=2)
                    image.save(args.output / f'page-{number}-{label}.png')
                    bitmap.close()
                report['pages'].append(result)
    report['counts'] = dict(Counter(f['kind'] for p in report['pages'] for f in p['findings']))
    report['geometry_clear'] = not report['counts']
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'geometry_clear': report['geometry_clear'], 'counts': report['counts'],
                      'report': str(args.output / 'report.json')}, ensure_ascii=True))
    return 0 if report['geometry_clear'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
