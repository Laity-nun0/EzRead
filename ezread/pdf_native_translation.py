"""上游提示词的传输与缓存边界；不修改 PDF 分段、公式或排版。"""
import hashlib
import json

from . import pdf_translations as layout
from .api_usage import numeric_usage
from .translation_validation import (
    ResponseValidationError as ResponseValidationError,
    batch_values as batch_values,
    load_response_json,
)

BATCH_INPUT = '## Here is the input:\n\n'
SINGLE_INPUT = 'Now translate the following text:\n\n'


def request_units(request):
    """固定上游版本的请求契约；未知格式失败，不猜测论文中的 JSON。"""
    prompt = request['prompt']
    if request.get('rate_limit_params', {}).get('request_json_mode'):
        if BATCH_INPUT not in prompt:
            raise ValueError('上游批次格式已变化，未调用模型。')
        units = json.loads(prompt.rsplit(BATCH_INPUT, 1)[1])
        if (not isinstance(units, list) or not units
                or any(not isinstance(u, dict) or type(u.get('id')) is not int
                       or not isinstance(u.get('input'), str) or not u['input'].strip() for u in units)
                or len({u['id'] for u in units}) != len(units)):
            raise ValueError('上游批次文字单元无效。')
        return units, True
    if request.get('mode') == 'text':
        return [{'id': 0, 'input': prompt}], False
    if SINGLE_INPUT not in prompt:
        raise ValueError('上游单段格式已变化，未调用模型。')
    return [{'id': 0, 'input': prompt.split(SINGLE_INPUT, 1)[1]}], False


def normalize_batch_response(response):
    """只恢复完整 JSON 字符串的一层额外转义，不修改已合法的批次数组。"""
    if not isinstance(response, str):
        return response
    try:
        value = load_response_json(response)
    except ValueError:
        try:
            # 部分供应商返回 JSON 字符串内部的转义内容，漏掉外围引号。
            candidate = load_response_json('"' + response + '"')
        except ValueError:
            return response
    else:
        if not isinstance(value, str):
            return response
        candidate = value
    try:
        if isinstance(load_response_json(candidate), list):
            return candidate
    except ValueError:
        pass
    return response


def response_values(units, batch, response, *, aliases=None, reviews=None, native_flow=False):
    if not isinstance(response, str) or not response.strip():
        raise ResponseValidationError('上游响应为空。', len(units), 'batch_empty')
    translated = batch_values(units, response) if batch else {0: response}
    errors = []
    for unit in units:
        value = translated[unit['id']]
        if not isinstance(value, str) or not value.strip():
            errors.append(('译文单元为空。', 'batch_empty'))
            continue
        assessment = layout.assess_translation(unit['input'], value, aliases=aliases, native_flow=native_flow)
        if assessment['error']:
            errors.append((assessment['error'], 'batch_placeholders'))
        elif assessment['warnings'] and reviews is not None:
            reviews.append({'id': unit['id'], 'source': unit['input'], 'translation': value,
                            'warnings': assessment['warnings']})
    if errors:
        raise ResponseValidationError('译文未提交：' + errors[0][0], len(errors), errors[0][1])
    return translated


def encode_response(units, batch, values):
    if batch:
        return json.dumps([{'id': u['id'], 'output': values[u['id']]} for u in units], ensure_ascii=False)
    return values[0]


