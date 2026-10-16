"""失败响应只保留在任务私有目录，不作为缓存、日志或导出内容。"""
import base64
import json
import os
from pathlib import Path
import re
import tempfile


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
ERROR_CODES = {
    'api_incomplete', 'api_too_large', 'api_json', 'api_finish', 'api_refusal',
    'api_empty', 'api_envelope', 'api_content_json', 'batch_json', 'batch_ids',
    'batch_empty', 'batch_placeholders', 'api_budget',
}
PUBLIC_FIELDS = {
    'request_id', 'provider', 'model', 'http_status', 'response_bytes', 'captured_bytes',
    'truncated', 'stage', 'finish_reason', 'json_line', 'json_column', 'json_position',
    'usage', 'diagnostic_id', 'diagnostic_saved', 'diagnostic_error', 'response_received',
    'body_complete', 'expected_response_bytes',
}


def public_details(value):
    return {key: value[key] for key in PUBLIC_FIELDS if key in value}


def safe_reason(value):
    # 供应商字段也不可信，只公开已知状态；未知正文只存在脱敏私有文件中。
    return value if value in ('completed', 'incomplete', 'failed', 'cancelled', 'stop', 'length',
                             'content_filter', 'tool_calls', 'end_turn', 'max_tokens', 'refusal',
                             'STOP', 'MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER') else 'unknown'


def _redact_json_secret(body, secret):
    """仅重编码命中密钥的字符串 token；不要求整个响应是完整 JSON。"""
    def redact(match):
        value, end = match.group('value', 'end')
        complete = end == b'"'
        if not complete:
            # 捕获上限可能切进 Unicode 转义；不把缺失字符补成真实响应。
            value = re.sub(rb'\\u[0-9a-fA-F]{0,3}$', b'', value)
        try:
            decoded = json.loads('"' + value.decode('utf-8', errors='surrogateescape') + '"', strict=False)
        except (ValueError, RecursionError):
            return match.group()
        redacted = decoded.replace(secret, '[REDACTED]')
        if not complete:
            for length in range(min(len(secret) - 1, len(redacted)), 0, -1):
                if redacted.endswith(secret[:length]):
                    redacted = redacted[:-length] + '[REDACTED]'
                    break
        if redacted == decoded:
            return match.group()
        encoded = json.dumps(redacted, ensure_ascii=False)[1:-1].encode('utf-8', errors='surrogateescape')
        return b'"' + encoded + (b'"' if complete else b'')

    return re.sub(rb'"(?P<value>(?:\\.|[^"\\])*)(?P<end>"|\\?$)', redact, body)


def save_failure(folder, metadata, raw, secret):
    """原子新增独立记录；大小受控，保留非法 UTF-8，不写任何请求头。"""
    body = raw[:MAX_CAPTURE_BYTES]
    if secret:
        body = _redact_json_secret(body, secret)
        # 无引号或另有坏转义时仍匹配已知密钥的一层 JSON 编码，不修复响应。
        pattern = b''.join(b'(?:' + b'|'.join({
            re.escape(char.encode()), re.escape(json.dumps(char)[1:-1].encode()),
            b'(?i:' + rb'\\+u' + f'{ord(char):04x}'.encode() + b')',
        }) + b')' for char in secret)
        body = re.sub(pattern, b'[REDACTED]', body)
        for token in (secret.encode(), json.dumps(secret)[1:-1].encode()):
            body = body.replace(token, b'[REDACTED]')
            # 截断点可能位于密钥中间，连末尾的部分密钥也不能留存。
            for length in range(min(len(token) - 1, len(body)), 0, -1):
                if body.endswith(token[:length]):
                    body = body[:-length] + b'[REDACTED]'
                    break
    body = re.sub(rb'(?i)(bearer\s+)[^\s"\\,;<>]+', rb'\1[REDACTED]', body)
    body = re.sub(
        rb'(?i)(["\']?(?:authorization|proxy-authorization|cookie|set-cookie|api[_-]?key|x-api-key|x-goog-api-key|access_token|refresh_token)["\']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|\'[^\']*\'|[^\r\n,;}]+)',
        rb'\1"[REDACTED]"', body)
    record = {**metadata, 'captured_bytes': len(raw[:MAX_CAPTURE_BYTES]),
              'truncated': metadata.get('truncated', False) or len(body) > MAX_CAPTURE_BYTES
              or metadata.get('response_bytes', 0) > len(raw[:MAX_CAPTURE_BYTES]),
              'raw_base64': base64.b64encode(body[:MAX_CAPTURE_BYTES]).decode('ascii'),
              'redacted': body != raw[:MAX_CAPTURE_BYTES], 'encoding': 'base64'}
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.failure-', dir=directory)
        if os.name != 'nt':
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # request_id 每次由本机随机生成，不覆盖原有失败或成功缓存。
        target = directory / (metadata['request_id'] + '.json')
        if target.exists():
            raise OSError('diagnostic collision')
        os.replace(temporary, target)
        return {**public_details(record), 'diagnostic_id': target.stem, 'diagnostic_saved': True}
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
