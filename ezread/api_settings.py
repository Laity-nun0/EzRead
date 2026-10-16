"""全文 API 私有配置。Windows 密钥由当前用户 DPAPI 保护；其他平台仅存于 0600 文件。

文件位于实际 data 目录，不进入普通设置、SQLite 或文献备份。读取接口只返回 has_key。
"""
import base64
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

PROVIDERS = [
    {'id': 'deepseek', 'label': 'DeepSeek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash'},
    {'id': 'openai', 'label': 'OpenAI', 'base_url': 'https://api.openai.com/v1', 'model': ''},
    {'id': 'openai_compatible', 'label': 'OpenAI 兼容', 'base_url': '', 'model': ''},
    {'id': 'anthropic', 'label': 'Anthropic', 'base_url': 'https://api.anthropic.com/v1', 'model': ''},
    {'id': 'gemini', 'label': 'Gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta', 'model': ''},
]
PURPOSES = [{'id': 'translation', 'label': '全文翻译'}, {'id': 'chat', 'label': '论文问答'}]
_FEATURE_IDS = {'translation': 'deepseek_translation', 'chat': 'deepseek_chat'}
CHAT_MODELS = [{'model': 'deepseek-flash', 'label': 'DeepSeek Flash'},
               {'model': 'deepseek-v4-pro', 'label': 'DeepSeek Pro'}]
_LEGACY_PURPOSES = {'overview', 'structure', 'selection', 'retranslate', 'journal'}
_FEATURE_BASE = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                 'model': 'deepseek-flash', 'max_output_tokens': 393216}
_LOCK = threading.RLock()
_FILENAME = 'translation-api.private.json'


def validate_thinking(data):
    """任务专用参数；共享默认设置不保存或自动添加这些字段。"""
    if 'thinking' in data and (data.get('provider') != 'deepseek' or data['thinking'] not in ('enabled', 'disabled')):
        raise ValueError('thinking 仅支持 DeepSeek 的 enabled 或 disabled。')
    if 'reasoning_effort' in data:
        provider = data.get('provider')
        effort = data['reasoning_effort']
        allowed = ('low', 'high', 'max') if provider == 'deepseek' and data.get('thinking') == 'enabled' else ()
        if provider in ('openai', 'openai_compatible'):
            allowed = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
        if effort not in allowed:
            raise ValueError('此 API 供应商或思考模式不支持所选 reasoning_effort。')


def _defaults(provider):
    entry = next((item for item in PROVIDERS if item['id'] == provider), None)
    if entry is None:
        raise ValueError('不支持的翻译 API 供应商。')
    return {'provider': provider, 'base_url': entry['base_url'], 'model': entry['model'], 'max_output_tokens': 8192}


def _validate(data):
    provider = data.get('provider', 'deepseek')
    config = _defaults(provider)
    config.update({key: data[key] for key in config if key in data})
    validate_thinking({**data, 'provider': provider})
    config.update({key: data[key] for key in ('thinking', 'reasoning_effort') if key in data})
    url = config['base_url']
    try:
        if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url:
            raise ValueError
        parts = urlsplit(url)
        host = parts.hostname
        loopback = host == 'localhost'
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or '?' in url or '#' in url or parts.scheme not in ('https', 'http')
                or (parts.scheme == 'http' and not loopback)):
            raise ValueError
        parts.port
        config['base_url'] = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/'), '', ''))
    except (ValueError, TypeError):
        raise ValueError('Base URL 必须是无凭据、查询参数和片段的 HTTPS 地址；仅回环地址允许 HTTP。') from None
    model = config['model']
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in model):
        raise ValueError('请填写不超过 200 字符且无空白的模型名称。')
    if type(config['max_output_tokens']) is not int or not 1 <= config['max_output_tokens'] <= 1048576:
        raise ValueError('输出 token 上限必须是 1 至 1048576 的整数。')
    if 'profile_id' in data:
        if not isinstance(data['profile_id'], str) or not data['profile_id'] or len(data['profile_id']) > 200:
            raise ValueError('模型配置 ID 无效。')
        config['profile_id'] = data['profile_id']
    return config


