"""只读取本机 API 配置及本次运行用量，不查询供应商或登录账号。"""
from . import api_settings


def configuration(data_dir):
    value = {'provider': None, 'label': 'AI API', 'model': '', 'base_url': '',
             'configured': False, 'message': '请先在设置中保存 API 配置和密钥。'}
    try:
        checked = api_settings.resolve_config(data_dir, purpose='translation')
        value.update(profile_id=checked['profile_id'], provider='deepseek', label='DeepSeek',
                     model=checked['model'], base_url=checked['base_url'])
        api_settings.get_api_key(data_dir, checked)
        value.update(configured=True, message='本地配置已就绪；保存和刷新不会发起在线验证。')
    except ValueError as exc:
        value['message'] = str(exc)
    return value


def snapshot(app):
    with app.LOCK:
        queue = [{'paper_id': pid, 'kind': kind} for pid, kind in app.QUEUED]
    return {'configuration': configuration(app.DATA), 'usage': app.API_USAGE.snapshot(),
            'queue': queue, 'version': app.VERSION, 'data_dir': str(app.DATA)}


def models(data_dir):
    """旧模型目录端点改为公开的已保存配置列表，不宣称远程可用性。"""
    settings = api_settings.public_settings(data_dir)
    labels = {item['id']: item['label'] for item in settings['purposes']}
    return {'source': 'local_settings', 'online_verified': False,
            'models': [{**profile, 'purpose': purpose,
                        'display_name': f"{labels[purpose]} · DeepSeek · {profile['model']}"}
                       for purpose, profile in settings['features'].items()]}
