"""生成保留版式的派生 PDF；不改写原文块、正文译文或批注。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time

from . import pdf_layout_quality, translation_numbers
from .context import ApplicationContext
from .translation_validation import MARKERS as MARKERS, emptied_styles, markers_match, native_marker_assessment

TASK = 'pdf_layout_translation'
CURRENT = 'pdf_layout_current'
VERSIONS = 'pdf_layout_versions'
DRAFTS = 'pdf_layout_drafts'
NATIVE_PROTOCOL = 'babeldoc-native-v3'
LEGACY_PROTOCOL = 'babeldoc-native-v2'
_MODEL_DIAGNOSTIC = re.compile(r'[【\[][^】\]\n]{0,80}(?:原文.{0,12}(?:截断|识别残缺|句子片段)|无法(?:可靠|完整)翻译)')


def task_protocol(task):
    if 'batch_policy' in task:
        raise ValueError('分批实验已停用，请重新翻译。')
    protocol = task.get('protocol', LEGACY_PROTOCOL)
    if protocol not in (LEGACY_PROTOCOL, NATIVE_PROTOCOL):
        raise ValueError('未知的版式翻译协议，未调用模型。')
    return protocol


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def source_file(app, pid):
    if not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{16}', pid):
        raise ValueError('文献标识无效。')
    base = (app.LIBRARY / pid).resolve()
    source = (base / 'original.pdf').resolve()
    if not base.is_relative_to(app.LIBRARY.resolve()) or not source.is_relative_to(base) or not source.is_file():
        raise ValueError('原 PDF 文件不存在。')
    return source


def run_folder(app, pid, run_id):
    source_file(app, pid)
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{16}', run_id):
        raise ValueError('版式译文版本无效。')
    base = (app.LIBRARY / pid).resolve()
    folder = (base / 'pdf-layout' / run_id).resolve()
    if not folder.is_relative_to(base):
        raise ValueError('版式译文目录无效。')
    return folder


def _has_edits(doc):
    return any(isinstance(b.get('text_override'), str) and b['text_override'] != b.get('text', '')
               for b in doc.get('blocks', []))


def check_edits(doc):
    # PDF 引擎读取原文件，无法可靠地把任意选区修订映射回 PDF 字形。
    if _has_edits(doc):
        raise ValueError('本篇英文有手动修订。版式译文读取原 PDF，暂不支持将修订写回；已有校订和译文保持不变。')


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def prepare(app: ApplicationContext, doc, options=None):
    import pdf_layout_engine
    from . import api_settings, translation_glossary
    options = options or {}
    if isinstance(options, dict) and 'batch_policy' in options:
        raise ValueError('分批实验已停用，请重新翻译。')
    allowed = {'profile_id', 'model', 'reasoning_effort', 'retranslate', 'provider', 'base_url', 'max_output_tokens', 'glossary', 'thinking'}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError('版式翻译配置包含未知字段。')
    if options.get('provider', 'deepseek') != 'deepseek':
        raise ValueError('全文翻译仅支持 DeepSeek；旧任务保留，请使用翻译设置重新翻译。')
    if 'retranslate' in options and type(options['retranslate']) is not bool:
        raise ValueError('重新翻译选项必须是布尔值。')
    for key in ('model', 'reasoning_effort'):
        if key in options and (not isinstance(options[key], str) or len(options[key]) > 160):
            raise ValueError('模型与推理强度必须是有效选项。')
    previous = doc.get(TASK, {})
    if previous.get('run_id') and not options.get('retranslate'):
        protocol = task_protocol(previous)
    check_edits(doc)
    available = pdf_layout_engine.availability(app.ROOT)
    if not available['available']:
        raise ValueError(available['message'])
    source_hash = file_hash(source_file(app, doc['id']))
    reuse_from = None
    seed = translation_glossary.validate_seed(options.get('glossary', previous.get('glossary_seed', [])))
    if previous.get('run_id') and not options.get('retranslate'):
        config = previous['config']
        if not config.get('provider'):
            raise ValueError('旧 Codex 任务不能继续翻译；请确认重新翻译，使用 API 重新生成并保留旧草稿。')
        if config['provider'] != 'deepseek':
            raise ValueError('当前仅支持 DeepSeek；旧译文与草稿保留，请使用翻译设置重新生成。')
        if options.get('provider', config['provider']) != config['provider']:
            raise ValueError('此版式任务已固定供应商；更换配置请明确选择重新翻译。')
        requested = api_settings.resolve_config(app.DATA, {**config, **options})
        if requested != config or seed != previous.get('glossary_seed', []):
            raise ValueError('此版式任务已固定模型配置；更换配置请明确选择重新翻译。')
        if previous.get('source_hash') != source_hash:
            raise ValueError('原 PDF 已变化，请重新翻译，不能混合续译。')
        published = any(v and v.get('id') == previous['run_id'] for v in [doc.get(CURRENT), *doc.get(VERSIONS, [])])
        if (not published and previous.get('engine_version') == available['version']
                and previous.get('engine_fingerprint') == available.get('fingerprint')):
            return
        # 已发布目录保持只读；继续时新建产物，复用同源、同模型的完整请求缓存。
        reuse_from = {k: copy.deepcopy(previous.get(k)) for k in
                      ('run_id', 'source_hash', 'config', 'engine_version', 'engine_fingerprint')}
    else:
        protocol = NATIVE_PROTOCOL
        config = api_settings.resolve_config(app.DATA, options, purpose='translation')
        if config['provider'] != 'deepseek':
            raise ValueError('全文翻译仅支持 DeepSeek，请使用设置中的翻译配置。')
        if config['provider'] == 'deepseek':
            config.setdefault('thinking', 'disabled')
            if config['thinking'] == 'enabled':
                config.setdefault('reasoning_effort', 'high')
    if protocol == NATIVE_PROTOCOL:
        seed = translation_glossary.validate_native_seed(seed)
    archive_draft(doc)
    doc[TASK] = {'status': 'queued', 'run_id': secrets.token_hex(8), 'config': copy.deepcopy(config),
                 'created_at': app.now(), 'source_hash': source_hash, 'engine_version': available['version'],
                 'engine_fingerprint': available.get('fingerprint'),
                 'protocol': protocol,
                 'stage': '等待本地版面分析', 'done': 0, 'total': 0, 'received': 0,
                 'review_pending': 0, 'rejected': 0, 'reused': 0, 'model_calls': 0, 'error': '', 'warnings': [], 'thread_id': None}
    if config.get('provider'):
        doc[TASK].update(glossary_seed=seed, glossary=translation_glossary.PaperGlossary(seed).snapshot())
    if reuse_from:
        doc[TASK]['reuse_from'] = reuse_from
        doc[TASK]['stage'] = '重新分析版面，并复用已译正文'


def assess_translation(source, translated, *, aliases=None, native_flow=False):
    marker_assessment = native_marker_assessment(source, translated) if native_flow else None
    if marker_assessment is not None and marker_assessment['error']:
        return marker_assessment
    if not native_flow and not markers_match(source, translated):
        return {'error': '公式或字体占位符缺失、重复、新增或配对结构改变', 'warnings': []}
    assessment = translation_numbers.assess_numbers(source, translated, aliases=aliases)
    warnings = [*(marker_assessment['warnings'] if marker_assessment else []), *assessment['warnings']]
    if emptied_styles(source, translated):
        warnings.append('部分字体片段在译文中为空，可能来自连字拆分或译序变化，请核对。')
    # 内容检测器是启发式；其差异报告不能成为整篇翻译或缓存复用的闸门。
    if assessment['error']:
        warnings.append('数字内容待核对：' + assessment['error'])
    if any(match[0] not in source for match in _MODEL_DIAGNOSTIC.finditer(translated)):
        warnings.append('译文含疑似原文残缺或无法翻译说明，请对照原 PDF 核对。')
    result = {'error': '', 'warnings': list(dict.fromkeys(warnings))}
    if marker_assessment is not None:
        result['style_degraded'] = marker_assessment['style_degraded']
    return result


def translation_issue(source, translated, *, aliases=None):
    return assess_translation(source, translated, aliases=aliases)['error']


def source_unit_aliases(path):
    """只在本地读取本篇 PDF 的明确单位定义，不猜测跨论文缩写。"""
    import pdf_tools
    import pypdfium2 as pdfium
    texts = []
    with pdf_tools._PDF_LOCK:
        with pdfium.PdfDocument(path) as pdf:
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        texts.append(textpage.get_text_range())
                    finally:
                        textpage.close()
                finally:
                    page.close()
    return translation_numbers.parse_unit_aliases(' '.join(' '.join(texts).split()))


def _pdf_geometry(path):
    import pdf_tools
    import pypdfium2 as pdfium
    with pdf_tools._PDF_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            sizes = []
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                        raise ValueError('译文 PDF 页面尺寸无效。')
                    sizes.append((width, height))
                finally:
                    page.close()
            return sizes
        finally:
            pdf.close()


def _output_file(folder, relative):
    if not isinstance(relative, str):
        raise ValueError('版式引擎未返回 PDF 路径。')
    result = (folder / relative).resolve()
    if not result.is_relative_to(folder.resolve()) or not result.is_file() or result.suffix.lower() != '.pdf':
        raise ValueError('版式 PDF 产物无效或越出任务目录。')
    return result


def _cache_sources(folder, task, identity):
    """仅续译时查找同论文、同请求身份的祖先；不改历史运行目录。"""
    if not task.get('reuse_from'):
        return [], []
    base = folder.parent.resolve()
    direct = task['reuse_from'].get('run_id')
    candidates = sorted(base.iterdir(), key=lambda item: (item.name != direct, item.name))
    sources, diagnostics = [], []
    for candidate in candidates:
        if candidate == folder or not re.fullmatch(r'[a-f0-9]{16}', candidate.name):
            continue
        resolved = candidate.resolve()
        manifest = resolved / 'native-session.json'
        if not resolved.is_relative_to(base) or not manifest.resolve().is_relative_to(resolved):
            diagnostics.append({'run_id': candidate.name, 'status': 'unsafe_path'})
            continue
        try:
            saved = _read_json(manifest, None)
        except (OSError, ValueError):
            diagnostics.append({'run_id': candidate.name, 'status': 'invalid_manifest'})
            continue
        fields = ('source_hash', 'config', 'engine_version', 'protocol')
        matches = (isinstance(saved, dict) and 'batch_policy' not in saved
                   and all(k in saved and saved[k] == identity[k] for k in fields))
        if matches and resolved not in sources:
            sources.append(resolved)
        diagnostics.append({'run_id': candidate.name, 'status': 'matched' if matches else 'identity_mismatch'})
    return sources, diagnostics


def execute(app: ApplicationContext, pid, event):
    from contextlib import ExitStack
    import codex_bridge
    import pdf_layout_engine
    from . import api_settings, translation_api, translation_glossary
    doc = app.get_doc(pid)
    task = copy.deepcopy(doc[TASK])
    protocol = task_protocol(task)
    check_edits(doc)
    run_id, config = task['run_id'], task['config']
    standard_native = protocol == NATIVE_PROTOCOL
    if not config.get('provider'):
        raise ValueError('旧 Codex 任务已停止；请确认重新翻译，使用 API 重新生成并保留旧草稿。')
    if any(v and v.get('id') == run_id for v in [doc.get(CURRENT), *doc.get(VERSIONS, [])]):
        raise ValueError('该版式任务目录已发布，不能覆盖；请重新确认继续翻译以创建新版本。')
    source = source_file(app, pid)
    folder = run_folder(app, pid, run_id)
    folder.mkdir(parents=True, exist_ok=True)

    def cancelled():
        if event.is_set() or app.SHUTDOWN.is_set():
            raise codex_bridge.TranslationError('版式翻译已暂停，已完成的文字单元保留。', code='cancelled')

    def update(**fields):
        def merge(d):
            if d.get(TASK, {}).get('run_id') != run_id:
                raise ValueError('版式翻译任务已变化，旧结果未覆盖新任务。')
            d[TASK].update(fields)
        app.update_doc(pid, merge)

    cancelled()
    if file_hash(source) != task['source_hash']:
        raise ValueError('原 PDF 已变化，请重新翻译。')
    update(status='running', error='', error_code='', error_details={}, stage='正在本地分析版面')
    from .pdf_native_translation import RequestCache, request_units
    glossary = translation_glossary.PaperGlossary(task.get('glossary_seed', []))
    identity = {'source_hash': task['source_hash'], 'config': config, 'engine_version': task['engine_version'],
                'engine_fingerprint': task.get('engine_fingerprint'),
                'protocol': protocol, 'glossary_seed': task.get('glossary_seed', [])}
    if pdf_layout_engine.availability(app.ROOT).get('fingerprint') != task.get('engine_fingerprint'):
        raise ValueError('版式组件已变化，请继续以新组件重新分析，已有译文仍保留。')
    manifest_path = folder / 'native-session.json'
    manifest = _read_json(manifest_path, None)
    if manifest is not None and manifest != identity:
        raise ValueError('版式草稿来源、模型或组件不一致，未混合续译。')
    _write_json(manifest_path, identity)
    previous_folders, cache_sources = _cache_sources(folder, task, identity)
    if task.get('reuse_from'):
        _write_json(folder / 'cache-sources.json', cache_sources)
    aliases = source_unit_aliases(source)
    _write_json(folder / 'unit-aliases.json', {
        'source_hash': task['source_hash'],
        'aliases': {name: [value[0], str(value[1])] if value else None for name, value in aliases.items()}})
    cache = RequestCache(folder, identity,
                         previous_folders=previous_folders, retry_rejected=True, aliases=aliases,
                         native_flow=True)
    thread_id = task.get('thread_id')
    model_seconds = 0.0
    transport = None
    usage_by_request = {}

    def call(request):
        nonlocal model_seconds, transport
        started = time.monotonic()
        try:
            if transport is None:
                transport = connections.enter_context(translation_api.ApiTranslationSession(
                    config, api_settings.get_api_key(app.DATA, config), usage=getattr(app, 'API_USAGE', None),
                    diagnostics_dir=folder / 'api-diagnostics'))
            return transport.translate(request['prompt'], request.get('translation_context', []), cancel_event=event,
                                       native_batch=request.get('rate_limit_params', {}).get('request_json_mode') is True,
                                       native=True, **({'standard_native': True} if standard_native else {}))
        finally:
            model_seconds += time.monotonic() - started

    def counts():
        return dict(received=cache.received, done=cache.done, review_pending=cache.review_pending,
                    rejected=cache.rejected, reused=cache.reused, model_calls=cache.model_calls)

    def translate(request):
        cancelled()
        units, batch = request_units(request)
        if not standard_native:
            request = {**request, 'translation_context': glossary.context(units)}
        try:
            response = cache.resolve(request, call)
            if not cache.rejected:
                glossary.accept(units, response, cache.last_metadata.get('terms', []), batch=batch)
            # 从冻结种子与已验证缓存重建术语；不把上次末尾状态当成第一批的上下文。
            _write_json(folder / 'glossary.json', glossary.snapshot())
            update(glossary=glossary.snapshot())
        finally:
            usage_key = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            usage_by_request[usage_key] = cache.last_metadata.get('usage', {})
            # 响应失败也公布已收到的数量，不能用 0/0 掩盖已付费返回。
            update(**counts(), thread_id=thread_id, warnings=[*cache.warnings, *glossary.warnings],
                   stage='响应校验未通过；已返回内容已保留' if cache.rejected else '上游正在翻译与排版')
        cancelled()
        return response

    def progress(value):
        stage = value.get('stage', '')
        update(stage='上游 PDF 流程：' + stage if stage else '正在执行上游 PDF 流程')

    engine_started = time.monotonic()
    try:
        with ExitStack() as connections:
            glossary_options = {'glossary': translation_glossary.validate_native_seed(task.get('glossary_seed', []))} if standard_native else {}
            result = pdf_layout_engine.run_native(app.ROOT, source, folder, translate=translate,
                                                 cancel_event=event, on_progress=progress, **glossary_options)
            if standard_native:
                # 引擎已核验最终正文和公式，缓存层的格式疑点不再是未通过单元。
                cache.rejected = 0
    finally:
        # 本次执行计时，不混入旧缓存的耗时；失败和取消也留下定位依据。
        elapsed = time.monotonic() - engine_started
        _write_json(folder / 'translation-timing.json', {
            'engine_seconds': round(elapsed, 3), 'model_roundtrip_seconds': round(model_seconds, 3),
            'local_and_transport_seconds': round(max(0, elapsed - model_seconds), 3), **counts()})
        _write_json(folder / 'api-usage.json', {'requests': usage_by_request,
                    'note': '按唯一已接受请求记录供应商返回的用量，包含缓存中原调用记录，不代表本次计费。'})
    warnings = [*result.get('warnings', []), *cache.warnings, *glossary.warnings]
    cancelled()
    mono = _output_file(folder, result.get('mono_pdf'))
    dual = _output_file(folder, result.get('dual_pdf'))
    source_sizes, sizes = _pdf_geometry(source), _pdf_geometry(mono)
    dual_sizes = _pdf_geometry(dual)
    if (not sizes or len(sizes) != len(source_sizes) or len(dual_sizes) != len(sizes)
            or any(abs(x - y) > .1 for before, after in zip(source_sizes, sizes) for x, y in zip(before, after))):
        raise ValueError('译文页数或页面尺寸与原 PDF 不一致，旧版本仍保留。')
    if any(abs(dw - sw * 2) > .1 or abs(dh - sh) > .1
           for (sw, sh), (dw, dh) in zip(source_sizes, dual_sizes)):
        raise ValueError('双语 PDF 页面结构不一致，旧版本仍保留。')
    update(stage='正在本地核对译文字形与排版')
    quality = pdf_layout_quality.inspect(source, mono, folder / result.get('audit_path', 'native-text-audit.json'),
                                         result.get('protected_regions', []), cancel_event=event)
    _write_json(folder / 'body-quality.json', quality)
    cancelled()
    # 字形重叠、字号等阈值仅反映疑似排版风险，原始失败证据仍保留于私有报告。
    quality_warnings = [finding.get('message') or f"第 {finding.get('page', '?')} 页疑似排版异常，请核对。"
                        for finding in [*quality['warnings'], *quality['failures']]]
    warnings = list(dict.fromkeys([*warnings, *quality_warnings]))
    artifact = {'id': run_id, 'created_at': app.now(), 'config': config, 'source_hash': task['source_hash'],
                'protocol': protocol,
                'engine_version': task['engine_version'], 'page_count': len(sizes),
                'engine_fingerprint': task.get('engine_fingerprint'),
                'pages': [{'number': i + 1, 'width': w, 'height': h} for i, (w, h) in enumerate(sizes)],
                'mono': mono.relative_to(folder).as_posix(), 'dual': dual.relative_to(folder).as_posix(),
                'mono_hash': file_hash(mono), 'dual_hash': file_hash(dual),
                'warnings': warnings, 'quality': 'needs_review' if warnings or result.get('quality') != 'passed'
                or quality['status'] != 'passed' else 'passed'}
    _write_json(folder / 'artifact.json', artifact)

    def publish(d):
        cancelled()
        check_edits(d)
        if d.get(TASK, {}).get('run_id') != run_id or file_hash(source) != task['source_hash'] or d.get('deleted'):
            raise ValueError('文献或任务已变化，结果保留为草稿，未覆盖当前译文 PDF。')
        previous = d.get(CURRENT)
        if previous and previous['id'] != run_id and not any(v['id'] == previous['id'] for v in d.get(VERSIONS, [])):
            d.setdefault(VERSIONS, []).append(copy.deepcopy(previous))
        d[CURRENT] = artifact
        d[TASK].update(status='completed', **counts(), total=cache.done, warnings=warnings,
                       stage='已生成版式译文，部分内容需核对' if warnings else '已生成版式译文', error='')
    app.update_doc(pid, publish)


def _public_artifact(app, pid, artifact, *, pages=True):
    result = {k: copy.deepcopy(artifact.get(k)) for k in ('id', 'created_at', 'config', 'page_count', 'quality', 'warnings')}
    prefix = f"pdf-layout/{artifact['id']}/"
    result['mono_url'] = app.media_url(pid, prefix + 'mono.pdf')
    result['dual_url'] = app.media_url(pid, prefix + 'dual.pdf')
    if pages:
        result['pages'] = [{**p, 'image_url': app.media_url(pid, prefix + f"page-{p['number']:03}.jpg")}
                           for p in artifact.get('pages', [])]
    return result


def public_state(app, doc, full=False):
    task = doc.get(TASK, {})
    result = {k: copy.deepcopy(task[k]) for k in ('status', 'run_id', 'config', 'stage', 'done', 'total', 'error', 'warnings', 'error_code', 'error_details',
                                  'received', 'review_pending', 'rejected', 'reused', 'model_calls',
                                  'glossary_seed', 'glossary') if k in task}
    result.setdefault('status', 'idle')
    result['current'] = _public_artifact(app, doc['id'], doc[CURRENT], pages=full) if doc.get(CURRENT) else None
    if result['current'] and _has_edits(doc):
        result['current']['quality'] = 'needs_review'
        result['current']['warnings'] = [*result['current'].get('warnings', []),
                                          '旧正文修订仍保留；此 PDF 依据原文件生成，未包含修订。']
    result['versions'] = [_public_artifact(app, doc['id'], v, pages=False) for v in reversed(doc.get(VERSIONS, []))]
    result['drafts'] = [{k: copy.deepcopy(task.get(k)) for k in ('created_at', 'config', 'done', 'total', 'stage', 'status',
                                                                              'received', 'review_pending', 'rejected', 'reused', 'model_calls')}
                        | {'id': task['run_id']} for task in reversed(doc.get(DRAFTS, []))]
    return result


def archive_draft(doc):
    task = doc.get(TASK, {})
    if task.get('run_id') and task.get('status') != 'completed':
        drafts = doc.setdefault(DRAFTS, [])
        drafts[:] = [d for d in drafts if d['run_id'] != task['run_id']]
        drafts.append(copy.deepcopy(task))


def restore_draft(app, pid, draft_id):
    import pdf_layout_engine
    with app.LOCK:
        if (pid, 'pdf_translate') in app.QUEUED:
            raise ValueError('请先暂停版式翻译并等待当前任务停止。')
        doc = app.get_doc(pid)
        task = next((t for t in doc.get(DRAFTS, []) if t['run_id'] == draft_id), None)
        if task is None:
            raise ValueError('版式翻译草稿不存在。')
        check_edits(doc)
        available = pdf_layout_engine.availability(app.ROOT)
        if task.get('source_hash') != file_hash(source_file(app, pid)):
            raise ValueError('草稿的原 PDF 已变化，请重新翻译。')
        archive_draft(doc)
        doc[TASK] = copy.deepcopy(task)
        message = ('已恢复版式翻译草稿，可确认继续。' if task['config'].get('provider') else
                   '已恢复旧 Codex 草稿；请使用 API 重新生成，旧草稿与缓存将继续保留。')
        if task.get('engine_fingerprint') != available.get('fingerprint'):
            message += '组件已变化，确认继续后将在新目录重新分析并核对可复用缓存。'
        doc[TASK].update(status='paused', stage='翻译已暂停', error=message)
        doc['updated_at'] = app.now()
        app.put_doc(doc)


def restore(app, pid, version_id):
    with app.LOCK:
        if (pid, 'pdf_translate') in app.QUEUED:
            raise ValueError('请先暂停版式翻译并等待当前任务停止。')
        doc = app.get_doc(pid)
        saved = next((v for v in doc.get(VERSIONS, []) if v['id'] == version_id), None)
        if saved is None:
            raise ValueError('版式译文版本不存在。')
        folder = run_folder(app, pid, saved['id'])
        for key in ('mono', 'dual'):
            if file_hash(_output_file(folder, saved[key])) != saved[key + '_hash']:
                raise ValueError('该版式译文文件已变化，未恢复。')
        if doc.get(CURRENT) and doc[CURRENT]['id'] != saved['id'] and not any(v['id'] == doc[CURRENT]['id'] for v in doc.get(VERSIONS, [])):
            doc.setdefault(VERSIONS, []).append(copy.deepcopy(doc[CURRENT]))
        doc[CURRENT] = copy.deepcopy(saved)
        doc['updated_at'] = app.now()
        app.put_doc(doc)


def media_file(app, pid, relative):
    match = re.fullmatch(r'pdf-layout/([a-f0-9]{16})/(mono\.pdf|dual\.pdf|page-(\d{3,})\.jpg)', relative)
    if not match:
        raise ValueError('无效的版式译文文件。')
    doc = app.get_doc(pid)
    run_id, name, number = match.groups()
    versions = [doc.get(CURRENT), *doc.get(VERSIONS, [])]
    artifact = next((v for v in versions if v and v['id'] == run_id), None)
    if artifact is None:
        raise ValueError('版式译文尚未提交或版本不存在。')
    folder = run_folder(app, pid, run_id)
    if number is None:
        return _output_file(folder, artifact['mono' if name == 'mono.pdf' else 'dual'])
    if not 1 <= int(number) <= artifact['page_count']:
        raise ValueError('版式译文页码超出范围。')
    target = (folder / name).resolve()
    if not target.is_relative_to(folder.resolve()):
        raise ValueError('无效的版式译文页面路径。')
    if not target.is_file():
        import pdf_tools
        pdf_tools.render_page(_output_file(folder, artifact['mono']), int(number), target)
    return target
