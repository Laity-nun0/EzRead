"""Isolated BabelDOC adapter. Importing this module needs no PDF engine packages."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading

from codex_bridge import TranslationError
from ezread import processes
from ezread.pdf_native_adapter import native_translation_scope
from ezread.translation_validation import native_marker_assessment
from ezread.translation_validation import markers_match as markers_match

ENGINE_VERSION = '0.6.4'
_PROTOCOL = sys.stdout


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _paths(root):
    base = Path(root).resolve() / '.deps' / 'babeldoc'
    return base, base / 'runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def runtime_environment(root):
    base, _ = _paths(root)
    home, temporary = base / 'home', base / 'tmp'
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(HOME=str(home), USERPROFILE=str(home), TEMP=str(temporary), TMP=str(temporary),
               PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    env.pop('PYTHONPATH', None)
    return env


def availability(root):
    base, executable = _paths(root)
    result = {'available': False, 'engine': 'BabelDOC', 'version': ENGINE_VERSION,
              'message': '专业 PDF 引擎未准备，请运行 scripts/setup-pdf-layout.py。'}
    try:
        manifest = _read(base / 'runtime-manifest.json')
        if (not executable.is_file() or manifest.get('version') != ENGINE_VERSION
                or manifest.get('status') != 'verified' or not manifest.get('assets')):
            return result
        for asset in manifest['assets']:
            path = (base / 'home' / '.cache' / 'babeldoc' / asset['path']).resolve()
            if not path.is_relative_to(base.resolve()) or not path.is_file() or path.stat().st_size != asset['size']:
                result['message'] = '专业 PDF 引擎资源缺失或变化，请重新准备运行时。'
                return result
        fingerprint = hashlib.sha256((base / 'runtime-manifest.json').read_bytes()
                                     + (Path(root) / 'requirements-pdf-layout.txt').read_bytes()
                                     + Path(__file__).read_bytes()
                                     + (Path(__file__).parent / 'ezread/pdf_native_adapter.py').read_bytes()
                                     + (Path(__file__).parent / 'ezread/translation_validation.py').read_bytes()).hexdigest()
        result.update(available=True, fingerprint=fingerprint,
                      message='BabelDOC 0.6.4 独立运行时准备校验有效；任务启动时复核资源。')
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def _stop(process):
    if process.poll() is None:
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    processes.release(process)


def run_native(root, source, run_dir, *, translate, cancel_event=None, on_progress=None, glossary=None):
    """运行上游完整 LLM 流程；translate 接收原生 prompt 请求并返回原始响应。"""
    return run_stage(root, source, run_dir, 'native', translate=translate,
                     cancel_event=cancel_event, on_progress=on_progress, glossary=glossary)


def run_stage(root: Path, source: Path, run_dir: Path, mode: str, *, translations=None,
              translate=None, cancel_event=None, on_progress=None, glossary=None):
    if mode != 'native':
        raise TranslationError('旧版 collect/render 流程已停用，请重新运行原生 PDF 翻译。',
                               retryable=False, code='pdf_engine_legacy')
    if not callable(translate):
        raise ValueError('Native PDF translation requires a translation callback.')
    if cancel_event is not None and cancel_event.is_set():
        raise TranslationError('PDF 任务已取消。', retryable=False, code='cancelled')
    root, source, run_dir = Path(root).resolve(), Path(source).resolve(), Path(run_dir).resolve()
    if not source.is_file():
        raise TranslationError('PDF 原文文件不存在。', retryable=False, code='pdf_source')
    status = availability(root)
    if not status['available']:
        raise TranslationError(status['message'], retryable=False, code='pdf_engine_unavailable')
    run_dir.mkdir(parents=True, exist_ok=True)
    source_sha = _sha(source)
    request = {'source': str(source), 'source_sha256': source_sha, 'run_dir': str(run_dir), 'mode': mode}
    if glossary is not None:
        from ezread.translation_glossary import validate_native_seed
        request['glossary'] = validate_native_seed(glossary)
    request_path, result_path = run_dir / 'native-request.json', run_dir / 'native-result.json'
    _write(request_path, request)
    result_path.unlink(missing_ok=True)
    (run_dir / 'rotation-output-validation.json').unlink(missing_ok=True)
    _, executable = _paths(root)
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    events = queue.Queue()
    with (run_dir / 'native-stderr.log').open('w', encoding='utf-8') as errors:
        process = processes.spawn([str(executable), str(root / 'pdf_layout_engine.py'),
                                   '--worker', str(request_path)], cwd=root,
                                  env=runtime_environment(root), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8', **flags)
        def read_events():
            try:
                for entry in process.stdout:
                    events.put(entry)
            finally:
                events.put(None)
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        seen = set()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise TranslationError('PDF 任务已取消。', retryable=False, code='cancelled')
                try:
                    entry = events.get(timeout=.1)
                except queue.Empty:
                    continue
                if entry is None:
                    break
                try:
                    event = json.loads(entry)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'translation_request':
                    key = event.get('id')
                    if (not isinstance(key, str) or key in seen or event.get('mode') not in ('llm', 'text')
                            or not isinstance(event.get('prompt'), str) or not event['prompt'].strip()
                            or not isinstance(event.get('rate_limit_params'), dict)):
                        raise TranslationError('PDF 引擎返回无效翻译请求。', code='pdf_engine_protocol')
                    seen.add(key)
                    response = translate(event)
                    if cancel_event is not None and cancel_event.is_set():
                        raise TranslationError('PDF 任务已取消。', retryable=False, code='cancelled')
                    if not isinstance(response, str) or not response.strip():
                        raise TranslationError('PDF 翻译响应为空。', code='pdf_engine_response')
                    process.stdin.write(json.dumps({'id': key, 'response': response}, ensure_ascii=False) + '\n')
                    process.stdin.flush()
                elif on_progress:
                    on_progress(event)
            code = process.wait(timeout=10)
            if code or not result_path.is_file():
                rotation_check = run_dir / 'rotation-output-validation.json'
                if rotation_check.is_file() and _read(rotation_check).get('blocked_pages'):
                    raise TranslationError('旋转页面的正文或图文内容缺失，译文未提交；原 PDF 和响应缓存已保留。',
                                           retryable=False, code='pdf_rotation_content')
                raise TranslationError('专业 PDF 引擎未完成；诊断保存在本任务日志。', code='pdf_engine')
            result = _read(result_path)
            if result.get('source_sha256') != source_sha or _sha(source) != source_sha:
                raise TranslationError('PDF 引擎返回了不匹配的原文版本。', code='pdf_engine')
            for name in ('mono_pdf', 'dual_pdf', 'audit_path'):
                output = (run_dir / result[name]).resolve()
                if (not output.is_relative_to(run_dir) or not output.is_file()
                        or (name != 'audit_path' and output.stat().st_size < 100)):
                    raise TranslationError('PDF 引擎缺少有效产物。', code='pdf_engine')
            if result.get('translation_requests') != len(seen) or not seen:
                raise TranslationError('PDF 引擎翻译请求计数不一致。', code='pdf_engine')
            return result
        finally:
            _stop(process)
            reader.join(timeout=2)
            if not reader.is_alive():
                process.stdout.close()
            process.stdin.close()


class _TranslationChannel:
    """上游线程通过一个受锁保护的 JSONL 通道请求模型，不改写其 prompt。"""
    def __init__(self, incoming=None, outgoing=None):
        self.incoming = incoming if incoming is not None else sys.stdin
        self.outgoing = outgoing if outgoing is not None else _PROTOCOL
        self.lock = threading.Lock()
        self.requests = []
        self.failures = []

    def request(self, prompt, rate_limit_params=None, mode='llm'):
        if prompt is None:  # 上游能力探测，不是翻译请求。
            return ''
        with self.lock:
            if self.failures:
                raise ValueError('Native translation has already failed; no further request was sent.')
            key = str(len(self.requests) + 1)
            event = {'type': 'translation_request', 'id': key, 'mode': mode,
                     'prompt': prompt, 'rate_limit_params': rate_limit_params or {}}
            self.requests.append(event)
            try:
                self.outgoing.write(json.dumps(event, ensure_ascii=False) + '\n')
                self.outgoing.flush()
                line = self.incoming.readline()
                if not line:
                    raise ValueError('Translation channel closed without a response.')
                response = json.loads(line)
                if (not isinstance(response, dict) or response.get('id') != key
                        or not isinstance(response.get('response'), str) or not response['response'].strip()):
                    raise ValueError('Invalid native translation response.')
                return response['response']
            except Exception as exc:
                self.failures.append(str(exc))
                raise


def _audit_tracking(tracking, min_text_length=5, *, warnings=None):
    """核对最终对象契约；上游恢复标记仅提示核对，不能代替最终输出判断。"""
    warnings = warnings if warnings is not None else []
    if not isinstance(tracking, dict) or not all(key in tracking for key in ('page', 'cross_page', 'cross_column')):
        raise ValueError('Missing native translation tracking groups.')
    paragraphs, fallback_count = [], 0
    for group in ('page', 'cross_page', 'cross_column'):
        for page_index, page in enumerate(tracking[group]):
            for item in page['paragraph']:
                source = item.get('input')
                if not isinstance(source, str):
                    raise ValueError('Invalid native paragraph input.')
                attempts = item.get('llm_translate_trackers', [])
                if not attempts and len(source) < min_text_length:
                    continue  # 上游显式跳过短文本。
                output = item.get('output')
                final = attempts[-1] if attempts else {}
                if not isinstance(output, str) or not output.strip():
                    raise ValueError('Native paragraph translation was not fully applied.')
                assessment = native_marker_assessment(source, output)
                if assessment['error']:
                    raise ValueError('Native paragraph placeholders changed.')
                warnings.extend(assessment['warnings'])
                if (not final.get('output') or final.get('fallback_to_translate')
                        or not final.get('placeholder_full_match') or item.get('removed_hallucinated_placeholders')):
                    raise ValueError('Native final translation attempt was not fully applied.')
                if final.get('has_error'):
                    # 上游把单项异常写到整批 tracker；原子 post 已检查实际回填，不能误拒同批成功项。
                    warnings.append(f'上游 {group} 分组第 {page_index + 1} 项带有共享批次错误标记，最终回填已完成，请核对。')
                if any(attempt.get('has_error') or attempt.get('fallback_to_translate') for attempt in attempts[:-1]):
                    warnings.append(f'上游 {group} 分组第 {page_index + 1} 项包含恢复或占位符核对记录，请核对最终段落。')
                fallback_count += max(0, len(attempts) - 1)
                paragraphs.append({**item, 'group': group, 'group_page': page_index + 1})
    if not paragraphs:
        raise ValueError('No translated native paragraphs were verified.')
    return paragraphs, fallback_count


def _unchanged_paragraphs(paragraphs):
    """姓名、缩写或代码可以保持原样；长自然语言原样返回须明确待核对。"""
    unchanged = [item for item in paragraphs if item['input'].strip() == item['output'].strip()]
    prose = [item for item in unchanged if len(item['input']) >= 50
             and len(re.findall(r'[A-Za-z]+', item['input'])) >= 8]
    return len(unchanged), len(prose)


def _verify_output_characters(translated, paragraphs):
    """跨页跟踪可能重复、PDF 文字层可能不完整，字符计数差异仅作核对提示。"""
    is_cjk = lambda char: '\u3400' <= char <= '\u9fff' or '\U00020000' <= char <= '\U0002fa1f'
    expected = Counter(char for item in paragraphs for char in item['output'] if is_cjk(char))
    actual = Counter(char for page in translated for char in page.get_text() if is_cjk(char))
    missing = sum((expected - actual).values())
    return [f'PDF 文字层与上游记录的中文字符计数相差 {missing} 次，请对照页面核对是否漏字。'] if missing else []


def _source_audit(source, paragraphs):
    """仅对可唯一对应原 PDF 文字块的段落导出几何；其余明确未评估。"""
    normalize = lambda text: re.sub(r'\s+', '', text or '')
    blocks = []
    for index, page in enumerate(source):
        for block in page.get_text('blocks'):
            if block[6] != 0:
                continue
            if getattr(page, 'rotation', 0):
                import pymupdf
                box = pymupdf.Rect(block[:4]) * page.rotation_matrix
                block = (*box, *block[4:])
            blocks.append((index + 1, page.rect.height, block))
    result = []
    for paragraph in paragraphs:
        matches = [(page, height, block) for page, height, block in blocks
                   if normalize(block[4]) == normalize(paragraph.get('pdf_unicode'))]
        if len(matches) == 1:
            page, height, block = matches[0]
            result.append({'page': page, 'bbox': [block[0], height - block[3], block[2], height - block[1]],
                           'text': paragraph['pdf_unicode'], 'input': paragraph['input'],
                           'layout': 'plain text', 'glyphs': []})
    return result


def _page_text_rotation(page):
    """按原始内容坐标的正文字符数确定整页主方向，不以 /Rotate 猜测字形方向。"""
    votes = Counter()
    for block in page.get_text('dict', flags=0)['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            if line.get('wmode', 0) != 0:
                continue  # 原生竖排字体不等于横排正文整体旋转。
            x, y = line['dir']
            angle = math.degrees(math.atan2(-y, x)) % 360
            quadrant = round(angle / 90) * 90 % 360
            if abs((angle - quadrant + 180) % 360 - 180) < .1:
                votes[quadrant] += sum(len(span['text'].strip()) for span in line['spans'])
    return votes.most_common(1)[0][0] if votes else 0


def _normalize_source_rotation(source_path, run_dir):
    """在任务副本使用水平内容坐标；最终恢复原显示方向，原文件始终只读。"""
    import pymupdf
    with pymupdf.open(source_path) as source:
        plans = [{'page': index + 1, 'original_rotation': page.rotation,
                  'processing_rotation': _page_text_rotation(page)} for index, page in enumerate(source)]
        changed = [item for item in plans if item['original_rotation'] or item['processing_rotation']]
        if not changed:
            return Path(source_path), {'normalized': False, 'pages': []}
        folder = Path(run_dir) / 'native-input'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / Path(source_path).name
        if target.resolve() == Path(source_path).resolve():
            raise ValueError('Rotation normalization cannot overwrite the source PDF.')
        temporary = target.with_suffix('.normalizing.pdf')
        temporary.unlink(missing_ok=True)
        report = {'normalized': False, 'pages': changed, 'method': 'canonical-content-coordinates',
                  'input': target.relative_to(run_dir).as_posix()}
        try:
            with pymupdf.open() as normalized:
                for page, item in zip(source, plans):
                    angle = item['processing_rotation']
                    item['restore_rotation'] = (item['original_rotation'] - angle) % 360
                    page.set_rotation(0)
                    if angle:
                        rect = page.rect
                        width, height = ((rect.height, rect.width) if angle in (90, 270)
                                         else (rect.width, rect.height))
                        output = normalized.new_page(width=width, height=height)
                        # remove_rotation 在偏移 CropBox 上可能扩页；使用原生矢量嵌页保留可视裁切。
                        output.show_pdf_page(output.rect, source, page.number, rotate=-angle)
                    else:
                        normalized.insert_pdf(source, from_page=page.number, to_page=page.number)
                normalized.set_metadata(source.metadata)
                if source.get_xml_metadata():
                    normalized.set_xml_metadata(source.get_xml_metadata())
                normalized.save(temporary, garbage=3, deflate=True)
            with pymupdf.open(source_path) as original, pymupdf.open(temporary) as normalized:
                for item in changed:
                    number = item['page']
                    before, after = original[number - 1], normalized[number - 1]
                    if after.rotation:
                        raise ValueError('Canonical input still has page rotation.')
                    # 归一化输入的方向可暂时改变；还原后必须与源页几何、像素及文字等价。
                    after.set_rotation(item['restore_rotation'])
                    if any(abs(a - b) > .01 for a, b in zip(before.rect, after.rect)):
                        raise ValueError('Rotation normalization changed page geometry.')
                    matrix = pymupdf.Matrix(1.5, 1.5)
                    a = before.get_pixmap(matrix=matrix, alpha=False, annots=False)
                    b = after.get_pixmap(matrix=matrix, alpha=False, annots=False)
                    if (a.width, a.height, a.n) != (b.width, b.height, b.n):
                        raise ValueError('Rotation normalization changed raster geometry.')
                    differences = [abs(x - y) for x, y in zip(a.samples, b.samples)]
                    mean = sum(differences) / len(differences)
                    changed_ratio = sum(delta > 8 for delta in differences) / len(differences)
                    text_before = Counter(c for c in before.get_text() if not c.isspace())
                    text_after = Counter(c for c in after.get_text() if not c.isspace())
                    item.update(size=[before.rect.width, before.rect.height],
                                mean_channel_difference=mean, changed_channel_ratio=changed_ratio,
                                text_characters_equal=text_before == text_after,
                                source_vectors=len(before.get_drawings()), normalized_vectors=len(after.get_drawings()),
                                source_images=len(before.get_images()), normalized_images=len(after.get_images()))
                    if mean > .05 or changed_ratio > .001 or text_before != text_after:
                        raise ValueError('Rotation normalization changed visible page content.')
            temporary.replace(target)
            report['normalized'] = True
        except Exception as exc:
            report['error'] = str(exc)
            _write(Path(run_dir) / 'rotation-normalization.json', report)
            raise
    _write(Path(run_dir) / 'rotation-normalization.json', report)
    return target, report


def _restore_source_rotation(source_path, mono, dual, report, config):
    """恢复单语显示方向，再复用上游双栏拼版；不修改文字、公式或翻译响应。"""
    if not report['normalized']:
        return
    import pymupdf
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
    temporary = mono.with_suffix('.restoring.pdf')
    try:
        temporary.unlink(missing_ok=True)
        with pymupdf.open(source_path) as source, pymupdf.open(mono) as translated, pymupdf.open() as restored:
            if len(source) != len(translated):
                raise ValueError('Cannot restore rotation after a page count change.')
            plans = {item['page']: item for item in report['pages']}
            for index, (before, after) in enumerate(zip(source, translated)):
                expected = before.rect
                original_rotation = before.rotation
                angle = plans.get(index + 1, {}).get('processing_rotation', 0)
                if angle:
                    before.set_rotation(0)
                    after.set_rotation(0)
                    page = restored.new_page(width=before.rect.width, height=before.rect.height)
                    page.show_pdf_page(page.rect, translated, index, rotate=angle)
                else:
                    restored.insert_pdf(translated, from_page=index, to_page=index)
                    page = restored[-1]
                page.set_rotation(original_rotation)
                if any(abs(a - b) > .01 for a, b in zip(expected, page.rect)):
                    raise ValueError('Restored translation page geometry changed.')
            restored.set_metadata(translated.metadata)
            if translated.get_xml_metadata():
                restored.set_xml_metadata(translated.get_xml_metadata())
            restored.save(temporary, garbage=3, deflate=True)
        temporary.replace(mono)
        temporary = dual.with_suffix('.restoring.pdf')
        temporary.unlink(missing_ok=True)
        with pymupdf.open(source_path) as source, pymupdf.open(mono) as translated:
            # 上游拼版只使用路径供异常日志；实际内容直接取已打开的两份 PDF。
            from types import SimpleNamespace
            creator = SimpleNamespace(original_pdf_path=source_path)
            with PDFCreater.create_side_by_side_dual_pdf(creator, source, translated, str(temporary), config) as both:
                both.set_metadata(translated.metadata)
                if translated.get_xml_metadata():
                    both.set_xml_metadata(translated.get_xml_metadata())
                both.save(temporary, garbage=3, deflate=True)
        # 上游会记录嵌页异常后继续返回；左右半必须实际等价于各自输入，尺寸不能代替内容。
        checks = []
        try:
            with pymupdf.open(source_path) as source, pymupdf.open(mono) as translated, pymupdf.open(temporary) as both:
                if len(source) != len(both):
                    raise ValueError('Restored bilingual PDF page count changed.')
                for index, (before, after, page) in enumerate(zip(source, translated, both)):
                    width, height = before.rect.width, before.rect.height
                    for side, expected in enumerate((before, after)):
                        page.set_cropbox(pymupdf.Rect(side * width, 0, (side + 1) * width, height))
                        options = {'matrix': pymupdf.Matrix(.5, .5), 'colorspace': pymupdf.csGRAY,
                                   'alpha': False, 'annots': False}
                        a, b = expected.get_pixmap(**options), page.get_pixmap(**options)
                        if (a.width, a.height) != (b.width, b.height):
                            raise ValueError('Restored bilingual PDF raster geometry changed.')
                        differences = [abs(x - y) for x, y in zip(a.samples, b.samples)]
                        mean = sum(differences) / len(differences)
                        changed = sum(delta > 8 for delta in differences) / len(differences)
                        checks.append({'page': index + 1, 'side': 'original' if side == 0 else 'translation',
                                       'mean_channel_difference': mean, 'changed_channel_ratio': changed})
                        if mean > .05 or changed > .001:
                            raise ValueError('Restored bilingual PDF content differs from its input.')
        finally:
            _write(dual.parent / 'rotation-dual-validation.json', checks)
        temporary.replace(dual)
        report['output_restored'] = True
    finally:
        temporary.unlink(missing_ok=True)


def _rotation_output_coverage(source, translated, page_numbers):
    """粗网格检测旋转页的大面积内容消失；不能替代公式或逐字质量核对。"""
    import pymupdf
    def ink(page):
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(.5, .5), colorspace=pymupdf.csGRAY, alpha=False)
        tiles = [0] * 64
        for index, value in enumerate(pixmap.samples):
            if value < 223:
                x, y = index % pixmap.width, index // pixmap.width
                tiles[min(7, y * 8 // pixmap.height) * 8 + min(7, x * 8 // pixmap.width)] += 1
        return tiles
    results = []
    for number in page_numbers:
        before, after = ink(source[number - 1]), ink(translated[number - 1])
        total = sum(before)
        retained = sum(count for count, output in zip(before, after) if output >= max(3, .1 * count))
        ratio = retained / total if total else 1
        missing_text = (len(source[number - 1].get_text().strip()) >= 100
                        and not translated[number - 1].get_text().strip())
        results.append({'page': number, 'source_ink_pixels': total, 'output_ink_pixels': sum(after),
                        'occupied_region_retention': ratio,
                        'missing_text': missing_text,
                        'blocked': missing_text or (total >= 100 and sum(after) < .15 * total and ratio < .2),
                        'needs_review': total >= 100 and (sum(after) < .45 * total or ratio < .7)})
    return results


def _block_asset_downloads():
    from babeldoc.assets import assets
    attempts = []
    async def blocked(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError('PDF translation worker cannot download missing assets.')
    assets.get_fastest_upstream_for_font = blocked
    assets.get_fastest_upstream_for_model = blocked
    return attempts


async def _worker(request):
    if 'batch_policy' in request:
        raise ValueError('分批实验已停用，请重新翻译。')
    import logging
    import pymupdf
    from babeldoc.const import __version__
    from babeldoc.format.pdf.high_level import async_translate
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.translator.translator import BaseTranslator, OpenAITranslator

    if __version__ != ENGINE_VERSION or request['mode'] != 'native':
        raise ValueError('Unsupported native BabelDOC request.')
    _probe()
    network_attempts = _block_asset_downloads()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    run_dir, source_path = Path(request['run_dir']), Path(request['source'])
    if _sha(source_path) != request['source_sha256']:
        raise ValueError('Source PDF changed before worker start.')
    engine_input, rotation_report = _normalize_source_rotation(source_path, run_dir)
    channel = _TranslationChannel()

    class NativeTranslator(OpenAITranslator):
        name = 'ezread-native-llm'
        def __init__(self):
            # 缓存由父进程按原文、模型及完整 prompt 快照管理。
            BaseTranslator.__init__(self, 'en', 'zh', ignore_cache=True)
            self.model = 'ezread-native-snapshot'

        def do_llm_translate(self, text, rate_limit_params=None):
            output = channel.request(text, rate_limit_params, 'llm')
            if text is not None and 'glossary' in request and (rate_limit_params or {}).get('request_json_mode') is True:
                from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly
                from ezread.translation_validation import batch_values
                inputs = json.loads(text.rsplit('## Here is the input:\n\n', 1)[1])
                # 复用上游清理；结构异常在通道之外抛出，由原生循环安排单段恢复。
                batch_values(inputs, ILTranslatorLLMOnly._clean_json_output(None, output))
            return output

        def do_translate(self, text, rate_limit_params=None):
            return channel.request(text, rate_limit_params, 'text')

    output_dir, working_dir = run_dir / 'native/pdf', run_dir / 'native/working'
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)
    glossary_options = {}
    if 'glossary' in request:
        from babeldoc.glossary import Glossary, GlossaryEntry
        from ezread.translation_glossary import validate_native_seed
        seed = validate_native_seed(request['glossary'])
        glossary_options['glossaries'] = [Glossary('Paper glossary', [
            GlossaryEntry(entry['source'], entry['target'], 'zh') for entry in seed])] if seed else []
    config = TranslationConfig(
        translator=NativeTranslator(), input_file=engine_input, lang_in='en', lang_out='zh',
        doc_layout_model=None, output_dir=output_dir, working_dir=working_dir,
        debug=False, use_rich_pbar=False, min_text_length=5,
        pool_max_workers=1, auto_extract_glossary=False,
        save_auto_extracted_glossary=False,
        watermark_output_mode=WatermarkOutputMode.NoWatermark, **glossary_options)
    tracking_path = Path(config.get_working_file_path('translate_tracking.json'))
    tracking_path.unlink(missing_ok=True)
    finished = None
    validation_audit = {'failures': channel.failures, 'warnings': []}
    try:
        with native_translation_scope(channel.failures) as validation_audit:
            async for event in async_translate(config):
                if event['type'] == 'finish':
                    finished = event['translate_result']
                    break
                if event['type'] == 'error':
                    raise ValueError('BabelDOC reported a processing error.')
                with channel.lock:
                    print(json.dumps({key: value for key, value in event.items() if key != 'translate_result'},
                                     default=str, ensure_ascii=False), file=_PROTOCOL, flush=True)
    finally:
        _write(run_dir / 'native-translation-validation.json', validation_audit)
    if finished is None or channel.failures or network_attempts or not channel.requests:
        raise ValueError('Native engine did not finish every translation request.')
    review_warnings = list(validation_audit['warnings'])
    paragraphs, fallback_count = _audit_tracking(_read(tracking_path), warnings=review_warnings)
    unchanged, unchanged_prose = _unchanged_paragraphs(paragraphs)
    mono, dual = Path(finished.mono_pdf_path), Path(finished.dual_pdf_path)
    for path in (mono, dual):
        if not path.resolve().is_relative_to(run_dir.resolve()) or not path.is_file() or path.stat().st_size < 100:
            raise ValueError('Native engine PDF artifact missing.')
    _restore_source_rotation(source_path, mono, dual, rotation_report, config)
    if rotation_report['normalized']:
        _write(run_dir / 'rotation-normalization.json', rotation_report)
    with pymupdf.open(source_path) as source, pymupdf.open(mono) as translated, pymupdf.open(dual) as bilingual:
        if len(source) != len(translated) or len(source) != len(bilingual):
            raise ValueError('Native PDF page count changed.')
        for before, after, both in zip(source, translated, bilingual):
            if (abs(before.rect.width - after.rect.width) > 1 or abs(before.rect.height - after.rect.height) > 1
                    or abs(both.rect.width - 2 * before.rect.width) > 1
                    or abs(both.rect.height - before.rect.height) > 1):
                raise ValueError('Native PDF page geometry changed.')
        rotation_coverage = _rotation_output_coverage(
            source, translated, [item['page'] for item in rotation_report['pages']])
        blocked_pages = [item['page'] for item in rotation_coverage if item['blocked']]
        _write(run_dir / 'rotation-output-validation.json',
               {'pages': rotation_coverage, 'blocked_pages': blocked_pages})
        if blocked_pages:
            raise ValueError('Rotated page content missing; PDF publication blocked.')
        review_warnings.extend(_verify_output_characters(translated, paragraphs))
        review_warnings.extend(f"第 {item['page']} 页图文覆盖范围变化较大，请对照原 PDF 核对。"
                               for item in rotation_coverage if item['needs_review'])
        audit = _source_audit(source, paragraphs)
        page_count = len(source)
    if _sha(source_path) != request['source_sha256']:
        raise ValueError('Source PDF changed while engine was running.')
    audit_path = run_dir / 'native-text-audit.json'
    _write(audit_path, audit)
    result = {'engine_version': ENGINE_VERSION, 'pipeline': 'babeldoc-native-llm',
              'source_sha256': request['source_sha256'], 'page_count': page_count,
              'rotation_normalization': rotation_report, 'rotation_output_coverage': rotation_coverage,
              'upstream_configuration': {'skip_clean': config.skip_clean},
              'mono_pdf': mono.relative_to(run_dir).as_posix(), 'dual_pdf': dual.relative_to(run_dir).as_posix(),
              'audit_path': audit_path.name, 'translation_requests': len(channel.requests),
              'translation_audit': {'paragraphs': len(paragraphs), 'fallback_attempts': fallback_count,
                                    'geometry_matched_paragraphs': len(audit),
                                    'unchanged_paragraphs': unchanged, 'unchanged_prose_paragraphs': unchanged_prose},
              'quality': 'needs_review', 'protected_regions': [],
              'warnings': [*dict.fromkeys(review_warnings), '采用 BabelDOC 原生排版；图、表、公式及正文完整性仍需核对。',
                           f'原生翻译核验 {len(paragraphs)} 段，几何唯一匹配 {len(audit)} 段；未覆盖段落未评估。']}
    if unchanged_prose:
        result['warnings'].append(f'{unchanged_prose} 段较长英文原样返回，不能视为已完成中文翻译，请核对。')
    _write(run_dir / 'native-result.json', result)
    print(json.dumps({'type': 'finish', 'stage': 'native', 'page_count': page_count}), file=_PROTOCOL, flush=True)


def _probe(prepare=False, assets_list=False):
    from importlib.metadata import version
    import pymupdf
    import onnxruntime
    from bitstring import BitStream
    from babeldoc.assets.assets import generate_all_assets_file_list, warmup
    from babeldoc.const import CACHE_FOLDER
    if version('BabelDOC') != ENGINE_VERSION or version('bitstring') != '4.4.0':
        raise ValueError('Pinned runtime dependency versions do not match.')
    if assets_list:
        return generate_all_assets_file_list()
    if prepare:
        warmup()
    assets = []
    for folder, entries in generate_all_assets_file_list().items():
        for item in entries:
            path = CACHE_FOLDER / folder / item['name']
            if not path.is_file():
                raise ValueError(f'Missing official asset: {folder}/{item["name"]}')
            digest = hashlib.sha3_256(path.read_bytes()).hexdigest()
            if digest != item['sha3_256']:
                raise ValueError(f'Official asset checksum mismatch: {folder}/{item["name"]}')
            assets.append({'path': f'{folder}/{item["name"]}', 'size': path.stat().st_size,
                           'sha256': _sha(path), 'sha3_256': digest})
    return {'version': ENGINE_VERSION, 'status': 'verified', 'assets': assets,
            'pymupdf': pymupdf.VersionBind, 'onnxruntime': onnxruntime.__version__,
            'bitstream_import': bool(BitStream)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--prepare-assets', action='store_true')
    parser.add_argument('--assets-list', action='store_true')
    args = parser.parse_args()
    # Third-party prints must never contaminate the JSONL protocol.
    protocol = sys.stdout
    if args.worker:
        processes.attach_job()
        sys.stdout = sys.stderr
        asyncio.run(_worker(_read(args.worker)))
    else:
        sys.stdout = sys.stderr
        result = _probe(args.prepare_assets, args.assets_list)
        protocol.write(json.dumps(result, ensure_ascii=False) + '\n')
