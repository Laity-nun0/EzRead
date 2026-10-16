"""本次应用运行的 API 尝试、状态和已报告用量；无磁盘写入或账户余额推算。"""

from copy import deepcopy
from datetime import datetime, timezone
import threading
from .api_diagnostics import ERROR_CODES


_FIELDS = (
    'input_tokens', 'output_tokens', 'total_tokens', 'prompt_tokens', 'completion_tokens',
    'cache_creation_input_tokens', 'cache_read_input_tokens', 'prompt_cache_hit_tokens',
    'prompt_cache_miss_tokens', 'promptTokenCount', 'candidatesTokenCount', 'totalTokenCount',
    'cachedContentTokenCount', 'thoughtsTokenCount', 'toolUsePromptTokenCount',
)
_DETAILS = {
    'input_tokens_details': ('cached_tokens',),
    'output_tokens_details': ('reasoning_tokens',),
    'prompt_tokens_details': ('cached_tokens', 'audio_tokens'),
    'completion_tokens_details': ('reasoning_tokens', 'audio_tokens', 'accepted_prediction_tokens',
                                 'rejected_prediction_tokens'),
    'cache_creation': ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'),
}
_CODES = {'translation', 'configuration', 'authentication', 'quota', 'api_server',
          'api_request', 'api_response', 'api_transport', 'timeout', 'cancelled'} | ERROR_CODES
_CHANNELS = {'pdf', 'chat', 'overview', 'structure', 'selection', 'retranslate', 'journal'}


def numeric_usage(data, provider):
    """只读白名单数字；不保留网关错误文本、任意键或非整数值。"""
    if not isinstance(data, dict):
        return {}
    usage = data.get('usageMetadata' if provider == 'gemini' else 'usage')
    if not isinstance(usage, dict):
        return {}
    result = {key: usage[key] for key in _FIELDS
              if type(usage.get(key)) is int and usage[key] >= 0}
    for field, names in _DETAILS.items():
        details = usage.get(field)
        if isinstance(details, dict):
            for name in names:
                if type(details.get(name)) is int and details[name] >= 0:
                    result[f'{field}.{name}'] = details[name]
    return result


def _row():
    return {'requests': {'attempted': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0},
            'tokens': {'input': None, 'output': None, 'total': None}, 'usage_reported': 0,
            'usage': {}, 'last': None}


class ApiUsage:
    def __init__(self):
        self._lock = threading.Lock()
        self._total = _row()
        self._models = {}
        self._pending = {}
        self._sequence = 0

    def _status(self, entry, status, code):
        last = {'provider': entry['provider'], 'model': entry['model'], 'channel': entry['channel'],
                'status': status, 'code': code,
                'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        self._total['last'] = last
        self._models[(entry['provider'], entry['model'])]['last'] = last

    def begin(self, provider, model, channel):
        """只由 transport 在 HTTP 提交边界调用，连接准备及缓存命中不能调用。"""
        with self._lock:
            self._sequence += 1
            request_id = self._sequence
            entry = {'provider': provider, 'model': model,
                     'channel': channel if channel in _CHANNELS else 'unknown', 'reported': False}
            self._pending[request_id] = entry
            row = self._models.setdefault((provider, model), _row())
            self._total['requests']['attempted'] += 1
            row['requests']['attempted'] += 1
            self._status(entry, 'pending', None)
            return request_id

    def add_usage(self, request_id, usage):
        """每次尝试最多累计一次已报告用量；取消后迟到结果不覆盖最终状态。"""
        if not isinstance(usage, dict):
            return
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is None or entry['reported'] or not usage:
                return
            allowed = set(_FIELDS) | {f'{field}.{name}' for field, names in _DETAILS.items() for name in names}
            clean = {key: value for key, value in usage.items()
                     if key in allowed and type(value) is int and value >= 0}
            if not clean:
                return
            entry['reported'] = True
            provider = entry['provider']
            if provider == 'gemini':
                token_fields = ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount')
            elif provider in ('deepseek', 'openai_compatible'):
                token_fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
            else:
                token_fields = ('input_tokens', 'output_tokens', 'total_tokens')
            for row in (self._total, self._models[(provider, entry['model'])]):
                row['usage_reported'] += 1
                for key, value in clean.items():
                    row['usage'][key] = row['usage'].get(key, 0) + value
                for name, field in zip(('input', 'output', 'total'), token_fields):
                    if field in clean:
                        row['tokens'][name] = (row['tokens'][name] or 0) + clean[field]

    def finish(self, request_id, status, code=None):
        if status not in ('success', 'failed', 'cancelled'):
            raise ValueError('API 请求结束状态无效。')
        with self._lock:
            entry = self._pending.pop(request_id, None)
            if entry is None:
                return
            field = 'succeeded' if status == 'success' else status
            self._total['requests'][field] += 1
            self._models[(entry['provider'], entry['model'])]['requests'][field] += 1
            self._status(entry, status, code if code in _CODES else ('unknown' if code else None))

    def snapshot(self):
        with self._lock:
            return deepcopy({'scope': 'application_run', 'requests': self._total['requests'],
                             'tokens': self._total['tokens'], 'usage_reported': self._total['usage_reported'],
                             'last': self._total['last'], 'account_balance': None,
                             'models': [{'provider': provider, 'model': model, **row}
                                        for (provider, model), row in sorted(self._models.items())]})