def _read(data_dir):
    try:
        value = json.loads((Path(data_dir) / _FILENAME).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('profiles'), dict):
            raise ValueError
        if (any(not isinstance(pid, str) or not isinstance(profile, dict) for pid, profile in value['profiles'].items())
                or any(key in value and not isinstance(value[key], str) for key in ('selected_provider', 'selected_profile'))
                or not isinstance(value.get('assignments', {}), dict)):
            raise ValueError
        return value
    except FileNotFoundError:
        return {'selected_provider': 'deepseek', 'profiles': {}}
    except (OSError, ValueError, TypeError):
        raise ValueError('本机翻译 API 设置无法读取，请检查私有设置文件。') from None


def _crypt(value, decrypt=False):
    if os.name != 'nt':
        return value
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('当前 Windows 用户无法保护或读取 API 密钥。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


def _feature_source(stored, purpose):
    pid = _FEATURE_IDS[purpose]
    if pid in stored['profiles']:
        profile = stored['profiles'][pid]
        if profile.get('key') or profile.get('key_reference') != 'deepseek':
            return profile
    # 未保存的用途或明确保留的引用只读复用旧密钥；清除后没有引用，绝不回退。
    legacy = stored['profiles'].get('deepseek', {})
    if legacy.get('provider', 'deepseek') == 'deepseek' and isinstance(legacy.get('base_url'), str) and legacy['base_url'].rstrip('/') == _FEATURE_BASE['base_url']:
        return legacy
    return {}


def _feature_config(stored, purpose):
    saved = stored['profiles'].get(_FEATURE_IDS[purpose], {})
    thinking = saved.get('thinking', 'enabled' if purpose == 'chat' else 'disabled')
    config = {**_FEATURE_BASE, 'profile_id': _FEATURE_IDS[purpose], 'thinking': thinking}
    if purpose == 'chat':
        config['model'] = saved.get('model', _FEATURE_BASE['model'])
        if config['model'] not in tuple(item['model'] for item in CHAT_MODELS):
            raise ValueError('问答模型仅支持 DeepSeek Flash 或 Pro。')
    if thinking == 'enabled':
        config['reasoning_effort'] = saved.get('reasoning_effort', 'high')
    return _validate(config)


def _profile_id(stored, provider=None):
    selected = stored.get('selected_profile', stored.get('selected_provider', 'deepseek'))
    profiles = stored['profiles']
    if selected in profiles and (provider is None or profiles[selected].get('provider', selected) == provider):
        return selected
    return next((pid for pid, profile in profiles.items() if profile.get('provider', pid) == provider), provider)


def _public(stored):
    models = []
    for pid, profile in stored['profiles'].items():
        provider = profile.get('provider', pid)
        config = _defaults(provider)
        config.update({key: profile[key] for key in config if key in profile})
        models.append({**config, 'profile_id': pid, 'has_key': bool(profile.get('key'))})
    selected = _profile_id(stored)
    selected_model = next((item for item in models if item['profile_id'] == selected), None)
    profiles = {}
    for entry in PROVIDERS:
        pid = _profile_id(stored, entry['id'])
        profiles[entry['id']] = next((dict(item) for item in models if item['profile_id'] == pid),
                                     {**_defaults(entry['id']), 'has_key': False})
    return {'selected_provider': selected_model['provider'] if selected_model else stored.get('selected_provider', 'deepseek'),
            'selected_profile': selected or '', 'models': models,
            'features': {purpose: {**_feature_config(stored, purpose), 'has_key': bool(_feature_source(stored, purpose).get('key'))}
                         for purpose in _FEATURE_IDS},
            'purposes': [dict(item) for item in PURPOSES],
            'chat_model_options': [dict(item) for item in CHAT_MODELS],
            'providers': [dict(item) for item in PROVIDERS], 'profiles': profiles}


def public_settings(data_dir):
    with _LOCK:
        return _public(_read(data_dir))


def _save_key(profile, old, data):
    if old.get('provider') == profile['provider'] and old.get('base_url') == profile['base_url'] and old.get('key'):
        profile['key'] = old['key']
        profile['key_encoding'] = old.get('key_encoding')
    if type(data.get('clear_key', False)) is not bool:
        raise ValueError('清除密钥标记必须是布尔值。')
    key = data.get('api_key', '')
    if not isinstance(key, str) or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('API Key 必须是无空白的 ASCII 字符串，最多 8192 字符。')
    if data.get('clear_key'):
        profile.pop('key', None)
        profile.pop('key_encoding', None)
    elif key:
        profile['key'] = base64.b64encode(_crypt(key.encode())).decode('ascii')
        profile['key_encoding'] = 'dpapi' if os.name == 'nt' else 'file-permissions'


def save_settings(data_dir, data):
    if not isinstance(data, dict):
        raise ValueError('翻译 API 设置必须是对象。')
    feature = 'purpose' in data
    allowed = {'purpose', 'api_key', 'clear_key', 'thinking', 'reasoning_effort'} if feature else {
        'provider', 'base_url', 'model', 'max_output_tokens', 'api_key', 'clear_key'}
    if feature and data.get('purpose') == 'chat':
        allowed.add('model')
    if set(data) - allowed:
        raise ValueError('翻译 API 设置含不支持的字段。')
    with _LOCK:
        stored = _read(data_dir)
        if feature:
            purpose = data['purpose']
            if not isinstance(purpose, str) or purpose not in _FEATURE_IDS:
                raise ValueError('仅支持翻译与问答两项 DeepSeek 设置。')
            config = _feature_config(stored, purpose)
            config.update({key: data[key] for key in ('model', 'thinking', 'reasoning_effort') if key in data})
            if purpose == 'chat' and config['model'] not in tuple(item['model'] for item in CHAT_MODELS):
                raise ValueError('问答模型仅支持 DeepSeek Flash 或 Pro。')
            if data.get('thinking') == 'disabled' and 'reasoning_effort' not in data:
                config.pop('reasoning_effort', None)
            if config.get('thinking') == 'enabled':
                config.setdefault('reasoning_effort', 'high')
            config = _validate(config)
            pid = _FEATURE_IDS[purpose]
            old = stored['profiles'].get(pid, {})
        else:
            # 历史脚本与任务的离线兼容入口，产品 HTTP 只开放 purpose 设置。
            config = _validate(data)
            pid = config['provider']
            old = stored['profiles'].get(pid, {})
        profile = {key: value for key, value in config.items() if key != 'profile_id'}
        _save_key(profile, old, data)
        if (feature and not profile.get('key') and not data.get('clear_key') and not data.get('api_key')
                and (old.get('key_reference') == 'deepseek'
                     or pid not in stored['profiles'] and _feature_source(stored, purpose).get('key'))):
            # 保存思考设置时仅保留旧密钥引用，不复制明文或加密 blob。
            profile['key_reference'] = 'deepseek'
        stored['profiles'][pid] = profile
        if not feature:
            stored['selected_provider'] = config['provider']
            stored['selected_profile'] = pid
        folder = Path(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            descriptor, temp_path = tempfile.mkstemp(prefix='.translation-api-', dir=folder)
            if os.name != 'nt':
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(stored, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, folder / _FILENAME)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return _public(stored)


def resolve_config(data_dir, options=None, *, purpose=None):
    if options is not None and not isinstance(options, dict):
        raise ValueError('翻译 API 配置必须是对象。')
    if purpose is not None and (not isinstance(purpose, str) or purpose not in set(_FEATURE_IDS) | _LEGACY_PURPOSES):
        raise ValueError('不支持的模型用途。')
    with _LOCK:
        stored = _read(data_dir)
        options = options or {}
        if (not any(key in options for key in ('profile_id', 'base_url', 'model', 'max_output_tokens'))
                and options.get('provider', 'deepseek') == 'deepseek'):
            feature_purpose = 'chat' if purpose == 'chat' else 'translation'
            config = _feature_config(stored, feature_purpose)
            config.update({key: options[key] for key in ('thinking', 'reasoning_effort') if key in options})
            if options.get('thinking') == 'disabled' and 'reasoning_effort' not in options:
                config.pop('reasoning_effort', None)
            if config.get('thinking') == 'enabled':
                config.setdefault('reasoning_effort', 'high')
            return _validate(config)
        if options.get('profile_id') in _FEATURE_IDS.values():
            if all(key in options for key in ('provider', 'base_url', 'model', 'max_output_tokens')):
                # 已冻结任务按原样恢复，缺省思考字段不能被新设置补入。
                return _validate(options)
            feature_purpose = next(key for key, value in _FEATURE_IDS.items() if value == options['profile_id'])
            config = _feature_config(stored, feature_purpose)
            config.update({key: options[key] for key in config if key in options})
            config.update({key: options[key] for key in ('thinking', 'reasoning_effort') if key in options})
            if options.get('thinking') == 'disabled' and 'reasoning_effort' not in options:
                config.pop('reasoning_effort', None)
            if config.get('thinking') == 'enabled':
                config.setdefault('reasoning_effort', 'high')
            return _validate(config)
        if 'profile_id' in options and (not isinstance(options['profile_id'], str) or not options['profile_id']):
            raise ValueError('模型配置 ID 无效。')
        pid = options.get('profile_id')
        if pid is not None:
            if not isinstance(pid, str) or pid not in stored['profiles']:
                raise ValueError('模型配置不存在，请重新选择。')
        else:
            pid = _profile_id(stored, options.get('provider'))
        saved = stored['profiles'].get(pid, {})
        provider = options.get('provider', saved.get('provider', stored.get('selected_provider', 'deepseek')))
        profile = {**_defaults(provider), **saved}
        profile.update({key: options[key] for key in ('provider', 'base_url', 'model', 'max_output_tokens', 'thinking', 'reasoning_effort') if key in options})
        # 旧完整快照不补 ID，确保按原供应商与地址恢复绑定密钥。
        legacy_snapshot = 'profile_id' not in options and all(key in options for key in ('provider', 'base_url', 'model', 'max_output_tokens'))
        if pid in stored['profiles'] and (options.get('profile_id') is not None or (pid != provider and not legacy_snapshot)):
            profile['profile_id'] = pid
        return _validate(profile)


def get_api_key(data_dir, config):
    config = _validate(config)
    with _LOCK:
        stored = _read(data_dir)
        if config.get('profile_id') in _FEATURE_IDS.values():
            purpose = next(key for key, value in _FEATURE_IDS.items() if value == config['profile_id'])
            candidates = [_feature_source(stored, purpose)]
        elif 'profile_id' in config:
            candidates = [stored['profiles'].get(config['profile_id'], {})]
        else:
            candidates = [profile for profile in stored['profiles'].values()
                          if profile.get('provider') == config['provider']
                          and profile.get('base_url') == config['base_url']
                          and profile.get('model') == config['model']]
            # 无精确模型条目时，仅保留历史 provider 键的旧快照恢复方式。
            if not candidates:
                legacy = stored['profiles'].get(config['provider'], {})
                candidates = [legacy]
        candidates = [profile for profile in candidates
                      if profile.get('provider') == config['provider']
                      and profile.get('base_url') == config['base_url'] and profile.get('key')]
        if len(candidates) > 1:
            raise ValueError('旧模型配置的密钥绑定不唯一，请重新选择模型。')
        profile = candidates[0] if candidates else {}
        if not profile:
            raise ValueError('此模型配置的供应商和 Base URL 尚未保存 API Key，请先在设置中保存。')
        expected = 'dpapi' if os.name == 'nt' else 'file-permissions'
        if profile.get('key_encoding') != expected:
            raise ValueError('API Key 属于其他系统，请重新保存。')
        try:
            return _crypt(base64.b64decode(profile['key'], validate=True), decrypt=True).decode('utf-8')
        except (ValueError, UnicodeError):
            raise ValueError('本机 API Key 无法解密，请重新保存。') from None


def configured_chat_models(data_dir):
    """问答仅公开固定 DeepSeek 配置，不查询远端。"""
    try:
        config = resolve_chat_config(data_dir)
    except ValueError:
        return []
    labels = {'disabled': '关闭思考', 'low': '低', 'high': '高', 'max': '最高'}
    return [{**config, **model,
             'reasoning_options': [{'value': value, 'label': label} for value, label in labels.items()]}
            for model in CHAT_MODELS]


def resolve_chat_config(data_dir, options=None, require_configured=False):
    """问答使用独立 DeepSeek 配置；旧非 DeepSeek 会话配置拒绝执行。"""
    if options is not None and not isinstance(options, dict):
        raise ValueError('问答 API 配置必须是对象。')
    options = dict(options or {})
    if not any(key in options for key in ('profile_id', 'provider', 'base_url', 'max_output_tokens')):
        options['profile_id'] = _FEATURE_IDS['chat']
    with _LOCK:
        config = resolve_config(data_dir, options, purpose='chat')
        expected = resolve_config(data_dir, purpose='chat')
        fields = ('provider', 'base_url', 'max_output_tokens', 'profile_id')
        if (any(config.get(key) != expected[key] for key in fields)
                or config['model'] not in tuple(item['model'] for item in CHAT_MODELS)):
            raise ValueError('论文问答仅支持官方 DeepSeek Flash 或 Pro 配置。')
        get_api_key(data_dir, config)
        return config
