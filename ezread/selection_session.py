"""旧准备接口只核验本机 API 配置，不建立连接或发送模型测试。"""
import threading

from . import api_settings


class SelectionSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.worker = None
        self.state = {'status': 'idle', 'message': ''}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, app):
        with self.lock:
            if app.SHUTDOWN.is_set():
                self.state.update(status='stopped', message='应用正在关闭。')
                return self.snapshot()
            try:
                config = api_settings.resolve_config(app.DATA, purpose='selection')
                api_settings.get_api_key(app.DATA, config)
                self.state.update(status='ready', message='本机 API 配置可用；未连接模型，实际翻译时建立连接。')
            except ValueError as exc:
                self.state.update(status='failed', message=str(exc))
            if app.SHUTDOWN.is_set():
                self.state.update(status='stopped', message='应用已关闭。')
            return self.snapshot()