class RequestCache:
    def __init__(self, folder, identity, *, previous_folder=None, previous_folders=(),
                 retry_rejected=False, aliases=None, native_flow=False):
        self.folder, self.identity = folder.resolve(), identity
        self.previous_folder = previous_folder
        self.previous_folders = list(dict.fromkeys(folder.resolve() for folder in
            ([previous_folder] if previous_folder is not None else []) + list(previous_folders)))
        self.cache_warnings = []
        self.retry_rejected = retry_rejected
        self.native_flow = native_flow
        self.upstream_fallback = identity.get('protocol') == 'babeldoc-native-v3'
        self.aliases = aliases or {}
        self.attempted = set()
        self.reused = self.done = self.model_calls = 0
        self.received = self.rejected = 0
        self.reviews = {}
        self.last_metadata = {}

    @property
    def review_pending(self):
        return sum(len(values) for values in self.reviews.values())

    @property
    def warnings(self):
        def excerpt(text):
            text = ' '.join(text.split())
            return text[:120] + ('…' if len(text) > 120 else '')
        return list(dict.fromkeys([*self.cache_warnings, *(' '.join(value['warnings']) +
                                  f" 原文片段：{excerpt(value['source'])}；译文片段：{excerpt(value['translation'])}"
                                  for values in self.reviews.values() for value in values)]))

    def _archive_bytes(self, key, contents):
        target = self.folder / 'native-replies-rejected' / key / (hashlib.sha256(contents).hexdigest() + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(contents)
        temporary.replace(target)

    def _cached(self, key, key_data, units, batch):
        """先纯校验所有候选，避免坏缓存遮住好回复或膨胀接收计数。"""
        invalid, current_bytes, current_error = None, None, None
        for folder in [self.folder, *self.previous_folders]:
            path = folder / 'native-replies' / (key + '.json')
            if not path.resolve().is_relative_to(folder):
                self.cache_warnings.append('历史缓存路径越界，已跳过；原文件保持不变。')
                if folder == self.folder:
                    raise ValueError('译文缓存路径越界。')
                continue
            if not path.is_file():
                continue
            try:
                contents = path.read_bytes()
                if folder == self.folder:
                    current_bytes = contents
                saved = load_response_json(contents)
                if not isinstance(saved, dict) or saved.get('request') != key_data:
                    raise ValueError('译文缓存来源不匹配。')
                if 'response' not in saved or not isinstance(saved.get('metadata', {}), dict):
                    raise ValueError('译文缓存记录不完整。')
            except OSError:
                if folder == self.folder:
                    raise  # 无法读取当前证据时禁止覆盖，不能只凭祖先回复继续写入。
                self.cache_warnings.append('部分历史缓存记录无法读取，已跳过；原文件保持不变。')
                continue
            except ValueError as exc:
                self.cache_warnings.append('部分历史缓存记录无法核对，已跳过；原文件保持不变。')
                if folder == self.folder:
                    current_error = exc
                continue
            response = (normalize_batch_response(saved['response'])
                        if batch and not self.upstream_fallback else saved['response'])
            try:
                response_values(units, batch, response, aliases=self.aliases, native_flow=self.native_flow)
            except ResponseValidationError:
                if invalid is None:
                    invalid = (saved, contents)
                continue
            if current_bytes is not None and folder != self.folder:
                self._archive_bytes(key, current_bytes)
            return saved, contents
        if current_error is not None:
            raise current_error
        return invalid

    def _validate(self, key, units, batch, response, metadata=None):
        self.received += len(units)
        self.rejected = 0
        reviews = []
        try:
            if batch and not self.upstream_fallback:
                response = normalize_batch_response(response)
            response_values(units, batch, response, aliases=self.aliases, reviews=reviews, native_flow=self.native_flow)
            return response
        except ResponseValidationError as exc:
            self.rejected = exc.rejected
            if batch and (self.upstream_fallback or (self.native_flow and exc.code == 'batch_placeholders')):
                # 原批次交回上游解析或单段恢复，不在缓存层重译整批。
                self.cache_warnings.append('批次响应交由 BabelDOC 解析或单段恢复。' if self.upstream_fallback
                                           else '批次占位符未通过校验，已交由 BabelDOC 单段恢复。')
                return response
            exc.details.update(diagnostic_id='cache-' + key, diagnostic_saved=True)
            usage = (metadata or {}).get('usage', {})
            exc.details['usage'] = numeric_usage({'usage': usage}, 'normalized')
            raise
        finally:
            self.reviews[key] = reviews
            layout._write_json(self.folder / 'numeric-reviews' / (key + '.json'), reviews)

    def resolve(self, request, call):
        self.last_metadata = {}
        units, batch = request_units(request)
        # 上下文、模型、源文件和请求模式均参与标识；不因引擎消息ID变化而重复付费。
        cache_identity = {k: self.identity[k] for k in ('source_hash', 'config', 'engine_version', 'protocol')}
        if 'batch_policy' in self.identity:
            cache_identity['batch_policy'] = self.identity['batch_policy']
        key_data = {'identity': cache_identity, 'prompt': request['prompt'],
                    'mode': request.get('mode'), 'parameters': request.get('rate_limit_params', {})}
        if 'translation_context' in request:
            key_data['translation_context'] = request['translation_context']
        key = hashlib.sha256(json.dumps(key_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.folder / 'native-replies' / (key + '.json')
        cached = self._cached(key, key_data, units, batch)
        saved = cached[0] if cached is not None else None
        retry = False
        if saved is not None:
            if saved.get('request') != key_data:
                raise ValueError('译文缓存来源不匹配。')
            response = saved['response']
            try:
                response = self._validate(key, units, batch, response, saved.get('metadata'))
            except ValueError:
                if not self.retry_rejected or key in self.attempted:
                    raise
                # 显式继续只补译失败请求一次；按内容归档原回复，保留已付费证据。
                rejected_hash = hashlib.sha256(json.dumps(saved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                layout._write_json(self.folder / 'native-replies-rejected' / key / (rejected_hash + '.json'), saved)
                retry = True
            else:
                if not path.is_file() or path.read_bytes() != cached[1]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix('.tmp')
                    temporary.write_bytes(cached[1])
                    temporary.replace(path)
                self.reused += len(units)
                self.last_metadata = saved.get('metadata', {})
        if saved is None or retry:
            self.model_calls += 1
            self.attempted.add(key)
            raw_response = call(request)
            response = raw_response.get('response') if isinstance(raw_response, dict) else raw_response
            saved = {'request': key_data, 'response': response, 'origin': 'model'}
            if isinstance(raw_response, dict):
                saved['raw_response'] = raw_response
                saved['metadata'] = {name: raw_response[name] for name in ('terms', 'usage') if name in raw_response}
            # 先持久化返回，再核验：取消或排版失败不会丢失已消耗额度的结果。
            layout._write_json(path, saved)
            self.last_metadata = saved.get('metadata', {})
            response = self._validate(key, units, batch, response, saved.get('metadata'))
        self.done += len(units)
        return response
