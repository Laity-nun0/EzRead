"""只读回放完整原生 BabelDOC 流程；保留英文不等于中文翻译验收。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_layout_engine as engine


def _save(path, value):
    engine._write(path, value)


def load_cache(folder, source_hash):
    """只复用同源、无公式标记、无失败记录且当前校验通过的精确原文。"""
    from ezread import pdf_translations as layout
    if folder is None:
        return {}, {'eligible': 0, 'rejected': 0, 'ambiguous': 0}
    manifest = engine._read(folder / 'source-units.json')
    if manifest.get('source_hash') != source_hash:
        raise ValueError('旧译文缓存与原 PDF 的 SHA-256 不一致。')
    translated = engine._read(folder / 'translated-units.json')
    review_path = folder / 'validation-review.json'
    reviews = engine._read(review_path) if review_path.is_file() else {}
    cached, ambiguous, rejected = {}, set(), 0
    for item in manifest['units']:
        text, key = item['text'], item['id']
        value = translated.get(key)
        if (key in reviews or layout.MARKERS.search(text) or not isinstance(value, str)
                or not value.strip() or layout.translation_issue(text, value)):
            rejected += 1
            continue
        if text in cached and cached[text] != value:
            ambiguous.add(text)
        cached[text] = value
    return {k: v for k, v in cached.items() if k not in ambiguous}, {
        'eligible': len(cached) - len(ambiguous), 'rejected': rejected, 'ambiguous': len(ambiguous)}


def inspect_native(source_path, output, pages):
    """在隔离 PDF 运行时内只读检查实际产物，不启用上游 debug 或内部 hooks。"""
    import pymupdf
    run = output / 'run'
    working = run / 'native/working' / source_path.stem
    tracking_path = working / 'translate_tracking.json'
    tracking = engine._read(tracking_path) if tracking_path.is_file() else {}
    paragraphs, inventory = [], Counter()
    for group in ('page', 'cross_page', 'cross_column'):
        for page_index, page in enumerate(tracking.get(group, [])):
            for paragraph in page.get('paragraph', []):
                attempts = paragraph.get('llm_translate_trackers', [])
                inventory[group + '_paragraphs'] += 1
                inventory['attempts'] += len(attempts)
                inventory['fallback_attempts'] += max(0, len(attempts) - 1)
                final = attempts[-1] if attempts else {}
                success = bool(paragraph.get('output') and final.get('output')
                               and final.get('placeholder_full_match') and not final.get('has_error')
                               and not final.get('fallback_to_translate'))
                inventory['applied' if success else 'not_applied'] += 1
                if paragraph.get('input') == paragraph.get('output'):
                    inventory['unchanged'] += 1
                if attempts:
                    if isinstance(paragraph.get('pdf_unicode'), str) and isinstance(paragraph.get('input'), str):
                        paragraphs.append({**paragraph, 'group': group, 'group_page': page_index + 1})
    stats = {'tracking_path': tracking_path.relative_to(output).as_posix(), 'tracking': dict(inventory),
             'outputs': {}, 'geometry_errors': [], 'content_errors': [], 'previews': []}
    paths = {}
    for kind in ('mono', 'dual'):
        matches = list((run / 'native/pdf').glob('*.' + kind + '.pdf'))
        if len(matches) == 1:
            paths[kind] = matches[0]
            stats['outputs'][kind] = matches[0].relative_to(output).as_posix()
    with pymupdf.open(source_path) as source:
        stats['source_pages'] = len(source)
        stats['source_sizes'] = [[page.rect.width, page.rect.height] for page in source]
        audit = engine._source_audit(source, paragraphs)
        _save(output / 'native-text-audit.json', audit)
        stats['geometry_matched_paragraphs'] = len(audit)
        for number in pages:
            if not 1 <= number <= len(source):
                raise ValueError('代表页超出原 PDF 页数。')
            path = output / f'page-{number:03}-source.png'
            source[number - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(path)
            stats['previews'].append(path.name)
        for kind, path in paths.items():
            with pymupdf.open(path) as pdf:
                stats[kind + '_pages'] = len(pdf)
                stats[kind + '_sizes'] = [[page.rect.width, page.rect.height] for page in pdf]
                if len(pdf) != len(source):
                    stats['geometry_errors'].append(kind + ': page count differs')
                for index, (before, after) in enumerate(zip(source, pdf)):
                    factor = 2 if kind == 'dual' else 1
                    if (abs(before.rect.width * factor - after.rect.width) > 1
                            or abs(before.rect.height - after.rect.height) > 1):
                        stats['geometry_errors'].append(f'{kind}: page {index + 1} dimensions differ')
                if kind == 'mono':
                    rotated = [index + 1 for index, page in enumerate(source) if page.rotation]
                    try:
                        stats['rotation_output_coverage'] = engine._rotation_output_coverage(source, pdf, rotated)
                        stats['content_errors'].extend(f"Page {item['page']} coverage needs review."
                                                       for item in stats['rotation_output_coverage'] if item['needs_review'])
                    except ValueError as exc:
                        stats['content_errors'].append(str(exc))
                    counter = Counter(char for page in pdf for char in page.get_text()
                                      if '\u3400' <= char <= '\u9fff' or '\U00020000' <= char <= '\U0002fa1f')
                    stats['output_cjk_characters'] = sum(counter.values())
                    expected = Counter(char for item in paragraphs
                                       for char in (item['output'] if isinstance(item.get('output'), str) else '')
                                       if '\u3400' <= char <= '\u9fff' or '\U00020000' <= char <= '\U0002fa1f')
                    stats['tracker_cjk_characters'] = sum(expected.values())
                    stats['missing_cjk_characters'] = sum((expected - counter).values())
                    for number in pages:
                        if number <= len(pdf):
                            image = output / f'page-{number:03}-target.png'
                            pdf[number - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(image)
                            stats['previews'].append(image.name)
    _save(output / 'artifact-inspection.json', stats)
    print(json.dumps({'artifact_inspection_written': True}), flush=True)


def verify(source, output, mode, cache_folder=None, pages=()):
    from ezread import pdf_layout_quality
    from ezread.pdf_native_translation import encode_response, request_units, response_values
    source, output = source.resolve(), output.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if (output / 'run').exists():
        raise ValueError('输出目录已有回放任务；请指定新目录以免混用旧产物。')
    if mode == 'cached' and cache_folder is None:
        raise ValueError('cached 模式必须指定 --cache-folder。')
    output.mkdir(parents=True, exist_ok=True)
    source_hash = engine._sha(source)
    cached, cache_info = load_cache(cache_folder, source_hash) if mode == 'cached' else ({}, {})
    counts = Counter()
    unique = {'cache': set(), 'identity': set()}
    requests = []
    report = {'schema_version': 1, 'mode': mode, 'source_sha256': source_hash,
              'source': str(source), 'real_model_calls': 0, 'cache': cache_info,
              'scope': '完整原生引擎离线回放；缓存未覆盖的单元保留原文，不构成全文中文或语义验收。',
              'production_gate_accepted': False, 'replay_completed': False,
              'quality_status': 'unassessed', 'errors': []}
    _save(output / 'report.json', report)
    def translate(request):
        units, batch = request_units(request)
        values, hits, unchanged = {}, [], []
        for unit in units:
            text = unit['input']
            category = 'cache' if text in cached else 'identity'
            value = cached[text] if category == 'cache' else text
            if category == 'cache':
                # 再走当前完整响应校验，不只相信旧文件曾经通过。
                response_values([{'id': 0, 'input': text}], False, value)
                hits.append(unit['id'])
            else:
                unchanged.append(unit['id'])
            values[unit['id']] = value
            counts[category + '_units'] += 1
            unique[category].add(hashlib.sha256(text.encode()).hexdigest())
        counts['requests'] += 1
        counts['batch_requests' if batch else 'single_requests'] += 1
        response = encode_response(units, batch, values)
        requests.append({'request': request, 'response': response, 'cache_ids': hits, 'identity_ids': unchanged})
        _save(output / 'replay-responses.json', requests)
        if counts['requests'] == 1 or counts['requests'] % 20 == 0:
            print(json.dumps({'request': counts['requests'], 'cache_units': counts['cache_units'],
                              'identity_units': counts['identity_units']}), flush=True)
        return response
    started = time.monotonic()
    try:
        result = engine.run_native(ROOT, source, output / 'run', translate=translate)
        report['production_gate_accepted'] = True
        report['native_result'] = result
    except Exception as exc:
        report['errors'].append({'stage': 'production_gate', 'type': type(exc).__name__, 'message': str(exc)})
    report['elapsed_engine_seconds'] = round(time.monotonic() - started, 3)
    report['requests'] = dict(counts)
    report['unique_cache_units'] = len(unique['cache'])
    report['unique_identity_units'] = len(unique['identity'])
    report['source_unchanged'] = engine._sha(source) == source_hash
    # 即使生产门槛按预期拒绝 identity，也只读检查已生成的完整上游产物。
    _, executable = engine._paths(ROOT)
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    command = [str(executable), '-X', 'utf8', str(Path(__file__).resolve()), '--inspect-native',
               '--source', str(source), '--output', str(output)]
    if pages:
        command += ['--pages', ','.join(map(str, pages))]
    process = None
    try:
        with (output / 'inspection-stderr.log').open('w', encoding='utf-8') as errors:
            process = subprocess.run(command, cwd=ROOT, env=engine.runtime_environment(ROOT),
                                     stdout=subprocess.PIPE, stderr=errors, encoding='utf-8', timeout=300, **flags)
    except (OSError, subprocess.SubprocessError) as exc:
        report['errors'].append({'stage': 'artifact_inspection', 'type': type(exc).__name__, 'message': str(exc)})
    if process is None or process.returncode or not (output / 'artifact-inspection.json').is_file():
        report['errors'].append({'stage': 'artifact_inspection', 'message': '产物检查失败，见 inspection-stderr.log。'})
    else:
        inspection = engine._read(output / 'artifact-inspection.json')
        report['artifacts'] = inspection
        report['replay_completed'] = bool(report['source_unchanged'] and len(inspection['outputs']) == 2
                                          and not inspection['geometry_errors']
                                          and not inspection.get('content_errors')
                                          and not inspection.get('missing_cjk_characters', 0)
                                          and inspection['tracking'].get('attempts', 0) > 0)
        mono = inspection['outputs'].get('mono')
        if mono:
            try:
                quality = pdf_layout_quality.inspect(source, output / mono, output / 'native-text-audit.json', [])
                _save(output / 'body-quality.json', quality)
                report['quality_status'] = quality['status']
                report['quality_counts'] = quality['counts']
                report['quality_coverage'] = quality['coverage']
            except Exception as exc:
                report['errors'].append({'stage': 'quality', 'type': type(exc).__name__, 'message': str(exc)})
    report['source_unchanged'] = engine._sha(source) == source_hash
    report['replay_completed'] = report['replay_completed'] and report['source_unchanged']
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report['translated_full_document_accepted'] = False
    _save(output / 'report.json', report)
    print(json.dumps({key: report[key] for key in ('replay_completed', 'production_gate_accepted',
                                                  'quality_status', 'requests', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--mode', choices=('identity', 'cached'), default='identity')
    parser.add_argument('--cache-folder', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pages', default='', help='仅指定预览页，版式处理和质量检查仍覆盖完整文档。')
    parser.add_argument('--inspect-native', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    pages = [int(number) for number in args.pages.split(',') if number]
    if args.inspect_native:
        inspect_native(args.source.resolve(), args.output.resolve(), pages)
        return 0
    report = verify(args.source, args.output, args.mode, args.cache_folder, pages)
    return 0 if report['replay_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
